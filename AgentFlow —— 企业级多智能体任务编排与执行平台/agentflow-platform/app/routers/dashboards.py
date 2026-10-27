"""W6：大盘聚合（缺口 G2，PRD §7.1 / FR-016）。

- ``GET /dashboards/summary``：前端 P02 五张指标卡；viewer 脱敏（token 类字段 None）；
- ``GET /dashboards/tools``：工具调用分布，数据源 ``task_executions`` 按 tool_name 聚合
  （真实执行记录；不用 subtasks.tool_hint——那是规划意图不是实际调用）；
- ``GET /dashboards/failure-reasons``：失败原因 Top 5，数据源 ``tasks.error.code``
  （原型 P02「失败原因 Top 5」；缺码的 FAILED 归入 ``UNKNOWN``）。

只读聚合，不建新表、不改存量路由。

归档口径（刻意如此，勿"顺手优化"）
------------------------------------
大盘聚合**不排除已归档任务**（``tasks.archived_at IS NOT NULL``）。

理由：归档（见 ``app/routers/task_archive.py``）只是个人收藏夹式的**列表整理**动作，
任务本身仍然是**真实发生过的执行**——它消耗过 token、跑过工具、产出过图表、贡献过失败率。
把这些从大盘里抠掉会让「本月完成了多少个任务」「平均耗时多少」这类**经营口径**失真，
而且是不可解释的失真（同一时间段，数据会因为谁点了归档而漂移）。

反之，操作者想看"还剩多少活"的诉求由 ``GET /tasks?archived=`` 的列表筛选承担——
**列表是工作视图，大盘是事实视图**，两者口径本就不同。

唯一的例外是第二阶段「软删除（``deleted_at``）」：那是**用户明确宣告这份数据无效**，
届时**所有**读路径（含本文件 4 处聚合、列表、详情、导出、审批收件箱、评测）统一加
``deleted_at IS NULL``，一次性对齐，不在归档阶段提前埋半套过滤。

TODO（2026-10 评审结论，本轮刻意不做，避免混改动污染回归）
----------------------------------------------------------
大盘五张卡的**取值本身是真实的**（前端用"今日窗口 + 昨日窗口"两次请求自算环比，
``DashboardView.vue::yesterdayWindow/deltaOf``），但三处口径会误导人，待单独一轮修：
1. ``n=1`` 时把单点值说成"完成率"：样本数 < 5 应加 ``n=1`` 角标，或把一位小数收掉；
2. 「总 Token 消耗」比的是**绝对量**（1 个任务 vs 25 个任务），"↓95.8%" 没有信息量，
   应改成"每任务均值"才可比；
3. 「待审批数量」曾把已超期、批不动的僵尸单算进去（v1.3 已由收件箱口径修复，
   但卡片本身仍应标注"其中 X 条已过期"）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_role
from app.db.session import get_db
from app.schemas.auth import CurrentUser
from app.schemas.common import ApiResponse
from app.schemas.enums import TaskStatus, UserRole
from app.schemas.orm_models import Task, TaskExecution
from app.schemas.w6 import (
    DashboardSummary,
    FailureReasonData,
    FailureReasonItem,
    ToolStatItem,
    ToolStatsData,
    TrendData,
    TrendPoint,
    WindowRange,
)
from app.services.eval_run_service import default_window
from app.utils.query_parsing import parse_datetime

router = APIRouter(prefix="/dashboards", tags=["dashboards"])

# 六键恒齐（W6 文档 §2.2）；SUMMARIZING 为短瞬态，若窗口内存在则以额外键透出（保证 total 可对账）
_BREAKDOWN_KEYS = (
    TaskStatus.PLANNING,
    TaskStatus.RUNNING,
    TaskStatus.AWAITING_APPROVAL,
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
)
_TERMINAL = (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)


async def _load_summary(session: AsyncSession, start: datetime, end: datetime, *, mask_token: bool) -> DashboardSummary:
    # ---- 窗口内任务与状态分布 ----
    # 刻意不加 archived_at 过滤：归档只是列表整理，任务仍是真实发生过的执行，
    # 抠掉会让完成率 / 平均耗时 / token 统计随"谁点了归档"漂移。详见模块 docstring「归档口径」。
    in_window = Task.created_at >= start
    in_window &= Task.created_at <= end

    status_rows = (
        await session.execute(select(Task.status, func.count(Task.id)).where(in_window).group_by(Task.status))
    ).all()
    breakdown: dict[str, int] = {s.value: 0 for s in _BREAKDOWN_KEYS}
    task_total = 0
    for status_value, count in status_rows:
        key = status_value.value if hasattr(status_value, "value") else str(status_value)
        breakdown[key] = int(count)
        task_total += int(count)

    completed = breakdown.get(TaskStatus.COMPLETED.value, 0)
    terminal_total = sum(breakdown.get(s.value, 0) for s in _TERMINAL)
    completion_rate = round(completed / terminal_total, 4) if terminal_total else 0.0

    # ---- 待审批积压（不限定窗口，与 GET /approvals 的 total 同源） ----
    pending_approvals = (
        await session.execute(select(func.count(Task.id)).where(Task.status == TaskStatus.AWAITING_APPROVAL))
    ).scalar_one()

    # ---- 平均端到端耗时：窗口内终态任务（跨库时间差函数不同，行级 Python 计算） ----
    terminal_rows = (
        (
            await session.execute(
                select(Task.created_at, Task.finished_at).where(in_window, Task.status.in_(_TERMINAL))
            )
        )
        .all()
    )
    durations = [
        (finished - created).total_seconds()
        for created, finished in terminal_rows
        if created is not None and finished is not None
    ]
    avg_duration = round(sum(durations) / len(durations)) if durations else None

    # ---- Token 消耗（viewer 脱敏 → None） ----
    token_sum: int | None = None
    token_avg: float | None = None
    if not mask_token:
        token_sum = int(
            (await session.execute(select(func.coalesce(func.sum(Task.token_usage), 0)).where(in_window))).scalar_one()
        )
        token_avg_raw = (await session.execute(select(func.avg(Task.token_usage)).where(in_window))).scalar_one()
        token_avg = round(float(token_avg_raw), 2) if token_avg_raw is not None else None

    return DashboardSummary(
        window=WindowRange(start=start.isoformat(), end=end.isoformat()),
        task_total=task_total,
        completion_rate=completion_rate,
        pending_approvals=int(pending_approvals or 0),
        avg_duration_s=avg_duration,
        token_usage_total=token_sum,
        avg_token_per_task=token_avg,
        status_breakdown=breakdown,
    )


@router.get("/summary", summary="大盘指标汇总（P02 五张指标卡）", response_model=ApiResponse[DashboardSummary])
async def dashboard_summary(
    start: str | None = Query(default=None, description="窗口起点，默认今日 00:00，ISO8601"),
    end: str | None = Query(default=None, description="窗口终点，默认今日 24:00，ISO8601"),
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER, UserRole.VIEWER)),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[DashboardSummary]:
    default_start, default_end = default_window()
    start_dt = parse_datetime(start, field="start") or default_start
    end_dt = parse_datetime(end, field="end") or default_end
    if end_dt <= start_dt:
        from app.core.exceptions import AppException, ErrorCode

        raise AppException(ErrorCode.VALIDATION_ERROR, "参数 end 必须晚于 start")
    return ApiResponse.ok(await _load_summary(session, start_dt, end_dt, mask_token=user.role == UserRole.VIEWER))


@router.get("/tools", summary="工具调用分布（按 task_executions 聚合）", response_model=ApiResponse[ToolStatsData])
async def dashboard_tools(
    start: str | None = Query(default=None, description="窗口起点，默认今日 00:00，ISO8601"),
    end: str | None = Query(default=None, description="窗口终点，默认今日 24:00，ISO8601"),
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER, UserRole.VIEWER)),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[ToolStatsData]:
    default_start, default_end = default_window()
    start_dt = parse_datetime(start, field="start") or default_start
    end_dt = parse_datetime(end, field="end") or default_end
    if end_dt <= start_dt:
        from app.core.exceptions import AppException, ErrorCode

        raise AppException(ErrorCode.VALIDATION_ERROR, "参数 end 必须晚于 start")

    rows = (
        await session.execute(
            select(
                TaskExecution.tool_name,
                func.count(TaskExecution.id),
                func.sum(func.coalesce(TaskExecution.tool_error.is_(None), 0)),
                func.avg(TaskExecution.latency_ms),
            )
            .where(
                TaskExecution.tool_name.is_not(None),
                TaskExecution.started_at >= start_dt,
                TaskExecution.started_at <= end_dt,
            )
            .group_by(TaskExecution.tool_name)
        )
    ).all()

    items = [
        ToolStatItem(
            tool_name=str(tool_name),
            call_count=int(count),
            success_rate=round(float(success) / int(count), 4) if count else 0.0,
            # 全量无耗时记录（latency_ms 恒 0）时 None
            avg_latency_ms=(round(float(latency), 2) if latency and float(latency) > 0 else None),
        )
        for tool_name, count, success, latency in rows
    ]
    items.sort(key=lambda item: item.call_count, reverse=True)
    return ApiResponse.ok(ToolStatsData(window=WindowRange(start=start_dt.isoformat(), end=end_dt.isoformat()), items=items))


@router.get(
    "/failure-reasons",
    summary="失败原因 Top 5（按 tasks.error.code 聚合）",
    response_model=ApiResponse[FailureReasonData],
)
async def dashboard_failure_reasons(
    start: str | None = Query(default=None, description="窗口起点，默认今日 00:00，ISO8601"),
    end: str | None = Query(default=None, description="窗口终点，默认今日 24:00，ISO8601"),
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER, UserRole.VIEWER)),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[FailureReasonData]:
    default_start, default_end = default_window()
    start_dt = parse_datetime(start, field="start") or default_start
    end_dt = parse_datetime(end, field="end") or default_end
    if end_dt <= start_dt:
        from app.core.exceptions import AppException, ErrorCode

        raise AppException(ErrorCode.VALIDATION_ERROR, "参数 end 必须晚于 start")

    rows = (
        await session.execute(
            select(Task.error).where(
                Task.status == TaskStatus.FAILED,
                Task.created_at >= start_dt,
                Task.created_at <= end_dt,
            )
        )
    ).scalars().all()

    # JSON 取键在 MySQL（JSON_UNQUOTE(JSON_EXTRACT)）与 SQLite（json_extract）语法不一致，
    # 失败任务量级小 → Python 侧聚合，跨方言且能把异常 error 结构统一归入 UNKNOWN。
    buckets: dict[str, int] = {}
    samples: dict[str, str] = {}
    for err in rows:
        payload: dict[str, object] | None = None
        if isinstance(err, dict):
            payload = err
        elif isinstance(err, str) and err.strip().startswith("{"):
            import json

            try:
                loaded = json.loads(err)
                payload = loaded if isinstance(loaded, dict) else None
            except ValueError:
                payload = None
        raw_code = (payload or {}).get("code")
        code = str(raw_code) if raw_code else "UNKNOWN"
        buckets[code] = buckets.get(code, 0) + 1
        raw_message = (payload or {}).get("message")
        if raw_message and code not in samples:
            samples[code] = str(raw_message)

    items = [
        FailureReasonItem(reason_code=code, count=count, sample_message=samples.get(code))
        for code, count in sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    ]
    return ApiResponse.ok(
        FailureReasonData(
            window=WindowRange(start=start_dt.isoformat(), end=end_dt.isoformat()),
            failed_total=len(rows),
            items=items,
        )
    )


@router.get("/trend", summary="任务完成趋势（按天聚合，缺口 G4-趋势卡）", response_model=ApiResponse[TrendData])
async def dashboard_trend(
    start: str | None = Query(default=None, description="窗口起点，默认近 7 天 00:00，ISO8601"),
    end: str | None = Query(default=None, description="窗口终点，默认今日 24:00，ISO8601"),
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER, UserRole.VIEWER)),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[TrendData]:
    """按天聚合任务量与完成率，供前端「任务完成趋势」卡绘制折线。

    口径：task_total = 当日新建（created_at），completed = 当日 COMPLETED，
    completion_rate = completed / 当日终态总数（与 summary 口径一致）。
    """
    from app.core.exceptions import AppException, ErrorCode

    _, default_end = default_window()
    default_start = default_end - timedelta(days=7)
    start_dt = parse_datetime(start, field="start") or default_start
    end_dt = parse_datetime(end, field="end") or default_end
    if end_dt <= start_dt:
        raise AppException(ErrorCode.VALIDATION_ERROR, "参数 end 必须晚于 start")

    # 按 UTC naive 日期分组；跨库日期函数不同，拉行级数据用 Python 聚合（窗口通常 ≤ 30 天，量小）
    rows = (
        await session.execute(
            select(Task.created_at, Task.status).where(Task.created_at >= start_dt, Task.created_at <= end_dt)
        )
    ).all()

    day_buckets: dict[str, dict[str, int]] = {}
    # 预置窗口内每一天（含空桶，保证折线连续）
    day = start_dt.date()
    end_day = end_dt.date()
    while day <= end_day:
        day_buckets[day.isoformat()] = {"task_total": 0, "completed": 0, "terminal": 0}
        day += timedelta(days=1)

    for created_at, status in rows:
        key = created_at.date().isoformat()
        if key not in day_buckets:
            continue
        day_buckets[key]["task_total"] += 1
        status_value = status.value if hasattr(status, "value") else str(status)
        if status_value in {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}:
            day_buckets[key]["terminal"] += 1
        if status_value == TaskStatus.COMPLETED.value:
            day_buckets[key]["completed"] += 1

    points = [
        TrendPoint(
            date=key,
            task_total=bucket["task_total"],
            completed=bucket["completed"],
            completion_rate=round(bucket["completed"] / bucket["terminal"], 4) if bucket["terminal"] else 0.0,
        )
        for key, bucket in sorted(day_buckets.items())
    ]
    return ApiResponse.ok(
        TrendData(window=WindowRange(start=start_dt.isoformat(), end=end_dt.isoformat()), points=points)
    )


__all__ = ["router"]
