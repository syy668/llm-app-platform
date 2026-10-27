#!/usr/bin/env python
"""W4 脚本公共基建（仅新增，不改动任何 W1/W2/W3 业务代码）。

为什么脚本需要这一层：
- W4 的四类脚本（评测跑批 / 种子校验 / A-B 实验 / Demo）都要「提交任务 → 轮询终态 → 读指标」，
  逻辑完全一致，重复实现必然漂移；
- 真实环境依赖 MySQL + Redis，本地/CI 上跑不通；而 W3 的 e2e 测试已经证明「SQLite + fakeredis +
  进程内 MCP（InMemoryTransport）」可以完整跑通 7 节点编排，因此脚本复用同一套装配方式，
  用 ``--embedded`` 默认走嵌入式环境，用 ``--base-url`` 对接真实服务，两条路径共用同一份
  ``TaskApi`` 客户端代码（同一个 ``POST /api/v1/tasks`` + 轮询 ``GET /api/v1/tasks/{id}``）。

模块职责：
1. ``bootstrap_env()``：在 import ``app.*`` 之前把离线开关铺好（只 setdefault，不覆盖外部显式设置）；
2. ``EmbeddedEnv``：自建 SQLite(fakeredis+进程内 MCP) + FastAPI app + ASGI 客户端；
3. ``TaskApi``：任务全生命周期客户端（提交/查询/SSE/审批/取消/轨迹/快照）；
4. ``wait_terminal()``：轮询到终态，识别 ``REPLAN_EXHAUSTED`` 与超时；
5. 指标层：分位/分节点 token/关键字召回/GMV 结论判定/幻觉计数；
6. ``JudgeClient``：LLM-as-judge 忠实度评分（评测模型与生成模型分离），无 key 时退化为本地规则；
7. 输出层：json / csv / markdown 写法。

**重要**：本模块必须在 import ``app.*`` 之前被 import（脚本里放在第一行 import）。
"""

from __future__ import annotations

import asyncio
import csv
import json
import math
import os
import re
import statistics
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# 0. 环境预设（必须早于 app.* 的 import；一律 setdefault，尊重外部显式配置）
# ---------------------------------------------------------------------------
_ENV_DEFAULTS: dict[str, str] = {
    "APP_ENV": "local",
    # 与 W1 测试用同一把弱密钥，避免 local 环境触发「生产密钥」校验；脚本不会部署
    "SECRET_KEY": "w4-script-secret-key-0123456789abcdef0123456789",
    "BCRYPT_ROUNDS": "4",  # 种子账号哈希提速
    "LOG_LEVEL": "WARNING",
    "LOG_JSON": "false",
    # 离线三开关：不连外部 RAG / 外部评测 / 真实 LLM
    "MOCK_EXTERNAL_RAG": "true",
    "MOCK_EXTERNAL_EVAL": "true",
    "LLM_MOCK": "true",
    # 不连 Redis：checkpointer 走内存；事件总线由 fakeredis 承担
    "CHECKPOINTER_BACKEND": "memory",
    "CHART_USE_PROCESS_POOL": "false",
    "TOOL_RETRY_BACKOFF_ENABLED": "false",
    "NOTIFY_DRY_RUN": "true",
    # 脚本里不让 SSE 阻塞空转（仅影响嵌入式 Demo）
    "SSE_BLOCK_MS": "0",
    "SSE_POLL_INTERVAL_MS": "50",
}


def bootstrap_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """铺默认环境变量，返回本次真正生效的离线开关快照（写进报告用）。"""
    for key, value in _ENV_DEFAULTS.items():
        os.environ.setdefault(key, value)
    for key, value in (overrides or {}).items():
        os.environ[key] = str(value)
    return {
        "LLM_MOCK": os.environ.get("LLM_MOCK", ""),
        "MOCK_EXTERNAL_RAG": os.environ.get("MOCK_EXTERNAL_RAG", ""),
        "MOCK_EXTERNAL_EVAL": os.environ.get("MOCK_EXTERNAL_EVAL", ""),
        "CHECKPOINTER_BACKEND": os.environ.get("CHECKPOINTER_BACKEND", ""),
    }


bootstrap_env()

