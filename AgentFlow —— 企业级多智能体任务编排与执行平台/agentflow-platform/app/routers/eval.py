"""W6：评测接口组 /eval/runs（缺口 G3，FR-016 P1）。

- 列表/触发：A/R（viewer 403，PRD L562：V 仅汇总）；
- summary / metrics：A/R/V（viewer 的「汇总」入口，token 类字段脱敏为 None）；
- 触发链路与 mock 语义见 ``app/services/eval_run_service.py``（对齐 PRD L704-706）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_role
from app.db.session import get_db
from app.schemas.auth import CurrentUser
from app.schemas.common import ApiResponse, PageData, PageMeta
from app.schemas.enums import EvalRunStatus, EvalSource, EvalVariant, UserRole
from app.schemas.w6 import (
    EvalMetricsData,
    EvalRunCreateRequest,
    EvalRunCreateResult,
    EvalRunItem,
    EvalRunSummaryData,
)
from app.services import eval_run_service
from app.utils.query_parsing import parse_datetime, parse_multi_enum

router = APIRouter(prefix="/eval", tags=["eval"])


@router.get("/runs", summary="评测运行分页列表（缺口 G3）", response_model=ApiResponse[PageData[EvalRunItem]])
async def list_runs(
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    size: int = Query(default=20, ge=1, le=100, description="每页条数，1~100"),
    source: str | None = Query(default=None, description="来源过滤，可多值英文逗号分隔（reviewer_gate/regression/ab_test）"),
    variant: str | None = Query(default=None, description="A/B 分组过滤，可多值英文逗号分隔（compress_on/compress_off）"),
    status: str | None = Query(default=None, description="状态过滤，可多值英文逗号分隔（PENDING/RUNNING/DONE/FAILED）"),
    eval_tag: str | None = Query(default=None, description="评测标签过滤（G8）"),
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER)),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[PageData[EvalRunItem]]:
    result = await eval_run_service.list_runs(
        session,
        page=page,
        size=size,
        sources=parse_multi_enum(source, EvalSource, field="source"),
        variants=parse_multi_enum(variant, EvalVariant, field="variant"),
        statuses=parse_multi_enum(status, EvalRunStatus, field="status"),
        eval_tag=eval_tag,
    )
    data = PageData[EvalRunItem](
        items=result["items"],
        meta=PageMeta(page=page, size=size, total=result["total"]),
    )
    return ApiResponse.ok(data)


@router.get("/runs/summary", summary="A/B 分组聚合（上下文压缩对比卡）", response_model=ApiResponse[EvalRunSummaryData])
async def runs_summary(
    variant: list[str] = Query(default=[], description="A/B 分组，可重复传参；不传 = 全部 variant 各聚合一行"),
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER, UserRole.VIEWER)),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[EvalRunSummaryData]:
    variants = parse_multi_enum(",".join(variant) if variant else None, EvalVariant, field="variant")
    mask_token = user.role == UserRole.VIEWER
    return ApiResponse.ok(await eval_run_service.summarize_runs(session, variants=variants, mask_token=mask_token))


@router.get("/metrics", summary="指标按天趋势（P06 折线）", response_model=ApiResponse[EvalMetricsData])
async def metrics_trend(
    start: str | None = Query(default=None, description="窗口起点，默认近 7 天（含今日）00:00，ISO8601"),
    end: str | None = Query(default=None, description="窗口终点，默认明日 00:00，ISO8601"),
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER, UserRole.VIEWER)),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[EvalMetricsData]:
    # 趋势看走势，默认窗口必须跨天：今日窗口在「当日还没跑出 DONE」时必然空态，
    # 被用户误读为 bug（default_window 仅今日，留给 dashboards 用，此处不复用）。
    default_start, default_end = eval_run_service.default_trend_window()
    start_dt = parse_datetime(start, field="start") or default_start
    end_dt = parse_datetime(end, field="end") or default_end
    mask_token = user.role == UserRole.VIEWER
    return ApiResponse.ok(
        await eval_run_service.metrics_by_day(session, start=start_dt, end=end_dt, mask_token=mask_token)
    )


@router.post("/runs", summary="触发回归评测（FR-016 验收标准 2）", response_model=ApiResponse[EvalRunCreateResult])
async def create_run(
    request: EvalRunCreateRequest,
    user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER)),
    session: AsyncSession = Depends(get_db),
) -> ApiResponse[EvalRunCreateResult]:
    result = await eval_run_service.create_run(session, request)
    return ApiResponse.ok(EvalRunCreateResult(**result))


__all__ = ["router"]
