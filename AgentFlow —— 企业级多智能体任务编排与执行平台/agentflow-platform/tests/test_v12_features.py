"""v1.1 增补功能测试：工具注册表 / 全局限流 / 评审 REJECT 人工批注。

三块能力各自独立，测试也分开组织，便于定位回归：

1. ``/tools``：目录元信息、配置脱敏、RBAC（读=ADMIN/APPROVER，写=ADMIN）、启停落库 + 审计；
   以及运行时过滤 ``filter_enabled_tools``（含"全部禁用 → 退回全量"的安全兜底）。
2. 限流：请求分类（api/llm/kb）、固定窗口计数（含 429 判定）、纯 ASGI 中间件端到端 429；
   测试环境默认关闭（``APP_ENV=test``），这里显式打开 ``app.state.rate_limit_enabled``。
3. review_gate：REJECT 挂起、批注注入 ``replan_context.human_comment``、人工放行短路汇总，
   以及"仅 REJECT 挂起、REVISE 直接放行"的边界。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.agents.review_gate import (
    INTERVENTION_TYPE,
    make_review_gate_node,
    route_after_review_gate,
)
from app.core.rate_config import rate_limit as rl
from app.core.rate_limit import RateLimiter, RateLimitMiddleware, classify_request, is_exempt
from app.schemas.agent_io import ToolDef
from app.schemas.graph_state import TaskState
from app.services.tool_registry import TOOL_CATALOG, ToolRegistry, filter_enabled_tools

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


# ===========================================================================
# 1. 工具注册表
# ===========================================================================
@pytest.mark.asyncio
async def test_tool_catalog_contains_builtin_tools(client: AsyncClient) -> None:
    token = await _login(client, "admin_user")
    response = await client.get("/api/v1/tools", headers=_auth_header(token))
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    names = {item["name"] for item in data["items"]}
    assert names == set(TOOL_CATALOG)
    assert "kb-search" in names
    assert data["total"] == len(TOOL_CATALOG)
    assert data["enabled"] == len(TOOL_CATALOG)  # 默认全启用
    assert data["disabled"] == 0

    kb = next(item for item in data["items"] if item["name"] == "kb-search")
    assert kb["display_name"] == "知识库检索"
    assert kb["external"] is True
    assert "query" in kb["input_schema"]["properties"]
    assert kb["input_schema"]["required"] == ["query"]
    assert "chunks" in kb["output_schema"]
    # 配置项必须可读但不泄露密钥（服务账号密码不在 config_keys 里，且值都做过脱敏）
    keys = {hint["key"] for hint in kb["config_hints"]}
    assert "MOCK_EXTERNAL_RAG" in keys
    assert all("PASSWORD" not in hint["key"] or hint["value"] in {"", "***"} for hint in kb["config_hints"])


@pytest.mark.asyncio
async def test_tool_catalog_permissions(client: AsyncClient) -> None:
    approver = await _login(client, "approver_user")
    viewer = await _login(client, "viewer_user")

    assert (await client.get("/api/v1/tools", headers=_auth_header(approver))).status_code == 200
    # 访客不参与运维，读工具目录也 403
    assert (await client.get("/api/v1/tools", headers=_auth_header(viewer))).status_code == 403

    # 写操作仅 ADMIN：APPROVER 即使能读也不能切开关
    denied = await client.patch(
        "/api/v1/tools/kb-search", json={"enabled": False}, headers=_auth_header(approver)
    )
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_tool_toggle_persists_and_audits(client: AsyncClient) -> None:
    token = await _login(client, "admin_user")

    off = await client.patch(
        "/api/v1/tools/kb-search",
        json={"enabled": False, "reason": "演示临时下线知识库"},
        headers=_auth_header(token),
    )
    assert off.status_code == 200, off.text
    assert off.json()["data"]["enabled"] is False
    assert off.json()["data"]["updated_by"] == "admin_user"

    listed = await client.get("/api/v1/tools", headers=_auth_header(token))
    kb = next(item for item in listed.json()["data"]["items"] if item["name"] == "kb-search")
    assert kb["enabled"] is False
    assert listed.json()["data"]["disabled"] == 1
    # 审计留痕
    assert any(row.get("tool") == "kb-search" and row.get("enabled") is False for row in listed.json()["data"]["audit"])

    # 复位，避免影响同一进程内其它用例（fake redis 在同一 fixture 内共享）
    on = await client.patch("/api/v1/tools/kb-search", json={"enabled": True}, headers=_auth_header(token))
    assert on.json()["data"]["enabled"] is True


@pytest.mark.asyncio
async def test_tool_detail_and_unknown(client: AsyncClient) -> None:
    token = await _login(client, "admin_user")
    ok = await client.get("/api/v1/tools/sql-query", headers=_auth_header(token))
    assert ok.status_code == 200
    assert ok.json()["data"]["category"] == "database"

    missing = await client.get("/api/v1/tools/not-exist", headers=_auth_header(token))
    assert missing.status_code == 404
    assert missing.json()["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_filter_enabled_tools(fake_redis) -> None:
    registry = ToolRegistry(redis=fake_redis)
    tools = [
        ToolDef(name="kb-search", server="kb-search"),
        ToolDef(name="sql-query", server="sql-query"),
    ]
    await registry.set_enabled("kb-search", False, operator="tester")

    kept = await filter_enabled_tools(tools, registry=registry)
    assert [tool.name for tool in kept] == ["sql-query"]

    # 全部禁用时退回全量：宁可让 Planner 有工具可用，也不让任务必然产出空计划
    await registry.set_enabled("sql-query", False, operator="tester")
    kept_all = await filter_enabled_tools(tools, registry=registry)
    assert {tool.name for tool in kept_all} == {"kb-search", "sql-query"}


# ===========================================================================
# 2. 全局限流
# ===========================================================================
def test_classify_request_buckets() -> None:
    assert classify_request("POST", "/api/v1/tasks") == ["llm", "api"]
    assert classify_request("POST", "/api/v1/tasks/stream") == ["llm", "api"]
    assert classify_request("POST", "/api/v1/tasks/12/resume") == ["llm", "api"]
    assert classify_request("POST", "/api/v1/approvals/batch") == ["llm", "api"]
    assert classify_request("GET", "/api/v1/kb/status") == ["kb", "api"]
    assert classify_request("GET", "/api/v1/tasks/12/trace") == ["api"]
    # 健康检查 / 文档 / 静态资源不进限流
    assert is_exempt("/healthz") is True
    assert is_exempt("/docs") is True
    assert is_exempt("/api/v1/tasks") is False


@pytest.mark.asyncio
async def test_rate_limiter_window_and_usage() -> None:
    limiter = RateLimiter(redis=None, limits={"kb": 2, "llm": 1})

    first = await limiter.hit("kb", user_id=7)
    second = await limiter.hit("kb", user_id=7)
    third = await limiter.hit("kb", user_id=7)
    assert (first.allowed, second.allowed, third.allowed) == (True, True, False)
    assert third.remaining == 0
    assert third.as_headers()["X-RateLimit-Limit"] == "2"

    # 不同用户互不影响
    other = await limiter.hit("kb", user_id=8)
    assert other.allowed is True

    usage = await limiter.usage("kb", user_id=7)
    assert usage["limit"] == 2 and usage["used"] == 3 and usage["remaining"] == 0

    # 任务维度：同一任务计数，不同任务独立
    task_a = await limiter.hit("task", task_id=101)
    task_b = await limiter.hit("task", task_id=102)
    assert task_a.subject == "task:101" and task_b.subject == "task:102"
    assert task_a.allowed and task_b.allowed


@pytest_asyncio.fixture
async def rate_limited_app() -> FastAPI:
    """最小应用：只挂限流中间件 + 一个探针路由，避免鉴权/DB 干扰限流断言。"""
    app = FastAPI()

    @app.get("/api/v1/probe")
    async def probe() -> dict:
        return {"code": "OK", "data": {"ok": True}}

    app.add_middleware(RateLimitMiddleware)
    app.state.rate_limit_enabled = True
    app.state.rate_limit_limiter = RateLimiter(redis=None, limits={"api": 2, "llm": 1})
    return app


@pytest.mark.asyncio
async def test_middleware_returns_429_with_standard_envelope(rate_limited_app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=rate_limited_app), base_url="http://testserver") as http:
        first = await http.get("/api/v1/probe")
        second = await http.get("/api/v1/probe")
        third = await http.get("/api/v1/probe")

    assert first.status_code == 200
    assert first.headers["X-RateLimit-Limit"] == "2"
    assert first.headers["X-RateLimit-Remaining"] == "1"
    assert second.status_code == 200
    assert third.status_code == 429
    assert third.headers["Retry-After"].isdigit()
    body = third.json()
    assert body["code"] == "RATE_LIMITED"
    assert body["data"]["detail"]["kind"] == "api"
    assert body["data"]["detail"]["limit"] == 2
    assert body["data"]["detail"]["retry_after"] >= 1


@pytest.mark.asyncio
async def test_middleware_exempt_path_not_counted(rate_limited_app: FastAPI) -> None:
    @rate_limited_app.get("/healthz")
    async def health() -> dict:
        return {"status": "ok"}

    async with AsyncClient(transport=ASGITransport(app=rate_limited_app), base_url="http://testserver") as http:
        for _ in range(5):
            assert (await http.get("/healthz")).status_code == 200


def test_rate_limit_config_defaults_are_ordered() -> None:
    """配额关系必须自洽：LLM（最贵）< KB（外部依赖）< 普通接口。"""
    assert rl.RATE_LIMIT_LLM_PER_MINUTE < rl.RATE_LIMIT_KB_PER_MINUTE
    assert rl.RATE_LIMIT_KB_PER_MINUTE < rl.RATE_LIMIT_API_PER_MINUTE
    assert 1 <= rl.RATE_LIMIT_WINDOW_SECONDS <= 3600


# ===========================================================================
# 3. review_gate：REJECT 人工批注
# ===========================================================================
def _review_state(*, verdict: str = "REJECT", total: float = 2.0) -> TaskState:
    return {
        "task_id": 1,
        "goal": "分析数码配件 GMV 下滑原因",
        "replan_count": 1,
        "review": {
            "verdict": verdict,
            "total": total,
            "confidence": 0.32,
            "confidence_reason": "评分 2.0/5 × 证据覆盖 20%",
            "suggestions": [{"seq": 1, "advice": "补充断货 SKU 的库存周转证据"}],
        },
    }


def _gate_graph(enabled: bool = True):
    builder = StateGraph(TaskState)
    builder.add_node("review_gate", make_review_gate_node(enabled=enabled))
    builder.add_node("replan", lambda state: {"messages": [{"role": "assistant", "content": "replanned"}]})
    builder.add_node("summarizer", lambda state: {"messages": [{"role": "assistant", "content": "done"}]})
    builder.add_edge(START, "review_gate")
    builder.add_conditional_edges(
        "review_gate", route_after_review_gate, {"replan": "replan", "summarizer": "summarizer"}
    )
    builder.add_edge("replan", END)
    builder.add_edge("summarizer", END)
    return builder.compile(checkpointer=MemorySaver())


@pytest.mark.asyncio
async def test_review_gate_interrupts_on_reject() -> None:
    graph = _gate_graph()
    config = {"configurable": {"thread_id": "review-gate-reject"}}

    await graph.ainvoke(_review_state(), config)
    snapshot = await graph.aget_state(config)
    pending = [(task.name, len(task.interrupts)) for task in snapshot.tasks]
    assert ("review_gate", 1) in pending

    payload = snapshot.tasks[0].interrupts[0].value
    assert payload["type"] == INTERVENTION_TYPE
    assert payload["preview"]["verdict"] == "REJECT"
    assert payload["preview"]["total"] == 2.0
    assert payload["preview"]["suggestions"][0]["seq"] == 1
    assert "REJECT" in payload["reason"]


@pytest.mark.asyncio
async def test_review_gate_annotation_flows_into_replan_context() -> None:
    graph = _gate_graph()
    config = {"configurable": {"thread_id": "review-gate-annotate"}}
    await graph.ainvoke(_review_state(), config)

    result = await graph.ainvoke(
        Command(resume={"decision": "REJECTED", "comment": "按环比口径重算，并单列断货 SKU", "decided_by": 9}),
        config,
    )

    assert result["review_intervention"]["status"] == "ANNOTATED"
    assert result["review_intervention"]["comment"] == "按环比口径重算，并单列断货 SKU"
    # 批注必须进 replan_context.human_comment（replan 的 _build_context 以此为首要约束）
    assert result["replan_context"]["cause"] == "human_rejected"
    assert "环比" in result["replan_context"]["human_comment"]
    assert result["status"] == "RUNNING"


@pytest.mark.asyncio
async def test_review_gate_manual_approve_skips_replan() -> None:
    graph = _gate_graph()
    config = {"configurable": {"thread_id": "review-gate-approve"}}
    await graph.ainvoke(_review_state(), config)

    result = await graph.ainvoke(Command(resume={"decision": "APPROVED", "decided_by": 9}), config)
    assert result["review_intervention"]["status"] == "APPROVED"
    assert result["status"] == "SUMMARIZING"
    assert result["degraded"] is True
    # 走了 summarizer 分支：replan_context 不被写入
    assert not result.get("replan_context")


@pytest.mark.asyncio
async def test_review_gate_only_interrupts_on_reject() -> None:
    """REVISE 是常规"改改就行"路径，不能被拦下来等人工。"""
    graph = _gate_graph()
    config = {"configurable": {"thread_id": "review-gate-revise"}}
    result = await graph.ainvoke(_review_state(verdict="REVISE", total=3.2), config)
    snapshot = await graph.aget_state(config)
    assert all(not task.interrupts for task in snapshot.tasks)
    assert result["messages"][-1]["content"] == "replanned"


def test_route_after_review_gate() -> None:
    assert route_after_review_gate({"review_intervention": {"status": "APPROVED"}}) == "summarizer"
    assert route_after_review_gate({"review_intervention": {"status": "ANNOTATED"}}) == "replan"
    assert route_after_review_gate({}) == "replan"
    assert route_after_review_gate({"status": "FAILED"}) == "summarizer"


def test_graph_registers_review_gate_node(w2_graph) -> None:
    """真实装配的图里必须有 review_gate（否则 REJECT 无法挂起等批注）。"""
    nodes = set(w2_graph.get_graph().nodes)
    assert "review_gate" in nodes
