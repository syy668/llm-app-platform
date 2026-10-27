"""DAG 工具：拓扑排序 / 环检测 / 就绪子任务（PRD FR-002 验收 1）。

与 LangGraph 的分工：LangGraph 负责"节点级"流程编排（固定 7 节点），
这里负责"子任务级"的 DAG 校验与执行顺序——两者正交，互不耦合。
"""

from __future__ import annotations

from collections import defaultdict, deque

from app.schemas.agent_io import PlanValidationReport
from app.schemas.graph_state import SubtaskPlan


def _edge_map(subtasks: list[SubtaskPlan]) -> dict[int, set[int]]:
    return {st.seq: {d for d in st.depends_on if d != st.seq} for st in subtasks}


def topological_order(subtasks: list[SubtaskPlan]) -> tuple[list[int], list[str]]:
    """Kahn 拓扑排序。返回 (顺序, 错误列表)。

    错误类型：
    - 重复 seq
    - depends_on 指向不存在的 seq
    - 自依赖
    - 存在环（返回参与环的节点）
    """
    errors: list[str] = []
    seen: set[int] = set()
    for st in subtasks:
        if st.seq in seen:
            errors.append(f"seq {st.seq} 重复")
        seen.add(st.seq)

    edges = _edge_map(subtasks)
    for st in subtasks:
        for dep in st.depends_on:
            if dep == st.seq:
                errors.append(f"seq {st.seq} 依赖自身")
            elif dep not in seen:
                errors.append(f"seq {st.seq} 依赖不存在的 seq {dep}")

    indegree = {seq: 0 for seq in edges}
    children: dict[int, list[int]] = defaultdict(list)
    for seq, deps in edges.items():
        valid_deps = {d for d in deps if d in indegree}
        indegree[seq] = len(valid_deps)
        for dep in valid_deps:
            children[dep].append(seq)

    queue = deque(sorted(seq for seq, deg in indegree.items() if deg == 0))
    order: list[int] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for child in sorted(children.get(node, [])):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)

    if len(order) != len(edges):
        cyclic = sorted(seq for seq, deg in indegree.items() if deg > 0)
        errors.append(f"存在循环依赖，涉及 seq {cyclic}")
    return order, errors


def validate_plan(
    subtasks: list[SubtaskPlan],
    *,
    min_subtasks: int = 1,
    max_subtasks: int = 15,
) -> PlanValidationReport:
    """规划校验：数量区间 + DAG 无环 + 拓扑可执行。"""
    errors: list[str] = []
    if len(subtasks) < min_subtasks:
        errors.append(f"子任务数量 {len(subtasks)} 少于下限 {min_subtasks}")
    if len(subtasks) > max_subtasks:
        errors.append(f"子任务数量 {len(subtasks)} 超过上限 {max_subtasks}")

    order, dag_errors = topological_order(subtasks)
    errors.extend(dag_errors)
    return PlanValidationReport(ok=not errors, order=order, errors=errors)


def ready_subtasks(subtasks: list[SubtaskPlan], done: set[int]) -> list[SubtaskPlan]:
    """下一批可执行子任务：依赖全部完成且自身未完成。按 seq 升序保证确定性。"""
    ready = [
        st
        for st in subtasks
        if st.seq not in done and all(dep in done for dep in st.depends_on if dep != st.seq)
    ]
    return sorted(ready, key=lambda st: st.seq)


def blocked_seqs(subtasks: list[SubtaskPlan], done: set[int], failed: set[int]) -> set[int]:
    """因上游失败而被阻塞的子任务（不再尝试执行，直接 SKIPPED）。"""
    blocked: set[int] = set()
    for st in subtasks:
        if st.seq in done or st.seq in failed:
            continue
        if any(dep in failed or dep in blocked for dep in st.depends_on if dep != st.seq):
            blocked.add(st.seq)
    return blocked


def sort_by_order(subtasks: list[SubtaskPlan]) -> list[SubtaskPlan]:
    """按拓扑序返回子任务（无环时等价于依赖优先，便于阅读报告）。"""
    order, _ = topological_order(subtasks)
    index = {seq: i for i, seq in enumerate(order)}
    return sorted(subtasks, key=lambda st: index.get(st.seq, 999))


__all__ = ["topological_order", "validate_plan", "ready_subtasks", "blocked_seqs", "sort_by_order"]
