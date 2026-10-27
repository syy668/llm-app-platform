"""Executor 节点（PRD FR-004）：子任务 → 工具选择 → MCP 调用 → 重试/降级 → 结果瘦身。

严格复用 PRD 4.2 可靠性降级矩阵：

| 失败类型 | 重试 | 退避 | 降级路径 |
|---|---|---|---|
| SQL_TIMEOUT | 3 | 2s/4s | 缩小时间窗重写 SQL |
| SQL_SYNTAX / SQL_TOO_LARGE | 0（确定性） | — | 回喂错误改写 SQL，连续 3 次被拒 → FAILED |
| chart-render 失败 | 2 | 1s | 切 bar→line，仍失败输出 Markdown 表格兜底 |
| kb-search 失败 | 2 | 1s/2s | 返回 degraded，标注"引用缺失"，不阻塞 |
| notify 失败 | 1 | 2s | 直接 FAILED（Mock 下失败即配置错误） |

另有两道硬约束：
- **工具循环上限 8 次**（FR-004 验收 3）→ ``TOOL_LOOP_LIMIT``；
- **工具结果必须瘦身**后才进上下文（PRD 9.3），原始结果只留在本次执行的局部变量中。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Awaitable, Callable

import sqlglot
from pydantic import BaseModel
from sqlglot import exp

from app.agents.common import (
    analysis_subtasks,
    as_time_range,
    done_seqs,
    failed_seqs,
    ledger,
    node_error,
    plan_of,
    results_of,
)
from app.agents.llm import LLMClient, TokenBudgetExceededError
from app.agents.prompts import build_executor_messages
from app.agents.structured import complete_structured
from app.core.logging import get_logger
from app.core.w2_config import w2
from app.schemas.agent_io import (
    ExecutorInput,
    SubtaskExecution,
    ToolCallOutcome,
    ToolCallRequest,
    ToolDef,
)
from app.schemas.graph_state import ArtifactRef, SubtaskPlan, SubtaskResult, TaskState
from app.schemas.mcp import DETERMINISTIC_ERRORS, RETRYABLE_ERRORS, McpErrorCode
from app.services.context_service import ContextService
from app.services.stream_context import bind_subtask, unbind_subtask
from app.tools.mcp_client import MCPClientPool, MCPTransportError, ToolInvocationError
from app.utils.dag import blocked_seqs, sort_by_order
from app.utils.tokens import estimate_json_tokens

logger = get_logger(__name__)

AGENT_NAME = "executor"
Sleeper = Callable[[float], Awaitable[None]]

# 工具 -> (重试次数, 退避序列)（PRD 4.2）
RETRY_MATRIX: dict[str, tuple[int, list[float]]] = {
    "sql-query": (3, [2.0, 4.0, 4.0]),
    "chart-render": (2, [1.0, 1.0]),
    "kb-search": (2, [1.0, 2.0]),
    "notify": (1, [2.0]),
}
SQL_REWRITE_LIMIT = 3  # 连续 3 次 SQL 被拒 → 子任务 FAILED → Replan


class SqlRewriteRequest(BaseModel):
    """SQL 自反思重写的结构化出参（加固任务 2：错误 Observation 回喂 LLM）。"""

    sql: str
    max_rows: int = 200


class ExecutorError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(slots=True)
class ExecutorRun:
    """单子任务执行结果 + 本次产出的原始 payload（供图表类子任务复用，不落 state）。"""

    execution: SubtaskExecution
    payload: dict[str, Any] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0


class Executor:
    def __init__(
        self,
        pool: MCPClientPool,
        llm: LLMClient,
        context: ContextService,
        *,
        tools: list[ToolDef] | None = None,
        sleeper: Sleeper | None = None,
        backoff_enabled: bool | None = None,
        loop_limit: int | None = None,
        circuit: Any = None,
        tool_timeout: float | None = None,
    ) -> None:
        self.pool = pool
        self.llm = llm
        self.context = context
        self.tools = tools or pool.tools
        self.sleeper = sleeper or asyncio.sleep
        self.backoff_enabled = w2.TOOL_RETRY_BACKOFF_ENABLED if backoff_enabled is None else backoff_enabled
        self.loop_limit = loop_limit or w2.TOOL_LOOP_LIMIT
        # 加固任务 2：熔断器（None=不启用，单测路径可不传）
        self.circuit = circuit
        # 加固任务 2 → 任务 16：外层强制超时从"一刀切 5s"改为 per-tool 预算——
        # 按工具名读 w2 配置（sql=SQL_EXEC / kb=KB / chart=CHART / notify=NOTIFY），
        # 未命中映射用 DEFAULT_TOOL_TIMEOUT_SECONDS 兜底；显式传 tool_timeout（测试注入）优先。
        self._explicit_tool_timeout = None if tool_timeout is None else float(tool_timeout)
        self.tool_timeout = self._explicit_tool_timeout if self._explicit_tool_timeout is not None else float(
            w2.DEFAULT_TOOL_TIMEOUT_SECONDS
        )

    # 任务 16：per-tool 超时预算映射（executor 层预算 ≥ mcp_client 传输层预算，以宽者为准）
    _TOOL_BUDGETS: dict[str, str] = {
        "sql-query": "SQL_EXEC_TIMEOUT_SECONDS",
        "chart-render": "CHART_RENDER_TIMEOUT_SECONDS",
        "kb-search": "KB_TIMEOUT_SECONDS",
        "notify": "NOTIFY_TIMEOUT_SECONDS",
    }

    def _tool_budget(self, tool: str) -> float:
        """解析单个工具的强制超时预算：显式注入优先，否则按工具名读配置，默认 5s 兜底。"""
        if self._explicit_tool_timeout is not None:
            return self._explicit_tool_timeout
        value = getattr(w2, self._TOOL_BUDGETS.get(tool, ""), None)
        return float(value if value is not None else w2.DEFAULT_TOOL_TIMEOUT_SECONDS)

    # ------------------------------------------------------------------
    # 单子任务
    # ------------------------------------------------------------------
    async def run_subtask(
        self,
        subtask: SubtaskPlan,
        *,
        siblings: list[str],
        digest: str,
        source_payloads: dict[int, dict[str, Any]] | None = None,
        time_range: Any = None,
    ) -> ExecutorRun:
        tool = subtask.tool_name
        if tool == "none":
            text = self._compose_analysis(subtask, siblings)
            result = SubtaskResult(
                subtask_seq=subtask.seq,
                status="DONE",
                data_digest=text[:4000],
                artifacts=[],
                tool="none",
            )
            return ExecutorRun(
                execution=SubtaskExecution(
                    subtask_seq=subtask.seq,
                    name=subtask.name,
                    tool="none",
                    status="DONE",
                    result=result,
                )
            )

        arguments = await self._build_arguments(
            subtask,
            tool,
            siblings=siblings,
            digest=digest,
            source_payloads=source_payloads or {},
            time_range=time_range,
        )
        outcome = await self._call_with_retry(tool, arguments, subtask=subtask)
        slim = self.context.slim_tool_result(tool, outcome.payload) if outcome.ok else None

        if outcome.ok and slim is not None:
            result = SubtaskResult(
                subtask_seq=subtask.seq,
                status="DONE",
                data_digest=slim.digest,
                artifacts=self._artifacts_of(tool, outcome.payload),
                tool=tool,
                attempts=outcome.attempts,
                degraded=outcome.degraded,
                error=None,
            )
        elif tool == "chart-render":
            # 图表失败 → Markdown 表格兜底（PRD 4.2：表格兜底，不阻塞）
            fallback = self._chart_table_fallback(subtask, source_payloads or {})
            result = SubtaskResult(
                subtask_seq=subtask.seq,
                status="DONE" if fallback else "FAILED",
                data_digest=fallback or "",
                artifacts=[],
                tool=tool,
                attempts=outcome.attempts,
                degraded=True,
                error=None
                if fallback
                else {"code": outcome.error_code or "RENDER_FAILED", "message": outcome.error_message or ""},
            )
        else:
            result = SubtaskResult(
                subtask_seq=subtask.seq,
                status="FAILED",
                data_digest="",
                artifacts=[],
                tool=tool,
                attempts=outcome.attempts,
                degraded=outcome.degraded,
                error={
                    "code": outcome.error_code or "TOOL_ERROR",
                    "message": (outcome.error_message or "")[:300],
                },
            )

        return ExecutorRun(
            execution=SubtaskExecution(
                subtask_seq=subtask.seq,
                name=subtask.name,
                tool=tool,
                status="DONE" if result.status == "DONE" else "FAILED",
                result=result,
                outcome=outcome,
                loop_count=outcome.attempts,
                degraded=outcome.degraded,
            ),
            payload=outcome.payload,
        )

    # ------------------------------------------------------------------
    # 参数构造
    # ------------------------------------------------------------------
    async def _build_arguments(
        self,
        subtask: SubtaskPlan,
        tool: str,
        *,
        siblings: list[str],
        digest: str,
        source_payloads: dict[int, dict[str, Any]],
        time_range: Any,
    ) -> dict[str, Any]:
        if tool == "sql-query":
            return await self._build_sql_arguments(subtask, siblings, digest, time_range)
        if tool == "chart-render":
            return self._build_chart_arguments(subtask, source_payloads)
        if tool == "kb-search":
            return {
                "query": subtask.description[: w2.KB_MAX_QUERY_LEN],
                "top_k": w2.KB_DEFAULT_TOP_K,
                "tenant": w2.KB_TENANT,
            }
        if tool == "notify":
            return {
                "channel": "feishu",
                "title": subtask.name[:50],
                "content": (subtask.description or subtask.name)[: w2.NOTIFY_MAX_CONTENT_LEN],
                "receivers": ["ops_group"],
            }
        raise ExecutorError(McpErrorCode.TOOL_NOT_FOUND.value, f"未知工具 {tool}")

    async def _safe_schema_summary(self) -> str:
        """任务 A：拉取真实表结构摘要；任何异常都降级为空串（Prompt 不注入该段）。"""
        try:
            from app.tools.sql_schema import get_mall_schema_summary

            return await get_mall_schema_summary() or ""
        except Exception as exc:  # noqa: BLE001 - 结构不可得绝不阻塞执行
            logger.warning("executor_schema_summary_failed", extra={"error": str(exc)[:200]})
            return ""

    async def _build_sql_arguments(
        self, subtask: SubtaskPlan, siblings: list[str], digest: str, time_range: Any
    ) -> dict[str, Any]:
        text = f"{subtask.description}\n{subtask.acceptance}"
        # 情况 1：描述里已带 SQL，直接用（仍会经 AST 校验）
        if "select" in text.lower():
            start = text.lower().find("select")
            candidate = text[start:].strip().strip("`")
            return {"sql": candidate, "max_rows": 200}

        # 情况 2：让 LLM 按其工具 schema 生成参数（任务 A：注入真实表结构，消灭列名盲猜）
        payload = ExecutorInput(
            subtask=subtask,
            context_digest=digest,
            sibling_summaries=siblings,
            tools=self.tools,
            sql_schema=await self._safe_schema_summary(),
        )
        messages = build_executor_messages(payload)
        try:
            result = await complete_structured(
                self.llm, messages, ToolCallRequest, max_repair=1, use_fallback_once=False
            )
            request: ToolCallRequest = result.value
            if request.arguments.get("sql"):
                return {
                    "sql": str(request.arguments["sql"]),
                    "max_rows": int(request.arguments.get("max_rows", 200)),
                }
        except Exception as exc:  # noqa: BLE001 - 参数生成失败走确定性兜底
            logger.warning("executor_sql_llm_failed", extra={"error": str(exc)[:200]})

        # 情况 3：确定性兜底模板（对齐 PRD 11.1 orders 表结构），保证 demo 可跑
        window = as_time_range(time_range)
        return {"sql": _default_gmv_sql(window.start, window.end), "max_rows": 200}

    def _build_chart_arguments(
        self, subtask: SubtaskPlan, source_payloads: dict[int, dict[str, Any]]
    ) -> dict[str, Any]:
        """从上游 sql 结果的预览行构造图表数据（禁止编造：无数据则报错）。"""

        def _as_number(value: Any) -> float | int | None:
            # 生产链路上 MCP JSON 层会把 Decimal/数值序列化成字符串（实测 digest 里
            # "3888138.87"/"3" 全带引号），数值列判定必须同时认数字与数字字符串。
            if isinstance(value, bool):
                return None
            if isinstance(value, (int, float, Decimal)):
                return value
            if isinstance(value, str):
                text = value.strip().replace(",", "")
                if not text:
                    return None
                try:
                    return float(text)
                except ValueError:
                    return None
            return None

        rows: list[dict[str, Any]] = []
        for seq in sorted(source_payloads):
            payload = source_payloads[seq]
            columns = [str(c) for c in payload.get("columns") or []]
            preview = payload.get("rows") or []
            if not columns or not preview:
                continue
            # 任务 B 收尾：MySQL SUM/AVG 经 aiomysql 返回 decimal.Decimal，
            # 只认 int/float 会把聚合列误判为非数值 → 误报「无上游数据可绘图」。
            numeric_fields = [
                column
                for column in columns[1:]
                if _as_number(preview[0][columns.index(column)]) is not None
            ]
            if not numeric_fields:
                continue
            rows = [
                {columns[0]: row[0], **{f: _as_number(row[columns.index(f)]) for f in numeric_fields}}
                for row in preview[: w2.CHART_MAX_ROWS]
            ]
            break

        if not rows:
            raise ExecutorError(
                McpErrorCode.CHART_DATA_INVALID.value,
                f"子任务 #{subtask.seq} 无上游数据可绘图，拒绝编造数据",
            )

        x_field = str(next(iter(rows[0].keys())))
        y_fields = [key for key in rows[0] if key != x_field][:3]
        chart_type = (
            "pie"
            if any(word in subtask.name for word in ("占比", "饼"))
            else ("line" if any(word in subtask.name for word in ("趋势", "走势")) else "bar")
        )
        return {
            "chart_type": chart_type,
            "title": subtask.name[:60],
            "x_field": x_field,
            "y_fields": y_fields,
            "data": rows,
            "size": "m",
        }

    # ------------------------------------------------------------------
    # 调用 + 重试 / 降级
    # ------------------------------------------------------------------
    # v2.1 工具错误脱敏：喂给 LLM 的 Observation 只给模型能看懂的业务提示，
    # 不给原始 str(e)——底层堆栈/乱码只会把模型带偏；人类排查看 trace 原文即可。
    _CODE_HINTS: dict[str, str] = {
        McpErrorCode.MCP_TIMEOUT.value: "[系统提示] 外部工具调用超时，请提炼更短的关键词或缩小时间范围。",
        McpErrorCode.SQL_TIMEOUT.value: "[系统提示] 数据库查询超时，请缩小时间范围或降低聚合粒度。",
        McpErrorCode.KB_TIMEOUT.value: "[系统提示] 外部知识库检索超时，请尝试提炼更短的关键词。",
        McpErrorCode.SQL_SYNTAX.value: "[系统提示] 之前的 SQL 语法有误，请检查表名或字段名，仅使用白名单内的表。",
        McpErrorCode.SQL_TOO_LARGE.value: "[系统提示] 查询数据量过大，请缩小时间范围或减少返回行数。",
        McpErrorCode.DB_UNAVAILABLE.value: "[系统提示] 数据库暂时不可用，请稍后重试或改用其他数据来源。",
        McpErrorCode.TOOL_CIRCUIT_OPEN.value: "[系统提示] 该工具连续失败已熔断，请改用其他方式完成本子任务。",
    }
    _ERROR_MAX_CHARS = 200

    # 任务 A：SQL 错误回喂列名白名单。只从原文提取**结构化标识符模式**中的表/列名
    # （\w 与点号，天然不含引号/空格/手机号等 PII），其余原文（堆栈/上下文）一律不进 Prompt。
    _SQL_IDENT_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
        ("未知列", re.compile(r"unknown column '([\w.]+)'", re.IGNORECASE)),
        ("未知列", re.compile(r"no such column:?\s*([\w.]+)", re.IGNORECASE)),
        ("未知表", re.compile(r"unknown table '([\w.]+)'", re.IGNORECASE)),
        ("未知表", re.compile(r"no such table:?\s*([\w.]+)", re.IGNORECASE)),
        ("未知表", re.compile(r"table '([\w.]+)' doesn't exist", re.IGNORECASE)),
    )

    @classmethod
    def _extract_sql_idents(cls, raw_text: str) -> list[str]:
        """从 SQL 错误原文提取安全的标识符（列名/表名），按原文出现顺序去重，最多 5 个。"""
        hits: list[tuple[int, str]] = []
        for _, pattern in cls._SQL_IDENT_PATTERNS:
            for match in pattern.finditer(raw_text):
                ident = match.group(1)
                if ident:
                    hits.append((match.start(), ident))
        found: list[str] = []
        for _, ident in sorted(hits, key=lambda item: item[0]):
            if ident not in found:
                found.append(ident)
        return found[:5]

    @classmethod
    def sanitize_tool_error(cls, error: Any) -> str:
        """把工具/SQL 异常转换为简短的业务提示（≤200 字符，绝不携带堆栈）。

        匹配优先级：错误码（ToolInvocationError 携带 PRD 错误码）→ 异常类型名 /
        消息关键词（超时、连接拒绝、语法错）→ 通用兜底（只留错误码，不留原文）。

        任务 A 增强：SQL_SYNTAX 且原文命中「Unknown column / no such table」等
        结构化模式时，把捕获的**标识符**（列名/表名）带进提示——配合 Prompt 注入的
        真实表结构，SQL 修复循环不再是盲修；原文其余部分（含潜在 PII/堆栈）仍然丢弃。
        """
        code = str(getattr(error, "code", "") or "")
        hint = cls._CODE_HINTS.get(code)
        raw_text = " ".join(str(error).split())
        if hint is None:
            type_name = type(error).__name__.lower()
            text = raw_text.lower()[:500]
            if "timeout" in type_name or "timeout" in text or "timed out" in text:
                hint = cls._CODE_HINTS[McpErrorCode.MCP_TIMEOUT.value]
            elif "syntax" in type_name or "syntax" in text:
                hint = cls._CODE_HINTS[McpErrorCode.SQL_SYNTAX.value]
            elif "connection" in text and ("refused" in text or "reset" in text or "closed" in text):
                hint = cls._CODE_HINTS[McpErrorCode.DB_UNAVAILABLE.value]
            else:
                hint = f"[系统提示] 工具调用失败（错误码: {code or 'UNKNOWN'}），请修正参数后重试。"
        if code == McpErrorCode.SQL_SYNTAX.value or "SQL 语法有误" in hint:
            idents = cls._extract_sql_idents(raw_text)
            if idents:
                ident_text = "、".join(f"'{ident}'" for ident in idents)
                hint = (
                    f"[系统提示] 之前的 SQL 语法有误：数据库提示引用了不存在的标识符 {ident_text}。"
                    "请对照任务上下文中的真实表结构修正列名/表名，仅使用白名单表。"
                )
        if code == McpErrorCode.MCP_TIMEOUT.value:
            # 任务 16：超时提示带上该工具的实际预算（per-tool 预算来自 _tool_budget），
            # 让"哪档超时杀了调用"在产物里可直接可见。
            budget_match = re.search(r"超过\s*([\d.]+)s\s*强制超时", raw_text)
            if budget_match:
                hint = f"{hint}（本工具预算 {budget_match.group(1)}s）"
        return " ".join(hint.split())[: cls._ERROR_MAX_CHARS]

    async def _call_with_retry(self, tool: str, arguments: dict[str, Any], *, subtask: SubtaskPlan) -> ToolCallOutcome:
        retry_limit, backoff = RETRY_MATRIX.get(tool, (1, [1.0]))
        attempts = 0
        loop_count = 0
        degraded = False
        last_code: str | None = None
        last_message: str | None = None
        current_args = dict(arguments)
        rewrite_count = 0
        started = time.perf_counter()

        while True:
            # 加固任务 2：熔断中快速失败，不再打工具（降级矩阵照常生效，如图表表格兜底）
            if self.circuit is not None and await self.circuit.is_open(tool):
                return ToolCallOutcome(
                    tool=tool,
                    ok=False,
                    payload={},
                    error_code=McpErrorCode.TOOL_CIRCUIT_OPEN.value,
                    error_message=f"工具 {tool} 连续失败已熔断，冷却 {self.circuit.cooldown}s 后自动恢复",
                    attempts=attempts,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    degraded=degraded,
                )
            if loop_count >= self.loop_limit:
                raise ExecutorError(
                    McpErrorCode.TOOL_LOOP_LIMIT.value,
                    f"子任务 #{subtask.seq} 工具调用超过 {self.loop_limit} 次，强制中断",
                )
            loop_count += 1
            attempts += 1
            tool_exc: ToolInvocationError | MCPTransportError | None = None
            tool_budget = self._tool_budget(tool)
            try:
                # 加固任务 2 → 任务 16：外层强制超时（asyncio.wait_for），per-tool 预算，
                # 超时转 MCP_TIMEOUT 走可重试矩阵。注意不能在 except TimeoutError 处理器里
                # 直接 raise 新异常——3.13 的 wait_for 内部是 asyncio.timeout 上下文，
                # 会与取消簿记冲突，所以先捕获成 tool_exc，在 try 之外统一走重试/降级矩阵。
                payload = await asyncio.wait_for(
                    self.pool.call(tool, current_args), timeout=tool_budget
                )
            except asyncio.TimeoutError:
                tool_exc = ToolInvocationError(
                    McpErrorCode.MCP_TIMEOUT.value,
                    f"工具 {tool} 调用超过 {tool_budget:g}s 强制超时",
                )
            except (ToolInvocationError, MCPTransportError) as exc:
                tool_exc = exc

            if tool_exc is None:
                if self.circuit is not None:
                    await self.circuit.record_success(tool)
                return ToolCallOutcome(
                    tool=tool,
                    ok=True,
                    payload=payload,
                    attempts=attempts,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    degraded=degraded,
                )

            code = getattr(tool_exc, "code", McpErrorCode.MCP_PROTOCOL_ERROR.value)
            # v2.1：进入重试矩阵 / 回喂 LLM 的错误文案一律脱敏，原始细节只留在 trace
            last_code, last_message = code, self.sanitize_tool_error(tool_exc)
            if self.circuit is not None:
                await self.circuit.record_failure(tool)
            logger.warning(
                "executor_tool_failed",
                extra={
                    "tool": tool,
                    "seq": subtask.seq,
                    "code": code,
                    "attempt": attempts,
                    "rewrite": rewrite_count,
                },
            )

            # 1) 确定性错误：不重试，把错误作为 Observation 回喂 LLM 改写 SQL（最多 3 次）
            if code in DETERMINISTIC_ERRORS:
                if tool == "sql-query" and rewrite_count < SQL_REWRITE_LIMIT:
                    rewrite_count += 1
                    degraded = True
                    current_args = await self._regenerate_sql(
                        current_args,
                        error_code=code,
                        error_message=self.sanitize_tool_error(tool_exc),
                        subtask=subtask,
                    )
                    continue
                break

            # 2) 可重试错误：按矩阵退避
            if code in RETRYABLE_ERRORS and attempts <= retry_limit:
                delay = backoff[min(attempts - 1, len(backoff) - 1)]
                if self.backoff_enabled and delay > 0:
                    await self.sleeper(delay)
                if tool == "sql-query" and attempts == retry_limit:
                    # 重试穷尽前的最后一步：缩小时间窗再试一次
                    current_args = self._narrow_time_window(current_args)
                    degraded = True
                continue

            # 3) 其余：按矩阵再试一轮后放弃（图表可切类型降级）
            if attempts <= retry_limit:
                delay = backoff[min(attempts - 1, len(backoff) - 1)]
                if self.backoff_enabled and delay > 0:
                    await self.sleeper(delay)
                if tool == "chart-render" and current_args.get("chart_type") == "bar":
                    current_args = {**current_args, "chart_type": "line"}
                    degraded = True
                continue
            break

        return ToolCallOutcome(
            tool=tool,
            ok=False,
            payload={},
            error_code=last_code,
            error_message=last_message,
            attempts=attempts,
            latency_ms=int((time.perf_counter() - started) * 1000),
            degraded=degraded,
        )

    async def _regenerate_sql(
        self,
        arguments: dict[str, Any],
        *,
        error_code: str,
        error_message: str,
        subtask: SubtaskPlan,
    ) -> dict[str, Any]:
        """SQL 确定性拒绝后的自反思重写（加固任务 2）。

        第一优先：把工具错误作为 Observation 追加进 Prompt，让 LLM 重新生成 SQL；
        LLM 不可用 / 产出非法时回退到原有的确定性缩窗（``_rewrite_sql``），
        保留既有降级能力不回退。重试预算由调用方 ``rewrite_count < SQL_REWRITE_LIMIT`` 控制。
        """
        regenerated = await self._llm_rewrite_sql(arguments, error_code, error_message, subtask)
        if regenerated is not None:
            logger.info(
                "executor_sql_llm_rewrite",
                extra={"seq": subtask.seq, "error_code": error_code},
            )
            return regenerated
        return self._rewrite_sql(arguments, error_code)

    async def _llm_rewrite_sql(
        self,
        arguments: dict[str, Any],
        error_code: str,
        error_message: str,
        subtask: SubtaskPlan,
    ) -> dict[str, Any] | None:
        """把 SQL 错误回喂 LLM 重新生成；任何失败都返回 None（交回退路径），绝不抛出。"""
        previous_sql = str(arguments.get("sql") or "")
        # 任务 A：修复循环同样注入真实表结构——修复模型拿到 1054 列名 + 全量列清单，不再是盲修
        schema_summary = await self._safe_schema_summary()
        schema_block = (
            f"数据库真实表结构（唯一可信列名来源，仅允许以下表与列）：\n{schema_summary}\n\n"
            if schema_summary
            else ""
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 SQL 修复专家。根据工具返回的错误信息修正 SQL：只修错误，不改查询语义，"
                    "只允许只读 SELECT。必须输出合法 JSON：{\"sql\": \"...\", \"max_rows\": 200}。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"子任务：#{subtask.seq} {subtask.name}\n{subtask.description}\n\n"
                    f"上次生成的 SQL：\n{previous_sql}\n\n"
                    f"{schema_block}"
                    f"工具返回的错误（Observation）：\n[{error_code}] {error_message}\n\n"
                    "请根据该错误重新生成可执行的 SQL，严格输出合法 JSON。"
                ),
            },
        ]
        try:
            result = await complete_structured(
                self.llm, messages, SqlRewriteRequest, max_repair=1, use_fallback_once=False
            )
            rewrite: SqlRewriteRequest = result.value
        except Exception as exc:  # noqa: BLE001 - LLM 任何失败都走确定性回退
            logger.warning("executor_sql_llm_rewrite_failed", extra={"error": str(exc)[:200]})
            return None

        sql = str(rewrite.sql or "").strip().strip("`")
        if not sql or "select" not in sql.lower():
            return None
        try:
            sqlglot.parse_one(sql, read="mysql")  # AST 预校验：非法直接交回退路径
        except Exception:  # noqa: BLE001
            logger.warning("executor_sql_llm_rewrite_invalid", extra={"seq": subtask.seq})
            return None
        return {"sql": sql, "max_rows": max(1, int(rewrite.max_rows))}

    def _rewrite_sql(self, arguments: dict[str, Any], error_code: str) -> dict[str, Any]:
        """确定性 SQL 拒绝后的降级：先缩窗，再降 max_rows（不改语义、不猜数据）。"""
        rewritten = self._narrow_time_window(arguments)
        if rewritten["sql"] == arguments["sql"]:
            rewritten = {**rewritten, "max_rows": min(int(arguments.get("max_rows", 200)), 50)}
        return rewritten

    @staticmethod
    def _narrow_time_window(arguments: dict[str, Any]) -> dict[str, Any]:
        """把 BETWEEN 时间窗对半收窄（PRD 4.2 的"缩小时间窗"降级路径）。"""
        sql = str(arguments.get("sql") or "")
        try:
            statement = sqlglot.parse_one(sql, read="mysql")
        except Exception:  # noqa: BLE001
            return arguments
        if not isinstance(statement, exp.Select):
            return arguments

        narrowed = False
        for between in statement.find_all(exp.Between):
            low, high = between.args.get("low"), between.args.get("high")
            try:
                start = date.fromisoformat(str(low.this))
                end = date.fromisoformat(str(high.this))
            except (AttributeError, TypeError, ValueError):
                continue
            span = (end - start).days
            if span < 1:
                continue
            between.set("low", exp.Literal.string((end - timedelta(days=max(span // 2, 0))).isoformat()))
            narrowed = True

        if not narrowed:
            return arguments
        return {**arguments, "sql": statement.sql(dialect="mysql")}

    # ------------------------------------------------------------------
    # 结果加工
    # ------------------------------------------------------------------
    @staticmethod
    def _artifacts_of(tool: str, payload: dict[str, Any]) -> list[ArtifactRef]:
        if tool != "chart-render":
            return []
        chart_id = payload.get("chart_id")
        if not chart_id:
            return []
        return [
            ArtifactRef(
                type="chart",
                ref=str(chart_id),
                url=payload.get("image_url"),
                title=payload.get("title") or "chart",
            )
        ]

    @staticmethod
    def _compose_analysis(subtask: SubtaskPlan, siblings: list[str]) -> str:
        """无工具子任务：把已有证据拼成结构化结论（不生成新数据，避免臆造）。"""
        if not siblings:
            return f"【{subtask.name}】缺少上游数据，无法形成结论（如实标注）。"
        body = "\n".join(f"- {item}" for item in siblings[:6])
        return f"【{subtask.name}】\n依据上游结果汇总：\n{body}"

    @staticmethod
    def _chart_table_fallback(subtask: SubtaskPlan, source_payloads: dict[int, dict[str, Any]]) -> str:
        for seq in sorted(source_payloads):
            payload = source_payloads[seq]
            columns = [str(c) for c in payload.get("columns") or []]
            preview = payload.get("rows") or []
            if not columns or not preview:
                continue
            header = "| " + " | ".join(columns) + " |"
            divider = "| " + " | ".join("---" for _ in columns) + " |"
            body = "\n".join("| " + " | ".join(str(v) for v in row) + " |" for row in preview[:20])
            return f"【{subtask.name}】图表渲染失败，已降级为 Markdown 表格：\n{header}\n{divider}\n{body}"
        return ""

    # ------------------------------------------------------------------
    # 整批执行（一个 executor 节点内）
    # ------------------------------------------------------------------
    async def run_pass(self, state: TaskState) -> dict[str, Any]:
        plan = plan_of(state)
        existing = results_of(state)
        done = done_seqs(existing)
        failed = failed_seqs(existing)
        skip = blocked_seqs(plan, done, failed)

        new_results: dict[str, SubtaskResult] = {}
        payloads: dict[int, dict[str, Any]] = {}
        artifacts: list[ArtifactRef] = []
        tool_messages: list[dict[str, Any]] = []

        ordered = sort_by_order([item for item in plan if item.tool_name != "notify"])
        for subtask in ordered:
            if subtask.seq in done:
                continue
            if subtask.seq in skip:
                new_results[str(subtask.seq)] = SubtaskResult(
                    subtask_seq=subtask.seq,
                    status="SKIPPED",
                    data_digest="上游子任务失败，已跳过",
                )
                continue

            siblings = [r.data_digest[:600] for r in existing.values() if r.status == "DONE"]
            siblings += [r.data_digest[:600] for r in new_results.values() if r.status == "DONE"]

            try:
                # G7：把当前子任务序号绑进上下文，工具/LLM 轨迹行会带上 subtask_seq，
                # 收尾落库后由 TaskService._finalize 回填 subtask_id（执行期计划还没落库，没有 id）
                subtask_token = bind_subtask(subtask.seq)
                try:
                    run = await self.run_subtask(
                        subtask,
                        siblings=siblings,
                        digest=str(state.get("context_digest") or ""),
                        source_payloads=payloads,
                        time_range=state.get("time_range"),
                    )
                finally:
                    unbind_subtask(subtask_token)
            except ExecutorError as exc:
                new_results[str(subtask.seq)] = SubtaskResult(
                    subtask_seq=subtask.seq,
                    status="FAILED",
                    error={"code": exc.code, "message": str(exc)[:300]},
                    tool=subtask.tool_name,
                )
                continue
            except TokenBudgetExceededError:
                # 任务 9：单任务 Token 预算熔断必须穿透执行图直接中断，
                # 不能被子任务级兜底吞掉后继续跑后续子任务
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("executor_subtask_crashed", extra={"seq": subtask.seq})
                new_results[str(subtask.seq)] = SubtaskResult(
                    subtask_seq=subtask.seq,
                    status="FAILED",
                    error={"code": "EXECUTOR_ERROR", "message": str(exc)[:300]},
                    tool=subtask.tool_name,
                )
                continue

            result = run.execution.result
            if result is not None:
                new_results[str(subtask.seq)] = result
                if run.payload:
                    payloads[subtask.seq] = run.payload
                artifacts.extend(result.artifacts)
                if run.execution.outcome is not None:
                    tool_messages.append(
                        {
                            "role": "tool",
                            "name": subtask.tool_name,
                            "content": result.data_digest[:1200],
                        }
                    )

        digest_text = self._compose_digest(plan, {**existing, **new_results})
        return {
            "subtask_results": new_results,
            "context_digest": digest_text,
            "artifacts": artifacts,
            "messages": tool_messages,
            "token_ledger": ledger(AGENT_NAME, estimate_json_tokens({"digest": digest_text}), 0),
            "status": "RUNNING",
        }

    @staticmethod
    def _compose_digest(plan: list[SubtaskPlan], results: dict[str, SubtaskResult]) -> str:
        lines = []
        for subtask in analysis_subtasks(plan):
            result = results.get(str(subtask.seq))
            if result is None:
                lines.append(f"- #{subtask.seq} {subtask.name}: 未执行")
            else:
                lines.append(f"- #{subtask.seq} {subtask.name} [{result.status}]: {result.data_digest[:400]}")
        return "\n".join(lines)[:8000]


def _default_gmv_sql(start: date, end: date) -> str:
    return (
        "SELECT category_id, SUM(amount) AS gmv, COUNT(*) AS order_cnt "
        "FROM orders "
        f"WHERE order_date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}' "
        "GROUP BY category_id ORDER BY gmv DESC"
    )


def make_executor_node(executor: Executor) -> Callable[[TaskState], Any]:
    async def executor_node(state: TaskState) -> dict[str, Any]:
        try:
            return await executor.run_pass(state)
        except TokenBudgetExceededError:
            raise  # 任务 9：预算熔断向上抛给图级处理，落 FAILED(TOKEN_BUDGET_EXCEEDED)
        except Exception as exc:  # noqa: BLE001
            logger.exception("executor_pass_failed")
            return {
                "error": node_error("EXECUTOR_ERROR", str(exc)[:500], AGENT_NAME),
                "status": "FAILED",
            }

    executor_node.__name__ = "executor_node"
    return executor_node


__all__ = [
    "AGENT_NAME",
    "RETRY_MATRIX",
    "SQL_REWRITE_LIMIT",
    "ExecutorError",
    "ExecutorRun",
    "Executor",
    "SqlRewriteRequest",
    "make_executor_node",
]
