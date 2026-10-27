"""双 Token 机制回归测试（离线：fakeredis + sqlite 内存库）。

覆盖：
- 登录同时下发 access_token + refresh_token；
- POST /auth/refresh-token：轮换签发双新令牌；旧 refresh_token 复用 → 401（一次性轮换）；
- 旧 POST /auth/refresh 兼容（同样轮换，返回双 token）；
- logout 拉黑：登出后 refresh_token 刷新 → 401（Redis 黑名单 + 主动踢下线）；
- Redis 存储规则：refresh_token 为 key、value=user_id、TTL 与 refresh 有效期一致；
- RBAC：get_current_admin 与 require_admin 语义一致；普通用户改配置 403。
"""
import pytest

from app.api import deps
from app.config import settings
from app.core import security
from conftest import assert_error, assert_ok

AUTH = "/api/v1/auth"


@pytest.fixture
def sqlite_file_db(tmp_path, monkeypatch):
    """把全局 engine 换成文件型 sqlite（StaticPool 跨线程共享同一连接）。

    conftest 的 :memory: sqlite 在 anyio 工作线程池中每请求独立建库，
    无法承载真实登录链路；文件库保证测试线程与应用线程看到同一份数据。
    """
    from sqlalchemy import BigInteger, create_engine
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import app.core.database as database
    from app.core.database import Base
    from app import models as _models  # noqa: F401  注册 ORM 到 Base.metadata

    @compiles(BigInteger, "sqlite")
    def _render_bigint_as_integer(type_, compiler, **kw):  # 恢复 INTEGER PRIMARY KEY 自增
        return "INTEGER"

    engine = create_engine(
        f"sqlite:///{tmp_path / 'auth_test.db'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    yield


@pytest.fixture
def seeded_user(client, sqlite_file_db):
    """在文件库中直接预置普通用户账号，返回 (username, password)。"""
    from app.core.database import SessionLocal
    from app.core.security import hash_password
    from app.models import User

    username, password = "dualtoken_user", "password-123456"
    db = SessionLocal()
    try:
        if not db.query(User).filter(User.username == username).first():
            db.add(User(
                username=username,
                password_hash=hash_password(password),
                display_name="双Token测试用户",
                role="user",
                status="active",
            ))
            db.commit()
    finally:
        db.close()
    return username, password


def _login(client, seeded_user):
    username, password = seeded_user
    resp = client.post(f"{AUTH}/login", json={"username": username, "password": password})
    return assert_ok(resp)


class TestDualToken:
    def test_login_returns_both_tokens(self, client, seeded_user):
        """登录响应同时包含 access_token 与 refresh_token。"""
        data = _login(client, seeded_user)
        assert data["access_token"]
        assert data["refresh_token"]
        assert data["user"]["username"] == seeded_user[0]

    def test_access_ttl_is_30min(self):
        """access（session）TTL 默认 30 分钟。"""
        assert settings.session_ttl == 1800

    def test_refresh_token_storage_rule(self, client, seeded_user):
        """Redis 规则：refresh_token 为 key、value=user_id、TTL=refresh 有效期。"""
        data = _login(client, seeded_user)
        token = data["refresh_token"]
        r = security.get_redis()
        key = security.REFRESH_PREFIX + token
        assert r.get(key) == str(data["user"]["id"])
        ttl = r.ttl(key)
        assert 0 < ttl <= settings.refresh_ttl

    def test_refresh_token_rotation(self, client, seeded_user):
        """刷新成功签发双新令牌；旧 refresh_token 立即失效（一次性轮换）。"""
        data = _login(client, seeded_user)
        old_refresh = data["refresh_token"]

        resp = client.post(f"{AUTH}/refresh-token", json={"refresh_token": old_refresh})
        new_tokens = assert_ok(resp)
        assert new_tokens["access_token"]
        assert new_tokens["refresh_token"]
        assert new_tokens["refresh_token"] != old_refresh

        # 旧 refresh_token 复用 → 401（已删除，防止重放）
        assert_error(
            client.post(f"{AUTH}/refresh-token", json={"refresh_token": old_refresh}),
            401, 401,
        )

    def test_refresh_blacklist_on_logout(self, client, seeded_user):
        """登出把 refresh_token 拉黑：后续刷新 401（主动踢下线）。"""
        data = _login(client, seeded_user)
        access, refresh = data["access_token"], data["refresh_token"]

        resp = client.post(
            f"{AUTH}/logout",
            json={"refresh_token": refresh},
            headers={"Authorization": f"Bearer {access}"},
        )
        assert_ok(resp)
        assert security.is_refresh_blacklisted(refresh)
        assert_error(
            client.post(f"{AUTH}/refresh-token", json={"refresh_token": refresh}),
            401, 401,
        )

    def test_logout_without_body_still_works(self, client, seeded_user):
        """旧客户端登出（无请求体）兼容：仅删除 access 会话，不报错。"""
        data = _login(client, seeded_user)
        resp = client.post(
            f"{AUTH}/logout",
            headers={"Authorization": f"Bearer {data['access_token']}"},
        )
        assert_ok(resp)

    def test_legacy_refresh_endpoint_compatible(self, client, seeded_user):
        """旧 /auth/refresh 保留：同样走一次性轮换并返回双 token。"""
        data = _login(client, seeded_user)
        resp = client.post(f"{AUTH}/refresh", json={"refresh_token": data["refresh_token"]})
        new_tokens = assert_ok(resp)
        assert set(new_tokens.keys()) == {"access_token", "refresh_token"}

    def test_invalid_signature_rejected(self, client):
        """伪造签名 / 任意字符串 → 401。"""
        assert_error(
            client.post(f"{AUTH}/refresh-token", json={"refresh_token": "not-a-token"}),
            401, 401,
        )


class TestRBAC:
    def test_get_current_admin_alias_compatible(self):
        """get_current_admin 为正式依赖；require_admin 保留为向后兼容别名。"""
        assert deps.require_admin is deps.get_current_admin

    def test_admin_dependency_rejects_normal_user(self, user_client, monkeypatch):
        """普通用户（role=user）访问管理员依赖 → 403（业务语义验证）。"""
        from app.core.exceptions import ForbiddenError

        normal = deps.CurrentUser(
            id=2, username="user", display_name=None, role="user", status="active",
        )
        with pytest.raises(ForbiddenError):
            deps.get_current_admin(normal)

    def test_system_config_write_admin_only(self, user_client):
        """系统配置修改（含服务商切换字段）普通用户 403。"""
        assert_error(
            user_client.put("/api/v1/system/config", json={"llm_provider": "openai"}),
            403, 403,
        )
