"""安全模块：bcrypt 密码哈希 + 基于 Redis 的 session 会话管理。

session token 采用“随机串.HMAC签名”形式，签名密钥来自 SECRET_KEY，
防止攻击者绕过认证逻辑直接向 Redis 注入任意 session。

【A3 绝对生命周期】token 改为「随机串~首签时间.签名」：
- 续期（滑动）只延长 Redis TTL，**不改 token 内容**，因此 iat 天然保持不变；
- iat 参与 HMAC，客户端改 iat 会直接验签失败；
- 超过 TOKEN_MAX_LIFETIME_SECONDS 后，session 与 refresh 双双失效，必须重新登录。
这样在**不引入 JWT 依赖**的前提下获得了「绝对过期」语义，且旧格式 token 全部兼容解析。
"""
import hashlib
import hmac
import json
import logging
import secrets
import time

import bcrypt

from ..config import settings
from .exceptions import BizError
from .redis import get_redis

logger = logging.getLogger(__name__)

# Redis key 前缀
SESSION_PREFIX = "kb:session:"
REFRESH_PREFIX = "kb:refresh:"
# 【双Token改造 新增】refresh_token 黑名单前缀：登出时写入，主动踢下线/防止登出后复用
REFRESH_BLACKLIST_PREFIX = "kb:refresh_blacklist:"

# 【A3】首签时间与随机串的分隔符。
# 刻意不用 "."：_verify_signature 依赖 rsplit(".", 1) 取签名段，用 "." 会破坏解析；
# secrets.token_urlsafe 的字符集是 [A-Za-z0-9_-]，不含 "~"，拼接无歧义。
_IAT_SEP = "~"

# bcrypt 只处理前 72 字节
_BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> str:
    """对密码做 bcrypt 哈希（自动加盐），返回可入库的字符串。"""
    _check_password_length(password)
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """校验明文密码与哈希是否匹配；任何异常都视为不匹配。"""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _check_password_length(password: str) -> None:
    if len(password.encode("utf-8")) > _BCRYPT_MAX_BYTES:
        raise BizError(400, f"密码长度不能超过 {_BCRYPT_MAX_BYTES} 字节")


