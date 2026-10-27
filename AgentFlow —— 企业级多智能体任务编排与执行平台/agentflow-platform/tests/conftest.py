"""pytest 公共夹具。

设计目标：**不依赖 MySQL / Redis 容器**即可跑通鉴权用例。
- MySQL -> SQLite 内存库（StaticPool 保证同一连接，create_all 建 7 张表）
- Redis -> fakeredis（异步客户端，行为与 redis-py 一致）
- FastAPI 依赖通过 dependency_overrides 替换，业务代码零改动
"""

from __future__ import annotations

import os

# 必须在导入 app.* 之前设置：Settings 为 import 期单例（lru_cache）
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789")
os.environ.setdefault("BCRYPT_ROUNDS", "4")  # 测试提速：4 轮足够验证哈希逻辑
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("MOCK_EXTERNAL_RAG", "true")
os.environ.setdefault("MOCK_EXTERNAL_EVAL", "true")
# ---- W2 夹具相关开关（必须在导入 app.core.w2_config 之前生效）----
os.environ.setdefault("LLM_MOCK", "true")  # 离线跑通全链路，不发外部 LLM 请求
os.environ.setdefault("CHECKPOINTER_BACKEND", "memory")  # 不连 Redis
os.environ.setdefault("CHART_USE_PROCESS_POOL", "false")  # 单测不起进程池
os.environ.setdefault("TOOL_RETRY_BACKOFF_ENABLED", "false")  # 关闭退避等待，测试不 sleep
os.environ.setdefault("NOTIFY_DRY_RUN", "true")  # 恒 Mock，不出网

from collections.abc import AsyncIterator  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from fastapi import Depends, FastAPI  # noqa: E402
from fakeredis.aioredis import FakeRedis  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.deps import require_role  # noqa: E402
from app.core.exceptions import register_exception_handlers  # noqa: E402
from app.core.redis_client import get_redis  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.core.w3_config import w3  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.schemas.auth import CurrentUser  # noqa: E402
from app.schemas.enums import UserRole  # noqa: E402
from app.schemas.orm_models import User  # noqa: E402
from app.routers.api_router import api_router  # noqa: E402

# 测试账号：与 README-W1.md 的演示账号保持一致（生产环境必须改密）
TEST_PASSWORD = "Passw0rd!"
TEST_USERS: tuple[tuple[str, UserRole, int], ...] = (
    ("admin_user", UserRole.ADMIN, 1),
    ("approver_user", UserRole.APPROVER, 1),
    ("viewer_user", UserRole.VIEWER, 1),
    ("disabled_user", UserRole.VIEWER, 0),
)


@pytest.fixture(autouse=True)
def _sync_trace_writes(monkeypatch: pytest.MonkeyPatch):
    """任务 13 兼容层：测试统一走同步 Trace 落库，保证 e2e 断言即时可见。

    异步缓冲由 test_task13_trace_pipeline.py 的专项单测覆盖（显式 buffered=True）。
    """
    monkeypatch.setattr(w3, "TRACE_ASYNC_BUFFER_ENABLED", False)


@pytest_asyncio.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """SQLite 内存库 + 全量建表（7 张表 DDL 与 MySQL 同源）。"""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        future=True,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def fake_redis() -> AsyncIterator[FakeRedis]:
    client = FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()


@pytest_asyncio.fixture
async def seeded_users(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    """预置 4 个账号（admin / approver / viewer / 已禁用）。返回 username -> id。"""
    ids: dict[str, int] = {}
    async with session_factory() as session:
        for username, role, status in TEST_USERS:
            user = User(
                username=username,
                password_hash=hash_password(TEST_PASSWORD),
                role=role,
                status=status,
            )
            session.add(user)
            await session.flush()
            ids[username] = user.id
        await session.commit()
    return ids


def _install_overrides(
    target: FastAPI,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: FakeRedis,
) -> None:
    async def _get_db_override() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    async def _get_redis_override() -> AsyncIterator[FakeRedis]:
        yield redis_client

    target.dependency_overrides[get_db] = _get_db_override
    target.dependency_overrides[get_redis] = _get_redis_override


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    fake_redis: FakeRedis,
    seeded_users: dict[str, int],
) -> AsyncIterator[AsyncClient]:
    """完整应用（含 TraceID 中间件、全局异常处理器、/api/v1 路由）。"""
    app = create_app()
    _install_overrides(app, session_factory, fake_redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as http_client:
        yield http_client


@pytest_asyncio.fixture
async def protected_client(
    session_factory: async_sessionmaker[AsyncSession],
    fake_redis: FakeRedis,
    seeded_users: dict[str, int],
) -> AsyncIterator[AsyncClient]:
    """最小 RBAC 探针应用：/admin-only 仅 admin 可访问，用于验证 require_role。"""
    app = FastAPI(title="rbac-probe")
    register_exception_handlers(app)
    _install_overrides(app, session_factory, fake_redis)
    # 复用真实鉴权路由（登录拿 token 后再打 /admin-only）
    app.include_router(api_router)

    @app.get("/admin-only")
    async def admin_only(user: CurrentUser = Depends(require_role(UserRole.ADMIN))) -> dict:
        return {"code": "OK", "data": {"user_id": user.id, "role": user.role.value}}

    @app.get("/any-login")
    async def any_login(user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER, UserRole.VIEWER))) -> dict:
        return {"code": "OK", "data": {"user_id": user.id}}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as http_client:
        yield http_client


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
async def _purge_shared_redis_state():
    """清理共享真实 Redis（db1）里会跨测试运行累加的状态键（fail-open）。

    背景：任务 9 的单任务 Token 预算键 ``task:token_usage:{task_id}`` 与任务 3 的
    日配额键 ``rl:daily:*`` 都写在共享 Redis，而 e2e 测试的 task_id / user_id 每次从
    1 起步——多次全量跑后同 key 反复累加，会假性触发 TokenBudgetExceededError / 429。
    生产环境 task_id 单调递增，无此问题；这里只修测试环境的跨运行污染。
    """
    try:
        from app.core.redis_client import get_shared_redis

        redis = get_shared_redis()
        stale: list[str] = []
        async for key in redis.scan_iter(match="task:token_usage:*"):
            stale.append(str(key))
        async for key in redis.scan_iter(match="rl:daily:*"):
            stale.append(str(key))
        if stale:
            await redis.delete(*stale)
    except Exception:  # noqa: BLE001 - Redis 不在线/故障时静默跳过（测试不依赖真实 Redis）
        pass
    yield