# --- 以下 import 依赖上面的环境变量，顺序不可调整 ---
from fakeredis.aioredis import FakeRedis  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import event as sa_event  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.agents.graph import AgentRuntime  # noqa: E402
from app.agents.llm import LLMClient  # noqa: E402
from app.core.redis_client import get_redis  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers.tasks import get_task_service  # noqa: E402
from app.schemas import orm_models_ext  # noqa: F401,E402  - 建表需要 mock_messages
from app.schemas.enums import TaskStatus, UserRole  # noqa: E402
from app.schemas.orm_models import User  # noqa: E402
from app.services.cancel_service import CancelSignal  # noqa: E402
from app.services.context_service import ContextService  # noqa: E402
from app.services.event_bus import EventBus  # noqa: E402
from app.services.stream_context import mcp_event_hook  # noqa: E402
from app.services.stream_service import StreamingTaskService  # noqa: E402
from app.services.trace_service import TraceService  # noqa: E402
from app.services.w3_runtime import create_w3_runtime  # noqa: E402
from app.tools import mcp_client as mcp_client_module  # noqa: E402
from app.tools.mcp_client import MCPClientPool  # noqa: E402
from app.tools.mcp_servers import chart_render, kb_search, notify, sql_query  # noqa: E402

PASSWORD = "Passw0rd!"
DEFAULT_WINDOW = {"start": "2026-10-12", "end": "2026-10-18"}
TERMINAL_STATUSES = {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
STATUS_COMPLETED = TaskStatus.COMPLETED.value
STATUS_FAILED = TaskStatus.FAILED.value
STATUS_AWAITING = TaskStatus.AWAITING_APPROVAL.value
REPLAN_EXHAUSTED = "REPLAN_EXHAUSTED"


# ---------------------------------------------------------------------------
# 1. 假 SQL 执行器（嵌入式环境用，不连 mall MySQL）
# ---------------------------------------------------------------------------
class FakeSqlExecutor(sql_query.SqlExecutor):
    """确定性假执行器：返回 mall 形状的指标行，供离线评测/演示使用。

    刻意保留「环比跌幅 -18%」这一埋因数字（PRD 11.3），让离线跑批的结论与真实数据同向，
    这样评测脚本的指标才不是全 0 的假绿。
    """

    COLUMNS = ["category_name", "gmv", "order_cnt", "conv_rate", "prev_gmv", "wow_pct"]
    ROWS = [
        ["数码配件", 8505000.0, 31720, 1.7, 10372000.0, -18.0],
        ["服饰鞋包", 9200000.0, 46000, 3.4, 9260000.0, -0.6],
        ["家居日用", 6100000.0, 50800, 2.9, 6120000.0, -0.3],
        ["食品生鲜", 4500000.0, 64200, 4.1, 4480000.0, 0.4],
        ["美妆个护", 3200000.0, 18200, 2.2, 3190000.0, 0.3],
    ]

    def __init__(
        self,
        columns: list[str] | None = None,
        rows: list[list[Any]] | None = None,
        *,
        scan_rows: int | None = 168000,
        delay: float = 0.0,
    ) -> None:
        self.columns = columns if columns is not None else list(self.COLUMNS)
        self.rows = rows if rows is not None else [list(r) for r in self.ROWS]
        self.scan_rows = scan_rows
        self.delay = delay
        self.calls: list[str] = []

    async def execute(self, sql: str, timeout: float) -> sql_query.QueryResult:
        self.calls.append(sql)
        if self.delay:
            await asyncio.sleep(self.delay)
        return sql_query.QueryResult(columns=list(self.columns), rows=[list(r) for r in self.rows], row_count=len(self.rows))

    async def estimate_scan_rows(self, sql: str, timeout: float) -> int | None:
        return self.scan_rows


# ---------------------------------------------------------------------------
# 2. 嵌入式运行环境（SQLite + fakeredis + 进程内 MCP + ASGI）
# ---------------------------------------------------------------------------
class EmbeddedEnv:
    """把 W3 e2e 夹具的装配方式搬成脚本可用的上下文管理器。

    与生产的差异（会在报告中标注）：
    - 存储：SQLite 文件库（WAL）替代 MySQL；
    - 事件总线 / 取消标记：fakeredis 替代 Redis；
    - checkpointer：InMemorySaver 替代 RedisSaver（``--require-redis`` 时由 verify_seed 另行校验）；
    - MCP：进程内 InMemoryTransport 替代 stdio 子进程（协议与工具实现完全相同）。
    """

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        compress: bool | None = None,
        force_compress: bool = False,
        llm_mock: bool = True,
        kb_provider: kb_search.KbProvider | None = None,
        sql_executor: sql_query.SqlExecutor | None = None,
    ) -> None:
        self._db_path = Path(db_path) if db_path else Path(PROJECT_ROOT / "storage" / "_w4_embedded.db")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._compress = compress
        self._force_compress = force_compress
        self._llm_mock = llm_mock
        self._kb_provider = kb_provider
        self._sql_executor = sql_executor or FakeSqlExecutor()

        self.engine: AsyncEngine | None = None
        self.session_factory: async_sessionmaker[AsyncSession] | None = None
        self.redis: FakeRedis | None = None
        self.bus: EventBus | None = None
        self.cancel: CancelSignal | None = None
        self.runtime: AgentRuntime | None = None
        self.service: StreamingTaskService | None = None
        self.app: Any = None
        self.client: AsyncClient | None = None
        self.api: TaskApi | None = None
        self.users: dict[str, int] = {}
        self.chart_tool: chart_render.ChartRenderTool | None = None
        self.pool: MCPClientPool | None = None

    # ---------------- 生命周期 ----------------
    async def __aenter__(self) -> "EmbeddedEnv":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def start(self) -> "EmbeddedEnv":
        # 1) SQLite 文件库 + WAL：图执行与轨迹写入是并发的，单连接内存库会撞
        #    "cannot commit transaction - SQL statements in progress"（W3 踩过）
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{self._db_path.as_posix()}", connect_args={"timeout": 30}
        )

        @sa_event.listens_for(self.engine.sync_engine, "connect")
        def _pragmas(dbapi_connection, _record):  # pragma: no cover - 驱动回调
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)

        # 2) 种子账号（与 scripts/seed_w3_demo_users.py 一致的角色矩阵）
        self.users = await self._seed_users()

        # 3) fakeredis：事件总线 + 取消标记
        self.redis = FakeRedis(decode_responses=True)
        self.bus = EventBus(redis_client=self.redis)
        self.cancel = CancelSignal(self.redis)

        # 4) 进程内 MCP 池（4 个 server 与生产同源，只是传输换成 InMemoryTransport）
        self.chart_tool = chart_render.ChartRenderTool(use_process_pool=False)
        apps = {
            "sql-query": sql_query.build_app(self._sql_executor),
            "chart-render": chart_render.build_app(self.chart_tool),
            "kb-search": kb_search.build_app(self._kb_provider or kb_search.MockKbProvider()),
            "notify": notify.build_app(notify.NotifyTool(session_factory=self.session_factory)),
        }
        self.pool = MCPClientPool.from_apps(apps, on_event=mcp_event_hook)
        await self.pool.discover()

        # 5) 图运行时：context 可注入 → A/B 实验的对照组就靠它关闭压缩
        context = self._build_context()
        self.runtime = await create_w3_runtime(
            pool=self.pool,
            llm=LLMClient(mock=self._llm_mock),
            checkpointer=self._build_checkpointer(),
            session_factory=self.session_factory,
            context=context,
            force_local_review=True,
            discover_tools=False,
        )

        # 6) 流式任务服务 + FastAPI（依赖覆盖到 SQLite / fakeredis / 本运行时）
        self.service = StreamingTaskService(
            bus=self.bus,
            trace=TraceService(self.session_factory),
            cancel=self.cancel,
            session_factory=self.session_factory,
            runtime=self.runtime,
        )
        self.app = self._build_app()
        self.client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://w4.local", timeout=180)
        self.api = TaskApi(self.client)
        await self.api.login("admin_user", PASSWORD)
        return self

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
        if self.pool is not None:
            await self.pool.aclose()
        if self.runtime is not None and getattr(self.runtime, "checkpointer", None) is not None:
            closer = getattr(self.runtime.checkpointer, "aclose", None)
            if callable(closer):
                try:
                    await closer()
                except Exception:  # noqa: BLE001
                    pass
        if self.redis is not None:
            try:
                await self.redis.flushall()
            finally:
                await self.redis.aclose()
        if self.engine is not None:
            await self.engine.dispose()

    # ---------------- 内部装配 ----------------
    def _build_context(self) -> ContextService:
        if self._force_compress:
            from app.schemas.context import CompressionConfig

            config = CompressionConfig(
                enabled=True if self._compress is None else self._compress,
                trigger_ratio=0.05,          # 强制触发：A/B 要放大压缩路径的差异
                message_rounds_threshold=0,
                keep_recent_rounds=1,
                max_layers=2,
            )
            return ContextService(compression=config)
        return ContextService(enabled=self._compress)

    @staticmethod
    def _build_checkpointer() -> Any:
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()

    async def _seed_users(self) -> dict[str, int]:
        assert self.session_factory is not None
        roles = {
            "admin_user": UserRole.ADMIN,
            "approver_user": UserRole.APPROVER,
            "viewer_user": UserRole.VIEWER,
        }
        ids: dict[str, int] = {}
        async with self.session_factory() as session:
            for username, role in roles.items():
                existing = (
                    await session.execute(select(User).where(User.username == username))
                ).scalar_one_or_none()
                if existing is not None:
                    ids[username] = int(existing.id)
                    continue
                user = User(username=username, password_hash=hash_password(PASSWORD), role=role, status=1)
                session.add(user)
                await session.flush()
                ids[username] = int(user.id)
            await session.commit()
        return ids

    def _build_app(self) -> Any:
        assert self.session_factory is not None and self.redis is not None
        session_factory = self.session_factory
        redis = self.redis
        service = self.service

        app = create_app()

        async def _get_db_override():
            async with session_factory() as session:
                try:
                    yield session
                except Exception:
                    await session.rollback()
                    raise

        async def _get_redis_override():
            yield redis

        app.dependency_overrides[get_db] = _get_db_override
        app.dependency_overrides[get_redis] = _get_redis_override
        # W2 的 GET /tasks/{id} 用自己的依赖；指向 W3 服务，复用同一份库与 runtime
        app.dependency_overrides[get_task_service] = lambda: service
        app.state.event_bus = self.bus
        app.state.stream_service = service
        return app

    # ---------------- 便于报告 ----------------
    @property
    def infra_summary(self) -> dict[str, Any]:
        return {
            "mode": "embedded",
            "db": f"sqlite:{self._db_path.name}",
            "redis": "fakeredis",
            "checkpointer": type(getattr(self.runtime, "checkpointer", None)).__name__,
            "compress_enabled": None if not self._force_compress else self._compress,
            "force_compress": self._force_compress,
            "llm_mock": self._llm_mock,
            "mcp_transport": "InMemoryTransport",
            "servers": list(self.pool.clients) if self.pool is not None else [],
        }