def _sign(raw: str) -> str:
    """使用 SECRET_KEY 对随机串做 HMAC-SHA256 签名，拼成最终 token。"""
    sig = hmac.new(
        settings.secret_key.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{raw}.{sig}"


def _verify_signature(token: str) -> bool:
    """校验 token 签名，防止伪造。"""
    if not token or "." not in token:
        return False
    raw, sig = token.rsplit(".", 1)
    expect = hmac.new(
        settings.secret_key.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(sig, expect)


def _new_token(iat: int | None = None) -> str:
    """生成 token；传 iat 时编码为「随机串~iat.签名」，不传则沿用旧格式。"""
    raw = secrets.token_urlsafe(32)
    if iat is not None:
        raw = f"{raw}{_IAT_SEP}{int(iat)}"
    return _sign(raw)


# ----------------------- 【A3】绝对生命周期 -----------------------

def parse_token_iat(token: str) -> int | None:
    """解析 token 内的首签时间（iat）；旧格式无 iat / 解析失败一律返回 None。

    刻意**不做签名校验**：调用方必须先过 `_verify_signature`，否则伪造 token 里的
    iat 就能绕过判断。本函数任何异常都吞掉返回 None（= 按旧行为处理），绝不外抛。
    """
    try:
        if not token or "." not in token:
            return None
        body = token.rsplit(".", 1)[0]
        if _IAT_SEP not in body:
            return None
        return int(body.rpartition(_IAT_SEP)[2])
    except (ValueError, TypeError, AttributeError):
        return None


def max_token_lifetime() -> int:
    """绝对生命周期上限（秒）。配置非法时回退 30 天，并保证不小于 1 小时。"""
    try:
        return max(3600, int(settings.token_max_lifetime))
    except (TypeError, ValueError):
        return 30 * 24 * 3600


def is_absolutely_expired(iat: int | None) -> bool:
    """首签时间距今是否超过绝对上限。

    iat 为 None（旧格式）→ 不判超期，交给调用方按各自策略处理；
    计算异常 → 返回 False（fail-safe：宁可放行旧行为，也不把用户挡在门外）。
    """
    if iat is None:
        return False
    try:
        return (int(time.time()) - int(iat)) > max_token_lifetime()
    except (TypeError, ValueError, OverflowError):
        logger.warning("绝对过期判断异常，已降级为不放行超期判定", exc_info=True)
        return False


def _new_session_token(first_iat: int | None) -> tuple[str, int]:
    """签发带 iat 的 token，返回 (token, 实际使用的 iat)。"""
    iat = int(time.time()) if first_iat is None else int(first_iat)
    return _new_token(iat), iat


# ----------------------- session（访问令牌） -----------------------

def create_session(user: dict, first_iat: int | None = None) -> str:
    """创建会话：把用户信息写入 Redis，返回访问 token。

    first_iat 是本次登录的首签时间。**refresh 换发新 access 时必须传入原值**，
    否则每次刷新都会把绝对生命周期重置回 30 天，A3 的约束就白加了。
    """
    token, _iat = _new_session_token(first_iat)
    get_redis().set(
        SESSION_PREFIX + token, json.dumps(user, ensure_ascii=False), ex=settings.session_ttl
    )
    return token


def get_session(token: str) -> dict | None:
    """根据 token 读取会话；命中后刷新过期时间（滑动过期），并校验绝对生命周期。

    【A3】两种判无效的情况（都会顺手删除 Redis 会话，避免每次请求重复判断）：
    1. iat 缺失 = 升级前签发的旧格式 token。无法把 iat 补进 token（改内容即毁签名），
       只能判无效——前端收到 401 会自动用 refresh 换新 token，用户无感，但从此刻起
       进入受绝对上限约束的新格式。这是唯一能真正闭环的取舍。
    2. 距首签已超过上限 → 强制重新登录。
    """
    if not _verify_signature(token):
        return None
    iat = parse_token_iat(token)
    if iat is None:
        logger.info("旧格式 session（无 iat）已失效，交由 refresh 换发新令牌")
        delete_session(token)
        return None
    if is_absolutely_expired(iat):
        logger.warning(
            "session 距首签已超过绝对生命周期（%ss），强制重新登录", max_token_lifetime()
        )
        delete_session(token)
        return None
    data = get_redis().get(SESSION_PREFIX + token)
    if data is None:
        return None
    # 滑动续期：每次访问都重置过期时间（只动 TTL，不动 token 内的 iat）
    get_redis().expire(SESSION_PREFIX + token, settings.session_ttl)
    return json.loads(data)


def delete_session(token: str) -> None:
    """删除会话（登出）。"""
    if token:
        get_redis().delete(SESSION_PREFIX + token)


def extract_token(authorization: str | None) -> str | None:
    """从 Authorization 头解析 Bearer token。"""
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


# ----------------------- refresh token -----------------------

def create_refresh_token(user_id: int, first_iat: int | None = None) -> str:
    """创建刷新令牌，映射到用户 ID（TTL 与 refresh 有效期一致）。

    first_iat 缺省取当前时间（= 新登录）；refresh 轮换时必须传旧值，否则
    「7 天不动才过期」会让活跃用户永久续命，绝对上限就失效了。
    """
    token, _iat = _new_session_token(first_iat)
    get_redis().set(REFRESH_PREFIX + token, str(user_id), ex=settings.refresh_ttl)
    return token


def is_refresh_blacklisted(token: str) -> bool:
    """【双Token改造 新增】校验 refresh_token 是否已在黑名单（登出/被拉黑）。"""
    if not token:
        return False
    return get_redis().exists(REFRESH_BLACKLIST_PREFIX + token) > 0


def check_refresh_token(token: str) -> tuple[int | None, str]:
    """校验刷新令牌，返回 (user_id, reason)。

    reason 取值：
    - `ok`      校验通过；
    - `invalid` 签名错 / 已拉黑 / 不存在（对外统一「无效或已过期」，不泄露具体原因）；
    - `expired` 签名有效但距首签已超过绝对生命周期 —— **必须与 invalid 分开**，
      否则用户只看到「令牌无效」，会以为是系统故障而不是该重新登录。

    顺序刻意保持「签名 → 黑名单 → 存在性 → 绝对超期」：拉黑是主动踢下线，
    优先级必须高于超期判断，否则两者同时成立时报错原因会漂移。
    """
    if not token or not _verify_signature(token):
        return None, "invalid"
    if is_refresh_blacklisted(token):
        return None, "invalid"
    value = get_redis().get(REFRESH_PREFIX + token)
    if value is None:
        return None, "invalid"
    try:
        user_id = int(value)
    except (ValueError, TypeError):
        return None, "invalid"
    if is_absolutely_expired(parse_token_iat(token)):
        logger.info("refresh_token 距首签已超过绝对生命周期，要求重新登录 user_id=%s", user_id)
        return user_id, "expired"
    return user_id, "ok"


def get_refresh_user_id(token: str) -> int | None:
    """校验刷新令牌并返回用户 ID；黑名单中的令牌一律视为无效。

    保留旧签名（调用方零改动）；绝对超期时同样返回 None，与「无效」在服务层不可区分。
    需要区分两种失败原因的调用方请直接用 `check_refresh_token`。
    """
    return check_refresh_token(token)[0]


def rotate_refresh_token(
    old_token: str, user_id: int, first_iat: int | None = None
) -> str:
    """【双Token改造 新增】refresh_token 一次性轮换：签发新令牌并立即删除旧令牌。

    防止刷新后的旧 refresh_token 被重复复用（重放攻击面收口）。
    顺序：先删旧再签新——若签新失败，旧令牌已失效，攻击者与用户均需重新登录（安全优先）。

    【A3】first_iat 缺省时从旧 token 解析，保证绝对生命周期**不因刷新而重置**；
    旧格式 token 解析不到 → 以当前时间为新起点（一次性宽限，此后不再放宽）。
    """
    if first_iat is None:
        first_iat = parse_token_iat(old_token)
    get_redis().delete(REFRESH_PREFIX + old_token)
    return create_refresh_token(user_id, first_iat=first_iat)


def revoke_refresh_token(token: str) -> None:
    """【双Token改造 新增】登出/踢下线：把 refresh_token 加入 Redis 黑名单并删除有效映射。

    黑名单 TTL 与 refresh 有效期一致——超过该时间后令牌本身已过期，黑名单自动清理。
    """
    if not token:
        return
    get_redis().delete(REFRESH_PREFIX + token)
    get_redis().set(REFRESH_BLACKLIST_PREFIX + token, "1", ex=settings.refresh_ttl)
