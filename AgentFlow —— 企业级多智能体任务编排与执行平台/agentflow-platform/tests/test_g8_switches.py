"""G8 任务级三开关（compress / use_memory / eval_tag）测试。

覆盖：
1. TaskCreateRequest 接收三开关（默认全关）+ 非法值 422；
2. create_task 落库三开关 + build_initial_state 注入 state + get_detail 透出；
3. memory_recall / context_compress 节点：开关关=空操作（默认链路不变），开=执行并产出；
4. 图条件路由：默认直连 planner / executor，开启才经新节点（test_graph_routing 已覆盖纯函数）；
5. eval_tag 绑定 eval_run + GET /eval/runs 按 eval_tag 筛选。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.schemas.agent_io import TaskCreateRequest
from app.schemas.enums import EvalSource, EvalRunStatus, UserRole
from app.schemas.graph_state import TaskState
from app.schemas.orm_models import EvalRun, Task, User
from app.utils.ids import new_trace_id
from app.utils.time import utcnow

TEST_PASSWORD = "Passw0rd!"


async def _login(client: AsyncClient, username: str) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": TEST_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["access_token"]


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1) 入参 schema 校验
# ---------------------------------------------------------------------------
def test_task_create_request_defaults() -> None:
    req = TaskCreateRequest(
        goal="分析GMV", time_range={"start": "2026-10-01", "end": "2026-10-21"}
    )
    assert req.compress is False
    assert req.use_memory is False
    assert req.eval_tag is None


def test_task_create_request_accepts_switches() -> None:
    req = TaskCreateRequest(
        goal="分析GMV",
        time_range={"start": "2026-10-01", "end": "2026-10-21"},
        compress=True,
        use_memory=True,
        eval_tag="campaign-a",
    )
    assert req.compress is True
    assert req.use_memory is True
    assert req.eval_tag == "campaign-a"


def test_task_create_request_eval_tag_too_long_rejected() -> None:
    with pytest.raises(Exception):
        TaskCreateRequest(
            goal="分析GMV",
            time_range={"start": "2026-10-01", "end": "2026-10-21"},
            eval_tag="x" * 65,
        )


# ---------------------------------------------------------------------------
# 2) 落库 + state 注入 + 详情透出
# ---------------------------------------------------------------------------
async def _new_task(session: AsyncSession, creator_id: int, **overrides) -> Task:
    task = Task(
        task_no=overrides.pop("task_no", f"AFG8-{new_trace_id()[:8]}"),
        creator_id=creator_id,
        goal=overrides.pop("goal", "分析GMV"),
        time_range={"start": "2026-10-01", "end": "2026-10-21"},
        status="PLANNING",
        replan_count=0,
        token_usage=0,
        trace_id=new_trace_id(),
        **overrides,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


@pytest.mark.asyncio
async def test_task_persists_switches(
    session_factory: async_sessionmaker[AsyncSession], seeded_users: dict[str, int],
):
    """ORM 层：三个开关字段可正确落库读回。"""
    async with session_factory() as session:
        task = await _new_task(
            session, seeded_users["admin_user"], compress=True, use_memory=True, eval_tag="campaign-a"
        )
        assert task.compress is True
        assert task.use_memory is True
        assert task.eval_tag == "campaign-a"


@pytest.mark.asyncio
async def test_task_switches_default_false(
    session_factory: async_sessionmaker[AsyncSession], seeded_users: dict[str, int],
):
    """默认值：不传开关时全为 false/None（不破坏默认链路）。"""
    async with session_factory() as session:
        task = await _new_task(session, seeded_users["admin_user"])
        assert task.compress is False
        assert task.use_memory is False
        assert task.eval_tag is None


@pytest.mark.asyncio
async def test_get_detail_transparent_switches(
    session_factory: async_sessionmaker[AsyncSession], seeded_users: dict[str, int],
):
    """详情透出：get_detail 返回三开关字段。"""
    from app.services.task_service import TaskService

    async with session_factory() as session:
        task = await _new_task(
            session, seeded_users["admin_user"], compress=True, use_memory=True, eval_tag="tag-1"
        )
        task_id = int(task.id)

    # 传入 mock runtime（graph 为空），restore 会失败但被 get_detail 的 try/except 吞掉
    from types import SimpleNamespace

    class _FakeRuntime:
        graph = None

    service = TaskService(session_factory=session_factory, runtime=_FakeRuntime())
    detail = await service.get_detail(task_id)
    assert detail["compress"] is True
    assert detail["use_memory"] is True
    assert detail["eval_tag"] == "tag-1"


@pytest.mark.asyncio
async def test_build_initial_state_injects_switches(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_users: dict[str, int],
):
    from app.services.task_service import TaskService

    service = TaskService(session_factory=session_factory)
    async with session_factory() as session:
        task = Task(
            task_no="AFG8TEST-0001",
            creator_id=seeded_users["admin_user"],
            goal="分析GMV",
            time_range={"start": "2026-10-01", "end": "2026-10-21"},
            status="PLANNING",
            replan_count=0,
            token_usage=0,
            trace_id=new_trace_id(),
            compress=True,
            use_memory=True,
            eval_tag="tag-1",
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)

        state: TaskState = service.build_initial_state(task)
        assert state.get("compress") is True
        assert state.get("use_memory") is True
        assert state.get("eval_tag") == "tag-1"


# ---------------------------------------------------------------------------
# 3) 节点：开关关=空操作，开=执行
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_memory_recall_node_off_is_noop():
    from app.agents.context_steps import make_memory_recall_node
    from app.services.context_service import ContextService

    node = make_memory_recall_node(ContextService())
    result = await node({"use_memory": False, "goal": "x"})
    assert result == {}


@pytest.mark.asyncio
async def test_context_compress_node_off_is_noop():
    from app.agents.context_steps import make_context_compress_node
    from app.services.context_service import ContextService

    node = make_context_compress_node(ContextService())
    result = await node({"compress": False, "messages": []})
    assert result == {}


@pytest.mark.asyncio
async def test_context_compress_node_on_with_empty_messages():
    from app.agents.context_steps import make_context_compress_node
    from app.services.context_service import ContextService

    node = make_context_compress_node(ContextService())
    # 无历史消息 → 无可压缩内容 → 空操作（不报错）
    result = await node({"compress": True, "messages": []})
    assert result == {}


@pytest.mark.asyncio
async def test_context_compress_node_on_produces_digest():
    from app.agents.context_steps import make_context_compress_node
    from app.services.context_service import ContextService

    node = make_context_compress_node(ContextService())
    # 构造足够长的历史，确保触发压缩（compress 显式调用，不依赖阈值）
    messages = [
        {"role": "user", "content": "查询A"},
        {"role": "assistant", "content": "结果是" + "长文本" * 500},
        {"role": "user", "content": "查询B"},
        {"role": "assistant", "content": "结果是" + "另一段" * 500},
    ]
    result = await node({"compress": True, "messages": messages})
    # 压缩显式执行后，应产出 digest 或记账（压缩无收益时也返回摘要/记账）
    assert "context_digest" in result or "token_ledger" in result


# ---------------------------------------------------------------------------
# 4) eval_tag 绑定 eval_run + 筛选
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_eval_run_binds_tag_and_filters(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession],
    seeded_users: dict[str, int],
):
    token = await _login(client, "admin_user")
    # 触发带 eval_tag 的评测
    resp = await client.post(
        "/api/v1/eval/runs",
        json={"dataset_id": "agentflow-30", "variant": "compress_on", "eval_tag": "campaign-a"},
        headers=_auth_header(token),
    )
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["data"]["run_id"]

    async with session_factory() as session:
        run = await session.get(EvalRun, run_id)
        assert run.eval_tag == "campaign-a"

    # 按 eval_tag 筛选命中
    resp = await client.get("/api/v1/eval/runs?eval_tag=campaign-a", headers=_auth_header(token))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["meta"]["total"] >= 1
    assert all(it["eval_tag"] == "campaign-a" for it in data["items"])

    # 按不存在的 tag 筛选为空
    resp = await client.get("/api/v1/eval/runs?eval_tag=no-such-tag", headers=_auth_header(token))
    assert resp.status_code == 200
    assert resp.json()["data"]["meta"]["total"] == 0


@pytest.mark.asyncio
async def test_eval_run_tag_defaults_none(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession],
    seeded_users: dict[str, int],
):
    token = await _login(client, "admin_user")
    resp = await client.post(
        "/api/v1/eval/runs",
        json={"dataset_id": "agentflow-30", "variant": "compress_off"},
        headers=_auth_header(token),
    )
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["data"]["run_id"]
    async with session_factory() as session:
        run = await session.get(EvalRun, run_id)
        assert run.eval_tag is None