# ---------------------------------------------------------------------------
# 3. 任务 API 客户端（嵌入式与真实 HTTP 共用）
# ---------------------------------------------------------------------------
class ApiError(RuntimeError):
    pass


class TaskApi:
    """薄封装：把 ``{"code": "OK", "data": ...}`` 统一拆包，非 OK 抛 ``ApiError``。"""

    def __init__(self, client: AsyncClient, token: str | None = None) -> None:
        self.client = client
        self.token = token

    # ---------------- 鉴权 ----------------
    async def login(self, username: str, password: str = PASSWORD) -> str:
        response = await self.client.post("/api/v1/auth/login", json={"username": username, "password": password})
        payload = _unwrap(response)
        self.token = str(payload["access_token"])
        return self.token

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    # ---------------- 任务 ----------------
    async def submit(self, goal: str, window: dict[str, str] | None = None, *, sync: bool = False) -> dict[str, Any]:
        response = await self.client.post(
            "/api/v1/tasks",
            headers=self.headers,
            json={"goal": goal, "time_range": window or DEFAULT_WINDOW, "sync": sync},
        )
        return _unwrap(response)

    async def detail(self, task_id: int) -> dict[str, Any]:
        response = await self.client.get(f"/api/v1/tasks/{task_id}", headers=self.headers)
        return _unwrap(response)

    async def status(self, task_id: int) -> str:
        return str((await self.detail(task_id)).get("status"))

    async def interrupt(self, task_id: int) -> dict[str, Any]:
        response = await self.client.get(f"/api/v1/tasks/{task_id}/interrupt", headers=self.headers)
        return _unwrap(response)

    async def resume(
        self,
        task_id: int,
        decision: str = "APPROVED",
        comment: str = "",
        *,
        sync: bool = True,
        approval_id: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"decision": decision, "comment": comment, "sync": sync}
        if approval_id is not None:
            body["approval_id"] = approval_id
        response = await self.client.post(f"/api/v1/tasks/{task_id}/resume", headers=self.headers, json=body)
        return _unwrap(response)

    async def cancel(self, task_id: int, reason: str = "") -> dict[str, Any]:
        response = await self.client.post(
            f"/api/v1/tasks/{task_id}/cancel", headers=self.headers, json={"reason": reason}
        )
        return _unwrap(response)

    async def snapshot(self, task_id: int) -> dict[str, Any]:
        response = await self.client.get(f"/api/v1/tasks/{task_id}/snapshot", headers=self.headers)
        return _unwrap(response)

    async def trace(self, task_id: int) -> dict[str, Any]:
        response = await self.client.get(f"/api/v1/tasks/{task_id}/trace", headers=self.headers)
        return _unwrap(response)

    async def approvals(self, *, status: str | None = None, size: int = 50) -> dict[str, Any]:
        params: dict[str, Any] = {"size": size}
        if status:
            params["status"] = status
        response = await self.client.get("/api/v1/approvals", headers=self.headers, params=params)
        return _unwrap(response)

    async def stream_text(self, task_id: int, *, last_event_id: str | None = None) -> str:
        """读完整条 SSE 文本（连接在终止帧后自然结束，故可一次性读取）。"""
        params: dict[str, Any] = {}
        if last_event_id:
            params["last_event_id"] = last_event_id
        response = await self.client.get(
            f"/api/v1/tasks/{task_id}/stream", headers=self.headers, params=params, timeout=180
        )
        if response.status_code != 200:
            raise ApiError(f"SSE 订阅失败：HTTP {response.status_code} {response.text[:200]}")
        return response.text


