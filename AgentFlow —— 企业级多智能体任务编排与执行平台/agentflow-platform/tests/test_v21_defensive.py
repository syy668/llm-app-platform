"""v2.1 防御性补丁三件套的单测。

1. 上下文超载硬熔断：truncate_context 纯函数 + complete_structured 公共入口拦截；
2. 工具错误脱敏：sanitize_tool_error（错误码/关键词映射，≤200 字符，无堆栈）；
3. 防幻觉强约束：SUMMARIZER_SYSTEM 注入溯源格式与 [NO_DATA] 死命令，
   summarizer 节点对 [NO_DATA] 输出触发 FAILED(NO_DATA_FOUND)。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agents.llm import LLMResponse
from app.agents.prompts import SUMMARIZER_SYSTEM
from app.agents.structured import complete_structured, truncate_context
from app.agents.summarizer import make_summarizer_node
from app.core.w2_config import w2
from app.schemas.agent_io import SummaryOutput
from app.services.context_service import ContextService


class StubLLM:
    """最小 LLM stub：返回固定 content，并捕获收到的 messages 供断言。"""

    def __init__(self, content: str) -> None:
        self.content = content
        self.captured: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = True,
    ) -> LLMResponse:
        self.captured.append(messages)
        return LLMResponse(content=self.content, model="stub", prompt_tokens=1, completion_tokens=1)


class FakeToolError(Exception):
    """模拟 ToolInvocationError：携带 PRD 错误码。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _summary_content(markdown: str) -> str:
    return json.dumps(
        {"markdown": markdown, "cited_charts": [], "cited_docs": [], "degraded": False},
        ensure_ascii=False,
    )


def _base_state() -> dict[str, Any]:
    return {
        "task_id": 1,
        "goal": "分析上周 GMV",
        "time_range": {"start": "2026-10-12", "end": "2026-10-18"},
        "plan": [],
        "subtask_results": {},
        "artifacts": [],
    }


# ---------------------------------------------------------------------------
# 改进 1：truncate_context
# ---------------------------------------------------------------------------
def test_truncate_within_budget_returns_original():
    messages = [
        {"role": "system", "content": "系统规则"},
        {"role": "user", "content": "问题"},
    ]
    assert truncate_context(messages, max_chars=12000) is messages  # 未超限零改动


def test_truncate_keeps_system_first_and_drops_oldest():
    system = {"role": "system", "content": "S" * 100}
    history = [{"role": "user", "content": f"H{i}" * 50} for i in range(5)]  # 每条 100 字符
    latest = {"role": "user", "content": "Q" * 100}
    messages = [system, *history, latest]  # 总 600

    result = truncate_context(messages, max_chars=400)
    total = sum(len(str(m["content"])) for m in result)

    assert result[0]["content"] == system["content"], "System Message 必须全量保留且在最前"
    assert total <= 400, "截断后必须达标"
    assert len(result) < len(messages), "必须丢弃 earliest 历史"
    # 保留的应是"最新"的消息（latest 必在），最早的 H0 优先被丢
    assert any(m["content"] == latest["content"] for m in result)
    assert all("H0" * 50 != m["content"] for m in result)


def test_truncate_hard_cuts_single_oversized_user_message():
    system = {"role": "system", "content": "S" * 100}
    huge_user = {"role": "user", "content": "U" * 5000}
    result = truncate_context([system, huge_user], max_chars=1200)

    assert len(result) == 2, "不能把唯一的 user 消息整条丢弃"
    assert result[0]["content"] == system["content"]
    body = str(result[1]["content"])
    assert len(body) <= 1200
    assert body.endswith("[上下文超限已截断]")


def test_truncate_logs_warning_with_stats(caplog):
    messages = [{"role": "system", "content": "S" * 100}, *[{"role": "user", "content": "H" * 200} for _ in range(5)]]
    with caplog.at_level(logging.WARNING, logger="app.agents.structured"):
        truncate_context(messages, max_chars=600)

    records = [r for r in caplog.records if r.getMessage() == "context_truncated"]
    assert records, "必须输出 context_truncated WARNING"
    extra = records[0].__dict__
    assert extra["dropped"] >= 1
    assert extra["original_chars"] == 100 + 5 * 200


