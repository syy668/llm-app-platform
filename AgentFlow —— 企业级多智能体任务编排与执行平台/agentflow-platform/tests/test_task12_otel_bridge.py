"""任务 12：自研 Trace ↔ OpenTelemetry 双轨打通。

既有基线：W5-Part2 已交付完整 OTel 桥（``app/observability/otel_tracing.py``，
w6.ENABLE_OTEL 开关 + BatchSpanProcessor + OTLP/console 降级 + GraphNodeSpanHandler），
本文件钉死任务 12 的增量验收：

1. ``ENABLE_OTEL=False`` → 不产生任何 span、不打任何 patch（零开销）；
2. ``ENABLE_OTEL=True`` → span 带 ``task.id`` / ``agent.name``，并自动注入
   ``agentflow.trace_id``（自研 MySQL trace 的 trace_id，双轨互查的关联键）；
3. exporter 抛异常 → 主业务流程照常走完（Fail-safe，含 SDK 行为钉死）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.context import task_id_var, trace_id_var
from app.core.w6_config import w6
from app.observability import otel_tracing as T

pytestmark = pytest.mark.unit

otel_sdk = pytest.importorskip("opentelemetry.sdk", reason="需要 opentelemetry-sdk（requirements-observability.txt）")

from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)


class ExplodingExporter:
    """export 恒抛异常的 exporter（模拟 OTLP collector 宕机 / 网络故障）。"""

    def __init__(self) -> None:
        self.export_calls = 0

    def export(self, spans: Any) -> Any:
        self.export_calls += 1
        raise RuntimeError("otlp collector down")

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


@pytest.fixture
def otel_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(w6, "ENABLE_OTEL", True)
    monkeypatch.setattr(w6, "OTEL_TRACE_HTTP", True)
    monkeypatch.setattr(w6, "OTEL_TRACE_GRAPH", True)
    monkeypatch.setattr(w6, "OTEL_TRACE_MCP", True)
    monkeypatch.setattr(w6, "OTEL_TRACE_LLM", True)
    monkeypatch.setattr(w6, "OTEL_TRACE_SQL", True)
    exporter = InMemorySpanExporter()
    assert T.init_tracing(span_exporter=exporter) is True
    assert T.install_instrumentation() is True
    try:
        yield exporter
    finally:
        T._reset_for_tests()


@pytest.fixture
def otel_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(w6, "ENABLE_OTEL", False)
    T._reset_for_tests()
    yield
    T._reset_for_tests()


# ---------------------------------------------------------------------------
# 验收 1：关闭时不产生额外 span
# ---------------------------------------------------------------------------
def test_disabled_produces_no_spans_and_no_patches(otel_off: None) -> None:
    from app.tools.mcp_client import MCPClient

    assert T.install_instrumentation() is False
    assert T.tracing_enabled() is False
    assert getattr(MCPClient, "_otel_patched", False) is False

    with T.span("disabled.span", attributes={"task.id": 1}):
        pass

    assert T.current_trace_id() == ""  # 无全局 provider → 无任何 span 被记录


# ---------------------------------------------------------------------------
# 验收 2：开启时 span 携带 task_id / agent_name / agentflow.trace_id
# ---------------------------------------------------------------------------
def test_enabled_span_carries_own_trace_correlation(otel_on: InMemorySpanExporter) -> None:
    """手动 span：自研 trace_id（ContextVar）自动注入 agentflow.trace_id 属性。"""
    token = trace_id_var.set("tr-manual-42")
    try:
        with T.span("biz.op", attributes={"custom": "v"}):
            pass
    finally:
        trace_id_var.reset(token)

    (span,) = otel_on.get_finished_spans()
    assert span.name == "biz.op"
    assert span.attributes["agentflow.trace_id"] == "tr-manual-42"
    assert span.attributes["custom"] == "v"


def test_graph_node_span_carries_task_agent_and_correlation(otel_on: InMemorySpanExporter) -> None:
    """节点 span：task.id / agent.name / agentflow.trace_id 三要素齐备（双轨互查键）。"""
    handler = T.GraphNodeSpanHandler(task_id=31, goal="分析上周 GMV")
    trace_token = trace_id_var.set("tr-task-31")
    try:
        handler.on_chain_start(
            {"name": "planner"},
            {"goal": "分析上周 GMV"},
            run_id="r1",
            metadata={"langgraph_node": "planner", "langgraph_step": "1"},
        )
        handler.on_chain_end({"ok": 1}, run_id="r1")
    finally:
        trace_id_var.reset(trace_token)

    (span,) = otel_on.get_finished_spans()
    assert span.name == "graph.node planner"
    assert span.attributes["task.id"] == 31
    assert span.attributes["agent.name"] == "planner"
    assert span.attributes["agentflow.trace_id"] == "tr-task-31"
    assert span.attributes["graph.goal"] == "分析上周 GMV"


def test_node_span_skips_route_functions(otel_on: InMemorySpanExporter) -> None:
    """路由函数（名称 != 节点名）不产生 span——agent.name 不会污染成 route_*。"""
    handler = T.GraphNodeSpanHandler(task_id=1, goal="g")
    handler.on_chain_start(
        {"name": "route_after_plan"},
        {},
        run_id="r2",
        metadata={"langgraph_node": "planner", "langgraph_step": "1"},
    )
    handler.on_chain_end({}, run_id="r2")
    assert list(otel_on.get_finished_spans()) == []


# ---------------------------------------------------------------------------
# 验收 3：exporter 抛异常 → 主流程照常走完（Fail-safe）
# ---------------------------------------------------------------------------
async def test_exporter_failure_does_not_break_business_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """export 恒炸：业务协程正常完成、span 生命周期不外泄异常、无未捕获错误。"""
    monkeypatch.setattr(w6, "ENABLE_OTEL", True)
    monkeypatch.setattr(w6, "OTEL_TRACE_GRAPH", True)
    monkeypatch.setattr(w6, "OTEL_TRACE_MCP", True)
    monkeypatch.setattr(w6, "OTEL_TRACE_LLM", True)
    monkeypatch.setattr(w6, "OTEL_TRACE_SQL", True)
    broken = ExplodingExporter()
    try:
        assert T.init_tracing(span_exporter=broken) is True
        assert T.install_instrumentation() is True

        # ① 手动 span 包住业务代码：exporter 炸 → 业务照常返回
        async def business() -> str:
            with T.span("biz.under_failure"):
                await asyncio.sleep(0)
            return "done"

        assert await business() == "done"

        # ② 节点回调路径：start/end 全程不外泄异常
        handler = T.GraphNodeSpanHandler(task_id=7, goal="g")
        handler.on_chain_start({"name": "executor"}, {}, run_id="r3", metadata={"langgraph_node": "executor"})
        handler.on_chain_end({}, run_id="r3")
        handler.close_pending()
    finally:
        T._reset_for_tests()

    assert broken.export_calls >= 1  # exporter 确实被调过（异常真实发生过）


async def test_exporter_failure_during_llm_patch_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM 补丁路径下 exporter 炸：LLM 调用结果不受影响（主备门面照常返回）。"""
    monkeypatch.setattr(w6, "ENABLE_OTEL", True)
    monkeypatch.setattr(w6, "OTEL_TRACE_LLM", True)
    broken = ExplodingExporter()
    try:
        assert T.init_tracing(span_exporter=broken) is True
        assert T.install_instrumentation() is True

        from app.agents.llm import LLMClient

        client = LLMClient(mock=True)
        response = await client.complete([{"role": "user", "content": "ping"}])
        assert response.content  # 业务结果正常
    finally:
        T._reset_for_tests()


# ---------------------------------------------------------------------------
# 双轨关联的上下文绑定冒烟：task_id ContextVar 与 OTel task.id 属性并存
# ---------------------------------------------------------------------------
def test_task_context_vars_coexist(otel_on: InMemorySpanExporter) -> None:
    """任务 7 的 task_id_var（日志用）与 OTel task.id 属性互不干扰。"""
    token = task_id_var.set(99)
    try:
        with T.span("coexist.check", attributes={"task.id": 99}):
            assert task_id_var.get() == 99
    finally:
        task_id_var.reset(token)

    (span,) = otel_on.get_finished_spans()
    assert span.attributes["task.id"] == 99
    assert "agentflow.trace_id" not in span.attributes  # 未绑定自研 trace_id → 不注入空键
