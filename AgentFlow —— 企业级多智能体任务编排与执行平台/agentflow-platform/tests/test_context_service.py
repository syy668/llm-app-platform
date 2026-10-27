"""上下文工程单测（FR-010 / PRD 第 9 章）。

三块能力分别验证：
1. **工具结果瘦身**：sql 前 20 行 + 全量统计、kb top-3/300 字、chart/notify 字段白名单；
2. **Token 预算**：超 80% 或消息轮数超阈值触发压缩，压缩后 Token 下降且保留关键数值；
3. **压缩**：保留最近 3 轮原文，最多压 2 层，摘要含"关键数值"白名单数字。
"""

from __future__ import annotations

import json

import pytest

from app.core.w2_config import w2
from app.services.context_service import ContextService

pytestmark = pytest.mark.unit


@pytest.fixture
def service() -> ContextService:
    return ContextService()


def long_body(index: int) -> str:
    """构造一条"真实体量"的历史消息（约 360 字），否则压缩不会有收益。"""
    return f"第 {index} 轮：GMV 125000.5，环比 -8.2%，" + "品类归因明细与库存周转说明 " * 24


# ---------------------------------------------------------------------------
# 1) 工具结果瘦身
# ---------------------------------------------------------------------------
def test_slim_sql_keeps_preview_rows_and_full_stats(service: ContextService) -> None:
    rows = [[i, 1000.0 + i, 10] for i in range(100)]
    payload = {
        "columns": ["category_id", "gmv", "order_cnt"],
        "rows": rows,
        "row_count": 100,
        "truncated": False,
        "applied_limit": 1000,
    }

    slim = service.slim_tool_result("sql-query", payload)

    assert slim.tool == "sql-query"
    assert len(slim.payload["preview_rows"]) == 20
    assert slim.payload["row_count"] == 100
    assert slim.payload["truncated"] is True  # 100 行 > 预览 20 行
    # 统计基于全量行，而非仅前 20 行（关键数字不能因截断丢失）
    assert slim.payload["stats"]["gmv"]["max"] == 1099.0
    assert slim.payload["stats"]["gmv"]["min"] == 1000.0
    assert slim.payload["stats"]["gmv"]["sum"] == round(sum(1000.0 + i for i in range(100)), 2)
    assert "rows[20:]" in slim.dropped_fields
    assert slim.after_tokens < slim.before_tokens


def test_slim_sql_rounds_floats(service: ContextService) -> None:
    payload = {
        "columns": ["gmv"],
        "rows": [[123.456789], [7.000001]],
        "row_count": 2,
    }

    slim = service.slim_tool_result("sql-query", payload)

    assert slim.payload["preview_rows"] == [[123.46], [7.0]]


def test_slim_kb_keeps_top3_and_truncates_content(service: ContextService) -> None:
    chunks = [
        {"doc_id": f"kb-{i}", "title": "周报", "content": "长" * 600, "score": 0.9 - i * 0.01, "source": "kb"}
        for i in range(5)
    ]

    slim = service.slim_tool_result("kb-search", {"chunks": chunks, "degraded": False})

    assert len(slim.payload["chunks"]) == 3
    assert len(slim.payload["chunks"][0]["content"]) == 300
    assert slim.truncated is True
    assert "chunks[3:]" in slim.dropped_fields


def test_slim_kb_degraded_adds_note(service: ContextService) -> None:
    slim = service.slim_tool_result("kb-search", {"chunks": [], "degraded": True})

    assert slim.payload["degraded"] is True
    assert "引用缺失" in slim.payload["note"]


def test_slim_chart_drops_base64(service: ContextService) -> None:
    payload = {
        "chart_id": "chart_abc",
        "image_url": "/static/charts/chart_abc.png",
        "title": "品类对比",
        "image_base64": "A" * 8000,
        "echarts_option": {"series": [1, 2, 3]},
        "render_ms": 120,
    }

    slim = service.slim_tool_result("chart-render", payload)

    assert set(slim.payload) <= {"chart_id", "image_url", "title"}
    assert "image_base64" in slim.dropped_fields
    assert "echarts_option" in slim.dropped_fields
    assert slim.after_tokens < slim.before_tokens


def test_slim_notify_drops_payload_fields(service: ContextService) -> None:
    payload = {"message_id": "m-1", "status": "SENT", "mock": True, "sent_at": "2026-10-19T10:00:00"}

    slim = service.slim_tool_result("notify", payload)

    assert set(slim.payload) == {"message_id", "status"}


