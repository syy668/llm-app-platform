"""W6 评测接口组服务（/eval/runs，缺口 G3，FR-016 P1）。

对齐 docs/W6接口设计文档.md §3：
- 数据源为已存在的 ``eval_runs`` 表（不建新表），本服务只做查询/聚合/状态回写；
- ``MOCK_EXTERNAL_EVAL=true`` 由内嵌 mock runner 生成 metrics 落库（PRD A-02），
  **该分支产生的是随机数，仅用于联调**，落库时 ``detail.mock=true``，前端据此
  显示黄标/红标/置灰；
- ``MOCK_EXTERNAL_EVAL=false`` → 真实评测，两条路：
  1. ``PROJECT1_EVAL_DATASET_ID > 0`` → 走 ``eval_project1_adapter`` 直连项目一
     的 RAGAS 底座（**v1.4 起的推荐路径**，产出 ragas 四项真实指标）；
  2. 否则回退旧的 ``EVALAGENT_BASE_URL`` 转发（假设存在项目二后端；本机无此服务）。

指标口径（v1.4 起）：真实评测的 metrics 用 ragas 原名
（faithfulness / answer_relevancy / context_precision / context_recall），
**不再强行映射到 task_completion / plan_f1 / tool_acc** —— 那三个是智能体编排
指标，与 RAG 质量指标语义不同，映射会造成「字段名与真实含义不一致」的伪装。
拿不到的字段写 None，前端显示「—」。
"""

from __future__ import annotations

import asyncio
import random
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.config import settings
from app.core.exceptions import AppException, ErrorCode
from app.core.logging import get_logger
from app.schemas.enums import EvalRunStatus, EvalSource, EvalVariant
from app.schemas.orm_models import EvalRun
from app.schemas.w6 import (
    EvalMetricsData,
    EvalMetricsPoint,
    EvalRunCreateRequest,
    EvalRunItem,
    EvalRunSummaryData,
    EvalRunSummaryGroup,
)
from app.services.eval_project1_adapter import (
    RAGAS_METRIC_NAMES,
    TERMINAL_STATUSES as PROJECT1_TERMINAL,
    ProjectOneEvalAdapter,
    ProjectOneEvalError,
    build_metrics_payload,
)
from app.utils.ids import new_run_no
from app.utils.time import utcnow

logger = get_logger(__name__)

# metrics 键名沿用 eval_runs.metrics 表注释既有键（W6 文档 §3.2 字段表）
_METRIC_KEYS = ("task_completion", "plan_f1", "tool_acc")
_DEFAULT_DATASET = "agentflow-30"


# ---------------------------------------------------------------------------
# run_no 编号（与 task_service._next_task_no 同款 count+1 规则）
# ---------------------------------------------------------------------------
async def _next_run_no(session: AsyncSession) -> str:
    day = date.today().strftime("%Y%m%d")
    prefix = f"AFRUN{day}-"
    total = (
        await session.execute(select(func.count(EvalRun.id)).where(EvalRun.run_no.like(f"{prefix}%")))
    ).scalar_one()
    return new_run_no(int(total or 0) + 1)


# ---------------------------------------------------------------------------
# §3.2 GET /eval/runs 列表
# ---------------------------------------------------------------------------
def _detail_extra(run: EvalRun) -> tuple[int | None, str | None]:
    detail = run.detail if isinstance(run.detail, dict) else None
    if not detail:
        return None, None
    sample_count = detail.get("samples_count")
    if sample_count is None and isinstance(detail.get("samples"), list):
        sample_count = len(detail["samples"])
    error_message = detail.get("error") or detail.get("error_message")
    return (
        int(sample_count) if sample_count is not None else None,
        str(error_message) if error_message else None,
    )