def _unwrap(response: Any) -> dict[str, Any]:
    if response.status_code >= 400:
        raise ApiError(f"HTTP {response.status_code}: {response.text[:300]}")
    try:
        body = response.json()
    except Exception as exc:  # noqa: BLE001
        raise ApiError(f"响应不是合法 JSON：{response.text[:200]}") from exc
    if isinstance(body, dict) and body.get("code") not in (None, "OK"):
        raise ApiError(f"{body.get('code')}: {body.get('message')}")
    data = body.get("data") if isinstance(body, dict) else body
    return data if isinstance(data, dict) else {"value": data}


def parse_sse(text: str) -> list[dict[str, Any]]:
    """把 SSE 文本解析成事件列表（`: ping` 注释帧忽略，data 多行拼接）。"""
    events: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    data_lines: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            if current or data_lines:
                if data_lines:
                    try:
                        current["data"] = json.loads("\n".join(data_lines))
                    except json.JSONDecodeError:
                        current["data"] = {"raw": "\n".join(data_lines)}
                events.append(current)
            current, data_lines = {}, []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.lstrip(" ")
        if field == "data":
            data_lines.append(value)
        elif field in {"id", "event", "retry"}:
            current[field] = value
    if current or data_lines:
        if data_lines:
            try:
                current["data"] = json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                current["data"] = {"raw": "\n".join(data_lines)}
        events.append(current)
    return events


