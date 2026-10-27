"""评测任务接口。

读接口 4 个聚合端点与项目一保持同一契约（前端 types.ts 已锁定字段名）：
  GET  /eval/runs/{id}/badcases         低分样本（后端筛选，前端不二次计算）
  GET  /eval/runs/{id}/stratum_metrics  分层聚合
  GET  /eval/runs/{id}/export           CSV 明细（带 BOM，Excel 不乱码）
  POST /eval/runs/compare               A/B 对齐 + 逐项 Δ

第六步新增两个「管理层/运维」端点：
  GET  /eval/runs/stats                 顶部大盘四指标（总数/成功率/平均忠实度/累计 Token）
  POST /eval/runs/{id}/cancel           止损：pending|running → cancelled（仅管理员）
"""

import csv
import io
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.auth import CurrentUser, require_admin
from app.core.config import settings
from app.core.database import get_db
from app.core.response import ok, paged
from app.models import (
    CORE_METRICS,
    RUN_CANCELLED,
    RUN_DONE,
    RUN_FAILED,
    RUN_PENDING,
    RUN_RUNNING,
    EvalDataset,
    EvalItem,
    EvalRun,
    EvalScore,
)
from app.schemas.eval import CompareRequest, RunCreate
from app.services.aggregate import compare_scores, stratum_metrics
from app.services.runner import execute_run, next_run_no, now
from app.services.runtime_config import build_judge_config
from app.services.serialize import run_to_dict, score_to_dict

logger = logging.getLogger("evalagent.api")
router = APIRouter(prefix="/eval/runs", tags=["评测任务"])


def _get_run(db: Session, run_id: int) -> EvalRun:
    run = db.get(EvalRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"评测任务 {run_id} 不存在")
    return run


def _require_metric(metric: str) -> str:
    if metric not in CORE_METRICS:
        raise HTTPException(
            status_code=422,
            detail=f"metric 非法：{metric}；可选 {', '.join(CORE_METRICS)}",
        )
    return metric


def _resolve_dataset(db: Session, dataset_id: int | str) -> EvalDataset:
    """把 dataset_id 解析成评测集对象：既支持本平台主键，也支持业务编码。

    为什么需要两种：前端 evalagent_vue 传的是本地主键（int），而项目三 AgentFlow 的
    ``EvalRunCreateRequest.dataset_id`` 是字符串语义（默认 ``"agentflow-30"``，代表它侧的
    数据集标识）。跨系统集成只能用业务编码绑定 —— 硬编码自增主键在换库后就对不上了。
    解析顺序：纯数字 → 主键；否则 → ``eval_datasets.code`` 唯一编码。
    """
    raw = dataset_id
    ds: EvalDataset | None = None
    if isinstance(raw, int) or (isinstance(raw, str) and raw.isdigit()):
        ds = db.get(EvalDataset, int(raw))
    if ds is None:
        code = str(raw).strip()
        ds = db.execute(
            select(EvalDataset).where(EvalDataset.code == code)
        ).scalars().first()
    if ds is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"评测集 {raw} 不存在：请先在本平台创建评测集，"
                "并用它的主键 id 或业务编码 code 触发评测"
            ),
        )
    return ds


# ============================ 列表 / 创建 ============================
@router.get("", summary="评测任务分页列表")
def list_runs(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: str | None = None,
    dataset_id: int | None = None,
    db: Session = Depends(get_db),
):
    stmt = select(EvalRun)
    if status:
        stmt = stmt.where(EvalRun.status == status)
    if dataset_id is not None:
        stmt = stmt.where(EvalRun.dataset_id == dataset_id)
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = (
        db.execute(stmt.order_by(EvalRun.id.desc()).offset((page - 1) * size).limit(size))
        .scalars()
        .all()
    )
    ds_ids = {r.dataset_id for r in rows}
    ds_map = (
        {d.id: d for d in db.execute(select(EvalDataset).where(EvalDataset.id.in_(ds_ids))).scalars()}
        if ds_ids
        else {}
    )
    return paged([run_to_dict(r, ds_map.get(r.dataset_id)) for r in rows], total, page, size)


