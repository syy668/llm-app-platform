"""对话页「参数配置」与实际生效的一致性护栏（v1.18）。

背景：这些开关在功能上早就生效（route_condition 会回退 direct），但
1. 条件边不改写 state["route"]，对外仍报「知识库检索」；
2. direct 分支的 system prompt 仍要求「没有资料就说知识库未找到」；
两者叠加，用户看到「关了开关 → 知识库检索 + 未找到」，自然判断成开关无效。
本文件把「对外报的路由 = 实际执行的路由」和「direct 不再自称未检索到」钉死。
"""
import pytest

from app.agent import graph as G
from app.config import settings
from app.core import runtime_config, tracing


# ---------------------------------------------------------------------------
# 一、被关闭的路由必须能被识别出来（而不是只影响走了哪条边）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", ["rag", "db_query", "report", "parallel_compare"])
def test_disabled_route_recognizes_each_tool_route(route):
    """四路工具任一被关都能识别，返回的就是被关的那一路。"""
    assert G.disabled_route({"route": route, "route_enabled": {route: False}}) == route
    assert G.route_condition({"route": route, "route_enabled": {route: False}}) == "direct"


@pytest.mark.parametrize("state", [
    {"route": "rag"},
    {"route": "rag", "route_enabled": None},
    {"route": "rag", "route_enabled": {"rag": True}},
    {"route": "direct"},
    {"route": "direct", "route_enabled": {"rag": False}},
    {},
])
def test_disabled_route_none_when_not_blocked(state):
    """未关 / 非工具路由 / 空 state 一律返回 None，避免误报「已关闭」。"""
    assert G.disabled_route(state) is None


# ---------------------------------------------------------------------------
# 二、run_agent 对外只报实际执行的路由
# ---------------------------------------------------------------------------

class _FakeGraph:
    def __init__(self, final_state: dict):
        self.final_state = final_state
        self.invoked = None

    def invoke(self, state, config=None):
        self.invoked = state
        return self.final_state


def _run_with_final_state(monkeypatch, final_state: dict) -> dict:
    fake = _FakeGraph(final_state)
    monkeypatch.setattr(G, "get_graph", lambda: fake)
    return G.run_agent([], question="研发部一线城市住宿标准", kb_id=6)


def test_run_agent_reports_direct_when_route_blocked(monkeypatch):
    """分类判 rag 但 rag 被关：对外 route=direct，并把被关的一路带出来。"""
    out = _run_with_final_state(monkeypatch, {
        "route": "rag",
        "route_enabled": {"rag": False, "db_query": True, "report": True, "parallel_compare": True},
        "final_answer": "500 元/天",
        "sources": [{"doc_name": "x", "chunk_no": 1}],
    })
    assert out["route"] == "direct"          # 实际执行的就是 direct
    assert out["route_requested"] == "rag"   # 分类本来想检索
    assert out["route_disabled"] == "rag"    # 前端据此显示「已关闭知识库检索」
    assert out["sources"] == [{"doc_name": "x", "chunk_no": 1}]


def test_run_agent_keeps_route_when_not_blocked(monkeypatch):
    """开关全开时行为不变：route 原样返回，route_disabled 为 None。"""
    out = _run_with_final_state(monkeypatch, {
        "route": "rag",
        "route_enabled": None,
        "final_answer": "500 元/天",
    })
    assert out["route"] == "rag"
    assert out["route_requested"] == "rag"
    assert out["route_disabled"] is None


def test_run_agent_direct_has_no_disabled_marker(monkeypatch):
    """本来就是 direct（寒暄/闲聊）：route_disabled 必须为空，否则会被标成"关了开关"。"""
    out = _run_with_final_state(monkeypatch, {"route": "direct", "final_answer": "你好"})
    assert out["route"] == "direct"
    assert out["route_disabled"] is None


# ---------------------------------------------------------------------------
# 三、direct 分支不得继承认「知识库未找到」的系统提示
# ---------------------------------------------------------------------------

def test_system_prompt_does_not_force_not_found_on_direct():
    """「资料里没有就说找不到」只在有资料时成立，不能写进全局 system prompt。

    写进全局会出现：用户主动关掉检索 → 模型没有资料 → 照着提示答
    「知识库中未找到相关信息」。用户视角就是"关了反而更差"。
    """
    prompt = G._ANSWER_SYSTEM_PROMPT
    assert "必须明确说明" not in prompt
    assert "仅在提供了资料/数据时" in prompt
    # 允许出现"知识库中未找到"这五个字，但只能作为**禁止**表述出现
    assert "不要输出“知识库中未找到相关信息”" in prompt


# ---------------------------------------------------------------------------
# 四、Trace 记录的模型名必须与配置页实际生效值同源
# ---------------------------------------------------------------------------

class _FakeSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _flush_model_name(monkeypatch) -> str:
    fake = _FakeSession()
    monkeypatch.setattr(tracing, "SessionLocal", lambda: fake)
    monkeypatch.setattr(tracing, "ensure_trace_tables", lambda: None)
    handler = tracing.TraceCallbackHandler(trace_id="t-1", user_id=1, question="q")
    handler.flush(status="success")
    rows = [o for o in fake.added if o.__class__.__name__ == "TraceLog"]
    assert rows, "flush 未写入 trace_logs"
    return rows[0].model


def test_trace_model_follows_runtime_config_override(monkeypatch):
    """配置页把模型改成 qwen3.7-plus 时，Trace 也必须记这个值。

    原实现只读 settings.llm_model（.env 快照），导致真实库 1538 条 trace
    全记成 deepseek-chat，按模型做成本/质量归因全部失真。
    """
    monkeypatch.setattr(settings, "llm_model", "deepseek-chat")
    monkeypatch.setattr(
        tracing.runtime_config, "get_value",
        lambda key: "qwen3.7-plus" if key == "llm_model" else None,
    )
    assert _flush_model_name(monkeypatch) == "qwen3.7-plus"


def test_trace_model_falls_back_to_env_then_default(monkeypatch):
    """配置页没覆盖时回退 .env；两者都空才用兜底值，顺序与工厂一致。"""
    monkeypatch.setattr(tracing.runtime_config, "get_value", lambda key: None)
    monkeypatch.setattr(settings, "llm_model", "glm-4")
    assert _flush_model_name(monkeypatch) == "glm-4"

    monkeypatch.setattr(settings, "llm_model", "")
    assert _flush_model_name(monkeypatch) == "deepseek-chat"
