"""鉴权：身份归项目一，项目二只校验不建用户表。

为什么不在项目二建 users 表：
  两个前端（5174 / 8010 自己的 5173）共用同一批账号，重复建表必然出现
  「这边是 admin、那边已被禁用」的口径分裂。项目二只做一件事——
  拿上游 token 调 `GET /auth/me`，读回 role，判断是否 admin。

失败口径（必须区分，否则排障会跑偏）：
  - 没带 token            → 401
  - 上游说 token 无效     → 401（不要把 401 透传成 500）
  - 上游连不上            → 503（配置/服务问题，不是用户没权限）
  - role 不是 admin       → 403
"""

import logging
import time
from dataclasses import dataclass

import httpx
from fastapi import Depends, Header, HTTPException

from app.core.config import settings

logger = logging.getLogger("evalagent.auth")

ADMIN_ROLE = "admin"

_cache: dict[str, tuple["CurrentUser", float]] = {}


@dataclass(frozen=True)
class CurrentUser:
    id: int | None
    username: str
    role: str
    display_name: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN_ROLE


def _auth_base() -> str:
    return (settings.AUTH_UPSTREAM_BASE_URL or settings.UPSTREAM_RAG_BASE_URL).rstrip("/")


def _cache_get(token: str) -> "CurrentUser | None":
    hit = _cache.get(token)
    if not hit:
        return None
    user, expire_at = hit
    if expire_at < time.time():
        _cache.pop(token, None)
        return None
    return user


def clear_auth_cache() -> None:
    _cache.clear()


async def _fetch_me(token: str) -> CurrentUser:
    url = f"{_auth_base()}/api/v1/auth/me"
    try:
        async with httpx.AsyncClient(
            timeout=settings.AUTH_TIMEOUT,
            # 身份服务是上游项目一（本机 8010），同样不能交给系统代理
            trust_env=settings.HTTP_TRUST_ENV,
        ) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        # 上游不可用 ≠ 用户无权限：503 让运维一眼看出是服务问题
        raise HTTPException(
            status_code=503,
            detail=f"身份服务不可用（{_auth_base()}）：{type(exc).__name__}",
        ) from exc

    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="token 已失效，请重新登录")
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=503,
            detail=f"身份服务返回 {resp.status_code}，无法完成鉴权",
        )

    try:
        body = resp.json()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="身份服务返回非 JSON") from exc

    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise HTTPException(status_code=503, detail="身份服务未返回用户信息")

    return CurrentUser(
        id=data.get("id"),
        username=str(data.get("username") or ""),
        role=str(data.get("role") or ""),
        display_name=data.get("display_name"),
    )


async def get_current_user(authorization: str | None = Header(default=None)) -> CurrentUser:
    """从 Authorization: Bearer <token> 解析当前用户（带 60s 缓存）。"""
    if not settings.AUTH_ENFORCE:
        # 仅本地调试可用：AUTH_ENFORCE=false 时视为本地管理员
        return CurrentUser(id=None, username="local-debug", role=ADMIN_ROLE, display_name="本地调试")

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="未提供有效凭据（需要 Authorization: Bearer <token>）")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="token 为空")

    cached = _cache_get(token)
    if cached is not None:
        return cached

    user = await _fetch_me(token)
    _cache[token] = (user, time.time() + max(5, settings.AUTH_CACHE_TTL))
    return user


async def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """写操作守卫：必须是管理员。"""
    if not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail=f"仅管理员可修改评测中台配置（当前角色：{user.role or '未知'}）",
        )
    return user
