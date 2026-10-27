"""工程化收尾回归测试（离线，复用 conftest 固化夹具）。

覆盖点：
  1. sqlite 测试兼容方案（conftest 固化版）：
     文件型 sqlite + StaticPool + BigInteger->INTEGER 后，主键不显式指定即可自增
     （多线程 TestClient 场景不再触发 NOT NULL constraint failed）；
  2. A/B 对比接口登录校验（未携带 token -> 401 统一错误体）；
  3. 刷新令牌接口无效 token -> 401（Redis 黑名单 / 不存在的令牌均拒绝）；
  4. 登出拉黑后 refresh_token 不可再用（服务级，走 fakeredis）。
"""
import pytest

from app.core import security
from app.services import auth_service
from conftest import assert_error, assert_ok

API = "/api/v1"


# ---------------------------------------------------------------------------
# 1. sqlite 兼容方案固化回归
# ---------------------------------------------------------------------------

class TestSqliteCompat:
    def test_primary_key_autoincrement_without_explicit_id(self, eval_db):
        """不显式给 id 连续插入两条 EvalRun，主键应自增为 1、2。"""
        from app.models.eval_run import EvalRun

        for i in range(2):
            eval_db.add(EvalRun(
                dataset_id=1, kb_id=1, model="m", status="done",
                total_items=0, success_items=0, created_by=1,
            ))
        eval_db.commit()
        rows = eval_db.query(EvalRun).order_by(EvalRun.id).all()
        assert [r.id for r in rows] == [1, 2]

    def test_sqlite_engine_is_file_based_and_thread_safe(self, sqlite_engine):
        """夹具为文件型 sqlite + StaticPool（跨线程共享同一连接）。"""
        from sqlalchemy.pool import StaticPool

        assert isinstance(sqlite_engine.pool, StaticPool)
        url = str(sqlite_engine.url)
        assert url.startswith("sqlite:///") and not url.endswith(":memory:") and url != "sqlite://"


# ---------------------------------------------------------------------------
# 2. A/B 对比接口登录校验
# ---------------------------------------------------------------------------

class TestCompareAuth:
    def test_compare_requires_login(self, client):
        """未携带 token 调用 A/B 对比 -> 401 统一错误体。"""
        assert_error(
            client.post(f"{API}/eval/runs/compare", json={"run_id_a": 1, "run_id_b": 2}),
            401, 401,
        )


# ---------------------------------------------------------------------------
# 3. 刷新令牌 / 登出拉黑
# ---------------------------------------------------------------------------

class TestRefreshAndBlacklist:
    def test_refresh_token_with_invalid_token_returns_401(self, client):
        """无效 / 不存在的 refresh_token -> 401 统一错误体。"""
        assert_error(
            client.post(f"{API}/auth/refresh-token", json={"refresh_token": "not-a-valid-token"}),
            401, 401,
        )

    def test_refresh_token_missing_body_returns_422(self, client):
        """缺参 -> 422 参数校验（统一错误体）。"""
        assert_error(client.post(f"{API}/auth/refresh-token", json={}), 422, 422)

    def test_blacklisted_refresh_token_rejected_by_service(self):
        """登出拉黑后：refresh() 应拒绝已拉黑令牌（fakeredis 黑名单生效）。"""
        from app.core.exceptions import UnauthorizedError

        token = security.create_refresh_token(user_id=1)
        auth_service.logout(None, token)  # 登出：refresh_token 进黑名单
        # 黑名单令牌在校验阶段即被拒，不会触达 db 查询，传 None 安全
        with pytest.raises(UnauthorizedError):
            auth_service.refresh(None, token)


# ---------------------------------------------------------------------------
# 4. 工程化收尾：Trace 链路 / 统一响应体 / 异常分级
# ---------------------------------------------------------------------------

class TestTraceAndEnvelope:
    def test_trace_id_in_response_header(self, client):
        """每个响应头都回写全局唯一 X-Trace-ID；透传上游 X-Request-ID 时复用。"""
        r1 = client.get("/")
        assert r1.status_code == 200
        trace_id_1 = r1.headers.get("X-Trace-ID")
        assert trace_id_1 and len(trace_id_1) >= 16

        r2 = client.get("/", headers={"X-Request-ID": "my-trace-001"})
        assert r2.headers.get("X-Trace-ID") == "my-trace-001"

        # 无上游头时每次请求生成不同 TraceID
        r3 = client.get("/")
        assert r3.headers.get("X-Trace-ID") != trace_id_1

    def test_error_responses_carry_trace_id_header(self, client):
        """错误响应同样回写 X-Trace-ID，便于与服务端 error 日志串联。"""
        resp = client.post(f"{API}/eval/runs/compare", json={"run_id_a": 1, "run_id_b": 2})
        assert resp.status_code == 401
        assert resp.headers.get("X-Trace-ID")

    def test_unified_envelope_all_endpoint_families(self, client, admin_client, monkeypatch):
        """各路由族返回结构完全一致：恒为 {code, message, data} 三键。

        注意：admin_client 与 client 共用同一 TestClient 实例（鉴权覆盖生效于
        整个用例期间），因此本用例内所有请求均为"管理员已登录"视角；
        未登录 401 场景见上方 test_compare_requires_login。
        """
        import app.services.eval_service as eval_service

        # compare 触库，这里只验证响应体结构，mock 掉服务层
        monkeypatch.setattr(
            eval_service, "compare_runs",
            lambda db, a, b: {"run_a": {}, "run_b": {}, "summary": [], "rows": []},
        )
        bodies = [
            client.get("/").json(),                                            # 根路由 200
            client.post(f"{API}/eval/runs/compare", json={}).json(),           # 422 校验失败
            client.post(f"{API}/auth/refresh-token", json={"refresh_token": "bad"}).json(),  # 401
            admin_client.get(f"{API}/eval/runs/1/badcases?metric=nope").json(),  # 422 白名单
        ]
        for body in bodies:
            assert set(body.keys()) == {"code", "message", "data"}

    def test_unhandled_exception_prod_vs_dev(self, client, monkeypatch):
        """未捕获异常：生产只返回简洁提示；开发环境 data 携带完整堆栈。"""
        import app.config as config_mod
        from fastapi.testclient import TestClient

        from app.main import app as fastapi_app

        @fastapi_app.get("/__test_boom__")
        def _boom_route():
            raise RuntimeError("boom-for-test")

        # raise_server_exceptions=False：让异常真正走全局异常处理器（而非测试器直接抛出）
        with TestClient(fastapi_app, raise_server_exceptions=False) as c:
            try:
                # 生产（默认）：data 为 None，不暴露堆栈
                monkeypatch.setattr(config_mod.settings, "debug", False)
                resp = c.get("/__test_boom__")
                body = resp.json()
                assert resp.status_code == 500
                assert body["code"] == 500 and body["data"] is None
                assert "boom-for-test" not in body["message"]
                assert resp.headers.get("X-Trace-ID")

                # 开发：data 携带完整堆栈
                monkeypatch.setattr(config_mod.settings, "debug", True)
                resp = c.get("/__test_boom__")
                body = resp.json()
                assert resp.status_code == 500
                assert body["data"]["trace_id"] == resp.headers.get("X-Trace-ID")
                assert "RuntimeError: boom-for-test" in body["data"]["stack"]
            finally:
                fastapi_app.router.routes = [
                    r for r in fastapi_app.router.routes if getattr(r, "path", "") != "/__test_boom__"
                ]