async def list_runs(
    session: AsyncSession,
    *,
    page: int = 1,
    size: int = 20,
    sources: list[EvalSource] | None = None,
    variants: list[EvalVariant] | None = None,
    statuses: list[EvalRunStatus] | None = None,
    eval_tag: str | None = None,
) -> dict[str, Any]:
    """分页查询评测运行（created_at 降序），响应 item 直接映射表列。"""
    conditions: list[ColumnElement[bool]] = []
    if sources:
        conditions.append(EvalRun.source.in_(sources))
    if variants:
        conditions.append(EvalRun.variant.in_(variants))
    if statuses:
        conditions.append(EvalRun.status.in_(statuses))
    if eval_tag:
        conditions.append(EvalRun.eval_tag == eval_tag.strip())

    where = conditions
    total = (await session.execute(select(func.count(EvalRun.id)).where(*where))).scalar_one()

    stmt = (
        select(EvalRun)
        .where(*where)
        .order_by(EvalRun.created_at.desc(), EvalRun.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    rows = (await session.execute(stmt)).scalars().all()

    items: list[EvalRunItem] = []
    for run in rows:
        sample_count, error_message = _detail_extra(run)
        detail = run.detail if isinstance(run.detail, dict) else {}
        finished_at = detail.get("finished_at")
        # is_mock：MOCK_EXTERNAL_EVAL 下落库的记录，指标由 _mock_metrics() 随机生成。
        # 必须随行下发——否则前端无法把"假数字"和"真实验"区分开，
        # 这套随机数摆在"用数据判断是否值得开启"的标语下就是误导。
        is_mock = bool(detail.get("mock"))
        items.append(
            EvalRunItem(
                run_id=run.id,
                run_no=run.run_no,
                source=run.source.value if hasattr(run.source, "value") else str(run.source),
                variant=run.variant.value if hasattr(run.variant, "value") and run.variant else None,
                dataset_id=run.dataset_id,
                eval_tag=run.eval_tag,
                ext_run_id=run.ext_run_id,
                status=run.status.value if hasattr(run.status, "value") else str(run.status),
                metrics=dict(run.metrics) if isinstance(run.metrics, dict) else None,
                # v1.4：真实评测的来源元信息随行下发，前端据此展示
                # 「项目一 RAGAS / 判分模型 / 指标口径」三个标签，避免被误读成
                # 「AgentFlow 端到端编排质量」。
                eval_backend=detail.get("eval_backend"),
                judge_model=detail.get("judge_model"),
                metrics_scope=detail.get("metrics_scope"),
                duration_s=(
                    float(detail["duration_s"])
                    if isinstance(detail.get("duration_s"), (int, float))
                    else (
                        float((run.metrics or {}).get("duration_s"))
                        if isinstance((run.metrics or {}).get("duration_s"), (int, float))
                        else None
                    )
                ),
                sample_count=sample_count,
                error_message=error_message,
                is_mock=is_mock,
                created_at=run.created_at.isoformat() if run.created_at else None,
                finished_at=str(finished_at) if finished_at else None,
            )
        )
    return {"items": items, "total": int(total or 0)}


# ---------------------------------------------------------------------------
# §3.3 /eval/runs/summary 与 §3.4 /eval/metrics 的聚合内核
# ---------------------------------------------------------------------------
def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _aggregate_rows(runs: list[EvalRun], *, mask_token: bool) -> EvalRunSummaryGroup:
    """对同一 variant 下的 DONE run 聚合一行。

    v1.4 起同时聚合 RAGAS 四指标：历史 mock 记录没有这四个键，
    ``_avg`` 拿到空列表返回 None，前端显示「—」—— 不补 0、不混算。
    """
    metric_rows = [r.metrics for r in runs if isinstance(r.metrics, dict)]
    group = EvalRunSummaryGroup(
        variant=(
            runs[0].variant.value
            if runs and hasattr(runs[0], "variant") and runs[0].variant and hasattr(runs[0].variant, "value")
            else None
        ),
        run_count=len(runs),
        avg_task_completion=_avg([float(m["task_completion"]) for m in metric_rows if m.get("task_completion") is not None]),
        avg_plan_f1=_avg([float(m["plan_f1"]) for m in metric_rows if m.get("plan_f1") is not None]),
        avg_tool_acc=_avg([float(m["tool_acc"]) for m in metric_rows if m.get("tool_acc") is not None]),
        avg_faithfulness=_avg([float(m["faithfulness"]) for m in metric_rows if m.get("faithfulness") is not None]),
        avg_answer_relevancy=_avg([float(m["answer_relevancy"]) for m in metric_rows if m.get("answer_relevancy") is not None]),
        avg_context_precision=_avg([float(m["context_precision"]) for m in metric_rows if m.get("context_precision") is not None]),
        avg_context_recall=_avg([float(m["context_recall"]) for m in metric_rows if m.get("context_recall") is not None]),
        # viewer 脱敏：token 类字段 None
        avg_token_cost=None if mask_token else _avg([float(m["token_cost"]) for m in metric_rows if m.get("token_cost") is not None]),
        avg_latency_p95_s=_avg([float(m["latency_p95"]) for m in metric_rows if m.get("latency_p95") is not None]),
    )
    return group


def _apply_saving_rate(groups: list[EvalRunSummaryGroup]) -> list[EvalRunSummaryGroup]:
    """token_saving_rate 口径（W6 文档 §3.3 写死）：
    以 compress_off 组 avg_token_cost 为基线，1 - 该组/基线；基线组自身 None；无基线组时两组均 None。"""
    baseline: float | None = None
    for group in groups:
        if group.variant == EvalVariant.COMPRESS_OFF.value and group.avg_token_cost is not None:
            baseline = group.avg_token_cost
            break
    if baseline is None or baseline <= 0:
        return groups
    for group in groups:
        if group.variant == EvalVariant.COMPRESS_OFF.value or group.avg_token_cost is None:
            continue
        group.token_saving_rate = round(1 - group.avg_token_cost / baseline, 4)
    return groups


async def summarize_runs(
    session: AsyncSession,
    *,
    variants: list[EvalVariant] | None = None,
    mask_token: bool = False,
) -> EvalRunSummaryData:
    """按 variant 聚合 DONE 状态 run；variants 为空 = 全部 variant 各一行。"""
    conditions: list[ColumnElement[bool]] = [EvalRun.status == EvalRunStatus.DONE]
    if variants:
        conditions.append(EvalRun.variant.in_(variants))
    rows = (
        (await session.execute(select(EvalRun).where(*conditions).order_by(EvalRun.created_at.asc())))
        .scalars()
        .all()
    )

    buckets: dict[str | None, list[EvalRun]] = {}
    for run in rows:
        key = run.variant.value if hasattr(run.variant, "value") and run.variant else None
        buckets.setdefault(key, []).append(run)

    groups = [_aggregate_rows(runs, mask_token=mask_token) for _key, runs in sorted(buckets.items(), key=lambda kv: (kv[0] is not None, kv[0] or ""))]
    return EvalRunSummaryData(window=None, groups=_apply_saving_rate(groups))


async def metrics_by_day(
    session: AsyncSession,
    *,
    start: datetime,
    end: datetime,
    mask_token: bool = False,
) -> EvalMetricsData:
    """窗口内按自然日对 DONE run 聚合；无数据的日期不出点；points 按日期升序。"""
    rows = (
        (
            await session.execute(
                select(EvalRun).where(
                    EvalRun.status == EvalRunStatus.DONE,
                    EvalRun.created_at >= start,
                    EvalRun.created_at <= end,
                )
            )
        )
        .scalars()
        .all()
    )

    buckets: dict[str, list[EvalRun]] = {}
    for run in rows:
        if run.created_at is None:
            continue
        buckets.setdefault(run.created_at.strftime("%Y-%m-%d"), []).append(run)

    points: list[EvalMetricsPoint] = []
    for day in sorted(buckets):
        runs = buckets[day]
        metric_rows = [r.metrics for r in runs if isinstance(r.metrics, dict)]
        points.append(
            EvalMetricsPoint(
                date=day,
                run_count=len(runs),
                avg_task_completion=_avg([float(m["task_completion"]) for m in metric_rows if m.get("task_completion") is not None]),
                avg_plan_f1=_avg([float(m["plan_f1"]) for m in metric_rows if m.get("plan_f1") is not None]),
                avg_tool_acc=_avg([float(m["tool_acc"]) for m in metric_rows if m.get("tool_acc") is not None]),
                avg_faithfulness=_avg([float(m["faithfulness"]) for m in metric_rows if m.get("faithfulness") is not None]),
                avg_answer_relevancy=_avg([float(m["answer_relevancy"]) for m in metric_rows if m.get("answer_relevancy") is not None]),
                avg_context_precision=_avg([float(m["context_precision"]) for m in metric_rows if m.get("context_precision") is not None]),
                avg_context_recall=_avg([float(m["context_recall"]) for m in metric_rows if m.get("context_recall") is not None]),
                avg_token_cost=None if mask_token else _avg([float(m["token_cost"]) for m in metric_rows if m.get("token_cost") is not None]),
                avg_latency_p95_s=_avg([float(m["latency_p95"]) for m in metric_rows if m.get("latency_p95") is not None]),
            )
        )
    return EvalMetricsData(points=points)


# ---------------------------------------------------------------------------
# §3.5 POST /eval/runs 触发链路
# ---------------------------------------------------------------------------
def _mock_metrics(dataset_id: str) -> dict[str, Any]:
    """内嵌 mock runner：由 run_no/dataset 派生确定性伪随机指标（联调期不依赖项目二）。"""
    seed = sum(dataset_id.encode("utf-8")) + random.randrange(1000)
    rng = random.Random(seed)

    def jitter(base: float, spread: float, digits: int = 3) -> float:
        return round(max(0.0, min(1.0, base + rng.uniform(-spread, spread))), digits)

    return {
        "task_completion": jitter(0.92, 0.05),
        "plan_f1": jitter(0.87, 0.05),
        "tool_acc": jitter(0.90, 0.05),
        "token_cost": rng.randint(1_200_000, 2_600_000),
        "latency_p50": rng.randint(120, 260),
        "latency_p95": rng.randint(320, 480),
    }


async def _mark_failed(session: AsyncSession, run: EvalRun, message: str) -> None:
    run.status = EvalRunStatus.FAILED
    detail = dict(run.detail) if isinstance(run.detail, dict) else {}
    detail["error"] = message
    detail["finished_at"] = utcnow().isoformat()
    run.detail = detail
    await session.commit()
    logger.warning("eval_run_failed", extra={"run_id": run.id, "run_no": run.run_no, "error": message[:200]})


async def _poll_upstream(run_id: int, ext_run_id: str) -> None:
    """后台任务：每 EVAL_POLL_INTERVAL_S 轮询项目二，DONE 回写 metrics（PRD L706 口径）。

    独立 session（请求早已返回）；连续失败 EVAL_POLL_MAX_RETRY 次置 FAILED。
    """
    from app.db.session import get_session_factory

    base_url = settings.EVALAGENT_BASE_URL.rstrip("/")
    for attempt in range(1, settings.EVAL_POLL_MAX_RETRY + 1):
        try:
            async with httpx.AsyncClient(timeout=settings.EXTERNAL_EVAL_TIMEOUT_SECONDS) as client:
                response = await client.get(f"{base_url}/api/v1/eval/runs/{ext_run_id}")
                response.raise_for_status()
                body = response.json()
            data = body.get("data") or body
            # 大小写不敏感：项目二契约是小写 done，本处历史上按大写 DONE 判，
            # 两端不一致会让轮询永远认不出完成（任务永远 PENDING）。
            upstream_status = str(data.get("status") or "").strip().upper()
            if upstream_status == "DONE" and isinstance(data.get("metrics"), dict):
                async with get_session_factory()() as session:
                    run = await session.get(EvalRun, run_id)
                    if run is None:
                        return
                    run.status = EvalRunStatus.DONE
                    run.metrics = data["metrics"]
                    detail = dict(run.detail) if isinstance(run.detail, dict) else {}
                    detail["finished_at"] = utcnow().isoformat()
                    if isinstance(data.get("detail"), dict) and data["detail"].get("samples_count") is not None:
                        detail["samples_count"] = data["detail"]["samples_count"]
                    run.detail = detail
                    await session.commit()
                logger.info("eval_run_done", extra={"run_id": run_id, "attempt": attempt})
                return
            # 上游已判失败/取消：不必等满 30 次重试，直接同步终态
            if upstream_status in ("FAILED", "CANCELLED"):
                async with get_session_factory()() as session:
                    run = await session.get(EvalRun, run_id)
                    if run is not None and run.status in (EvalRunStatus.PENDING, EvalRunStatus.RUNNING):
                        await _mark_failed(
                            session, run, f"上游评测{upstream_status}: {data.get('error_msg') or '未返回原因'}"
                        )
                logger.warning("eval_run_upstream_terminal", extra={"run_id": run_id, "upstream": upstream_status})
                return
        except Exception as exc:  # noqa: BLE001 - 轮询失败累计，超阈值置 FAILED
            logger.warning("eval_poll_failed", extra={"run_id": run_id, "attempt": attempt, "error": str(exc)[:200]})
        await asyncio.sleep(settings.EVAL_POLL_INTERVAL_S)

    async with get_session_factory()() as session:
        run = await session.get(EvalRun, run_id)
        if run is not None and run.status in (EvalRunStatus.PENDING, EvalRunStatus.RUNNING):
            await _mark_failed(session, run, f"upstream poll exceeded {settings.EVAL_POLL_MAX_RETRY} retries")


async def _mock_finish(session: AsyncSession, run: EvalRun) -> None:
    """mock 模式：内嵌 runner 直接生成 metrics 落库置 DONE（满足验收「≤10s 内落库」）。"""
    run.status = EvalRunStatus.DONE
    run.metrics = _mock_metrics(run.dataset_id or _DEFAULT_DATASET)
    detail = dict(run.detail) if isinstance(run.detail, dict) else {}
    detail["samples_count"] = 30
    detail["mock"] = True
    detail["finished_at"] = utcnow().isoformat()
    run.detail = detail
    await session.commit()
    logger.info("eval_run_mock_done", extra={"run_id": run.id, "run_no": run.run_no})


# ---------------------------------------------------------------------------
# v1.4 真实评测（直连项目一 RAGAS 底座）
# ---------------------------------------------------------------------------
async def _poll_project1(run_id: int, ext_run_id: int, judge_model: str | None) -> None:
    """后台轮询项目一评测结果，终态回写；超时置 FAILED 并写清原因。

    与旧 ``_poll_upstream``（转发项目二）的区别：
    - 状态值是项目一口径的**小写** ``pending/running/done/failed/cancelled``；
    - 指标用 ragas 原名，不做智能体指标映射（见模块 docstring）；
    - 轮询间隔/上限单独配置（``PROJECT1_EVAL_POLL_*``），因为项目一单样本要
      跑 4 个指标的多次判分，实测 5 条约 5 分钟，30 条约 30 分钟。

    独立 session（触发请求早已返回），且**每轮都重新构造 adapter**：
    令牌缓存挂在实例上，长轮询期间令牌会过期，靠 adapter 内 401 重登兜底。
    """
    from app.db.session import get_session_factory

    adapter = ProjectOneEvalAdapter()
    interval = max(1, int(settings.PROJECT1_EVAL_POLL_INTERVAL_S))
    max_attempt = max(1, int(settings.PROJECT1_EVAL_POLL_MAX_RETRY))
    last_error = ""

    for attempt in range(1, max_attempt + 1):
        try:
            upstream = await adapter.get_run(ext_run_id)
        except Exception as exc:  # noqa: BLE001 - 轮询失败累积，超阈值置 FAILED
            last_error = str(exc)[:200]
            logger.warning(
                "eval_poll_project1_failed",
                extra={"run_id": run_id, "attempt": attempt, "error": last_error},
            )
            await asyncio.sleep(interval)
            continue

        status = str(upstream.get("status") or "").lower()
        if status not in PROJECT1_TERMINAL:
            await asyncio.sleep(interval)
            continue

        async with get_session_factory()() as session:
            run = await session.get(EvalRun, run_id)
            if run is None:
                return
            detail = dict(run.detail) if isinstance(run.detail, dict) else {}
            detail["upstream_status"] = status
            detail["finished_at"] = utcnow().isoformat()

            if status == "done":
                metrics, extra = build_metrics_payload(
                    upstream, judge_model=judge_model, fallback_dataset_label=run.dataset_id
                )
                has_any = any(metrics.get(name) is not None for name in RAGAS_METRIC_NAMES)
                if not has_any:
                    # DONE 但四项全空 = 判分链路静默失败（项目一 v1.17 已不允许，
                    # 但跨服务边界必须自己再挡一层），按失败处理而不是记成成功
                    run.status = EvalRunStatus.FAILED
                    detail["error"] = "上游状态 done 但四项 RAGAS 指标均为空，判分链路异常"
                else:
                    run.status = EvalRunStatus.DONE
                    run.metrics = metrics
                    detail.update(extra)
            elif status == "cancelled":
                # AgentFlow 侧无 CANCELLED 状态，归入 FAILED 并保留原因（不伪装成成功）
                run.status = EvalRunStatus.FAILED
                detail["error"] = "上游评测运行被取消（cancelled）"
            else:
                run.status = EvalRunStatus.FAILED
                detail["error"] = str(upstream.get("error_msg") or "上游评测失败（未给出原因）")[:500]

            run.detail = detail
            await session.commit()
        logger.info(
            "eval_project1_finished",
            extra={"run_id": run_id, "upstream_status": status, "attempt": attempt},
        )
        return

    async with get_session_factory()() as session:
        run = await session.get(EvalRun, run_id)
        if run is not None and run.status in (EvalRunStatus.PENDING, EvalRunStatus.RUNNING):
            await _mark_failed(
                session,
                run,
                f"轮询项目一超时（{max_attempt} 次 × {interval}s），"
                f"最后一次错误：{last_error or '无'}",
            )


async def _trigger_project1(session: AsyncSession, run: EvalRun) -> dict[str, Any]:
    """真实评测：触发项目一并起后台轮询。失败抛 502（本地记录置 FAILED）。"""
    adapter = ProjectOneEvalAdapter()
    try:
        created = await adapter.create_run()
        judge_model = await adapter.get_judge_model()
    except ProjectOneEvalError as exc:
        await _mark_failed(session, run, f"项目一评测触发失败：{exc}")
        logger.warning(
            "eval_project1_unavailable",
            extra={"run_id": int(run.id), "error": str(exc)[:300]},
        )
        raise AppException(
            ErrorCode.EVAL_UPSTREAM_UNAVAILABLE,
            f"项目一真实评测不可用：{exc}",
        ) from exc

    ext_run_id = int(created["run_id"])
    run.ext_run_id = str(ext_run_id)
    run.status = EvalRunStatus.RUNNING
    detail = dict(run.detail) if isinstance(run.detail, dict) else {}
    detail.update(
        {
            "eval_backend": "project1_ragas",
            "judge_model": judge_model,
            "judge_temperature": 0.0,
            "metrics_scope": "kb_retrieval_chain",
            "upstream_run_id": ext_run_id,
            "upstream_dataset_id": adapter.dataset_id,
        }
    )
    run.detail = detail
    await session.commit()

    asyncio.create_task(_poll_project1(int(run.id), ext_run_id, judge_model))
    logger.info(
        "eval_run_project1_started",
        extra={"run_id": int(run.id), "run_no": run.run_no, "ext_run_id": ext_run_id},
    )
    return {"run_id": int(run.id), "run_no": run.run_no, "status": run.status.value}


async def create_run(session: AsyncSession, request: EvalRunCreateRequest) -> dict[str, Any]:
    """触发回归评测：本地插 PENDING 立即返回；随后按 mock / 非 mock 两路推进。

    - 并发约束：同 dataset_id+variant 存在 PENDING/RUNNING → 409 EVAL_RUN_DUPLICATE；
    - 非 mock 且 EVALAGENT_BASE_URL 为空或转发失败 → 502 EVAL_UPSTREAM_UNAVAILABLE，
      本地记录置 FAILED（error_message=upstream unreachable）。
    """
    variant: EvalVariant | None = None
    if request.variant:
        try:
            variant = EvalVariant(request.variant)
        except ValueError as exc:
            raise AppException(
                ErrorCode.VALIDATION_ERROR,
                f"非法 variant: {request.variant}",
            ) from exc

    # 并发约束（409）
    duplicate_stmt = select(EvalRun.id).where(
        EvalRun.status.in_((EvalRunStatus.PENDING, EvalRunStatus.RUNNING)),
        EvalRun.dataset_id == request.dataset_id,
    )
    if variant is not None:
        duplicate_stmt = duplicate_stmt.where(EvalRun.variant == variant)
    else:
        duplicate_stmt = duplicate_stmt.where(EvalRun.variant.is_(None))
    duplicate_id = (await session.execute(duplicate_stmt.limit(1))).scalar_one_or_none()
    if duplicate_id is not None:
        raise AppException(
            ErrorCode.EVAL_RUN_DUPLICATE,
            "同 dataset_id+variant 已存在进行中的评测运行",
            detail={"existing_run_id": int(duplicate_id)},
        )

    # 1) 本地插入 PENDING（source=regression），run_no 按既有编号规则生成
    run = EvalRun(
        run_no=await _next_run_no(session),
        source=EvalSource.REGRESSION,
        dataset_id=request.dataset_id,
        variant=variant,
        eval_tag=(request.eval_tag or "").strip() or None,
        status=EvalRunStatus.PENDING,
        detail={"note": request.note} if request.note else {},
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)

    run_no = run.run_no
    run_id = int(run.id)

    # 2) mock 模式：内嵌 runner 直接落库置 DONE（跳过转发与轮询）
    if settings.MOCK_EXTERNAL_EVAL:
        await _mock_finish(session, run)
        return {"run_id": run_id, "run_no": run_no, "status": run.status.value}

    # 3) v1.4 真实评测：优先直连项目一 RAGAS 底座。
    #    判定条件是「配了数据集 ID 且有凭据」，而不是只看开关 —— 这样运维只要
    #    填上 PROJECT1_EVAL_DATASET_ID 就自动切到真实评测，不必再改代码分支。
    project1 = ProjectOneEvalAdapter()
    if project1.configured:
        return await _trigger_project1(session, run)

    # 4) 回退：转发项目二（PRD L704-706 假设的形态；本机无此服务）
    base_url = settings.EVALAGENT_BASE_URL.strip().rstrip("/")
    if not base_url:
        await _mark_failed(
            session,
            run,
            "未配置真实评测后端：PROJECT1_EVAL_DATASET_ID 与 EVALAGENT_BASE_URL 均为空",
        )
        raise AppException(
            ErrorCode.EVAL_UPSTREAM_UNAVAILABLE,
            "未配置真实评测后端：请设置 PROJECT1_EVAL_DATASET_ID（项目一数据集 ID，"
            "推荐）或 EVALAGENT_BASE_URL（需真有该服务）",
        )

    payload = {
        "dataset_id": request.dataset_id,
        "target": "agentflow",
        "variant": variant.value if variant else None,
        "callback_run_no": run_no,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.EXTERNAL_EVAL_TIMEOUT_SECONDS) as client:
            response = await client.post(f"{base_url}/api/v1/eval/runs", json=payload)
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        await _mark_failed(session, run, "upstream unreachable")
        logger.warning("eval_upstream_unreachable", extra={"run_id": run_id, "error": str(exc)[:200]})
        raise AppException(ErrorCode.EVAL_UPSTREAM_UNAVAILABLE, "项目二 eval API 不可达 / 触发失败") from exc

    data = body.get("data") or body
    ext_run_id = data.get("run_id") or data.get("id")
    if ext_run_id is None:
        await _mark_failed(session, run, "upstream unreachable")
        raise AppException(ErrorCode.EVAL_UPSTREAM_UNAVAILABLE, "项目二 eval 接口未返回 run_id")

    run.ext_run_id = str(ext_run_id)
    run.status = EvalRunStatus.RUNNING
    await session.commit()

    # 4) 后台轮询回写（每 10s，最多 EVAL_POLL_MAX_RETRY 次）
    asyncio.create_task(_poll_upstream(run_id, str(ext_run_id)))
    return {"run_id": run_id, "run_no": run_no, "status": run.status.value}


def default_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """dashboards 默认窗口：今日 00:00 ~ 今日 24:00（UTC naive，与落库口径一致）。"""
    now = now or utcnow()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def default_trend_window(now: datetime | None = None, days: int = 7) -> tuple[datetime, datetime]:
    """趋势卡默认窗口：近 7 个自然日（含今日，UTC naive）。

    不能复用 ``default_window``（仅今日）：评测一天只跑零星几次，当日恰好没有
    DONE 运行时趋势卡必然空态，用户会误判为「没数据/有 bug」（2026-10-27 实测：
    今日仅 1 条 FAILED，昨日 4 条 DONE 全被今日窗口排除 →「窗口内暂无 DONE」）。
    趋势看的是走势，默认窗口必须跨天；显式传 start/end 仍可收窄。
    """
    now = now or utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return today_start - timedelta(days=days - 1), today_start + timedelta(days=1)


__all__ = [
    "list_runs",
    "summarize_runs",
    "metrics_by_day",
    "create_run",
    "default_window",
    "default_trend_window",
    "_METRIC_KEYS",
    "_DEFAULT_DATASET",
]
