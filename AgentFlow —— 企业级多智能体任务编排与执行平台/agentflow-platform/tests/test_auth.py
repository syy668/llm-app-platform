"""鉴权模块基础用例（12 条）。

覆盖 PRD W1 验收标准：「登录 / 刷新 / 注销 12 条鉴权用例通过」。
每条用例只校验一个行为，断言错误码而非仅状态码，便于定位契约回归。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import AsyncClient

from app.core.security import TokenType, create_token

pytestmark = [pytest.mark.unit, pytest.mark.auth]

LOGIN_URL = "/api/v1/auth/login"
REFRESH_URL = "/api/v1/auth/refresh"
LOGOUT_URL = "/api/v1/auth/logout"
ME_URL = "/api/v1/auth/me"

# 与 tests/conftest.py::TEST_PASSWORD 保持一致
PASSWORD = "Passw0rd!"


def _token_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


async def _login(client: AsyncClient, username: str = "admin_user", password: str = PASSWORD) -> dict:
    resp = await client.post(LOGIN_URL, json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# ---------------------------------------------------------------------------
# 1~3 登录
# ---------------------------------------------------------------------------
async def test_login_success_returns_token_pair(client: AsyncClient) -> None:
    body = await _login(client, "admin_user")
    assert body["access_token"] and body["refresh_token"]
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 120 * 60


async def test_login_with_wrong_password_returns_401(client: AsyncClient) -> None:
    resp = await client.post(LOGIN_URL, json={"username": "admin_user", "password": "wrong-password"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_INVALID_CREDENTIALS"


async def test_login_with_disabled_account_returns_403(client: AsyncClient) -> None:
    resp = await client.post(LOGIN_URL, json={"username": "disabled_user", "password": PASSWORD})
    assert resp.status_code == 403
    assert resp.json()["code"] == "AUTH_USER_DISABLED"


# ---------------------------------------------------------------------------
# 4~8 access token 校验
# ---------------------------------------------------------------------------
async def test_me_with_valid_access_token_returns_profile(client: AsyncClient) -> None:
    body = await _login(client, "approver_user")
    resp = await client.get(ME_URL, headers=_token_headers(body["access_token"]))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["username"] == "approver_user"
    assert data["role"] == "approver"


async def test_me_without_token_returns_401(client: AsyncClient) -> None:
    resp = await client.get(ME_URL)
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_TOKEN_MISSING"


async def test_me_with_malformed_token_returns_401(client: AsyncClient) -> None:
    resp = await client.get(ME_URL, headers=_token_headers("this-is-not-a-jwt"))
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_TOKEN_INVALID"


async def test_me_with_expired_access_token_returns_401(
    client: AsyncClient, seeded_users: dict[str, int]
) -> None:
    expired = create_token(
        user_id=seeded_users["admin_user"],
        username="admin_user",
        role="admin",
        token_type=TokenType.ACCESS,
        expires_delta=timedelta(seconds=-10),
    )
    resp = await client.get(ME_URL, headers=_token_headers(expired.token))
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_TOKEN_EXPIRED"


async def test_me_rejects_refresh_token_as_access(client: AsyncClient) -> None:
    body = await _login(client)
    resp = await client.get(ME_URL, headers=_token_headers(body["refresh_token"]))
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_TOKEN_TYPE_INVALID"


# ---------------------------------------------------------------------------
# 9 刷新轮换
# ---------------------------------------------------------------------------
async def test_refresh_rotates_token_and_rejects_replay(client: AsyncClient) -> None:
    old_refresh = (await _login(client))["refresh_token"]

    first = await client.post(REFRESH_URL, json={"refresh_token": old_refresh})
    assert first.status_code == 200
    new_pair = first.json()["data"]
    assert new_pair["refresh_token"] != old_refresh

    # 新 access token 可用
    me = await client.get(ME_URL, headers=_token_headers(new_pair["access_token"]))
    assert me.status_code == 200

    # 旧 refresh 已一次性消费 -> 重放被拒
    replay = await client.post(REFRESH_URL, json={"refresh_token": old_refresh})
    assert replay.status_code == 401
    assert replay.json()["code"] == "AUTH_REFRESH_REVOKED"


# ---------------------------------------------------------------------------
# 10 注销拉黑
# ---------------------------------------------------------------------------
async def test_logout_blacklists_access_token(client: AsyncClient) -> None:
    body = await _login(client)
    headers = _token_headers(body["access_token"])

    assert (await client.get(ME_URL, headers=headers)).status_code == 200

    logout = await client.post(LOGOUT_URL, headers=headers)
    assert logout.status_code == 200

    after = await client.get(ME_URL, headers=headers)
    assert after.status_code == 401
    assert after.json()["code"] == "AUTH_TOKEN_REVOKED"


# ---------------------------------------------------------------------------
# 11~12 角色校验 require_role
# ---------------------------------------------------------------------------
async def test_viewer_forbidden_on_admin_only_route(protected_client: AsyncClient) -> None:
    body = await _login(protected_client, "viewer_user")
    resp = await protected_client.get("/admin-only", headers=_token_headers(body["access_token"]))
    assert resp.status_code == 403
    payload = resp.json()
    assert payload["code"] == "FORBIDDEN"
    assert payload["data"]["detail"]["required_roles"] == ["admin"]
    assert payload["data"]["detail"]["actual_role"] == "viewer"


async def test_admin_allowed_on_admin_only_route(protected_client: AsyncClient) -> None:
    body = await _login(protected_client, "admin_user")
    resp = await protected_client.get("/admin-only", headers=_token_headers(body["access_token"]))
    assert resp.status_code == 200
    assert resp.json()["data"]["role"] == "admin"