@router.post("", summary="触发评测（异步，立即返回 run_id）")
def create_run(payload: RunCreate, background: BackgroundTasks, db: Session = Depends(get_db)):
    dataset = _resolve_dataset(db, payload.dataset_id)
    if not dataset.is_active:
        raise HTTPException(status_code=409, detail=f"评测集 {payload.dataset_id} 已归档，不能发起评测")
    if dataset.item_count == 0:
        raise HTTPException(status_code=409, detail="评测集没有样本，请先导入 items")

    # 裁判 + 向量模型快照必须取「生效配置」（runtime_configs > .env），
    # 不能取 settings.JUDGE_MODEL —— 管理员在界面改过模型后，.env 是旧的，
    # 快照写错会让详情页显示"用 A 模型打的分"，实际是 B（口径造假，比不写更糟）。
    # 向量模型也要记：换 embedding 前后 answer_relevancy 不可直接比（见第八步文档）。
    _judge_cfg = build_judge_config()
    run = EvalRun(
        run_no=next_run_no(db),
        dataset_id=dataset.id,
        dataset_version=dataset.version,
        target=payload.target or dataset.target_type or "kb",
        target_ref=payload.target_ref or dataset.target_ref,
        variant=payload.variant,
        rag_config=payload.rag_config,
        evaluator_config=payload.evaluator_config
        or {"judge_model": _judge_cfg.model, "judge_embedding_model": _judge_cfg.embedding_model},
        agent_config=payload.agent_config,
        status="pending",
        total_items=dataset.item_count,
        trace_id=payload.trace_id,
        ext_run_ref=payload.callback_run_no,
        created_by=payload.created_by,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    background.add_task(execute_run, run.id)
    logger.info("run %s 已入队（dataset=%s variant=%s）", run.run_no, dataset.name, run.variant)
    return ok(run_to_dict(run, dataset))


# ============================ 大盘统计（必须在 /{run_id} 之前注册）============================
@router.get("/stats", summary="任务大盘统计（总数 / 成功率 / 平均忠实度 / 累计 Token）")
def run_stats(
    dataset_id: int | None = Query(None, description="只看某个评测集；不传=全平台"),
    db: Session = Depends(get_db),
):
    """给列表页顶部大盘卡片用的轻量聚合。

    口径说明（刻意与详情页保持一致，避免「卡片和列表对不上」）：
      - 成功率 = done / total，**cancelled 与 failed 都算未成功**；
      - 平均 faithfulness = **样本级**均值（分母是真实算出分的样本数），
        不是 run 级均值的平均 —— 否则 2 条样本的任务会和 30 条样本的任务同权；
      - 累计 Token = 所有状态任务的总和（取消前已经烧掉的也要算进成本）。
    """
    scope = [EvalRun.dataset_id == dataset_id] if dataset_id is not None else []

    status_rows = db.execute(
        select(EvalRun.status, func.count()).where(*scope).group_by(EvalRun.status)
    ).all()
    counts = {status: int(n) for status, n in status_rows}
    total = sum(counts.values())
    done = counts.get(RUN_DONE, 0)

    avg_row = db.execute(
        select(func.avg(EvalScore.faithfulness), func.count(EvalScore.faithfulness))
        .join(EvalRun, EvalRun.id == EvalScore.run_id)
        .where(EvalRun.status == RUN_DONE, EvalScore.faithfulness.is_not(None), *scope)
    ).one()
    avg_faithfulness, scored_samples = avg_row[0], int(avg_row[1] or 0)

    tokens, cost = db.execute(
        select(
            func.coalesce(func.sum(EvalRun.total_tokens), 0),
            func.coalesce(func.sum(EvalRun.cost_usd), 0),
        ).where(*scope)
    ).one()

    return ok(
        {
            "total_runs": total,
            "done_runs": done,
            "failed_runs": counts.get(RUN_FAILED, 0),
            "cancelled_runs": counts.get(RUN_CANCELLED, 0),
            "running_runs": counts.get(RUN_RUNNING, 0),
            "pending_runs": counts.get(RUN_PENDING, 0),
            "success_rate": round(done / total, 4) if total else 0.0,
            "avg_faithfulness": round(float(avg_faithfulness), 6) if avg_faithfulness is not None else None,
            "scored_samples": scored_samples,
            "total_tokens": int(tokens or 0),
            "total_cost_usd": round(float(cost or 0), 6),
        }
    )


# ============================ A/B 对比（必须在 /{run_id} 之前注册）============================
@router.post("/compare", summary="A/B 对比（同数据集）")
def compare_runs(payload: CompareRequest, db: Session = Depends(get_db)):
    run_a = _get_run(db, payload.run_id_a)
    run_b = _get_run(db, payload.run_id_b)
    if run_a.dataset_id != run_b.dataset_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"两次任务不属于同一评测集（{run_a.dataset_id} vs {run_b.dataset_id}），"
                "问题无法逐条对齐"
            ),
        )
    scores_a = [score_to_dict(s) for s in _all_scores(db, run_a.id)]
    scores_b = [score_to_dict(s) for s in _all_scores(db, run_b.id)]
    result = compare_scores(
        run_to_dict(run_a, db.get(EvalDataset, run_a.dataset_id)),
        run_to_dict(run_b, db.get(EvalDataset, run_b.dataset_id)),
        scores_a,
        scores_b,
        settings.COMPARE_DIFF_THRESHOLD,
    )
    return ok(result)


