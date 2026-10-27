#!/usr/bin/env python
"""W4 Demo：三条可复现的演示链路（离线可跑，不需要 MySQL / Redis / LLM Key）。

    python scripts/demo_three_flows.py             # 三条链路全跑
    python scripts/demo_three_flows.py --flow 1    # 只跑链路 1
    python scripts/demo_three_flows.py --live      # 打到真实服务（需先起 API）

三条链路
--------
1. **审批闭环**：提交高风险任务 → SSE 推送（首段收尾于 ``interrupt``）→ 审批待办落库
   → ``resume(APPROVED)`` 断点续跑 → SSE 第二段收尾于 ``complete`` → 完成 + 轨迹回放。
2. **驳回重规划**：提交高风险任务 → 审批驳回 → ``replan`` 重排计划 → 再次走到 notify
   仍无审批 → ``APPROVAL_MISSING`` 失败（``replan_count=1``），轨迹里能看到 replan 节点。
3. **取消冻结**：任务卡在审批点 → ``cancel`` → 状态 ``CANCELLED``、审批单过期、
   断点被清理（``pending_interrupts()==[]``），快照仍可回放、再 resume 会 409。

设计要点
--------
* **SSE 两段式**：审批挂起时流会以 ``interrupt`` 事件收尾（前端据此关连接、走审批），
  审批后再用 ``Last-Event-ID`` 续订以 ``complete`` 收尾。这正是 W3 前端的真实用法，
  脚本用同一套语义，不依赖"边跑边读"的时序运气。
* 事件总线是 Redis Stream，历史事件可从头重放（``last_id=0-0``），所以即使任务
  跑得比订阅还快，也能完整回放出推送序列。
* 不修改任何 W1/W2/W3 代码，全部通过公开 HTTP 契约驱动。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
for path in (str(PROJECT_ROOT), str(SCRIPT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import w4_common as W  # noqa: E402

# ---------------------------------------------------------------------------
# 演示素材
# ---------------------------------------------------------------------------
#: 命中 Mock Planner 的高危分支（含「推送」→ tool_hint=notify, risk_level=high）
GOAL_HIGH_RISK = "分析上周数码配件 GMV 下滑原因，并推送运营群通知"
#: 全部子任务 risk_level=normal，不进 human_gate
GOAL_NORMAL = "分析上周数码配件 GMV 下滑原因，并给出品类归因与图表"

STATUS_AWAITING = "AWAITING_APPROVAL"
TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}


# ---------------------------------------------------------------------------
# 记录器
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Step:
    """一条演示步骤：给人和给机器看的两种形态。"""

    name: str
    ok: bool
    detail: str = ""
    elapsed_ms: int = 0


@dataclass(slots=True)
class FlowResult:
    flow_id: str
    title: str
    goal: str
    task_id: int = 0
    passed: bool = False
    status: str = ""
    steps: list[Step] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    elapsed_ms: int = 0

    def step(self, name: str, ok: bool, detail: str = "", started: float | None = None) -> Step:
        item = Step(
            name=name,
            ok=bool(ok),
            detail=detail,
            elapsed_ms=int((time.perf_counter() - started) * 1000) if started else 0,
        )
        self.steps.append(item)
        mark = "PASS" if ok else "FAIL"
        print(f"    [{mark}] {name}{(' — ' + detail) if detail else ''}")
        return item

    @property
    def failed(self) -> list[Step]:
        return [item for item in self.steps if not item.ok]


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
async def wait_status(
    api: Any,
    task_id: int,
    targets: Sequence[str],
    *,
    timeout: float = 60.0,
    interval: float = 0.1,
) -> tuple[str, dict[str, Any]]:
    """轮询到目标状态（或终态）为止，返回 (status, detail)。"""
    wanted = set(targets) | TERMINAL_STATUSES
    started = time.perf_counter()
    detail: dict[str, Any] = {}
    while True:
        detail = await api.detail(task_id)
        status = str(detail.get("status"))
        if status in wanted:
            return status, detail
        if time.perf_counter() - started > timeout:
            return status, detail
        await asyncio.sleep(interval)


def sse_events(text: str) -> list[dict[str, Any]]:
    """解析 SSE 文本 → 事件列表（保留 id/event/data 三元组）。"""
    return W.parse_sse(text)


def sse_timeline(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """把事件压成「id / event / node / phase」时间轴，便于在报告里逐行展示。"""
    rows: list[dict[str, Any]] = []
    for event in events:
        data = event.get("data") or {}
        rows.append(
            {
                "id": str(event.get("id") or ""),
                "event": str(event.get("event") or ""),
                "node": str(data.get("node") or ""),
                "phase": str(data.get("phase") or ""),
                "status": str(data.get("status") or ""),
            }
        )
    return rows


def last_event_id(events: Sequence[dict[str, Any]]) -> str:
    for event in reversed(list(events)):
        if event.get("id"):
            return str(event["id"])
    return ""


def event_counts(events: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        key = str(event.get("event") or "?")
        counts[key] = counts.get(key, 0) + 1
    return counts


async def find_approval_id(api: Any, task_id: int) -> int | None:
    """在审批收件箱里找到该任务的审批单（演示里用于「带上 approval_id 续跑」）。

    收件箱条目形如 ``{"task_id": 34, "approval_id": 6, "status": "AWAITING_APPROVAL", ...}``，
    只有 ``AWAITING_APPROVAL`` 的任务会出现在这里——这正是"待办"的定义。
    """
    try:
        inbox = await api.approvals(size=50)
    except W.ApiError:
        return None
    for item in (inbox.get("items") or inbox.get("records") or []):
        if int(item.get("task_id") or 0) != int(task_id):
            continue
        for key in ("approval_id", "id"):
            value = int(item.get(key) or 0)
            if value:
                return value
    return None


async def approval_status(api: Any, approval_id: int | None) -> str:
    """审批单当前状态（PENDING / APPROVED / REJECTED / EXPIRED）。"""
    if not approval_id:
        return ""
    try:
        response = await api.client.get(f"/api/v1/approvals/{approval_id}", headers=api.headers)
        payload = W._unwrap(response)
    except W.ApiError:
        return ""
    return str(payload.get("status") or "")


# ---------------------------------------------------------------------------
# 链路 1：提交 → SSE → 审批 → 续跑 → 完成 + 轨迹回放
# ---------------------------------------------------------------------------
async def flow_1_approval(api: Any, *, timeout: float) -> FlowResult:
    result = FlowResult(flow_id="1", title="审批闭环：提交 → SSE → 审批 → 续跑 → 完成", goal=GOAL_HIGH_RISK)
    result.started_at = W.now_tag()
    started = time.perf_counter()

    # ---- 1) 提交（异步执行，立即返回 task_id）----
    t = time.perf_counter()
    submitted = await api.submit(GOAL_HIGH_RISK, sync=False)
    task_id = int(submitted.get("task_id") or submitted.get("id") or 0)
    result.task_id = task_id
    result.step("提交高风险任务", task_id > 0, f"task_id={task_id} status={submitted.get('status')}", t)

    # ---- 2) SSE 第一段：应从 0-0 重放，收尾于 interrupt ----
    t = time.perf_counter()
    status, detail = await wait_status(api, task_id, (STATUS_AWAITING,), timeout=timeout)
    result.evidence["awaiting_detail"] = {
        "status": status,
        "current_node": detail.get("current_node"),
        "risk_level": detail.get("risk_level"),
    }
    result.step("任务挂起在审批点", status == STATUS_AWAITING, f"status={status}", t)

    t = time.perf_counter()
    text1 = await api.stream_text(task_id)
    events1 = sse_events(text1)
    tail1 = str(events1[-1].get("event")) if events1 else ""
    result.evidence["sse_segment_1"] = {
        "frames": len(events1),
        "counts": event_counts(events1),
        "tail_event": tail1,
        "timeline": sse_timeline(events1)[:40],
    }
    result.step(
        "SSE 第一段推送（收尾于 interrupt）",
        tail1 == "interrupt",
        f"frames={len(events1)} tail={tail1 or '-'} 事件分布={event_counts(events1)}",
        t,
    )

    interrupt = await api.interrupt(task_id)
    high_risk = interrupt.get("high_risk") or interrupt.get("subtasks") or []
    result.evidence["interrupt"] = {
        "reason": interrupt.get("reason"),
        "approval_id": interrupt.get("approval_id"),
        "high_risk_count": len(high_risk) if isinstance(high_risk, list) else high_risk,
        "keys": sorted(interrupt.keys()),
    }
    result.step(
        "挂起上下文可查（含高危子任务预览）",
        bool(interrupt.get("reason") or interrupt.get("approval_id")),
        f"reason={str(interrupt.get('reason'))[:60]} approval_id={interrupt.get('approval_id')}",
    )

    # ---- 3) 审批待办落库 ----
    t = time.perf_counter()
    approval_id = await find_approval_id(api, task_id)
    result.evidence["approval_id"] = approval_id
    result.step("审批待办已进收件箱", approval_id is not None, f"approval_id={approval_id}", t)

    # ---- 4) 通过续跑：Command(resume=APPROVED)，human_gate 整节点重跑 ----
    t = time.perf_counter()
    resumed = await api.resume(
        task_id,
        decision="APPROVED",
        comment="Demo：数据口径已核对，同意推送",
        sync=True,
        approval_id=approval_id,
    )
    terminal = await W.wait_terminal(api, task_id, timeout=timeout, auto_approve=False)
    result.status = terminal.status
    ledger = terminal.detail.get("token_ledger") or {}
    result.evidence["resume_payload"] = {
        "resumed": resumed.get("resumed"),
        "status": resumed.get("status"),
    }
    result.evidence["terminal"] = {
        "status": terminal.status,
        "reason": terminal.terminal_reason,
        "error": terminal.detail.get("error"),
        "replan_count": terminal.detail.get("replan_count"),
        "notify_status": terminal.detail.get("notify_status"),
        "node_tokens": W.node_tokens(ledger),
        "total_tokens": W.total_tokens_of(ledger, int(terminal.detail.get("token_total") or 0)),
    }
    result.step(
        "审批通过 → 断点续跑 → 完成",
        terminal.status == "COMPLETED",
        f"status={terminal.status} tokens={result.evidence['terminal']['total_tokens']}",
        t,
    )

    # ---- 5) SSE 第二段：用 Last-Event-ID 续订，收尾于 complete ----
    t = time.perf_counter()
    text2 = await api.stream_text(task_id, last_event_id=last_event_id(events1))
    events2 = sse_events(text2)
    tail2 = str(events2[-1].get("event")) if events2 else ""
    result.evidence["sse_segment_2"] = {
        "frames": len(events2),
        "counts": event_counts(events2),
        "tail_event": tail2,
        "nodes": [row["node"] for row in sse_timeline(events2) if row["node"]],
    }
    result.step(
        "SSE 第二段续订（收尾于 complete）",
        tail2 == "complete",
        f"frames={len(events2)} tail={tail2 or '-'} 事件分布={event_counts(events2)}",
        t,
    )

    # ---- 6) 轨迹回放 ----
    t = time.perf_counter()
    trace = await api.trace(task_id)
    steps = trace.get("steps") or []
    checkpoints = trace.get("checkpoints") or []
    node_seq: list[str] = []
    for item in steps:
        node = str(item.get("node") or "")
        if node and (not node_seq or node_seq[-1] != node):
            node_seq.append(node)
    result.evidence["replay"] = {
        "total_steps": trace.get("total_steps") or len(steps),
        "checkpoints": len(checkpoints),
        "node_sequence": node_seq,
        "events": sorted({str(item.get("event")) for item in steps}),
        "trace_id": trace.get("trace_id"),
    }
    notify_seen = any(str(item.get("node") or "").startswith("notif") for item in steps)
    result.step(
        "轨迹回放（步骤 / 断点 / 节点序列）",
        len(steps) > 0 and len(checkpoints) > 0 and notify_seen,
        f"steps={len(steps)} checkpoints={len(checkpoints)} nodes={'→'.join(node_seq)}",
        t,
    )

    result.passed = result.status == "COMPLETED" and not result.failed
    result.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return result


# ---------------------------------------------------------------------------
# 链路 2：高风险 → 驳回 → replan 重规划
# ---------------------------------------------------------------------------
async def flow_2_reject_replan(api: Any, *, timeout: float) -> FlowResult:
    result = FlowResult(flow_id="2", title="驳回重规划：驳回 → replan → 无审批再拦截", goal=GOAL_HIGH_RISK)
    result.started_at = W.now_tag()
    started = time.perf_counter()

    t = time.perf_counter()
    submitted = await api.submit(GOAL_HIGH_RISK, sync=False)
    task_id = int(submitted.get("task_id") or submitted.get("id") or 0)
    result.task_id = task_id
    result.step("提交高风险任务", task_id > 0, f"task_id={task_id}", t)

    t = time.perf_counter()
    status, _ = await wait_status(api, task_id, (STATUS_AWAITING,), timeout=timeout)
    result.step("任务挂起在审批点", status == STATUS_AWAITING, f"status={status}", t)

    # ---- 驳回：decision=REJECTED → human_gate 产出驳回信号 → replan ----
    t = time.perf_counter()
    await api.resume(task_id, decision="REJECTED", comment="Demo：取数窗口与财务口径不一致，请重排", sync=False)
    terminal = await W.wait_terminal(api, task_id, timeout=timeout, auto_approve=False)
    result.status = terminal.status
    error = terminal.detail.get("error") or {}
    ledger = terminal.detail.get("token_ledger") or {}
    result.evidence["terminal"] = {
        "status": terminal.status,
        "reason": terminal.terminal_reason,
        "error": error,
        "replan_count": terminal.detail.get("replan_count"),
        "total_tokens": W.total_tokens_of(ledger, int(terminal.detail.get("token_total") or 0)),
    }
    result.step(
        "驳回后按预期失败（未放行发送）",
        terminal.status == "FAILED",
        f"status={terminal.status} code={error.get('code')} replan_count={terminal.detail.get('replan_count')}",
        t,
    )

    # ---- 轨迹里必须有 replan 节点，且计划被重排 ----
    t = time.perf_counter()
    trace = await api.trace(task_id)
    steps = trace.get("steps") or []
    node_seq: list[str] = []
    for item in steps:
        node = str(item.get("node") or "")
        if node and (not node_seq or node_seq[-1] != node):
            node_seq.append(node)
    replan_enters = [
        item for item in steps if str(item.get("node")) == "replan" and str(item.get("event")) == "enter"
    ]
    notify_steps = [
        item
        for item in steps
        if str(item.get("node") or "").startswith("notif") and str(item.get("event")) == "enter"
    ]
    result.evidence["replay"] = {
        "total_steps": trace.get("total_steps") or len(steps),
        "checkpoints": len(trace.get("checkpoints") or []),
        "node_sequence": node_seq,
        "replan_enters": len(replan_enters),
        "notify_enters": len(notify_steps),
    }
    result.step(
        "轨迹含 replan 节点（重排发生）",
        len(replan_enters) >= 1 and "replan" in node_seq,
        f"replan_enter={len(replan_enters)} notify_enter={len(notify_steps)} nodes={'→'.join(node_seq)}",
        t,
    )

    # ---- 重规划后仍需审批 → notify 返回 APPROVAL_MISSING ----
    code = str(error.get("code") or terminal.terminal_reason)
    result.step(
        "二次走到 notify 仍无审批 → APPROVAL_MISSING",
        code == "APPROVAL_MISSING" and int(terminal.detail.get("replan_count") or 0) >= 1,
        f"code={code} replan_count={terminal.detail.get('replan_count')}",
    )

    result.passed = result.status == "FAILED" and not result.failed
    result.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return result


# ---------------------------------------------------------------------------
# 链路 3：任务取消，冻结断点
# ---------------------------------------------------------------------------
async def flow_3_cancel(api: Any, *, timeout: float) -> FlowResult:
    result = FlowResult(flow_id="3", title="取消冻结：审批点取消 → 状态收口 → 断点清理", goal=GOAL_HIGH_RISK)
    result.started_at = W.now_tag()
    started = time.perf_counter()

    t = time.perf_counter()
    submitted = await api.submit(GOAL_HIGH_RISK, sync=False)
    task_id = int(submitted.get("task_id") or submitted.get("id") or 0)
    result.task_id = task_id
    result.step("提交高风险任务", task_id > 0, f"task_id={task_id}", t)

    t = time.perf_counter()
    status, _ = await wait_status(api, task_id, (STATUS_AWAITING,), timeout=timeout)
    result.step("任务挂起在审批点", status == STATUS_AWAITING, f"status={status}", t)

    # ---- 取消前的断点快照：要有 interrupt、有 next 节点 ----
    t = time.perf_counter()
    before = await api.snapshot(task_id)
    approval_id = await find_approval_id(api, task_id)
    result.evidence["approval_id"] = approval_id
    result.evidence["snapshot_before"] = {
        "status": before.get("status"),
        "next_nodes": before.get("next_nodes"),
        "interrupt_count": before.get("interrupt_count"),
        "step": before.get("step"),
        "plan_size": len((before.get("values") or {}).get("plan") or []),
        "approval_status": await approval_status(api, approval_id),
    }
    result.step(
        "取消前：断点存在且挂起 1 个 interrupt",
        int(before.get("interrupt_count") or 0) >= 1 and bool(before.get("next_nodes")),
        f"interrupt_count={before.get('interrupt_count')} next={before.get('next_nodes')} "
        f"step={before.get('step')} approval={result.evidence['snapshot_before']['approval_status'] or '-'}",
        t,
    )

    # ---- 取消 ----
    t = time.perf_counter()
    canceled = await api.cancel(task_id, reason="Demo：口径需重核，先终止任务")
    detail = await api.detail(task_id)
    result.status = str(detail.get("status"))
    result.evidence["cancel"] = {
        "response": canceled,
        "status": detail.get("status"),
        "error": detail.get("error"),
        "finished_at": detail.get("finished_at"),
    }
    result.step(
        "取消成功 → 状态 CANCELLED",
        str(detail.get("status")) == "CANCELLED",
        f"status={detail.get('status')} code={(detail.get('error') or {}).get('code')}",
        t,
    )

    # ---- 冻结断点：快照仍可回放，但挂起已被清理 ----
    t = time.perf_counter()
    after = await api.snapshot(task_id)
    result.evidence["snapshot_after"] = {
        "status": after.get("status"),
        "next_nodes": after.get("next_nodes"),
        "interrupt_count": after.get("interrupt_count"),
        "step": after.get("step"),
        "values_keys": sorted((after.get("values") or {}).keys())[:12],
        "plan_size": len((after.get("values") or {}).get("plan") or []),
        "messages_count": after.get("messages_count"),
    }
    frozen = (
        str(after.get("status")) == "CANCELLED"
        and int(after.get("interrupt_count") or 0) == 0
        and len((after.get("values") or {}).get("plan") or []) >= 1
    )
    result.step(
        "断点已冻结：状态/计划留存，挂起清空",
        frozen,
        f"status={after.get('status')} interrupt_count={after.get('interrupt_count')} "
        f"plan_size={len((after.get('values') or {}).get('plan') or [])}",
        t,
    )

    # ---- 再 resume 必须被拒（断点不可复用）----
    t = time.perf_counter()
    resume_error = ""
    try:
        await api.resume(task_id, decision="APPROVED", comment="Demo：取消后误操作", sync=False)
    except W.ApiError as exc:
        resume_error = str(exc)
    result.evidence["resume_after_cancel"] = resume_error
    result.step(
        "取消后再 resume 被拒绝",
        bool(resume_error),
        resume_error[:100] or "意外成功：取消态仍可恢复",
        t,
    )

    # ---- 审批单应过期，收件箱里不再待办 ----
    t = time.perf_counter()
    status_after = await approval_status(api, approval_id)
    approvals = await api.approvals(size=50)
    items = approvals.get("items") or approvals.get("records") or []
    still_pending = [item for item in items if int(item.get("task_id") or 0) == int(task_id)]
    result.evidence["approvals"] = {
        "approval_id": approval_id,
        "status_after_cancel": status_after,
        "still_in_inbox": len(still_pending),
        "inbox_total": len(items),
    }
    result.step(
        "审批单已置 EXPIRED 且退出待办列表",
        status_after == "EXPIRED" and len(still_pending) == 0,
        f"approval_id={approval_id} status={status_after or '-'} 仍在收件箱={len(still_pending)} 全局待办={len(items)}",
        t,
    )

    # ---- 轨迹仍可回放（冻结 ≠ 删除）----
    t = time.perf_counter()
    trace = await api.trace(task_id)
    steps = trace.get("steps") or []
    checkpoints = trace.get("checkpoints") or []
    node_seq: list[str] = []
    for item in steps:
        node = str(item.get("node") or "")
        if node and (not node_seq or node_seq[-1] != node):
            node_seq.append(node)
    result.evidence["replay"] = {
        "total_steps": trace.get("total_steps") or len(steps),
        "checkpoints": len(checkpoints),
        "node_sequence": node_seq,
    }
    result.step(
        "轨迹与断点时间轴仍可回放（冻结不等于删除）",
        len(steps) > 0 and len(checkpoints) >= 1,
        f"steps={len(steps)} checkpoints={len(checkpoints)} nodes={'→'.join(node_seq)}",
        t,
    )

    result.passed = result.status == "CANCELLED" and not result.failed
    result.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return result


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def build_report(results: Sequence[FlowResult], infra: dict[str, Any], tag: str) -> str:
    total = len(results)
    passed = sum(1 for item in results if item.passed)
    lines: list[str] = [
        "# W4 Demo 三条链路执行报告",
        "",
        f"- 生成时间：{W.now_tag()}",
        f"- 运行模式：`{infra.get('mode')}`（db={infra.get('db')} / redis={infra.get('redis')} / "
        f"checkpointer={infra.get('checkpointer')} / mcp={infra.get('mcp_transport')}）",
        f"- LLM：`{'mock' if infra.get('llm_mock') else 'real'}`",
        f"- 结果：**{passed}/{total} 条链路通过**",
        "",
        "> 离线模式使用 Mock LLM + SQLite + fakeredis + InMemoryTransport，"
        "编排、SSE、审批、断点、取消全部走真实实现；只有模型输出是确定性模板。",
        "",
        "## 汇总",
        "",
        W.markdown_table(
            [
                {
                    "链路": item.flow_id,
                    "标题": item.title,
                    "task_id": item.task_id,
                    "终态": item.status,
                    "步骤": f"{len(item.steps) - len(item.failed)}/{len(item.steps)}",
                    "耗时(ms)": item.elapsed_ms,
                    "结论": "通过" if item.passed else "未通过",
                }
                for item in results
            ],
            ["链路", "标题", "task_id", "终态", "步骤", "耗时(ms)", "结论"],
        ),
        "",
    ]

    for item in results:
        lines += [
            f"## 链路 {item.flow_id}：{item.title}",
            "",
            f"- 目标（goal）：`{item.goal}`",
            f"- 任务 ID：`{item.task_id}`；终态：`{item.status}`；耗时：{item.elapsed_ms} ms",
            "",
            "### 执行步骤",
            "",
            W.markdown_table(
                [
                    {
                        "步骤": idx + 1,
                        "动作": step.name,
                        "结果": "PASS" if step.ok else "FAIL",
                        "耗时(ms)": step.elapsed_ms,
                        "细节": step.detail[:160],
                    }
                    for idx, step in enumerate(item.steps)
                ],
                ["步骤", "动作", "结果", "耗时(ms)", "细节"],
            ),
            "",
        ]

        seg1 = item.evidence.get("sse_segment_1")
        seg2 = item.evidence.get("sse_segment_2")
        if seg1 or seg2:
            lines += ["### SSE 推送序列", ""]
            for label, seg in (("第一段（提交→审批挂起）", seg1), ("第二段（审批通过→完成）", seg2)):
                if not seg:
                    continue
                counts = ", ".join(f"{k}×{v}" for k, v in sorted(seg.get("counts", {}).items()))
                lines.append(f"- **{label}**：{seg.get('frames')} 帧，收尾事件 `{seg.get('tail_event')}`，分布 {counts}")
            lines.append("")
            timeline = (seg1 or {}).get("timeline") or []
            if timeline:
                lines += [
                    "第一段事件时间轴（截取前 20 帧）：",
                    "",
                    W.markdown_table(
                        [
                            {"#": idx + 1, "事件": row["event"], "节点": row["node"] or "-", "phase": row["phase"] or "-", "status": row["status"] or "-"}
                            for idx, row in enumerate(timeline[:20])
                        ],
                        ["#", "事件", "节点", "phase", "status"],
                    ),
                    "",
                ]

        replay = item.evidence.get("replay") or {}
        if replay:
            lines += [
                "### 轨迹回放",
                "",
                f"- 步骤数：{replay.get('total_steps')}；断点数：{replay.get('checkpoints')}",
                f"- 节点序列：`{' → '.join(replay.get('node_sequence') or []) or '-'}`",
            ]
            if replay.get("events"):
                lines.append(f"- 记录的事件类型：{', '.join(replay['events'])}")
            if "replan_enters" in replay:
                lines.append(f"- replan enter 次数：{replay['replan_enters']}；notify enter 次数：{replay.get('notify_enters')}")
            lines.append("")

        terminal = item.evidence.get("terminal")
        if terminal:
            lines += ["### 终态明细", "", "```json", json.dumps(terminal, ensure_ascii=False, indent=2), "```", ""]

        if item.flow_id == "3":
            for key, label in (("snapshot_before", "取消前快照"), ("snapshot_after", "取消后快照")):
                block = item.evidence.get(key)
                if block:
                    lines += [f"**{label}**", "", "```json", json.dumps(block, ensure_ascii=False, indent=2), "```", ""]
            if item.evidence.get("resume_after_cancel"):
                lines += [
                    f"取消后再 resume 的返回：`{item.evidence['resume_after_cancel'][:160]}`",
                    "",
                ]

        if item.failed:
            lines += ["> 未通过步骤：" + "；".join(f"{step.name}（{step.detail[:80]}）" for step in item.failed), ""]

    lines += [
        "## 复现",
        "",
        "```bash",
        "# 离线（默认）：三条链路全跑",
        "python scripts/demo_three_flows.py",
        "",
        "# 只跑某一条",
        "python scripts/demo_three_flows.py --flow 1",
        "",
        "# 浏览器里手动点一遍（起服务 + 前端；前端是纯静态，无 npm/无构建）",
        "docker compose up -d && alembic upgrade head && python -m app.main",
        "python -m http.server -d frontend 5173   # 浏览器打开 http://127.0.0.1:5173/",
        "```",
        "",
        "## 与生产链路的差异（演示可外推的边界）",
        "",
        "1. 存储：SQLite 文件库替代 MySQL；事件总线 / 取消标记用 fakeredis；checkpointer 用 `InMemorySaver`；",
        "2. MCP：`InMemoryTransport` 替代 stdio 子进程，**工具实现与协议完全一致**（同一份 server 代码）；",
        "3. LLM：Mock 输出为确定性模板，因此「报告内容质量」不可用于验收；编排 / SSE / 审批 / 断点 / 取消的**行为**可验收；",
        "4. `--live` 模式直连真实服务（MySQL + Redis + 真实 LLM），除基建外脚本逻辑不变。",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
async def run_embedded(flows: Sequence[str], *, timeout: float, tag: str) -> int:
    W.bootstrap_env()
    env = W.EmbeddedEnv()
    started = time.perf_counter()
    try:
        await env.start()
        infra = env.infra_summary
        print(f"[demo] 基础设施：{json.dumps(infra, ensure_ascii=False)}")
        results: list[FlowResult] = []
        if "1" in flows:
            print("\n[demo] === 链路 1：审批闭环 ===")
            results.append(await flow_1_approval(env.api, timeout=timeout))
        if "2" in flows:
            print("\n[demo] === 链路 2：驳回 → replan ===")
            results.append(await flow_2_reject_replan(env.api, timeout=timeout))
        if "3" in flows:
            print("\n[demo] === 链路 3：取消冻结断点 ===")
            results.append(await flow_3_cancel(env.api, timeout=timeout))
    finally:
        await env.aclose()

    out_dir = W.ensure_dir(PROJECT_ROOT / "eval_output")
    report = build_report(results, infra, tag)
    md_path = W.write_text(out_dir / f"Demo三条链路_{tag}.md", report)
    json_path = W.write_json(
        out_dir / f"demo_detail_{tag}.json",
        {
            "tag": tag,
            "infra": infra,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "flows": [
                {
                    "flow_id": item.flow_id,
                    "title": item.title,
                    "task_id": item.task_id,
                    "status": item.status,
                    "passed": item.passed,
                    "elapsed_ms": item.elapsed_ms,
                    "steps": [
                        {"name": s.name, "ok": s.ok, "detail": s.detail, "elapsed_ms": s.elapsed_ms}
                        for s in item.steps
                    ],
                    "evidence": item.evidence,
                }
                for item in results
            ],
        },
    )

    passed = sum(1 for item in results if item.passed)
    print("\n=== Demo 结果 ===")
    print(W.markdown_table(
        [
            {
                "链路": item.flow_id,
                "标题": item.title,
                "task_id": item.task_id,
                "终态": item.status,
                "步骤": f"{len(item.steps) - len(item.failed)}/{len(item.steps)}",
                "结论": "PASS" if item.passed else "FAIL",
            }
            for item in results
        ],
        ["链路", "标题", "task_id", "终态", "步骤", "结论"],
    ))
    print(f"\n[demo] 报告 {md_path.name} / 明细 {json_path.name}；目录 {out_dir}")
    return 0 if passed == len(results) else 1


async def run_live(flows: Sequence[str], *, base_url: str, timeout: float, tag: str) -> int:
    """直连真实服务：登录 → 同样三条链路（不再覆盖依赖，走真 MySQL/Redis/LLM）。"""
    from httpx import AsyncClient

    W.bootstrap_env()
    results: list[FlowResult] = []
    infra = {"mode": "live", "db": "mysql", "redis": "redis", "checkpointer": "redis", "base_url": base_url}
    async with AsyncClient(base_url=base_url, timeout=timeout) as client:
        api = W.TaskApi(client)
        await api.login("admin_user")
        if "1" in flows:
            print("\n[demo] === 链路 1：审批闭环 ===")
            results.append(await flow_1_approval(api, timeout=timeout))
        if "2" in flows:
            print("\n[demo] === 链路 2：驳回 → replan ===")
            results.append(await flow_2_reject_replan(api, timeout=timeout))
        if "3" in flows:
            print("\n[demo] === 链路 3：取消冻结断点 ===")
            results.append(await flow_3_cancel(api, timeout=timeout))

    out_dir = W.ensure_dir(PROJECT_ROOT / "eval_output")
    report = build_report(results, infra, tag)
    md_path = W.write_text(out_dir / f"Demo三条链路_{tag}.md", report)
    print(f"\n[demo] 报告 {md_path.name}；目录 {out_dir}")
    return 0 if all(item.passed for item in results) else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="W4 Demo：三条演示链路（审批闭环 / 驳回重规划 / 取消冻结）")
    parser.add_argument("--flow", action="append", choices=("1", "2", "3"), help="只跑指定链路（可重复）")
    parser.add_argument("--live", action="store_true", help="直连真实服务（需先启动 API）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="--live 时的服务地址")
    parser.add_argument("--timeout", type=float, default=90.0, help="单链路等待终态超时（秒）")
    parser.add_argument("--tag", default="offline", help="输出文件名后缀")
    args = parser.parse_args(argv)

    flows = tuple(args.flow) if args.flow else ("1", "2", "3")
    if args.live:
        return asyncio.run(run_live(flows, base_url=args.base_url, timeout=args.timeout, tag=args.tag))
    return asyncio.run(run_embedded(flows, timeout=args.timeout, tag=args.tag))


if __name__ == "__main__":
    raise SystemExit(main())