# ---------------------------------------------------------------------------
# 4. 轮询到终态
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class TerminalResult:
    task_id: int
    status: str
    detail: dict[str, Any] = field(default_factory=dict)
    waited_ms: int = 0
    timed_out: bool = False
    polls: int = 0

    @property
    def terminal_reason(self) -> str:
        """COMPLETED / FAILED / CANCELLED，其中 FAILED 细分 REPLAN_EXHAUSTED。"""
        if self.status != STATUS_FAILED:
            return self.status
        code = ((self.detail or {}).get("error") or {}).get("code")
        return str(code) if code else STATUS_FAILED

    @property
    def ok(self) -> bool:
        return self.status == STATUS_COMPLETED


async def wait_terminal(
    api: TaskApi,
    task_id: int,
    *,
    timeout: float = 120.0,
    interval: float = 0.2,
    auto_approve: bool = True,
    approve_comment: str = "W4 自动审批（评测脚本）",
    reject: bool = False,
) -> TerminalResult:
    """轮询到终态；遇到 AWAITING_APPROVAL 时按 ``auto_approve`` 决定是否代审批。

    评测跑批必须能处理挂起，否则 6 条高风险用例会全部卡在审批点上。
    """
    started = time.perf_counter()
    polls = 0
    detail: dict[str, Any] = {}
    while True:
        polls += 1
        detail = await api.detail(task_id)
        status = str(detail.get("status"))
        if status in TERMINAL_STATUSES:
            return TerminalResult(
                task_id=task_id,
                status=status,
                detail=detail,
                waited_ms=int((time.perf_counter() - started) * 1000),
                polls=polls,
            )
        if status == STATUS_AWAITING and auto_approve:
            try:
                await api.resume(
                    task_id,
                    decision="REJECTED" if reject else "APPROVED",
                    comment="评测脚本自动驳回" if reject else approve_comment,
                    sync=False,
                )
            except ApiError as exc:
                # 并发下可能已被别的路径处理；不视为致命错误，继续轮询
                detail["_resume_warning"] = str(exc)[:200]
        if (time.perf_counter() - started) > timeout:
            return TerminalResult(
                task_id=task_id,
                status=status,
                detail=detail,
                waited_ms=int((time.perf_counter() - started) * 1000),
                timed_out=True,
                polls=polls,
            )
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# 5. 指标
# ---------------------------------------------------------------------------
def percentile(values: Sequence[float], p: float) -> float:
    """nearest-rank 分位（p 取 0~100），空序列返回 0.0。"""
    items = sorted(float(v) for v in values if v is not None)
    if not items:
        return 0.0
    if len(items) == 1:
        return round(items[0], 2)
    rank = max(1, math.ceil(p / 100.0 * len(items)))
    return round(items[min(rank, len(items)) - 1], 2)


