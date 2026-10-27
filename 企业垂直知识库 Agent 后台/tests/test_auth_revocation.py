"""批次1-修复A 回归测试：禁用/删除/降级后旧 Token 立即失效。

背景（缺陷）：`get_current_user` 原先只读 Redis session 快照，不查数据库；
而 `get_session` 每次访问都滑动续期 → token 只要持续使用就永不过期。
后果：管理员软删除/禁用账号、或把 admin 降级为 user 后，对方凭旧 token 仍可无限期使用。

修复后口径（本文件逐条验证）：
1. 用户被禁用（status=disabled）→ 旧 token **下一次请求**即 401，且会话被清除；
2. 用户被物理删除 → 旧 token 401（提示账号不存在）；
3. 管理员被降级 → 旧 token 立即失去管理接口权限（403），无需等 token 过期；
4. 禁用后 refresh_token 刷新同样被拒（原有 DB 校验，防绕过）；
5. 正常用户不受影响（多次请求均 200）。

测试方案与 tests/test_auth_dual_token.py 一致：文件型 sqlite（StaticPool 跨线程共享）
+ fakeredis，走真实 /auth/login → 真实 Bearer token → 真实 get_current_user，
不覆盖任何依赖，确保验证的是线上真实链路。
"""
import pytest

from app.core import security
from conftest import assert_error, assert_ok

AUTH = "/api/v1/auth"
USERS = "/api/v1/users"
PASSWORD = "password-123456"