# ============================ 详情 / 明细 ============================
@router.get("/{run_id}", summary="评测任务详情")
def get_run(run_id: int, db: Session = Depends(get_db)):
    run = _get_run(db, run_id)
    return ok(run_to_dict(run, db.get(EvalDataset, run.dataset_id)))


@router.get("/{run_id}/scores", summary="单条样本得分明细（分页）")
def list_scores(
    run_id: int,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None, description="ok / failed / skipped"),
    db: Session = Depends(get_db),
):
    _get_run(db, run_id)
    stmt = select(EvalScore).where(EvalScore.run_id == run_id)
    if status:
        stmt = stmt.where(EvalScore.status == status)
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = (
        db.execute(
            stmt.order_by(EvalScore.item_seq.asc(), EvalScore.id.asc())
            .offset((page - 1) * size)
            .limit(size)
        )
        .scalars()
        .all()
    )
    return paged([score_to_dict(r) for r in rows], total, page, size)


@router.get("/{run_id}/badcases", summary="BadCase 分页（后端按指标阈值筛选）")
def list_badcases(
    run_id: int,
    metric: str = Query("faithfulness"),
    threshold: float = Query(settings.BADCASE_DEFAULT_THRESHOLD, ge=0, le=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    _get_run(db, run_id)
    metric = _require_metric(metric)
    column = getattr(EvalScore, metric)

    # NULL 指标不参与筛选（与项目一统计口径一致：没算出来 ≠ 低分）
    stmt = (
        select(EvalScore, EvalItem.reference_answer)
        .outerjoin(EvalItem, EvalItem.id == EvalScore.item_id)
        .where(EvalScore.run_id == run_id, column.is_not(None), column < threshold)
    )
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(
        stmt.order_by(column.asc(), EvalScore.item_seq.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    items = []
    for score, ground_truth in rows:
        row = score_to_dict(score)
        row["ground_truth"] = ground_truth if ground_truth is not None else score.reference_answer
        items.append(row)
    return ok(
        {
            "total": int(total),
            "page": page,
            "page_size": page_size,
            "metric": metric,
            "threshold": threshold,
            "items": items,
        }
    )


@router.get("/{run_id}/stratum_metrics", summary="分层指标聚合")
def get_stratum_metrics(run_id: int, db: Session = Depends(get_db)):
    _get_run(db, run_id)
    rows = [
        {
            "stratum": s.stratum,
            "faithfulness": s.faithfulness,
            "answer_relevancy": s.answer_relevancy,
            "context_precision": s.context_precision,
            "context_recall": s.context_recall,
        }
        for s in db.execute(
            select(EvalScore).where(EvalScore.run_id == run_id, EvalScore.status == "ok")
        ).scalars()
    ]
    agg = stratum_metrics(rows)
    return ok({"run_id": run_id, "total": agg["total"], "items": agg["items"]})


@router.get("/{run_id}/export", summary="导出 CSV 明细")
def export_csv(run_id: int, db: Session = Depends(get_db)):
    run = _get_run(db, run_id)
    rows = _all_scores(db, run_id)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["序号", "问题", "回答", "标准答案", "忠实度", "答案相关性", "上下文精确率", "上下文召回率",
         "延迟(ms)", "可疑", "状态", "错误"]
    )
    for s in rows:
        writer.writerow([
            s.item_seq if s.item_seq is not None else "",
            s.question,
            s.answer or "",
            s.reference_answer or "",
            _fmt(s.faithfulness),
            _fmt(s.answer_relevancy),
            _fmt(s.context_precision),
            _fmt(s.context_recall),
            s.latency_ms,
            "是" if s.suspect else "否",
            s.status,
            s.error_msg or "",
        ])

    # 带 BOM，否则 Excel 打开中文乱码（项目三同款教训）
    content = "\ufeff" + buf.getvalue()
    filename = f"eval_run_{run.run_no}.csv"
    return Response(
        content=content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{run_id}/cancel", summary="停止评测任务（running / pending → cancelled）")
def cancel_run(
    run_id: int,
    _: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """止损：把还在排队/执行的任务置为 cancelled。

    只写状态，不在这里杀协程 —— 执行器每 RUN_CANCEL_POLL_SECONDS 轮询一次标记，
    看到 cancelled 就会自己停手、取消剩余打分任务、保留已产出的得分。
    为什么不直接 ``task.cancel()``：``execute_run`` 由 BackgroundTasks 调用，
    跑在**响应所在的 ASGI 任务**里，从另一个请求 cancel 它会把连接处理一起打掉。
    """
    run = _get_run(db, run_id)
    if run.status not in (RUN_PENDING, RUN_RUNNING):
        raise HTTPException(
            status_code=409,
            detail=(
                f"任务 {run.run_no} 当前状态为 {run.status}，"
                "只有 pending / running 可以停止（已结束的任务请用删除）"
            ),
        )

    previous = run.status
    run.status = RUN_CANCELLED
    run.finished_at = now()
    run.error_msg = f"用户手动停止（已处理 {run.processed_items}/{run.total_items}）"[:1000]
    db.commit()
    db.refresh(run)

    logger.warning("run %s 被手动停止（原状态 %s）", run.run_no, previous)
    data = run_to_dict(run, db.get(EvalDataset, run.dataset_id))
    data["previous_status"] = previous
    return ok(data)


@router.delete("/{run_id}", summary="删除评测任务（级联删明细）")
def delete_run(run_id: int, db: Session = Depends(get_db)):
    run = _get_run(db, run_id)
    deleted = db.execute(
        select(func.count()).select_from(EvalScore).where(EvalScore.run_id == run_id)
    ).scalar_one()
    db.execute(EvalScore.__table__.delete().where(EvalScore.run_id == run_id))
    db.delete(run)
    db.commit()
    return ok({"deleted_run_id": run_id, "deleted_scores": int(deleted)})


# ============================ 内部工具 ============================
def _all_scores(db: Session, run_id: int) -> list[EvalScore]:
    return db.execute(
        select(EvalScore).where(EvalScore.run_id == run_id).order_by(EvalScore.item_seq.asc())
    ).scalars().all()


def _fmt(value: float | None) -> str:
    return "" if value is None else f"{value:.6f}"