def mean(values: Iterable[float]) -> float:
    items = [float(v) for v in values if v is not None]
    return round(statistics.fmean(items), 2) if items else 0.0


def node_tokens(ledger: dict[str, Any] | None) -> dict[str, int]:
    """token_ledger -> {node: input+output}（分节点 token 消耗）。"""
    result: dict[str, int] = {}
    for node, entry in (ledger or {}).items():
        if not isinstance(entry, dict):
            continue
        result[str(node)] = int(entry.get("input") or 0) + int(entry.get("output") or 0)
    return result


def total_tokens_of(ledger: dict[str, Any] | None, fallback: int = 0) -> int:
    tokens = node_tokens(ledger)
    return sum(tokens.values()) if tokens else int(fallback or 0)


def llm_calls_of(ledger: dict[str, Any] | None) -> int:
    total = 0
    for entry in (ledger or {}).values():
        if isinstance(entry, dict):
            total += int(entry.get("calls") or 0)
    return total


def compressed_nodes(ledger: dict[str, Any] | None) -> dict[str, dict[str, int]]:
    """被压缩过的节点及前后 token（A/B 报告用）。"""
    result: dict[str, dict[str, int]] = {}
    for node, entry in (ledger or {}).items():
        if isinstance(entry, dict) and entry.get("compressed"):
            result[str(node)] = {
                "before": int(entry.get("compress_before") or 0),
                "after": int(entry.get("compress_after") or 0),
            }
    return result


_PCT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")
_DOWN_WORDS = ("下跌", "下滑", "下降", "跌幅", "承压", "减少", "降低", "负增长")
_UP_WORDS = ("上涨", "增长", "提升", "增幅", "上升", "正增长")
_FLAT_WORDS = ("持平", "基本持平", "波动不大", "±3%", "无明显变化")


def trend_of(text: str) -> str:
    lowered = text or ""
    down = sum(1 for w in _DOWN_WORDS if w in lowered)
    up = sum(1 for w in _UP_WORDS if w in lowered)
    flat = sum(1 for w in _FLAT_WORDS if w in lowered)
    if down == 0 and up == 0 and flat == 0:
        return "unknown"
    if down >= up and down >= flat:
        return "down"
    if up >= flat:
        return "up"
    return "flat"


def percentages_of(text: str) -> list[float]:
    return [float(m) for m in _PCT_RE.findall(text or "")]


def keyword_recall(text: str, keywords: Sequence[str]) -> float:
    """期望关键字里被提及的比例（0~1）。"""
    items = [k for k in keywords if k]
    if not items:
        return 1.0
    hit = sum(1 for k in items if k.lower() in (text or "").lower())
    return round(hit / len(items), 4)


def count_hallucinations(text: str, forbidden: Sequence[str]) -> int:
    """命中 must_not_mention 的次数（幻觉/错误论断计数）。"""
    return sum(1 for item in forbidden or [] if item and item in (text or ""))


def score_gmv_conclusion(text: str, expected: dict[str, Any]) -> dict[str, Any]:
    """GMV 结论判定：趋势 + 幅度区间 + 必须提及 + 禁止提及，四项全过才算正确。"""
    body = text or ""
    expected_trend = str(expected.get("trend") or "").lower()
    pct_range = expected.get("pct_range") or []
    must = list(expected.get("must_mention") or [])
    forbidden = list(expected.get("must_not_mention") or [])

    trend = trend_of(body)
    trend_ok = bool(expected_trend) and trend == expected_trend

    pcts = percentages_of(body)
    pct_ok = False
    matched_pct: float | None = None
    if len(pct_range) == 2 and pcts:
        low, high = float(pct_range[0]), float(pct_range[1])
        for value in pcts:
            if low <= value <= high:
                pct_ok = True
                matched_pct = value
                break

    cause_ok = all(item in body for item in must) if must else True
    clean = count_hallucinations(body, forbidden) == 0
    passed = trend_ok and cause_ok and clean and (pct_ok if pct_range else True)
    return {
        "gmv_pass": passed,
        "trend_detected": trend,
        "trend_ok": trend_ok,
        "pct_ok": pct_ok,
        "pct_matched": matched_pct,
        "cause_ok": cause_ok,
        "clean_ok": clean,
        "must_mentioned": [m for m in must if m in body],
        "must_missing": [m for m in must if m not in body],
        "forbidden_hit": [f for f in forbidden if f in body],
    }