def test_slim_unknown_tool_falls_back_to_json(service: ContextService) -> None:
    slim = service.slim_tool_result("mystery_tool", {"a": 1, "b": "x"})

    assert slim.tool == "mystery-tool"  # 下划线统一为连字符
    assert json.loads(slim.digest)["a"] == 1


# ---------------------------------------------------------------------------
# 2) 预算与压缩触发
# ---------------------------------------------------------------------------
def test_plan_context_within_budget_is_not_compressed(service: ContextService) -> None:
    build = service.plan_context(
        "planner",
        {"system": "s" * 200, "task": "t" * 200},
        reserved_output=500,
    )

    assert build.compressed is False
    assert build.budget == w2.TOKEN_BUDGET_PLANNER
    assert build.estimate.total > 0
    # 输出预留只参与估算，不进入正文
    assert set(build.content) <= {"s", "t", "\n"}


def test_plan_context_compresses_when_over_budget(service: ContextService) -> None:
    messages = [{"role": "assistant", "content": long_body(index)} for index in range(10)]

    build = service.plan_context(
        "reviewer",  # 预算最小（4000），便于触发
        {"system": "x" * 40000},
        messages=messages,
        reserved_output=500,
    )

    assert build.compressed is True
    assert build.after_tokens <= build.before_tokens
    assert build.history_digest


def test_plan_context_compresses_when_too_many_rounds(service: ContextService) -> None:
    messages = [{"role": "assistant", "content": long_body(index)} for index in range(14)]

    build = service.plan_context("planner", {"system": "短"}, messages=messages)

    assert len(messages) > w2.COMPRESS_MESSAGE_ROUNDS_THRESHOLD
    assert build.compressed is True


def test_plan_context_without_messages_never_compresses(service: ContextService) -> None:
    build = service.plan_context("planner", {"system": "x" * 40000})

    assert build.compressed is False
    assert build.compress_layers == 0


# ---------------------------------------------------------------------------
# 3) 压缩策略
# ---------------------------------------------------------------------------
def test_compress_keeps_recent_rounds(service: ContextService) -> None:
    messages = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": long_body(index)}
        for index in range(12)
    ]

    result = service.compress(messages)

    assert result.compressed is True
    assert result.kept_rounds == w2.COMPRESS_KEEP_RECENT_ROUNDS
    assert "[最近消息原文]" in result.digest
    assert "第 11 轮" in result.digest  # 最近一轮原文保留
    assert "关键数值" in result.digest
    assert "125000.5" in result.preserved_numbers
    assert result.after_tokens < result.before_tokens


def test_compress_second_layer_when_budget_tight(service: ContextService) -> None:
    messages = [{"role": "assistant", "content": long_body(index)} for index in range(12)]

    result = service.compress(messages, budget=100)

    assert result.compressed is True
    assert result.layers == 2  # 第二层只留"结论 + 关键数值"
    assert result.layers <= w2.COMPRESS_MAX_LAYERS


def test_compress_aborts_when_no_gain(service: ContextService) -> None:
    """历史本身很短时，模板摘要反而更长 —— 必须放弃压缩，不能越压越大。"""
    messages = [{"role": "assistant", "content": f"第 {index} 轮"} for index in range(12)]

    result = service.compress(messages)

    assert result.compressed is False
    assert result.digest == ""
    assert result.after_tokens == result.before_tokens


def test_compress_empty_messages(service: ContextService) -> None:
    result = service.compress([])

    assert result.compressed is False
    assert result.before_tokens == 0
    assert result.after_tokens == 0


def test_compress_disabled(service: ContextService) -> None:
    disabled = ContextService(enabled=False)
    messages = [{"role": "assistant", "content": f"第 {i} 轮"} for i in range(20)]

    build = disabled.plan_context("planner", {"system": "x" * 40000}, messages=messages)

    assert build.compressed is False


# ---------------------------------------------------------------------------
# 4) 记账
# ---------------------------------------------------------------------------
def test_ledger_entry(service: ContextService) -> None:
    build = service.plan_context("planner", {"system": "s" * 100})

    entry = ContextService.ledger_entry(input_tokens=120, output_tokens=80, build=build)

    assert entry["input"] == 120
    assert entry["output"] == 80
    assert entry["calls"] == 1
    assert entry["compressed"] is False
    assert entry["compress_before"] == build.before_tokens