@pytest.fixture
def auth_db(tmp_path, monkeypatch):
    """把全局 engine / SessionLocal 换成文件型 sqlite，并清空 fakeredis。

    注意：fixture 必须在 client 之后被解析（见各用例参数顺序），
    与 test_auth_dual_token.py 的既有做法保持一致。
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
        f"sqlite:///{tmp_path / 'revocation.db'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))

    security.get_redis().flushall()  # 隔离用例间 session / refresh_token
    yield engine
    security.get_redis().flushall()


def _seed_user(username: str, role: str = "user", status: str = "active") -> int:
    """直接在库里预置账号，返回 user_id。"""
    from app.core.database import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        user = User(
            username=username,
            password_hash=security.hash_password(PASSWORD),
            display_name=f"测试-{username}",
            role=role,
            status=status,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id
    finally:
        db.close()


def _set_status(user_id: int, status: str) -> None:
    """模拟管理员在数据库中直接改状态（绕过任何应用层缓存）。"""
    from app.core.database import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        user.status = status
        db.commit()
    finally:
        db.close()


def _set_role(user_id: int, role: str) -> None:
    from app.core.database import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        user.role = role
        db.commit()
    finally:
        db.close()


def _delete_user_row(user_id: int) -> None:
    from app.core.database import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        db.delete(db.get(User, user_id))
        db.commit()
    finally:
        db.close()


def _login(client, username: str) -> dict:
    """真实登录，返回 {access_token, refresh_token, user}。"""
    resp = client.post(f"{AUTH}/login", json={"username": username, "password": PASSWORD})
    return assert_ok(resp)


def _me(client, token: str):
    return client.get(f"{AUTH}/me", headers={"Authorization": f"Bearer {token}"})


class TestDisabledUserRevoked:
    """禁用 / 删除 → 旧 token 立即失效。"""

    def test_disable_in_db_rejects_old_token_on_next_request(self, client, auth_db):
        """核心用例：登录后管理员在库里置 disabled，下一次请求立刻 401。"""
        uid = _seed_user("revoke_user")
        tokens = _login(client, "revoke_user")
        access = tokens["access_token"]

        # 禁用前：正常可用
        assert assert_ok(_me(client, access))["username"] == "revoke_user"

        # 管理员在数据库中禁用该账号
        _set_status(uid, "disabled")

        # 禁用后：同一个 token 的下一次请求立刻失效
        body = assert_error(_me(client, access), 401, 401)
        assert "禁用" in body["message"]

        # 会话已被主动清除：即便有人再次拿这个 token 来，也走「无有效会话」分支
        assert security.get_session(access) is None
        assert_error(_me(client, access), 401, 401)

    def test_disable_via_admin_api_revokes_target_token(self, client, auth_db):
        """走真实管理接口 DELETE /users/{id}（软删除=置 disabled），旧 token 同样立即失效。"""
        _seed_user("admin_root", role="admin")
        target_id = _seed_user("fired_user")

        admin_token = _login(client, "admin_root")["access_token"]
        user_token = _login(client, "fired_user")["access_token"]

        assert assert_ok(_me(client, user_token))["username"] == "fired_user"

        resp = client.delete(
            f"{USERS}/{target_id}", headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert_ok(resp)

        assert_error(_me(client, user_token), 401, 401)

    def test_hard_deleted_user_rejected(self, client, auth_db):
        """库中物理删除该用户 → 旧 token 401 且提示账号不存在。"""
        uid = _seed_user("gone_user")
        access = _login(client, "gone_user")["access_token"]

        _delete_user_row(uid)

        body = assert_error(_me(client, access), 401, 401)
        assert "不存在" in body["message"]
        assert security.get_session(access) is None

    def test_disabled_user_cannot_refresh(self, client, auth_db):
        """禁用后 refresh_token 换发也被拒（防绕过：不能靠刷新续命）。"""
        uid = _seed_user("refresh_victim")
        tokens = _login(client, "refresh_victim")

        _set_status(uid, "disabled")

        assert_error(
            client.post(f"{AUTH}/refresh-token", json={"refresh_token": tokens["refresh_token"]}),
            401,
            401,
        )

    def test_reenable_requires_relogin(self, client, auth_db):
        """恢复启用后旧 token 仍不可用（会话已被清除），必须重新登录。"""
        uid = _seed_user("back_user")
        access = _login(client, "back_user")["access_token"]

        _set_status(uid, "disabled")
        assert_error(_me(client, access), 401, 401)

        _set_status(uid, "active")
        # 旧 token 对应会话已删除，不会自动复活
        assert_error(_me(client, access), 401, 401)
        # 重新登录后正常
        new_token = _login(client, "back_user")["access_token"]
        assert assert_ok(_me(client, new_token))["username"] == "back_user"


class TestRoleDowngradeEffective:
    """降级立即生效：角色以数据库为准，session 快照不再作为权限依据。"""

    def test_admin_downgrade_loses_admin_api_immediately(self, client, auth_db):
        uid = _seed_user("downgraded_admin", role="admin")
        access = _login(client, "downgraded_admin")["access_token"]

        # 降级前：可访问仅管理员接口
        resp = client.get("/api/v1/stats/overview", headers={"Authorization": f"Bearer {access}"})
        assert_ok(resp)

        _set_role(uid, "user")

        # 降级后：同一个 token 立即 403
        assert_error(
            client.get("/api/v1/stats/overview", headers={"Authorization": f"Bearer {access}"}),
            403,
            403,
        )

    def test_admin_promotion_effective_immediately(self, client, auth_db):
        """反向验证：升权也即时生效，证明角色确实取自 DB 而非缓存。"""
        uid = _seed_user("promoted_user", role="user")
        access = _login(client, "promoted_user")["access_token"]

        assert_error(
            client.get("/api/v1/stats/overview", headers={"Authorization": f"Bearer {access}"}),
            403,
            403,
        )

        _set_role(uid, "admin")

        assert_ok(
            client.get("/api/v1/stats/overview", headers={"Authorization": f"Bearer {access}"})
        )

    def test_me_reflects_db_role(self, client, auth_db):
        """当前用户信息与权限判定同源（均来自 DB），不会出现「显示 admin 却无权限」。"""
        uid = _seed_user("role_view_user", role="user")
        access = _login(client, "role_view_user")["access_token"]

        assert assert_ok(_me(client, access))["role"] == "user"

        _set_role(uid, "admin")
        assert assert_ok(_me(client, access))["role"] == "admin"


class TestActiveUserUnaffected:
    """回归保护：正常账号的行为与修复前完全一致。"""

    def test_active_user_can_call_repeatedly(self, client, auth_db):
        _seed_user("normal_user")
        access = _login(client, "normal_user")["access_token"]

        for _ in range(3):
            data = assert_ok(_me(client, access))
            assert data["username"] == "normal_user"
            assert data["status"] == "active"

    def test_invalid_token_still_rejected(self, client, auth_db):
        """伪造/无效 token：401，且不会误伤（不需要库里存在对应用户）。"""
        assert_error(_me(client, "forged-token-without-signature"), 401, 401)
        assert_error(_me(client, ""), 401, 401)

    def test_missing_token_rejected(self, client, auth_db):
        assert_error(client.get(f"{AUTH}/me"), 401, 401)