# ---------------------------------------------------------------------------
# 6. LLM-as-judge（评测模型与生成模型分离）
# ---------------------------------------------------------------------------
JUDGE_PROMPT = """你是严格的 RAG 忠实度评审员。请只根据「证据」判断「报告」中的论断是否有支撑。

【证据】
{evidence}

【报告】
{report}

评分口径（0~1）：
- 1.0：报告中的每个事实性论断都能在证据中找到依据；
- 0.7~0.9：主干论断有依据，个别细节（如具体百分比）证据未覆盖；
- 0.4~0.6：部分论断有依据，但存在证据未覆盖的关键结论；
- 0~0.3：大量论断缺少证据支撑或与证据矛盾。

只输出 JSON，不要解释：
{{"faithfulness": 0.0, "unsupported_claims": ["..."], "rationale": "不超过80字"}}"""


@dataclass(slots=True)
class JudgeResult:
    faithfulness: float
    mode: str
    unsupported: list[str] = field(default_factory=list)
    rationale: str = ""
    model: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "faithfulness": self.faithfulness,
            "judge_mode": self.mode,
            "judge_model": self.model,
            "unsupported_claims": self.unsupported[:5],
            "judge_rationale": self.rationale[:200],
        }


class JudgeClient:
    """忠实度评审：``JUDGE_*`` 环境变量配置独立模型；无 key 时退化为本地 2-gram 支撑率。

    为什么要"评测模型与生成模型分开"：同一模型自评会系统性偏高（self-preference bias），
    因此这里强制走独立配置（默认指向降级链上的 Qwen 而非生成用的 DeepSeek）。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 40.0,
        mock: bool | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("JUDGE_BASE_URL") or "").rstrip("/")
        self.api_key = api_key if api_key is not None else (os.environ.get("JUDGE_API_KEY") or "")
        self.model = model or os.environ.get("JUDGE_MODEL") or "Qwen/Qwen2.5-72B-Instruct"
        self.timeout = float(os.environ.get("JUDGE_TIMEOUT_SECONDS") or timeout)
        env_mock = os.environ.get("JUDGE_MOCK", "").lower() in {"1", "true", "yes"}
        self.mock = env_mock if mock is None else mock

    @property
    def mode(self) -> str:
        if self.mock:
            return "local_rule(mock)"
        return "llm" if (self.base_url and self.api_key) else "local_rule(no_key)"

    async def faithfulness(self, report: str, evidence: str, ground_truth: str = "") -> JudgeResult:
        if self.mode.startswith("llm"):
            try:
                return await self._llm_judge(report, evidence)
            except Exception as exc:  # noqa: BLE001 - 评审失败必须降级而不是中断批量
                result = self._local_judge(report, evidence, ground_truth)
                result.rationale = f"LLM 评审失败降级：{type(exc).__name__}: {str(exc)[:80]}"
                return result
        return self._local_judge(report, evidence, ground_truth)

    async def _llm_judge(self, report: str, evidence: str) -> JudgeResult:
        import httpx

        prompt = JUDGE_PROMPT.format(evidence=(evidence or "（无证据）")[:6000], report=(report or "")[:6000])
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你是严格的事实核查评审员，只输出 JSON。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        data = _loads_json_lenient(content)
        score = float(data.get("faithfulness", 0.0))
        return JudgeResult(
            faithfulness=round(max(0.0, min(1.0, score)), 4),
            mode="llm",
            unsupported=[str(x) for x in (data.get("unsupported_claims") or [])][:5],
            rationale=str(data.get("rationale") or "")[:200],
            model=self.model,
        )

    def _local_judge(self, report: str, evidence: str, ground_truth: str) -> JudgeResult:
        """本地兜底：按句子计算「能在证据/参考答案中找到 2-gram 支撑」的比例。"""
        support_pool = f"{evidence}\n{ground_truth}"
        sentences = [s for s in re.split(r"[。；\n]", report or "") if len(s.strip()) >= 6]
        if not sentences:
            return JudgeResult(faithfulness=0.0, mode=self.mode, rationale="报告为空", model="local-rule")
        pool_grams = _ngrams(support_pool)
        supported = 0
        unsupported: list[str] = []
        for sentence in sentences:
            grams = _ngrams(sentence)
            hit = len(grams & pool_grams) / max(1, len(grams))
            if hit >= 0.35:
                supported += 1
            else:
                unsupported.append(sentence.strip()[:40])
        score = round(supported / len(sentences), 4)
        return JudgeResult(
            faithfulness=score,
            mode=self.mode,
            unsupported=unsupported[:5],
            rationale=f"{supported}/{len(sentences)} 句有证据支撑（2-gram 兜底算法）",
            model="local-rule",
        )


def _ngrams(text: str, n: int = 2) -> set[str]:
    clean = re.sub(r"[\s\-_,，。；：:、|·*`#>\[\]()（）]+", "", text or "")
    if len(clean) <= n:
        return {clean} if clean else set()
    return {clean[i : i + n] for i in range(len(clean) - n + 1)}


def _loads_json_lenient(content: str) -> dict[str, Any]:
    raw = (content or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start : end + 1]
    return json.loads(raw)


# ---------------------------------------------------------------------------
# 7. 输出
# ---------------------------------------------------------------------------
def now_tag() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def ensure_dir(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def write_json(path: str | Path, payload: Any) -> Path:
    target = Path(path)
    ensure_dir(target.parent)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return target


def write_csv(path: str | Path, rows: Sequence[dict[str, Any]], headers: Sequence[str] | None = None) -> Path:
    target = Path(path)
    ensure_dir(target.parent)
    columns = list(headers or (rows[0].keys() if rows else []))
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _csv_cell(row.get(k)) for k in columns})
    return target


def _csv_cell(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def write_text(path: str | Path, content: str) -> Path:
    target = Path(path)
    ensure_dir(target.parent)
    target.write_text(content, encoding="utf-8")
    return target


def markdown_table(rows: Sequence[dict[str, Any]], headers: Sequence[str], *, aligns: Sequence[str] | None = None) -> str:
    """把行数据渲染成 markdown 表（报告与清单复用）。"""
    aligns = aligns or ["---"] * len(headers)
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(aligns) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(_csv_cell(row.get(h, ""))).replace("\n", " ") for h in headers) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 8. 命令行公用参数
# ---------------------------------------------------------------------------
def add_target_args(parser: Any) -> Any:
    """所有脚本共用的「跑在哪」参数。"""
    group = parser.add_argument_group("运行目标")
    group.add_argument("--base-url", default=None, help="真实服务地址（如 http://127.0.0.1:8000）；缺省走嵌入式环境")
    group.add_argument("--username", default="admin_user", help="登录账号（需 ADMIN 角色才能建任务）")
    group.add_argument("--password", default=PASSWORD, help="登录密码")
    group.add_argument("--timeout", type=float, default=120.0, help="单任务等待终态超时秒数")
    group.add_argument("--interval", type=float, default=0.2, help="轮询间隔秒数")
    return parser


async def build_target(args: Any) -> tuple[TaskApi, Any]:
    """按参数返回 (TaskApi, 资源句柄)。资源句柄为 None 或 EmbeddedEnv，调用方负责 aclose。"""
    if getattr(args, "base_url", None):
        client = AsyncClient(base_url=args.base_url.rstrip("/"), timeout=180)
        api = TaskApi(client)
        await api.login(args.username, args.password)
        return api, client
    env = await EmbeddedEnv().start()
    assert env.api is not None
    return env.api, env


async def close_target(handle: Any) -> None:
    if handle is None:
        return
    if isinstance(handle, EmbeddedEnv):
        await handle.aclose()
        return
    await handle.aclose()


__all__ = [
    "PROJECT_ROOT",
    "PASSWORD",
    "DEFAULT_WINDOW",
    "TERMINAL_STATUSES",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_AWAITING",
    "REPLAN_EXHAUSTED",
    "FakeSqlExecutor",
    "EmbeddedEnv",
    "TaskApi",
    "ApiError",
    "TerminalResult",
    "JudgeClient",
    "JudgeResult",
    "bootstrap_env",
    "parse_sse",
    "wait_terminal",
    "percentile",
    "mean",
    "node_tokens",
    "total_tokens_of",
    "llm_calls_of",
    "compressed_nodes",
    "trend_of",
    "percentages_of",
    "keyword_recall",
    "count_hallucinations",
    "score_gmv_conclusion",
    "now_tag",
    "ensure_dir",
    "write_json",
    "write_csv",
    "write_text",
    "markdown_table",
    "add_target_args",
    "build_target",
    "close_target",
]