# ---------------------------------------------------------------------------
# W2 夹具：LLM / 上下文工程 / MCP 工具层 / 图运行时
# ---------------------------------------------------------------------------
from app.agents.llm import LLMClient, MockLLMClient  # noqa: E402
from app.services.context_service import ContextService  # noqa: E402
from app.tools.mcp_client import MCPClientPool  # noqa: E402
from app.tools.mcp_servers import chart_render, kb_search, notify, sql_query  # noqa: E402


class FakeSqlExecutor(sql_query.SqlExecutor):
    """进程内假执行器：不连 MySQL，行为可控（可注入扫描量 / 延迟 / 异常）。"""

    def __init__(
        self,
        columns: list[str] | None = None,
        rows: list[list] | None = None,
        *,
        scan_rows: int | None = None,
        delay: float = 0.0,
        raise_exc: BaseException | None = None,
    ) -> None:
        self.columns = columns if columns is not None else ["category_id", "gmv", "order_cnt"]
        self.rows = (
            rows
            if rows is not None
            else [[1001, 125000.5, 320], [1002, 98000.0, 260], [1003, 76000.25, 210]]
        )
        self.scan_rows = scan_rows
        self.delay = delay
        self.raise_exc = raise_exc
        self.calls: list[str] = []

    async def execute(self, sql: str, timeout: float) -> sql_query.QueryResult:
        self.calls.append(sql)
        if self.delay:
            import asyncio

            await asyncio.sleep(self.delay)
        if self.raise_exc is not None:
            raise self.raise_exc
        rows = [list(row) for row in self.rows]
        return sql_query.QueryResult(columns=list(self.columns), rows=rows, row_count=len(rows))

    async def estimate_scan_rows(self, sql: str, timeout: float) -> int | None:
        return self.scan_rows


@pytest.fixture
def fake_sql_executor() -> FakeSqlExecutor:
    return FakeSqlExecutor()


@pytest.fixture
def context_service() -> ContextService:
    return ContextService()


@pytest.fixture
def mock_llm() -> LLMClient:
    """确定性 Mock LLM（按提示词标记返回结构合法 JSON）。"""
    return LLMClient(mock=True)


@pytest.fixture
def mock_llm_client() -> MockLLMClient:
    """可直接 push 预设响应的 Mock 客户端（用于修复重试类用例）。"""
    return MockLLMClient()


@pytest.fixture
def w2_pool_llm(mock_llm_client: MockLLMClient) -> LLMClient:
    return LLMClient(primary=mock_llm_client, mock=True)


@pytest.fixture
def chart_tool() -> chart_render.ChartRenderTool:
    # 单测走线程而非进程池，避免 Windows 上 spawn 开销
    return chart_render.ChartRenderTool(use_process_pool=False)


@pytest.fixture
def kb_provider() -> kb_search.MockKbProvider:
    return kb_search.MockKbProvider()


@pytest.fixture
def notify_tool(session_factory: async_sessionmaker[AsyncSession]) -> notify.NotifyTool:
    return notify.NotifyTool(session_factory=session_factory)


@pytest_asyncio.fixture
async def mcp_pool(
    session_factory: async_sessionmaker[AsyncSession],
    fake_sql_executor: FakeSqlExecutor,
    chart_tool: chart_render.ChartRenderTool,
    kb_provider: kb_search.MockKbProvider,
    notify_tool: notify.NotifyTool,
) -> AsyncIterator[MCPClientPool]:
    """进程内 MCP 连接池：4 个 server 直连，不起子进程、不连 MySQL。"""
    apps = {
        "sql-query": sql_query.build_app(fake_sql_executor),
        "chart-render": chart_render.build_app(chart_tool),
        "kb-search": kb_search.build_app(kb_provider),
        "notify": notify.build_app(notify_tool),
    }
    pool = MCPClientPool.from_apps(apps)
    await pool.discover()
    try:
        yield pool
    finally:
        await pool.aclose()


@pytest_asyncio.fixture
async def w2_graph(
    mcp_pool: MCPClientPool,
    mock_llm: LLMClient,
    context_service: ContextService,
):
    """完整 7 节点图（内存 checkpointer + 本地规则评审），用于端到端冒烟。"""
    from langgraph.checkpoint.memory import MemorySaver

    from app.agents.graph import build_graph

    return build_graph(
        llm=mock_llm,
        pool=mcp_pool,
        context=context_service,
        tools=mcp_pool.tools,
        checkpointer=MemorySaver(),
        force_local_review=True,
    )

