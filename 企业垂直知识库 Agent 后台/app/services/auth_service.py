"""认证业务编排：登录、刷新、当前用户、登出、修改密码。"""
import time

from sqlalchemy.orm import Session

from ..core import security
from ..core.exceptions import BizError, ForbiddenError, NotFoundError, UnauthorizedError
from ..models import STATUS_ACTIVE, User
from ..schemas.user import user_to_dict


def _session_payload(user: User) -> dict:
    """构造写入 Redis session 的用户信息。"""
    return {
        "user_id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "status": user.status,
    }


def login(db: Session, username: str, password: str) -> dict:
    """登录：校验密码，签发访问令牌与刷新令牌。"""
    user = db.query(User).filter(User.username == username).first()
    # 用户名不存在与密码错误统一返回同一提示，避免泄露账号是否存在
    if not user or not security.verify_password(password, user.password_hash):
        raise UnauthorizedError("用户名或密码错误")
    if user.status != STATUS_ACTIVE:
        raise ForbiddenError("账号已被禁用，请联系管理员")

    # 【A3】access 与 refresh 共用同一个首签时间——绝对生命周期从登录这一刻起算
    first_iat = int(time.time())
    access_token = security.create_session(_session_payload(user), first_iat=first_iat)
    refresh_token = security.create_refresh_token(user.id, first_iat=first_iat)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user": user_to_dict(user),
    }


def refresh(db: Session, refresh_token: str) -> dict:
    """【双Token改造】使用刷新令牌换发新令牌（一次性轮换）。

    - 校验 Redis 内 refresh_token 有效性（签名 + 黑名单 + 存在性 + 绝对生命周期）；
    - 成功后立即删除旧 refresh_token 并签发新的（防止重复复用）；
    - 返回新的 access_token + refresh_token（旧调用方兼容：多返回一个字段）。

    【A3】换发时把旧 token 的首签时间原样传给新 token：滑动续期只延长"多久不用过期"，
    绝不重置"最长能活多久"。超过上限时给专属文案，避免用户误判为系统故障。
    """
    user_id, reason = security.check_refresh_token(refresh_token)
    if user_id is None:
        raise UnauthorizedError("刷新令牌无效或已过期")
    if reason == "expired":
        days = max(1, security.max_token_lifetime() // 86400)
        raise UnauthorizedError(f"登录已超过最长有效期（{days} 天），请重新登录")
    user = db.get(User, user_id)
    if not user or user.status != STATUS_ACTIVE:
        raise UnauthorizedError("账号不可用")
    # 旧格式 token 解析不到 iat → 以当前时间为新起点（一次性宽限，此后不再放宽）
    first_iat = security.parse_token_iat(refresh_token)
    if first_iat is None:
        first_iat = int(time.time())
    return {
        "access_token": security.create_session(_session_payload(user), first_iat=first_iat),
        "refresh_token": security.rotate_refresh_token(
            refresh_token, user_id, first_iat=first_iat
        ),
    }


def get_me(db: Session, user_id: int) -> dict:
    """返回当前用户最新信息（从数据库读取，避免 session 数据过期）。"""
    user = db.get(User, user_id)
    if not user:
        raise NotFoundError("用户不存在")
    return user_to_dict(user)


def logout(token: str | None, refresh_token: str | None = None) -> None:
    """【双Token改造】登出：删除 access 会话，并把 refresh_token 加入 Redis 黑名单。

    refresh_token 由前端随登出请求体传入（可选，兼容旧调用不传）；
    拉黑后即使该 refresh_token 尚未过期也无法再次刷新，实现主动踢下线。
    """
    security.delete_session(token)
    if refresh_token:
        security.revoke_refresh_token(refresh_token)


def change_password(db: Session, user_id: int, old_password: str, new_password: str) -> None:
    """修改当前用户密码。"""
    user = db.get(User, user_id)
    if not user:
        raise NotFoundError("用户不存在")
    if not security.verify_password(old_password, user.password_hash):
        raise BizError(400, "原密码错误")
    if old_password == new_password:
        raise BizError(400, "新密码不能与原密码相同")
    user.password_hash = security.hash_password(new_password)
    db.commit()