async def test_complete_structured_applies_truncation():
    llm = StubLLM(_summary_content("# 报告\n结论：x 依据：y"))
    messages = [
        {"role": "system", "content": "S" * 200},
        {"role": "user", "content": "U" * (w2.CONTEXT_MAX_CHARS * 2)},
    ]
    result = await complete_structured(llm, messages, SummaryOutput)

    assert result.value.markdown.startswith("# 报告")
    sent = llm.captured[0]
    total = sum(len(str(m.get("content") or "")) for m in sent)
    assert total <= w2.CONTEXT_MAX_CHARS, "发给 LLM 的 messages 必须已熔断"
    assert sent[0]["role"] == "system"


# ---------------------------------------------------------------------------
# 改进 2：sanitize_tool_error
# ---------------------------------------------------------------------------
def test_sanitize_timeout_by_code():
    from app.agents.executor import Executor

    out = Executor.sanitize_tool_error(FakeToolError("MCP_TIMEOUT", "内部堆栈 " + "x" * 3000))
    assert out.startswith("[系统提示]")
    assert "超时" in out
    assert len(out) <= 200
    assert "x" * 100 not in out and "内部堆栈" not in out


def test_sanitize_sql_syntax_by_code():
    from app.agents.executor import Executor

    out = Executor.sanitize_tool_error(FakeToolError("SQL_SYNTAX", "Unknown column 'foo' ... 堆栈"))
    assert "SQL" in out and "语法" in out
    assert "Unknown column" not in out


def test_sanitize_keyword_fallback_connection_refused():
    from app.agents.executor import Executor

    err = ConnectionRefusedError("connection refused to 127.0.0.1:3307 " + "y" * 2000)
    out = Executor.sanitize_tool_error(err)
    assert out.startswith("[系统提示]")
    assert "数据库" in out
    assert len(out) <= 200
    assert "3307" not in out, "不得泄漏底层连接细节"


def test_sanitize_giant_unknown_exception_no_traceback():
    from app.agents.executor import Executor

    giant = Exception("Traceback (most recent call last):\n  at 0x7f... \n" + "z" * 5000)
    out = Executor.sanitize_tool_error(giant)
    assert len(out) <= 200, "必须硬上限 200 字符"
    assert "Traceback" not in out, "不得携带底层堆栈"
    assert out.startswith("[系统提示]")


def test_sanitize_timeout_exception_without_code():
    from app.agents.executor import Executor

    out = Executor.sanitize_tool_error(TimeoutError("operation timed out after 10s"))
    assert "超时" in out and len(out) <= 200


# ---------------------------------------------------------------------------
# 改进 3：防幻觉强约束 + [NO_DATA] 拦截
# ---------------------------------------------------------------------------
def test_summarizer_prompt_contains_anti_hallucination_directive():
    assert "[NO_DATA]" in SUMMARIZER_SYSTEM, "必须注入 [NO_DATA] 死命令"
    assert "依据：" in SUMMARIZER_SYSTEM, "必须注入「结论/依据」溯源格式"
    assert "严禁基于你的先验知识编造" in SUMMARIZER_SYSTEM


async def test_summarizer_no_data_fails_task():
    llm = StubLLM(_summary_content("结论：[GMV 上涨] 依据：[NO_DATA]"))
    node = make_summarizer_node(llm, ContextService())

    result = await node(_base_state())

    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "NO_DATA_FOUND"
    assert "messages" in result


async def test_summarizer_normal_output_not_blocked():
    llm = StubLLM(_summary_content("结论：[GMV 环比 -3.2%] 依据：[seq1 数据digest]"))
    node = make_summarizer_node(llm, ContextService())

    result = await node(_base_state())

    assert result.get("status") == "COMPLETED"
    assert "error" not in result
    assert result["summary"].startswith("结论：")
