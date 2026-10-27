"""A3 绝对生命周期护栏（离线：fakeredis + 文件型 sqlite）。

目标：一次登录（含任意次滑动续期与 refresh 轮换）最长存活
`TOKEN_MAX_LIFETIME_SECONDS`，到期必须重新登录。

设计要点（与实现一一对应）：
- iat 编进 token 并由 HMAC 覆盖 → 客户端改 iat 必然验签失败；
- 滑动续期只重置 Redis TTL，不改 token 内容 → iat 天然不变；
- access session 与 refresh token 共用同一个 iat → 两条链路无法互相"洗白"；
- 旧格式 token（无 iat）一律按 legacy 处理，绝不误抛异常。
"""
import json
import time

from app.config import settings
from app.core import security
from conftest import assert_ok
from test_auth_dual_token import _login, seeded_user, sqlite_file_db  # noqa: F401

AUTH = "/api/v1/auth"


def _now() -> int:
    return int(time.time())


def _lifetime() -> int:
    return security.max_token_lifetime()


# ---------------------------------------------------------------------------
# 一、token 自带 iat，且不可篡改
# ---------------------------------------------------------------------------

def test_login_tokens_carry_inline_iat(client, seeded_user):
    """登录下发的 access/refresh 都带 iat，且等于登录时刻（宽限 5s）。"""
    data = _login(client, seeded_user)
    for key in ("access_token", "refresh_token"):
        iat = security.parse_token_iat(data[key])
        assert iat is not None, f"{key} 未携带 iat"
        assert abs(iat - _now()) <= 5


def test_tampered_iat_breaks_signature(client, seeded_user):
    """把 iat 改大以延长寿命 → 签名失效，按无效处理（不是"超期"）。"""
    token = _login(client, seeded_user)["refresh_token"]
    body, sig = token.rsplit(".", 1)
    raw, _, iat = body.rpartition(security._IAT_SEP)
    forged = f"{raw}{security._IAT_SEP}{int(iat) + 100000}.{sig}"
    assert security.check_refresh_token(forged) == (None, "invalid")


def test_parse_iat_fail_safe_on_malformed_input():
    """畸形输入一律返回 None，绝不抛出（否则会把认证打成 500）。"""
    for bad in ("", "no-dot", "abc.def", f"abc{security._IAT_SEP}not-a-number.sig", None):
        assert security.parse_token_iat(bad) is None


# ---------------------------------------------------------------------------
# 二、续期 / 轮换不得重置绝对起点
# ---------------------------------------------------------------------------

def test_refresh_rotation_keeps_first_iat(client, seeded_user):
    """连续刷新 3 次，iat 始终保持登录时的值（不会被刷新刷成 now）。"""
    token = _login(client, seeded_user)["refresh_token"]
    first_iat = security.parse_token_iat(token)
    for _ in range(3):
        token = assert_ok(
            client.post(f"{AUTH}/refresh-token", json={"refresh_token": token})
        )["refresh_token"]
        assert security.parse_token_iat(token) == first_iat


def test_sliding_renewal_does_not_extend_absolute_lifetime():
    """滑动续期只改 TTL，不能延长绝对寿命：刚访问过的会话到期照样失效。"""
    payload = {"user_id": 1, "username": "u", "role": "user", "status": "active"}
    # 距首签只差 10 秒到上限：仍可用，且 TTL 被正常续期（留足余量防秒级抖动）
    token = security.create_session(payload, first_iat=_now() - _lifetime() + 10)
    assert security.get_session(token) == payload
    # 距首签已超上限：即使上一次访问刚续期过，也必须失效并清理
    expired = security.create_session(payload, first_iat=_now() - _lifetime() - 2)
    assert security.get_session(expired) is None
    assert security.get_redis().exists(security.SESSION_PREFIX + expired) == 0


# ---------------------------------------------------------------------------
# 三、超期判定：边界、文案、优先级
# ---------------------------------------------------------------------------

def test_refresh_within_lifetime_still_works(client, seeded_user):
    """未到上限（差 60s）照常刷新，不能误杀。"""
    data = _login(client, seeded_user)
    uid = data["user"]["id"]
    token = security.create_refresh_token(uid, first_iat=_now() - _lifetime() + 60)
    resp = client.post(f"{AUTH}/refresh-token", json={"refresh_token": token})
    assert assert_ok(resp)["access_token"]


def test_expired_refresh_rejected_with_explicit_message(client, seeded_user):
    """超期 refresh → 401，且文案明确指向"最长有效期"，不是笼统的"令牌无效"。"""
    data = _login(client, seeded_user)
    uid = data["user"]["id"]
    token = security.create_refresh_token(uid, first_iat=_now() - _lifetime() - 60)
    resp = client.post(f"{AUTH}/refresh-token", json={"refresh_token": token})
    assert resp.status_code == 401
    message = resp.json()["message"]
    assert "最长有效期" in message
    assert "重新登录" in message


def test_expired_access_session_rejected(client, seeded_user):
    """超期 access session → 401（deps 判定会话无效）。"""
    data = _login(client, seeded_user)
    uid = data["user"]["id"]
    payload = {"user_id": uid, "username": seeded_user[0], "role": "user", "status": "active"}
    token = security.create_session(payload, first_iat=_now() - _lifetime() - 60)
    resp = client.get(f"{AUTH}/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_blacklist_priority_over_expiry():
    """超期 + 已拉黑：按"无效"返回，避免错误原因漂移（拉黑是主动踢下线）。"""
    token = security.create_refresh_token(1, first_iat=_now() - _lifetime() - 60)
    security.get_redis().set(security.REFRESH_BLACKLIST_PREFIX + token, "1", ex=60)
    assert security.check_refresh_token(token) == (None, "invalid")


# ---------------------------------------------------------------------------
# 四、旧格式兼容（升级前签发的 token）
# ---------------------------------------------------------------------------

def test_legacy_refresh_token_accepted_and_upgraded(client, seeded_user):
    """旧格式 refresh（无 iat）仍可刷新，新签 token 带上 iat，此后受上限约束。"""
    data = _login(client, seeded_user)
    uid = data["user"]["id"]
    legacy = security._new_token()  # 刻意不传 iat：模拟升级前的存量令牌
    assert security.parse_token_iat(legacy) is None
    security.get_redis().set(security.REFRESH_PREFIX + legacy, str(uid), ex=settings.refresh_ttl)

    new_tokens = assert_ok(
        client.post(f"{AUTH}/refresh-token", json={"refresh_token": legacy})
    )
    new_iat = security.parse_token_iat(new_tokens["refresh_token"])
    assert new_iat is not None and abs(new_iat - _now()) <= 5


def test_legacy_access_session_invalidated():
    """旧格式 access session 无法补 iat（改内容即毁签名）→ 判无效，由 refresh 无感换新。"""
    legacy = security._new_token()
    payload = {"user_id": 1, "username": "u", "role": "user", "status": "active"}
    security.get_redis().set(
        security.SESSION_PREFIX + legacy, json.dumps(payload), ex=settings.session_ttl
    )
    assert security.get_session(legacy) is None
    assert security.get_redis().exists(security.SESSION_PREFIX + legacy) == 0
