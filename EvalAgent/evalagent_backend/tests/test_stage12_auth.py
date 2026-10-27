"""第十二步：鉴权「只校验不建表」的失败口径。

项目二不建 users 表，身份全归上游项目一（8010 /auth/me）。这里锁住三类
最容易在微服务里搞错的边界：

1. **上游 401 不能被透传成 500**：token 失效 → 401；上游连不上 → 503。
   两者排障方向完全不同，混了就会「用户没权限」被误判成「服务挂了」。
2. **缓存命中**：60s 内重复请求不该反复打上游（降上游压力 + 提速）。
3. **AUTH_ENFORCE=false 只允许本地调试**：不能因为省事把生产鉴权关掉。
"""

from __future__ import annotations

import time

import pytest
from fastapi import HTTPException
from httpx import ConnectError, Response

from app.core import auth as auth_mod
from app.core.config import settings
from app.core.auth import CurrentUser, _cache, _cache_get, clear_auth_cache


@pytest.fixture(autouse=True)
def _clean():
    clear_auth_cache()
    yield
    clear_auth_cache()


# ============================ CurrentUser 判定 ============================
def test_current_user_is_admin():
    assert CurrentUser(id=1, username="a", role="admin").is_admin is True
    assert CurrentUser(id=2, username="b", role="user").is_admin is False


# ============================ 缓存命中与过期 ============================
def test_cache_hit_returns_user_without_upstream(monkeypatch):
    _cache["tok-1"] = (CurrentUser(id=1, username="u", role="admin"), time.time() + 60)
    assert _cache_get("tok-1") is not None


def test_cache_expired_is_purged(monkeypatch):
    _cache["tok-old"] = (CurrentUser(id=1, username="u", role="admin"), time.time() - 1)
    assert _cache_get("tok-old") is None
    assert "tok-old" not in _cache


# ============================ 失败口径（核心）============================
@pytest.mark.asyncio
async def test_upstream_401_becomes_401_not_500(monkeypatch):
    """token 失效必须原样 401，绝不能透传成 500。"""
    async def fake_get(url, headers):
        return Response(401, text='{"detail": "invalid"}', request=None)

    class _C:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        get = staticmethod(fake_get)

    monkeypatch.setattr(auth_mod.httpx, "AsyncClient", lambda **kw: _C())
    with pytest.raises(HTTPException) as ei:
        await auth_mod._fetch_me("bad-token")
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_upstream_unreachable_becomes_503(monkeypatch):
    """上游连不上是服务问题 → 503，不是用户没权限。"""
    async def fake_get(url, headers):
        raise ConnectError("connection refused")

    class _C:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        get = staticmethod(fake_get)

    monkeypatch.setattr(auth_mod.httpx, "AsyncClient", lambda **kw: _C())
    with pytest.raises(HTTPException) as ei:
        await auth_mod._fetch_me("any")
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_upstream_500_becomes_503(monkeypatch):
    async def fake_get(url, headers):
        return Response(500, text="boom", request=None)

    class _C:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        get = staticmethod(fake_get)

    monkeypatch.setattr(auth_mod.httpx, "AsyncClient", lambda **kw: _C())
    with pytest.raises(HTTPException) as ei:
        await auth_mod._fetch_me("any")
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_non_json_body_becomes_503(monkeypatch):
    async def fake_get(url, headers):
        return Response(200, text="<html>not json</html>", request=None)

    class _C:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        get = staticmethod(fake_get)

    monkeypatch.setattr(auth_mod.httpx, "AsyncClient", lambda **kw: _C())
    with pytest.raises(HTTPException) as ei:
        await auth_mod._fetch_me("any")
    assert ei.value.status_code == 503


@pytest.mark.asyncio
async def test_missing_user_data_becomes_503(monkeypatch):
    async def fake_get(url, headers):
        return Response(200, json={"code": 0, "data": None}, request=None)

    class _C:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        get = staticmethod(fake_get)

    monkeypatch.setattr(auth_mod.httpx, "AsyncClient", lambda **kw: _C())
    with pytest.raises(HTTPException) as ei:
        await auth_mod._fetch_me("any")
    assert ei.value.status_code == 503


# ============================ get_current_user ============================
@pytest.mark.asyncio
async def test_missing_bearer_is_401(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENFORCE", True)
    with pytest.raises(HTTPException) as ei:
        await auth_mod.get_current_user(authorization=None)
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_empty_token_is_401(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENFORCE", True)
    with pytest.raises(HTTPException) as ei:
        await auth_mod.get_current_user(authorization="Bearer   ")
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_auth_disabled_returns_local_debug_admin(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENFORCE", False)
    user = await auth_mod.get_current_user(authorization=None)
    assert user.is_admin is True
    assert user.username == "local-debug"


# ============================ require_admin ============================
@pytest.mark.asyncio
async def test_require_admin_blocks_non_admin():
    """写操作守卫：非管理员必须 403，且报错写清当前角色。"""
    user = CurrentUser(id=2, username="u", role="user")
    with pytest.raises(HTTPException) as ei:
        await auth_mod.require_admin(user=user)
    assert ei.value.status_code == 403
    assert "user" in ei.value.detail


@pytest.mark.asyncio
async def test_require_admin_allows_admin():
    user = CurrentUser(id=1, username="a", role="admin")
    out = await auth_mod.require_admin(user=user)
    assert out is user
