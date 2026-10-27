"""Planner 节点（PRD FR-002 / FR-003）：目标 → 子任务 DAG，支持首次规划与重规划。

关键实现点：
- **结构化输出**：``PlanOutput`` Pydantic 校验 + JSON 修复重试（最多 2 次）；
- **DAG 校验**：拓扑排序无环、依赖存在、数量 3~15（重规划允许更少，仅重排受影响分支）；
- **重规划模式**：同一节点、不同提示模板（few-shot 第二例），保留已完成子任务结果；
- **temperature=0.2**（PRD 8.2），失败切备用模型（由 LLMClient + structured 负责）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from app.agents.common import as_time_range, ledger, node_error, results_of
from app.agents.llm import LLMClient
from app.agents.prompts import build_planner_messages
from app.agents.structured import StructuredOutputError, complete_structured
from app.core.logging import get_logger
from app.core.w2_config import w2
from app.schemas.agent_io import PlannerInput, ReplanContext, ToolDef
from app.schemas.context import ContextBuildResult
from app.schemas.graph_state import PlanOutput, SubtaskPlan, TaskState
from app.services.context_service import ContextService
from app.utils.dag import validate_plan

logger = get_logger(__name__)

AGENT_NAME = "planner"
PLANNER_TEMPERATURE = 0.2  # PRD 8.2 硬性要求


class PlannerError(RuntimeError):
    def __init__(self, code: str, message: str, *, raw: str = "", attempts: int = 0) -> None:
        self.code = code
        self.raw = raw
        self.attempts = attempts
        super().__init__(message)


@dataclass(slots=True)
class PlannerRun:
    plan: PlanOutput
    context: ContextBuildResult
    input_tokens: int = 0
    output_tokens: int = 0
    attempts: int = 1
    repair_errors: list[str] = field(default_factory=list)
    used_fallback: bool = False

    @property
    def subtasks(self) -> list[SubtaskPlan]:
        return self.plan.subtasks


async def _safe_schema_summary() -> str:
    """真实表结构摘要：任何异常都降级为空串（Prompt 不注入该段，行为回退到升级前）。"""
    try:
        from app.tools.sql_schema import get_mall_schema_summary

        return await get_mall_schema_summary() or ""
    except Exception as exc:  # noqa: BLE001 - 结构不可得绝不阻塞规划
        logger.warning("planner_schema_summary_failed", extra={"error": str(exc)[:200]})
        return ""


async def run_planner(
    llm: LLMClient,
    context: ContextService,
    payload: PlannerInput,
    *,
    history: list[dict[str, Any]] | None = None,
    max_validation_rounds: int = 2,
) -> PlannerRun:
    """规划主流程：LLM 结构化输出 + DAG 校验 + 校验失败回喂重排。"""
    messages = build_planner_messages(payload, sql_schema=await _safe_schema_summary())
    system_text = str(messages[0]["content"])
    user_text = str(messages[1]["content"])

    build = context.plan_context(
        "planner",
        {"system": system_text, "task": user_text},
        messages=history or [],
        reserved_output=2500,  # PRD 9.1：Planner 输出预留 2.5K
    )
    if build.history_digest:
        messages[1] = {"role": "user", "content": user_text + "\n\n[历史上下文摘要]\n" + build.history_digest}

    min_subtasks = 1 if payload.is_replan else w2.PLAN_MIN_SUBTASKS
    total_in = 0
    total_out = 0
    attempts = 0
    repair_errors: list[str] = []
    used_fallback = False

    for round_index in range(max_validation_rounds):
        try:
            result = await complete_structured(
                llm,
                messages,
                PlanOutput,
                temperature=PLANNER_TEMPERATURE,
                max_tokens=w2.LLM_MAX_TOKENS,
                use_fallback_once=True,
            )
        except StructuredOutputError as exc:
            raise PlannerError(
                "PLAN_INVALID",
                f"规划输出结构化失败（{exc.attempts} 次）：{exc}",
                raw=exc.raw,
                attempts=exc.attempts,
            ) from exc

        attempts += result.attempts
        total_in += result.input_tokens
        total_out += result.output_tokens
        used_fallback = used_fallback or result.used_fallback
        repair_errors.extend(result.repair_errors)

        plan: PlanOutput = result.value
        report = validate_plan(plan.subtasks, min_subtasks=min_subtasks, max_subtasks=w2.PLAN_MAX_SUBTASKS)
        if report.ok:
            plan.replan_count = max(0, min(plan.replan_count, w2.REPLAN_MAX_COUNT))
            return PlannerRun(
                plan=plan,
                context=build,
                input_tokens=total_in,
                output_tokens=total_out,
                attempts=attempts,
                repair_errors=repair_errors,
                used_fallback=used_fallback,
            )

        error_text = report.error_text
        repair_errors.append(error_text)
        logger.warning(
            "plan_dag_invalid",
            extra={"round": round_index + 1, "error": error_text, "subtasks": len(plan.subtasks)},
        )
        if round_index == max_validation_rounds - 1:
            break
        messages = [
            *messages,
            {"role": "assistant", "content": json.dumps(plan.model_dump(mode="json"), ensure_ascii=False)},
            {
                "role": "user",
                "content": (
                    "上面的 DAG 校验未通过，必须修复后重新输出完整 JSON：\n"
                    f"{error_text}\n"
                    f"要求：子任务数量 {min_subtasks}~{w2.PLAN_MAX_SUBTASKS}，depends_on 只能引用更小的 seq，禁止成环。"
                ),
            },
        ]

    raise PlannerError("PLAN_INVALID", f"DAG 校验连续失败：{repair_errors[-1] if repair_errors else 'unknown'}")


def _build_replan_context(state: TaskState) -> ReplanContext | None:
    raw = state.get("replan_context")
    if not raw:
        return None
    completed = {
        int(result.subtask_seq): result.data_digest[:300]
        for result in results_of(state).values()
        if result.status == "DONE"
    }
    return ReplanContext(
        cause=raw.get("cause", "tool_failed"),
        failed_seqs=[int(x) for x in raw.get("failed_seqs") or []],
        review_comment=str(raw.get("review_comment") or "")[:500],
        human_comment=str(raw.get("human_comment") or "")[:500],
        completed_summary=completed,
    )


def make_planner_node(
    llm: LLMClient,
    context: ContextService,
    *,
    tools: list[ToolDef] | None = None,
) -> Callable[[TaskState], Any]:
    """构造 LangGraph 节点：闭包捕获依赖，节点本身只读写 state。"""

    async def planner_node(state: TaskState) -> dict[str, Any]:
        replan_context = _build_replan_context(state)
        payload = PlannerInput(
            goal=str(state.get("goal") or ""),
            time_range=as_time_range(state.get("time_range")),
            memory_hits=list(state.get("memory_hits") or [])[:3],
            replan_context=replan_context,
            tools=tools or [],
        )

        try:
            run = await run_planner(llm, context, payload, history=list(state.get("messages") or []))
        except PlannerError as exc:
            logger.error("planner_failed", extra={"code": exc.code, "error": str(exc)[:300]})
            return {
                "error": node_error(exc.code, str(exc), AGENT_NAME),
                "status": "FAILED",
                "messages": [{"role": "assistant", "content": f"规划失败：{exc.code}"}],
            }

        replan_count = (state.get("replan_count") or 0) + 1 if replan_context else (state.get("replan_count") or 0)
        plan = run.plan
        plan.replan_count = min(replan_count, w2.REPLAN_MAX_COUNT)

        logger.info(
            "plan_generated",
            extra={
                "subtasks": len(plan.subtasks),
                "replan": replan_context is not None,
                "replan_count": plan.replan_count,
                "attempts": run.attempts,
                "compressed": run.context.compressed,
            },
        )

        return {
            "plan": plan.subtasks,
            "plan_assumptions": plan.assumptions,
            "replan_count": plan.replan_count,
            "replan_context": None,
            "token_ledger": ledger(
                AGENT_NAME,
                run.input_tokens,
                run.output_tokens,
                compress_before=run.context.before_tokens,
                compress_after=run.context.after_tokens,
                compressed=run.context.compressed,
            ),
            "context_meta": {
                "node": "planner",
                "budget": run.context.budget,
                "estimated": run.context.estimate.total,
                "compressed": run.context.compressed,
                "compress_layers": run.context.compress_layers,
                "before_tokens": run.context.before_tokens,
                "after_tokens": run.context.after_tokens,
                "kept_rounds": run.context.kept_rounds,
            },
            "messages": [
                {
                    "role": "assistant",
                    "content": (
                        f"规划完成：{len(plan.subtasks)} 个子任务，"
                        f"风险子任务 {sum(1 for s in plan.subtasks if str(s.risk_level).endswith('high'))} 个"
                    ),
                }
            ],
            "status": "PLANNING",
        }

    planner_node.__name__ = "planner_node"
    return planner_node


async def plan(llm: LLMClient, context: ContextService, payload: PlannerInput, **kwargs: Any) -> PlannerRun:
    """对外便捷入口（不带 state，供服务层 / 单测直接调用）。"""
    return await run_planner(llm, context, payload, **kwargs)


async def replan(
    llm: LLMClient,
    context: ContextService,
    payload: PlannerInput,
    **kwargs: Any,
) -> PlannerRun:
    """重规划入口：要求 payload 必须携带 replan_context。"""
    if payload.replan_context is None:
        raise PlannerError("REPLAN_INVALID", "重规划必须携带 replan_context")
    return await run_planner(llm, context, payload, **kwargs)


__all__ = [
    "AGENT_NAME",
    "PLANNER_TEMPERATURE",
    "PlannerError",
    "PlannerRun",
    "run_planner",
    "make_planner_node",
    "plan",
    "replan",
]
