"""RAGAS 评测服务：数据集管理、样本导入、异步评测执行、结果查询。"""
import asyncio
import concurrent.futures
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Callable, NamedTuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..agent.graph import run_agent
from ..agent.tools.db_query_tool import query_business_db
from ..config import settings
from ..core.database import SessionLocal, engine
from ..core.exceptions import BizError, NotFoundError
from ..core import runtime_config
from ..eval.metrics import RagasEvaluator, RagasScoringUnavailable
from ..models.eval_dataset import EvalDataset, EvalDatasetItem
from ..models.eval_run import (
    RUN_CANCELLED,
    RUN_DONE,
    RUN_FAILED,
    RUN_PENDING,
    RUN_RUNNING,
    TERMINAL_STATUSES,
    EvalRun,
    EvalRunScore,
)
from ..models.knowledge_base import KnowledgeBase
from ..models.operation_log import OperationLog
from ..rag import retriever
from ..schemas.eval import dataset_to_dict, item_to_dict, run_to_dict, score_to_dict

logger = logging.getLogger(__name__)

_tables_ready = False

# 【v1.17】熔断阈值：连续这么多个样本判分失败、且一条都没成功 → 判定为配置性故障并中止。
# 3 是本项目「能确认不是偶发」与「不白烧太多算力」的折中：单次上游抖动很少连续三发。
_EVAL_ABORT_CONSECUTIVE_FAILURES = 3

# ---------------------------------------------------------------------------
# 【B1-6 BUG-4 修复】统一空值口径（JSON null ↔ SQL NULL）
# ---------------------------------------------------------------------------
# 规则（全部统计/筛选路径必须一致）：
#   1. 指标未产出时，Python 侧为 None，落库为 SQL NULL（eval_run_scores 各指标列可空）；
#   2. 均值统计：仅对非空值求均值（等价于 SQL AVG 忽略 NULL），全空时结果为 null；
#   3. BadCase 阈值筛选（list_badcases）：SQL NULL 不参与比较（显式 is_not(None) 前置），
#      与第 2 条口径一致——NULL 表示"未测出"，既不算高分也不算低分。
# 前端 utils/abCompare.ts 的 avg() 已按同样规则过滤 null/非有限值，两端口径对齐。


def _mean_or_none(values: list[float]) -> float | None:
    """对非空指标值求均值并保留 6 位小数；空列表返回 None（对应 JSON null）。

    统一空值口径的唯一实现：均值统计禁止直接 sum(v)/len(v)（空列表除零），
    也禁止把 None 计入求和（TypeError 风险），一律先过滤 None 再计算。
    """
    valid = [v for v in values if v is not None]
    if not valid:
        return None
    return round(sum(valid) / len(valid), 6)


def _aggregate_metrics(metric_sums: dict[str, list], scored: int) -> dict:
    """指标均值 + missing_rates（各指标缺失率），让「虚高的均值」显形。

    【v1.19.x】metric_sums 只收非空值（上游 `if row[k] is not None` 才 append），
    所以 len(v) 就是有效样本数，缺失率 = 1 - len(v)/scored。
    修复前的口径缺陷：30 条只算了 21 条，faithfulness 仍显示 0.8587，
    看不出「分母被剔小」的真相（项目二同款教训：看均值前先看缺失率）。
    missing_rates 仅在确实存在缺失时写入；全 0 不加键，保持历史 JSON 结构。
    """
    metrics = {k: _mean_or_none(v) for k, v in metric_sums.items()}
    if scored > 0:
        rates = {
            k: round(max(0, scored - len(v)) / scored, 4)
            for k, v in metric_sums.items()
        }
        if any(r > 0 for r in rates.values()):
            metrics["missing_rates"] = rates
    return metrics


def ensure_eval_tables() -> None:
    """幂等自动建表（eval_datasets / eval_dataset_items / eval_runs / eval_run_scores）。

    【新增】并对已存在的旧表做轻量列迁移（无 alembic 环境的兜底方案）：
    eval_runs 缺少 rag_config 列时自动 ALTER TABLE 补列，历史记录该列为 NULL。
    """
    global _tables_ready
    if _tables_ready:
        return
    EvalDataset.__table__.create(bind=engine, checkfirst=True)
    EvalDatasetItem.__table__.create(bind=engine, checkfirst=True)
    EvalRun.__table__.create(bind=engine, checkfirst=True)
    EvalRunScore.__table__.create(bind=engine, checkfirst=True)
    _ensure_eval_runs_rag_config_column()
    _ensure_eval_runs_progress_columns()  # 【v1.13.1 新增】
    _ensure_eval_dataset_items_stratum_column()  # 【B2-4 新增】
    _tables_ready = True


def _ensure_eval_runs_progress_columns() -> None:
    """【v1.13.1 新增】旧库补列：eval_runs 缺 processed_items / failed_items 时补列（幂等）。

    老库这两列不存在 → 前端进度条拿不到值。历史行默认 0，语义上等于「未统计」，
    与「本来就没有」不可区分，但**不可用 success_items 回填**：跳过与失败的样本
    在旧数据里没有记录，任何回填都是编造。
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("eval_runs")}
    missing = [c for c in ("processed_items", "failed_items") if c not in columns]
    if not missing:
        return
    for col in missing:
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(f"ALTER TABLE eval_runs ADD COLUMN {col} INT NOT NULL DEFAULT 0")
                )
            logger.info("eval_runs 表已补充 %s 列", col)
        except Exception:
            logger.warning("eval_runs 补列 %s 失败（可能已存在），跳过", col, exc_info=True)


def _ensure_eval_runs_rag_config_column() -> None:
    """【新增】旧库补列：eval_runs 无 rag_config 列时执行 ALTER TABLE（幂等）。"""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("eval_runs")}
    if "rag_config" in columns:
        return
    try:
        with engine.begin() as conn:
            # MySQL 与 SQLite 均兼容该语句；历史行该列自动为 NULL
            conn.execute(text("ALTER TABLE eval_runs ADD COLUMN rag_config JSON NULL"))
        logger.info("eval_runs 表已补充 rag_config 列")
    except Exception:
        # 列已存在（并发迁移）等场景不阻塞启动，真实建表失败由后续查询暴露
        logger.warning("eval_runs 补列 rag_config 失败（可能已存在），跳过", exc_info=True)


def _ensure_eval_dataset_items_stratum_column() -> None:
    """【B2-4 新增】旧库补列：eval_dataset_items 无 stratum 列时执行 ALTER TABLE（幂等）。"""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("eval_dataset_items")}
    if "stratum" in columns:
        return
    try:
        with engine.begin() as conn:
            # 历史行该列自动为 NULL（未分层），完全兼容老数据
            conn.execute(text("ALTER TABLE eval_dataset_items ADD COLUMN stratum VARCHAR(64) NULL"))
        logger.info("eval_dataset_items 表已补充 stratum 列")
    except Exception:
        logger.warning("eval_dataset_items 补列 stratum 失败（可能已存在），跳过", exc_info=True)


# ---------------------------------------------------------------------------
# 僵死任务保护（应用启动钩子调用，不引入 Celery/APScheduler）
# ---------------------------------------------------------------------------

# 【B2-1 新增】进程被 kill / 重启时，后台评测线程随进程消亡，eval_runs 里的
# running 状态会永久滞留（前端轮询永远转圈）。最小保护：应用启动时扫描一次，
# 把"开始时间早于阈值且仍为 running"的任务判为 failed。不引入任何调度依赖。
def cleanup_stale_running_runs(max_age_seconds: int | None = None, clear_pending: bool = False) -> int:
    """启动时清理僵死评测任务：超阈值仍为 running 的 eval_runs 置为 failed。

    - 阈值默认 settings.eval_stale_run_seconds（EVAL_STALE_RUN_SECONDS，默认 600s）；
    - 开始时间取 COALESCE(started_at, created_at)，兼容 pending 直接被打断的旧数据；
    - 幂等可重复执行；只改 status/error_msg/finished_at，不动其他任何字段；
    - 任何异常只告警不抛出，保证不阻塞应用启动。
    返回本次修正的任务数量。

    【v1.13】`clear_pending=True`：**只在应用启动时传**，无条件清理残留 pending。
    理由：新进程不会去接管旧线程，此刻还停在 pending 的任务必然是僵尸（批量触发后
    进程重启留下的），它们会永久占住「同时运行任务数」的名额。刻意不按时间判 pending：
    created_at 由数据库 server_default 生成，其会话时区可能与 Python 的
    datetime.now() 不一致（MySQL 容器常见 UTC），按时间比会把刚创建的正常任务误判
    成僵尸 —— 这个坑在 sqlite 测试里已经显形过一次。
    """
    ensure_eval_tables()
    if max_age_seconds is None:
        max_age_seconds = settings.eval_stale_run_seconds
    cutoff = datetime.now() - timedelta(seconds=max_age_seconds)
    db = SessionLocal()
    try:
        stale_runs = (
            db.query(EvalRun)
            .filter(
                EvalRun.status == RUN_RUNNING,
                func.coalesce(EvalRun.started_at, EvalRun.created_at) < cutoff,
            )
            .all()
        )
        if clear_pending:
            # 无条件：进程刚起，任何残留 pending 都不会再有人推进
            stale_runs += db.query(EvalRun).filter(EvalRun.status == RUN_PENDING).all()
        for run in stale_runs:
            interrupted = run.status == RUN_RUNNING
            run.status = RUN_FAILED
            # 【v1.13.1】被打断前已落库的得分仍然有效，按得分回填指标均值。
            # 不回填的话列表里 4 项指标全是「—」，用户会以为这批样本白跑了。
            try:
                run.metrics = _metrics_from_scores(db, run.id) or run.metrics
            except Exception:
                logger.warning("僵死任务回填指标失败 run_id=%s", run.id, exc_info=True)
            run.error_msg = (
                "进程重启，任务中断"
                if interrupted
                else "任务未能启动（进程重启），已标记失败"
            )
            run.finished_at = datetime.now()
        if stale_runs:
            db.commit()
            logger.warning(
                "启动清理僵死评测任务 %d 个: ids=%s",
                len(stale_runs), [r.id for r in stale_runs],
            )
        return len(stale_runs)
    except Exception:  # noqa: BLE001 - 清理失败不允许阻塞应用启动
        db.rollback()
        logger.exception("清理僵死评测任务失败（不阻塞启动）")
        return 0
    finally:
        db.close()


def backfill_terminal_metrics(limit: int = 200) -> int:
    """【v1.13.1 新增】修复历史脏数据：已有得分、但汇总指标为空的终态任务。

    产生原因：v1.13.1 之前，任务走异常/中断路径时只改 status，不落缓冲区里的得分，
    也不汇总 metrics —— 数据库里出现一批「failed + 有 N 条得分 + 四项指标全 NULL」
    的记录（真实库 run 38/51 等）。列表里这些任务指标全是「—」，但点开能看到得分，
    口径自相矛盾。

    本函数只补 metrics 一列，不改 status/success_items（那些是被改过的历史值，
    无从还原；凭得分条数回填会编造出一条不存在的成功数）。幂等，可重复执行。
    返回本次修复的任务数。
    """
    ensure_eval_tables()
    db = SessionLocal()
    fixed = 0
    try:
        # 刻意不在 SQL 里过滤 `metrics IS NULL`：metrics 是 JSON 列，SQLAlchemy 的
        # JSON 默认 none_as_null=False —— 赋 None 落库的是 JSON 文本 'null'，不是
        # SQL NULL，`IS NULL` 一条都匹配不到（实测 matched=[]）。改在 Python 侧判断，
        # 顺带把历史遗留的 `{"faithfulness": null, ...}` 全空字典也一起算作待修。
        rows = (
            db.query(EvalRun)
            .filter(EvalRun.status.in_(TERMINAL_STATUSES))
            .order_by(EvalRun.id.desc())
            .limit(limit)
            .all()
        )
        for run in rows:
            current = run.metrics
            if current and any(v is not None for v in current.values()):
                continue  # 已有有效指标，不动
            metrics = _metrics_from_scores(db, run.id)
            if metrics:
                run.metrics = metrics
                fixed += 1
        if fixed:
            db.commit()
            logger.info("历史评测任务指标回填 %d 个", fixed)
        return fixed
    except Exception:  # noqa: BLE001 - 修复属尽力而为，不允许阻塞调用方
        db.rollback()
        logger.exception("回填历史评测指标失败")
        return 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 数据集
# ---------------------------------------------------------------------------

def create_dataset(db: Session, user, data) -> dict:
    ensure_eval_tables()
    kb = db.get(KnowledgeBase, data.kb_id)
    if not kb:
        raise NotFoundError("知识库不存在")
    ds = EvalDataset(kb_id=data.kb_id, name=data.name, description=data.description, item_count=0, created_by=user.id)
    db.add(ds)
    db.commit()
    db.refresh(ds)
    return dataset_to_dict(ds)


def list_datasets(db: Session, page: int, size: int, kb_id: int | None = None) -> dict:
    ensure_eval_tables()
    q = db.query(EvalDataset)
    if kb_id:
        q = q.filter(EvalDataset.kb_id == kb_id)
    total = q.count()
    items = q.order_by(EvalDataset.id.desc()).offset((page - 1) * size).limit(size).all()
    rows = [dataset_to_dict(d) for d in items]
    # 【v1.13 新增】参考答案覆盖率：一次聚合查出本页各集的「缺答案样本数」，
    # 避免前端按集逐个拉样本列表（N+1）。口径与 RAGAS 一致：
    # 空串与 NULL 都算缺失（badcase 自动沉淀的样本即为 reference_answer=""）。
    ds_ids = [d.id for d in items]
    missing_map: dict[int, int] = {}
    if ds_ids:
        pairs = (
            db.query(EvalDatasetItem.dataset_id, func.count(EvalDatasetItem.id))
            .filter(
                EvalDatasetItem.dataset_id.in_(ds_ids),
                or_(
                    EvalDatasetItem.reference_answer.is_(None),
                    func.trim(EvalDatasetItem.reference_answer) == "",
                ),
            )
            .group_by(EvalDatasetItem.dataset_id)
            .all()
        )
        missing_map = {int(k): int(v) for k, v in pairs}
    for row in rows:
        row["missing_reference"] = missing_map.get(int(row["id"]), 0)
    return {"total": total, "items": rows}


def _get_dataset(db: Session, dataset_id: int) -> EvalDataset:
    ds = db.get(EvalDataset, dataset_id)
    if not ds:
        raise NotFoundError("评测数据集不存在")
    return ds


def add_items(db: Session, dataset_id: int, data) -> dict:
    """批量导入评测样本。

    【B2-4 新增】支持 stratum 分层标签（可选）：随样本持久化，供分层聚合；
    前端 CSV 解析层（utils/csv.ts）后续增加 stratum 列后即自动透传，不传为 NULL。
    """
    ensure_eval_tables()
    ds = _get_dataset(db, dataset_id)
    rows = [
        EvalDatasetItem(
            dataset_id=dataset_id,
            question=it.question,
            reference_answer=it.reference_answer,
            reference_contexts=it.reference_contexts,
            stratum=it.stratum,  # 【B2-4 新增】
        )
        for it in data.items
    ]
    db.add_all(rows)
    ds.item_count = (ds.item_count or 0) + len(rows)
    db.commit()
    return {"imported": len(rows), "item_count": ds.item_count}


def list_items(db: Session, dataset_id: int, page: int, size: int) -> dict:
    ensure_eval_tables()
    _get_dataset(db, dataset_id)
    q = db.query(EvalDatasetItem).filter(EvalDatasetItem.dataset_id == dataset_id)
    total = q.count()
    items = q.order_by(EvalDatasetItem.id.asc()).offset((page - 1) * size).limit(size).all()
    return {"total": total, "items": [item_to_dict(i) for i in items]}


# 【新增】删除数据集：级联删除样本；已生成的 eval_runs 运行记录保留不删
def delete_dataset(db: Session, dataset_id: int) -> dict:
    ensure_eval_tables()
    ds = _get_dataset(db, dataset_id)
    deleted_items = (
        db.query(EvalDatasetItem)
        .filter(EvalDatasetItem.dataset_id == dataset_id)
        .delete(synchronize_session=False)
    )
    db.delete(ds)
    db.commit()
    return {"deleted_dataset_id": dataset_id, "deleted_items": deleted_items}


# ---------------------------------------------------------------------------
# 【v1.13 新增】样本修正：补参考答案 / 删脏样本
# ---------------------------------------------------------------------------
# 背景（真实缺口）：`add_badcase_from_message` 刻意把 reference_answer 写成空串
# （填 AI 的错误答案会让 RAGAS 把幻觉当标准答案反过来打分），但旧实现没有任何
# 接口能把答案补上 —— 样本永久缺参照，context_recall 永远算不出来，而前端点踩
# 成功的提示还写着「可在评测页补充参考答案」。这两个接口补齐这一步。

_AUDIT_TEXT_PREVIEW = 500


def _audit(
    db: Session, user, action: str, dataset_id: int, item_id: int, detail: dict
) -> None:
    """写操作审计（best-effort）：失败只告警，绝不回滚已提交的业务变更。"""
    try:
        db.add(
            OperationLog(
                user_id=getattr(user, "id", None),
                action=action,
                target_type="eval_dataset_item",
                target_id=str(item_id),
                detail={"dataset_id": dataset_id, **detail},
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001 - 审计失败不允许影响主流程
        db.rollback()
        logger.warning(
            "评测样本审计写入失败 action=%s item_id=%s", action, item_id, exc_info=True
        )


def _get_item(db: Session, dataset_id: int, item_id: int) -> EvalDatasetItem:
    """按 (数据集, 样本) 二元组取样本，做归属校验。"""
    item = db.get(EvalDatasetItem, item_id)
    # 归属校验：不允许借 A 数据集的路径改 B 数据集的样本
    if not item or item.dataset_id != dataset_id:
        raise NotFoundError("评测样本不存在")
    return item


def update_item(db: Session, dataset_id: int, item_id: int, data, user=None) -> dict:
    """局部更新样本（PATCH 语义）：只写请求体里显式出现的字段。

    - `reference_answer` 出现时必须非空（null / 空串 / 纯空白一律 400），
      理由见上方背景注释：留空样本等于让 context_recall 永久失效；
    - 未出现的字段保持原值，避免「只想补答案」却把 stratum 清空的覆盖式更新坑；
    - 参考答案属黄金集基准，变更写 operation_logs 留痕（含新旧值前 500 字）。
    """
    ensure_eval_tables()
    _get_dataset(db, dataset_id)
    item = _get_item(db, dataset_id, item_id)

    fields = data.model_dump(exclude_unset=True)
    if not fields:
        raise BizError(400, "没有需要更新的字段")
    if "reference_answer" in fields:
        answer = (fields["reference_answer"] or "").strip()
        if not answer:
            raise BizError(
                400, "参考答案不能置空：留空会导致该样本的 context_recall 无法计算"
            )
        fields["reference_answer"] = answer

    old_answer = item.reference_answer or ""
    old_stratum = item.stratum
    for key, value in fields.items():
        setattr(item, key, value)
    db.commit()
    db.refresh(item)

    detail: dict = {"fields": sorted(fields), "old_stratum": old_stratum, "new_stratum": item.stratum}
    if "reference_answer" in fields:
        detail["old_reference_answer"] = old_answer[:_AUDIT_TEXT_PREVIEW]
        detail["new_reference_answer"] = (item.reference_answer or "")[:_AUDIT_TEXT_PREVIEW]
        detail["old_len"] = len(old_answer)
        detail["new_len"] = len(item.reference_answer or "")
    _audit(db, user, "update_eval_item", dataset_id, item_id, detail)
    return item_to_dict(item)


def delete_item(db: Session, dataset_id: int, item_id: int, user=None) -> dict:
    """删除单条脏样本，并重算所属数据集的 item_count。

    item_count 用实际行数重算而非 `-= 1`：历史数据里「badcase 增量」「批量导入」
    「删集」多条路径都动过这个计数，以 COUNT 为准可顺带修掉历史漂移。
    """
    ensure_eval_tables()
    ds = _get_dataset(db, dataset_id)
    item = _get_item(db, dataset_id, item_id)
    snapshot = {
        "question": (item.question or "")[:200],
        "reference_answer": (item.reference_answer or "")[:_AUDIT_TEXT_PREVIEW],
    }
    db.delete(item)
    db.flush()
    ds.item_count = (
        db.query(EvalDatasetItem).filter(EvalDatasetItem.dataset_id == dataset_id).count()
    )
    db.commit()
    db.refresh(ds)
    _audit(db, user, "delete_eval_item", dataset_id, item_id, snapshot)
    return {"deleted_item_id": item_id, "item_count": ds.item_count}


# ---------------------------------------------------------------------------
# 【v1.9 数据飞轮】点踩 -> 评测集 badcase
# ---------------------------------------------------------------------------
# 集合名固定，按知识库聚合：同一 KB 只有一份「用户反馈Badcase」，
# 重复上报同一问题不重复入库（幂等），便于把用户侧坏例子持续沉淀成回归集。
BAD_CASE_DATASET_NAME = "用户反馈Badcase"
BAD_CASE_STRATUM = "feedback_badcase"


def _get_or_create_badcase_dataset(db: Session, kb_id: int, user) -> tuple[EvalDataset, bool]:
    """取该知识库的 badcase 集合，不存在则按固定名创建（同 KB 唯一）。

    返回 (数据集, 是否本次新建)，便于接口回显「集合是刚建出来的」。
    """
    ds = (
        db.query(EvalDataset)
        .filter(EvalDataset.kb_id == kb_id, EvalDataset.name == BAD_CASE_DATASET_NAME)
        .order_by(EvalDataset.id.asc())
        .first()
    )
    if ds:
        return ds, False
    ds = EvalDataset(
        kb_id=kb_id,
        name=BAD_CASE_DATASET_NAME,
        description="由用户点踩「一键加入评测集」自动沉淀的 badcase，参考答案需人工补全后再跑评测",
        item_count=0,
        created_by=user.id,
    )
    db.add(ds)
    db.flush()  # 拿到自增 id，供本次样本写入
    return ds, True


def _question_before(db: Session, msg) -> str:
    """取该条 AI 回复之前最近的一条用户提问（同会话、id 更小）。"""
    from ..models import Message

    prev = (
        db.query(Message)
        .filter(
            Message.conversation_id == msg.conversation_id,
            Message.role == "user",
            Message.id < msg.id,
        )
        .order_by(Message.id.desc())
        .first()
    )
    return (prev.content or "").strip() if prev else ""


def _mark_dislike(db: Session, user, message_id: int, reason: str | None) -> None:
    """点踩原因写进已有反馈表（复用 message_feedbacks，不新增列）。"""
    from ..models import MessageFeedback

    fb = (
        db.query(MessageFeedback)
        .filter(MessageFeedback.message_id == message_id, MessageFeedback.user_id == user.id)
        .first()
    )
    if fb:
        fb.rating = -1
        if reason:
            fb.comment = reason[:500]
    else:
        db.add(MessageFeedback(
            message_id=message_id, user_id=user.id, rating=-1,
            comment=reason[:500] if reason else None,
        ))


def add_badcase_from_message(
    db: Session, user, message_id: int, reason: str | None = None
) -> dict:
    """把一条被点踩的 AI 回答沉淀为评测集样本（数据飞轮入口）。

    设计取舍：
    - **只能上报自己的消息**（msg.user_id 校验），且写入的集合名固定，不接受任意
      dataset_id，避免普通用户借「上报」越权往别人的数据集塞样本；
    - **不新增数据库列**：点踩原因复用 message_feedbacks.comment 落库，
      评测样本复用 eval_dataset_items 现有字段；
    - `reference_answer` 刻意留空而不是填 AI 的错误答案 —— 填错答案会让 RAGAS
      把幻觉当成标准答案反过来打分。留空时 faithfulness / answer_relevancy /
      context_precision 仍可计算，仅 context_recall 缺参照，需人工补参考答案后再跑；
    - 同一问题重复上报按幂等处理（返回 duplicated=True），不重复入库。
    """
    from ..models import Conversation, Message

    ensure_eval_tables()
    msg = db.get(Message, message_id)
    if not msg or msg.user_id != user.id:
        raise NotFoundError("消息不存在")
    if msg.role != "assistant":
        raise BizError(400, "只能对 AI 回答上报 badcase")

    conv = db.get(Conversation, msg.conversation_id)
    if not conv:
        raise NotFoundError("会话不存在")
    if not conv.kb_id:
        raise BizError(400, "该会话未绑定知识库，无法加入评测集（评测集必须归属于某个知识库）")

    question = _question_before(db, msg)
    if not question:
        raise BizError(400, "未找到与该回答对应的用户提问")

    ds, created_dataset = _get_or_create_badcase_dataset(db, conv.kb_id, user)

    existed = (
        db.query(EvalDatasetItem)
        .filter(EvalDatasetItem.dataset_id == ds.id, EvalDatasetItem.question == question)
        .first()
    )
    _mark_dislike(db, user, message_id, reason)

    if existed:
        db.commit()
        return {
            "dataset_id": ds.id,
            "dataset_name": ds.name,
            "item_id": existed.id,
            "duplicated": True,
            "created_dataset": created_dataset,
            "needs_reference": not (existed.reference_answer or "").strip(),
            "message_id": message_id,
            "reason_saved": bool(reason),
        }

    item = EvalDatasetItem(
        dataset_id=ds.id,
        question=question,
        reference_answer="",  # 待人工补全，见 docstring 第 3 条
        reference_contexts=None,
        stratum=BAD_CASE_STRATUM,
    )
    db.add(item)
    ds.item_count = (ds.item_count or 0) + 1
    db.commit()
    db.refresh(item)
    logger.info(
        "点踩样本入评测集 dataset_id=%s item_id=%s message_id=%s", ds.id, item.id, message_id
    )
    return {
        "dataset_id": ds.id,
        "dataset_name": ds.name,
        "item_id": item.id,
        "duplicated": False,
        "created_dataset": created_dataset,
        "needs_reference": True,
        "message_id": message_id,
        "reason_saved": bool(reason),
    }


# 【新增】删除评测运行记录：级联删除该 run 的全部得分明细
def delete_run(db: Session, run_id: int) -> dict:
    ensure_eval_tables()
    run = db.get(EvalRun, run_id)
    if not run:
        raise NotFoundError("评测运行不存在")
    # 【v1.13】运行中的任务禁止删除：后台线程收尾时 db.get 会拿到 None，既写不进
    # 最终状态，中途 _flush_scores 还会往已删除的 run_id 插孤儿得分（脏数据）。
    # 要求先「停止」再删除 —— 停止是协作式的，等它真的落地再删才安全。
    if run.status in (RUN_PENDING, RUN_RUNNING):
        raise BizError(400, "任务正在运行，请先「停止」再删除")
    deleted_scores = (
        db.query(EvalRunScore)
        .filter(EvalRunScore.run_id == run_id)
        .delete(synchronize_session=False)
    )
    db.delete(run)
    db.commit()
    return {"deleted_run_id": run_id, "deleted_scores": deleted_scores}


# ---------------------------------------------------------------------------
# 评测运行
# ---------------------------------------------------------------------------

def create_run(db: Session, user, data) -> dict:
    """创建评测运行（pending），并启动后台线程异步执行。

    【新增】rag_config 可选：传入则持久化到 eval_runs.rag_config 并在评测执行时
    作为请求级临时参数透传问答链路；不传（None）沿用知识库自身配置，兼容旧调用。
    """
    ensure_eval_tables()
    ds = _get_dataset(db, data.dataset_id)
    # 【v1.13】并发闸：批量执行会连续调用本函数 M 次，每次起一个后台线程，
    # 线程内部还有 eval_max_concurrency 个 worker，实际并发 = M × N 路打同一个
    # 上游 key。不设闸的话「批量」不是效率功能而是 429 放大器。
    # 放在数据集校验之后：数据集不存在时应报 404 而不是 429，错误信息更准。
    #
    # 刻意**不做"撞闸即自动清理僵尸"**：那个版本我实现过并跑挂了 —— 一个 30 样本
    # 的任务跑过 10 分钟（EVAL_STALE_RUN_SECONDS 默认值）很正常，此时任何一次撞闸
    # 都会把它打成 failed，而它的线程其实还活着、跑完又写回 done，界面就会出现
    # 「失败了 → 又完成了」的鬼畜。在创建任务的路径上偷偷改别的任务状态，违反
    # 最小惊讶原则。僵尸清理改为：启动期自动 + 管理员显式触发 cleanup 接口。
    limit = settings.eval_max_active_runs
    active = _count_active_runs(db)
    if active >= limit:
        raise BizError(
            429,
            f"同时运行的评测任务已达上限 {limit} 个（当前 pending/running = {active}）。"
            f"请等待任务结束、或先停止部分任务；若确认界面上并没有任务在跑，"
            f"说明存在进程异常退出留下的僵尸任务，调用 cleanup-stale 接口清理后再试。",
        )
    # 【新增】仅持久化显式传入的键（exclude_none），未传字段不写死 null 值
    rag_config = (
        data.rag_config.model_dump(exclude_none=True) if getattr(data, "rag_config", None) else None
    )
    run = EvalRun(
        dataset_id=ds.id,
        kb_id=ds.kb_id,
        # 【v1.19.x 修复】记录「创建时实际会用的生成模型」：runtime_config（系统配置页）
        # 优先级高于 .env 的 settings.llm_model，配置页首次保存后两者即分叉。
        # 旧实现只记 settings 启动快照，导致界面模型列恒显 .env 默认值（deepseek-chat），
        # 与 trace_logs 里真实调用记录对不上。
        model=runtime_config.get_value("llm_model")
        or settings.llm_model
        or "deepseek-chat",
        status=RUN_PENDING,
        total_items=ds.item_count,
        success_items=0,
        rag_config=rag_config,  # 【新增】
        created_by=user.id,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    # 异步后台执行，不阻塞 HTTP 响应
    threading.Thread(target=_run_eval_background, args=(run.id,), daemon=True).start()
    return run_to_dict(run)


def _count_active_runs(db: Session) -> int:
    """当前占用并发名额的任务数（pending + running）。

    【v1.13】单独抽出来是为了撞闸自救时能重算一次 —— 清理僵尸任务之后必须
    重新数，否则还是会用清理前的旧值把用户挡在门外。
    """
    return (
        db.query(func.count(EvalRun.id))
        .filter(EvalRun.status.in_((RUN_PENDING, RUN_RUNNING)))
        .scalar()
        or 0
    )


def cancel_run(db: Session, run_id: int, user=None) -> dict:
    """停止评测任务（v1.13 新增）。

    语义是**协作式取消**，不是硬中断：这里只把 DB 状态改成 cancelled，真正的停
    发生在后台线程的下一个样本检查点（_is_cancel_requested）。所以本接口立即返回，
    任务可能还会跑完当前正在处理的那一条（LLM 调用是同步阻塞的，没有安全的中断手段）。
    返回体里带上 success_items，前端据此提示「已停止，保留已完成 N 条」。

    重复调用是幂等的：已经 cancelled 时直接返回当前状态而不报错 —— 前端可能因
    网络重试而重复发，回 400 反而让人以为没停下，进而反复点。
    """
    ensure_eval_tables()
    run = db.get(EvalRun, run_id)
    if not run:
        raise NotFoundError("评测运行不存在")
    if run.status == RUN_CANCELLED:
        return run_to_dict(run)
    if run.status in (RUN_DONE, RUN_FAILED):
        raise BizError(400, f"任务已结束（{run.status}），无法停止")

    run.status = RUN_CANCELLED
    db.commit()
    db.refresh(run)
    logger.info("评测任务已请求停止 run_id=%s 已完成=%s", run_id, run.success_items)

    # 审计：谁在什么时候停掉了哪个任务（best-effort，失败不影响已提交的状态变更）
    try:
        db.add(
            OperationLog(
                user_id=getattr(user, "id", None),
                action="eval_run_cancel",
                target_type="eval_run",
                target_id=str(run_id),
                detail={
                    "dataset_id": run.dataset_id,
                    "success_items": run.success_items,
                    "total_items": run.total_items,
                },
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.warning("评测取消审计写入失败 run_id=%s", run_id, exc_info=True)

    return run_to_dict(run)


def list_runs(db: Session, page: int, size: int, status: str | None = None) -> dict:
    ensure_eval_tables()
    q = db.query(EvalRun)
    if status:
        q = q.filter(EvalRun.status == status)
    total = q.count()
    items = q.order_by(EvalRun.id.desc()).offset((page - 1) * size).limit(size).all()
    return {"total": total, "items": [run_to_dict(r) for r in items]}


def run_stats(db: Session) -> dict:
    """大盘统计（v1.13.2 新增）：任务状态计数 + 四项指标均值。

    ## 为什么必须搬到后端
    原实现是前端 `listRuns({page:1, size:100})` 拉第一页再自己 reduce 求平均：
      - 任务数超过 100 之后，大盘只统计了最新的 100 条，**数字静默变小且不报错**；
      - 且不筛 status，failed / cancelled 的 run 也混进均值。
    这类「不报错但结果错」的偏差最难被发现，也是最不该出现在评测系统里的。

    ## 性能
    两条查询，**完全不碰 eval_run_scores**（样本表会到百万行量级）：
      1. 状态计数 → SQL GROUP BY，返回行数 = 状态种类数（≤5），与任务数无关；
      2. 指标均值 → 只取 done 任务的 (metrics, success_items) 两列在 Python 侧聚合，
         行数 = 已完成任务数。比 `AVG()` 跨样本表扫全表便宜一个数量级。

    ## 统计口径（刻意如此，不是随手写的）
    - **只统计 done**：failed / cancelled 样本数不齐，我们自己在 error_msg 里写的就是
      「指标仅代表已完成样本，不可与完整运行做 A/B 对比」。若混进大盘，取消一个大任务
      就会让整体分数掉下去，而系统能力其实没变 —— 那是「统计口径造成的假退化」。
    - **按 success_items 加权**，不是 run 级简单平均：否则一个 2 条样本的冒烟 run 和
      一个 30 条样本的黄金集 run 等权，大盘会被冒烟任务带偏。
    - **某指标为 null 时该项不进分母**（如缺参考答案导致 context_recall 算不出），
      所以四项的分母可能不同，各自在 metric_items 里给出，前端可核对。
    - **一条数据都没有就返回 None**，由前端显示「—」。绝不返回 0 —— 0 分是「测出很差」，
      None 是「没测」，两者语义天差地别。
    """
    ensure_eval_tables()

    counts = dict(
        db.query(EvalRun.status, func.count(EvalRun.id)).group_by(EvalRun.status).all()
    )
    done = int(counts.get(RUN_DONE, 0))

    rows = (
        db.query(EvalRun.metrics, EvalRun.success_items)
        .filter(EvalRun.status == RUN_DONE)
        .all()
    )

    weighted_sum: dict[str, float] = {k: 0.0 for k in _COMPARE_METRICS}
    weights: dict[str, int] = {k: 0 for k in _COMPARE_METRICS}
    scored_runs = 0   # 真正贡献了至少一项指标的 done 任务数
    for metrics, success in rows:
        if not isinstance(metrics, dict):
            continue
        weight = int(success or 0)
        if weight <= 0:
            # done 但 success_items=0：可能全部走 direct/report 被跳过（不计分），
            # 也可能是历史数据没写计数。没有权重可加，也不补默认值（那就是编数据）。
            continue
        hit = False
        for key in _COMPARE_METRICS:
            value = metrics.get(key)
            # bool 是 int 子类，显式排除，避免 True 被当成 1.0 计入
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            weighted_sum[key] += float(value) * weight
            weights[key] += weight
            hit = True
        if hit:
            scored_runs += 1

    metrics_out: dict[str, float | None] = {}
    for key in _COMPARE_METRICS:
        # 分母为 0 → None（前端显示「—」），不是 0
        metrics_out[key] = (
            round(weighted_sum[key] / weights[key], 6) if weights[key] else None
        )

    return {
        "total": sum(int(v) for v in counts.values()),
        "pending": int(counts.get(RUN_PENDING, 0)),
        "running": int(counts.get(RUN_RUNNING, 0)),
        "done": done,
        "failed": int(counts.get(RUN_FAILED, 0)),
        "cancelled": int(counts.get(RUN_CANCELLED, 0)),
        "metrics": metrics_out,
        "metric_items": weights,          # 每项指标的样本级分母，前端可显「基于 N 条样本」
        "metric_runs": scored_runs,       # 参与计算的任务数（分母的任务级视角）
        "scope": "done_weighted",         # 口径标识：仅已完成任务，按成功样本数加权
    }


def get_run(db: Session, run_id: int) -> dict:
    ensure_eval_tables()
    run = db.get(EvalRun, run_id)
    if not run:
        raise NotFoundError("评测运行不存在")
    return run_to_dict(run)


def list_scores(db: Session, run_id: int, page: int, size: int) -> dict:
    ensure_eval_tables()
    if not db.get(EvalRun, run_id):
        raise NotFoundError("评测运行不存在")
    q = db.query(EvalRunScore).filter(EvalRunScore.run_id == run_id)
    total = q.count()
    items = q.order_by(EvalRunScore.id.asc()).offset((page - 1) * size).limit(size).all()
    return {"total": total, "items": [score_to_dict(s) for s in items]}


# 【B1-4 新增】BadCase 筛选支持的指标列白名单（与 eval_run_scores 可空指标列一一对应）
_BADCASE_METRIC_COLUMNS = {
    "faithfulness": EvalRunScore.faithfulness,
    "answer_relevancy": EvalRunScore.answer_relevancy,
    "context_precision": EvalRunScore.context_precision,
    "context_recall": EvalRunScore.context_recall,
}


def list_badcases(
    db: Session,
    run_id: int,
    metric: str,
    threshold: float,
    page: int,
    page_size: int,
) -> dict:
    """【B1-4 新增】BadCase 后端分页查询：指定指标低于阈值的样本列表。

    - 筛选在后端完成（SQL 侧），不再把全量分数丢给前端计算；
    - ground_truth 来自评测数据集 reference_answer（eval_run_scores 无该列），
      按 item_id LEFT JOIN eval_dataset_items 补齐；数据集被删或 item 缺失时为 null；
    - 空值口径遵循 BUG-4 统一规则：SQL NULL（JSON null）不参与阈值筛选
      （显式 is_not(None) 前置），即"未测出"的指标既不算好也不算坏；
    - 排序：指标值升序（最差的排最前），同值按 id 升序保证分页稳定。
    """
    ensure_eval_tables()
    if not db.get(EvalRun, run_id):
        raise NotFoundError("评测运行不存在")

    metric_col = _BADCASE_METRIC_COLUMNS[metric]  # 入参白名单校验由路由层完成
    base_q = (
        db.query(EvalRunScore, EvalDatasetItem.reference_answer)
        .outerjoin(EvalDatasetItem, EvalRunScore.item_id == EvalDatasetItem.id)
        .filter(EvalRunScore.run_id == run_id)
    )
    # 统一空值口径：NULL 不参与比较；仅取 metric < threshold 的样本
    filtered_q = base_q.filter(metric_col.is_not(None), metric_col < threshold)

    total = filtered_q.count()
    rows = (
        filtered_q
        .order_by(metric_col.asc(), EvalRunScore.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items = []
    for score, ground_truth in rows:
        row = score_to_dict(score)
        row["ground_truth"] = ground_truth
        items.append(row)
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "metric": metric,
        "threshold": threshold,
        "items": items,
    }


# ---------------------------------------------------------------------------
# A/B 对比聚合（B1-5 新增：对齐/差值/汇总计算全部在后端完成）
# ---------------------------------------------------------------------------

# 参与对比的四项 RAGAS 指标（与 eval_run_scores 列、RAGAS 指标一一对应）
_COMPARE_METRICS = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")
# 差异样本判定阈值：任一指标 |Δ| > 0.1 即标记（与前端 utils/abCompare.ts 的 DIFF_THRESHOLD 对齐）
COMPARE_DIFF_THRESHOLD = 0.1


def _build_compare_row(score_a, score_b) -> dict:
    """构造单条逐样本对比行：question 对齐 + 四项指标差值。

    对齐与差值口径与前端 utils/abCompare.ts 完全一致（后端计算后前端可直接渲染）：
    - 两侧该指标均为非空才计算 Δ = b - a，否则 Δ 为 null；
    - 任一指标 |Δ| > COMPARE_DIFF_THRESHOLD 即 is_diff=true。
    """
    diffs: dict[str, float | None] = {}
    is_diff = False
    for k in _COMPARE_METRICS:
        va = getattr(score_a, k) if score_a is not None else None
        vb = getattr(score_b, k) if score_b is not None else None
        if va is None or vb is None:
            diffs[k] = None
        else:
            diffs[k] = round(vb - va, 6)
            if abs(diffs[k]) > COMPARE_DIFF_THRESHOLD:
                is_diff = True
    return {
        "question": (score_a or score_b).question,
        "a": score_to_dict(score_a) if score_a is not None else None,
        "b": score_to_dict(score_b) if score_b is not None else None,
        "diffs": diffs,
        "is_diff": is_diff,
    }


def compare_runs(db: Session, run_id_a: int, run_id_b: int) -> dict:
    """【B1-5 新增】A/B 两两对比聚合：样本对齐 + 指标差值 + 汇总，全部后端计算。

    - 前置校验：两个 run 必须存在且属于同一 dataset_id（不同数据集无法对齐样本）；
    - 样本对齐：question trim 后精确匹配（与前端 alignScores 口径一致），
      仅出现在单侧的样本保留展示（另一侧为 null），便于发现缺失样本；
    - 汇总指标：优先使用 run.metrics（评测落库均值），缺省时由对齐后的得分
      按 _mean_or_none（仅非空值）现算，口径与 BUG-4 统一空值规则一致；
    - 纯查询不落库，登录用户可读。
    """
    ensure_eval_tables()
    run_a = db.get(EvalRun, run_id_a)
    run_b = db.get(EvalRun, run_id_b)
    if not run_a:
        raise NotFoundError(f"评测运行不存在 run_id={run_id_a}")
    if not run_b:
        raise NotFoundError(f"评测运行不存在 run_id={run_id_b}")
    if run_a.dataset_id != run_b.dataset_id:
        raise BizError(400, f"两次评测运行不属于同一数据集（{run_a.dataset_id} vs {run_b.dataset_id}），无法对比")

    scores_a = db.query(EvalRunScore).filter(EvalRunScore.run_id == run_id_a).all()
    scores_b = db.query(EvalRunScore).filter(EvalRunScore.run_id == run_id_b).all()

    map_b = {s.question.strip(): s for s in scores_b}
    used_b: set[int] = set()
    rows: list[dict] = []
    for sa in scores_a:
        sb = map_b.get(sa.question.strip())
        if sb is not None:
            used_b.add(sb.id)
        rows.append(_build_compare_row(sa, sb))
    # 仅在 B 侧出现的 question（同数据集理论上不会出现，兜底保留展示）
    for sb in scores_b:
        if sb.id not in used_b:
            rows.append(_build_compare_row(None, sb))

    def _summary_side(run: EvalRun, scores: list) -> dict:
        """单侧汇总：优先落库的 run.metrics，缺省时由得分现算（仅非空值均值）。"""
        if run.metrics is not None:
            return {k: run.metrics.get(k) for k in _COMPARE_METRICS}
        return {k: _mean_or_none([getattr(s, k) for s in scores]) for k in _COMPARE_METRICS}

    summary_a = _summary_side(run_a, scores_a)
    summary_b = _summary_side(run_b, scores_b)
    summary = [
        {
            "metric": k,
            "avg_a": summary_a[k],
            "avg_b": summary_b[k],
            "diff": (
                round(summary_b[k] - summary_a[k], 6)
                if summary_a[k] is not None and summary_b[k] is not None
                else None
            ),
        }
        for k in _COMPARE_METRICS
    ]
    return {
        "run_a": run_to_dict(run_a),
        "run_b": run_to_dict(run_b),
        "diff_threshold": COMPARE_DIFF_THRESHOLD,
        "summary": summary,
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# 分层难例 stratum 聚合（B2-4 新增）
# ---------------------------------------------------------------------------

def get_stratum_metrics(db: Session, run_id: int) -> dict:
    """【B2-4 新增】按 stratum 分层聚合四项 RAGAS 指标（GET /runs/{run_id}/stratum_metrics）。

    - 聚合口径：SQL AVG 只统计非 NULL 值，与 _mean_or_none（BUG-4 统一空值规则）等价，
      某层全为 NULL 时该指标返回 null；
    - stratum 为 NULL 的样本归入"未分层"组（stratum 字段返回 null），含历史数据
      与数据集样本已被删除（LEFT JOIN 未命中）两类情况；
    - 返回 {run_id, total, items:[{stratum, sample_count, metrics}]}，按 stratum 升序。
    """
    ensure_eval_tables()
    if not db.get(EvalRun, run_id):
        raise NotFoundError("评测运行不存在")

    rows = (
        db.query(
            EvalDatasetItem.stratum,
            func.count(EvalRunScore.id),
            func.avg(EvalRunScore.faithfulness),
            func.avg(EvalRunScore.answer_relevancy),
            func.avg(EvalRunScore.context_precision),
            func.avg(EvalRunScore.context_recall),
        )
        .outerjoin(EvalDatasetItem, EvalRunScore.item_id == EvalDatasetItem.id)
        .filter(EvalRunScore.run_id == run_id)
        .group_by(EvalDatasetItem.stratum)
        .order_by(EvalDatasetItem.stratum.asc())
        .all()
    )

    def _avg6(value) -> float | None:
        """SQL AVG 结果规整：NULL 透传，非空保留 6 位小数（与 _mean_or_none 口径一致）。"""
        return round(float(value), 6) if value is not None else None

    items = []
    for stratum, cnt, f, ar, cp, cr in rows:
        items.append({
            "stratum": stratum,  # null = 未分层（含历史数据 / 数据集样本已删除）
            "sample_count": int(cnt),
            "metrics": {
                "faithfulness": _avg6(f),
                "answer_relevancy": _avg6(ar),
                "context_precision": _avg6(cp),
                "context_recall": _avg6(cr),
            },
        })
    return {"run_id": run_id, "total": len(items), "items": items}


# ---------------------------------------------------------------------------
# 后台评测执行
# ---------------------------------------------------------------------------

# 【新增】rag_config 中可请求级生效的键集合（与 agent.graph.run_agent options 白名单对齐）：
# top_k / search_mode 均随请求透传到问答检索链路（vector_only / bm25_vector_hybrid）；
# chunk_size 属于文档入库切片参数，查询期无法生效，仅持久化用于实验记录展示。
_RAG_OPTIONS_KEYS = ("top_k", "search_mode")


def _build_agent_options(rag_config: dict | None) -> dict | None:
    """【新增】把持久化的 rag_config 转换为 run_agent 的请求级 options。

    rag_config 为 None（旧任务/未传配置）时返回 None，问答链路走知识库原生配置。
    """
    if not rag_config:
        return None
    options = {k: rag_config[k] for k in _RAG_OPTIONS_KEYS if rag_config.get(k) is not None}
    return options or None


def _load_kb_detached(kb_id: int | None) -> KnowledgeBase | None:
    """【新增】加载知识库并脱离会话（expunge）。

    并发评测的工作线程需要只读访问 kb.top_k 等属性；
    脱离会话后属性已固化在实例上，跨线程读取不会触发懒加载，天然线程安全。
    """
    if kb_id is None:
        return None
    db = SessionLocal()
    try:
        kb = db.get(KnowledgeBase, kb_id)
        if kb is not None:
            db.expunge(kb)
        return kb
    finally:
        db.close()


class _SampleSpec(NamedTuple):
    """【新增】评测样本的纯值快照（脱离 ORM 会话，供并发线程安全访问）。"""

    id: int
    question: str
    reference_answer: str


def _retrieve_contexts(
    db: Session | None, kb: KnowledgeBase | None, route: str, question: str,
    top_k: int | None = None, search_mode: str | None = None,
) -> list[str]:
    """按路由重新获取检索上下文，供 RAGAS 指标计算。

    【新增】top_k / search_mode 参数：rag_config 传入时优先生效，
    否则回退知识库配置 kb.top_k 与默认混合检索，保证打分上下文与问答检索口径一致。
    【新增】kb 改为传入预加载的脱离对象（并发线程安全）；db 仅在 rag 检索时使用
    （由调用方提供本线程独立会话），db_query 路由不需要数据库。
    """
    if route == "rag":
        if kb is None:
            return []
        hits = retriever.hybrid_retrieve(
            db, kb, question, top_k or kb.top_k or 5,
            search_mode=search_mode,
        )
        return [h["content"] for h in hits]
    if route == "db_query":
        return [query_business_db(question)]
    return []


def _eval_one_sample_sync(item: _SampleSpec, ctx: dict) -> dict | None:
    """【新增】单样本评测（纯同步，串行/并发模式共用，保证分数计算口径完全一致）。

    - run_agent / RAGAS 调用参数与原串行实现逐字相同，指标结果不变；
    - 检索使用本线程独立会话（并发安全；测试中检索被替换为纯函数时不触库）；
    - 返回得分 dict；route 为 direct/report 时返回 None（不参与评测，与原逻辑一致）；
    - 写库（得分/进度/trace）不在此处发生：得分由主线程批量落库，trace 由
      TraceCallbackHandler.flush 在 run_agent 内部内存暂存、样本结束一次性写入。
    """
    start = time.time()
    trace_id = str(uuid.uuid4())
    result = run_agent([], item.question, ctx["kb_id"], trace_context={
        "trace_id": trace_id,
        "conversation_id": None,
        "user_id": ctx["user_id"],
        "kb_id": ctx["kb_id"],
        "question": item.question,
    }, options=ctx["agent_options"])
    route = result.get("route", "direct")
    if route not in ("rag", "db_query"):
        # 【B1-6 BUG-5 修复】direct/report 路由不参与评测是既定约束，但此前静默返回 None，
        # 样本"无声消失"难以排查；这里显式记录日志后再跳过，保证跳过行为可审计。
        logger.warning(
            "评测样本被跳过（route=%s 不参与评测） run_id=%s item_id=%s question=%s",
            route, ctx["run_id"], item.id, item.question[:50],
        )
        return None  # 约束：report/direct 跳过评测

    contexts: list[str] = []
    if route == "rag":
        db = SessionLocal()
        try:
            contexts = _retrieve_contexts(
                db, ctx["kb"], route, item.question,
                top_k=ctx["rag_top_k"], search_mode=ctx["rag_search_mode"],
            )
        finally:
            db.close()
    else:
        contexts = _retrieve_contexts(None, ctx["kb"], route, item.question)

    scores = ctx["evaluator"].evaluate_one(item.question, result["answer"], contexts, item.reference_answer)
    return {
        "run_id": ctx["run_id"],
        "item_id": item.id,
        "question": item.question,
        "answer": result["answer"],
        "retrieved_contexts": contexts,
        "faithfulness": scores["faithfulness"],
        "answer_relevancy": scores["answer_relevancy"],
        "context_precision": scores["context_precision"],
        "context_recall": scores["context_recall"],
        "latency_ms": int((time.time() - start) * 1000),
    }


def _eval_sample_with_retry(item: _SampleSpec, ctx: dict) -> tuple[str, object]:
    """【新增】单样本评测 + 失败自动重试（异常保护：失败不打断整体任务）。

    返回 (kind, payload)，kind ∈ {"ok", "skipped", "failed"}：
    - ok:      payload 为得分 dict；
    - skipped: payload 为 None（direct/report 路由，不参与评测）；
    - failed:  payload 为失败明细字符串（写入 run 错误摘要）。
    """
    attempts = max(1, int(ctx.get("retry_attempts", 1)))
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            row = _eval_one_sample_sync(item, ctx)
            return ("skipped", None) if row is None else ("ok", row)
        except RagasScoringUnavailable as exc:
            # 【v1.17】配置性故障（判分模型额度/密钥/模型名）不重试：
            # 同一条 403 再打 N 遍只会让用户多等，且掩盖真正的失败原因。
            logger.error(
                "评测样本判分不可用(不重试) run_id=%s item_id=%s: %s",
                ctx["run_id"], item.id, exc,
            )
            return ("failed", f"item#{item.id}({item.question[:30]}): {exc}")
        except Exception as exc:  # noqa: BLE001 - 单样本任何异常都不允许打断整体评测
            last_exc = exc
            logger.warning(
                "评测样本失败(第%s/%s次) run_id=%s item_id=%s: %s",
                attempt, attempts, ctx["run_id"], item.id, exc,
            )
            if attempt < attempts:
                time.sleep(min(2.0, 0.5 * attempt))  # 线性退避，缓解上游限流
    return ("failed", f"item#{item.id}({item.question[:30]}): {str(last_exc)[:200]}")


def _flush_scores(db: Session, rows: list[dict]) -> None:
    """【新增】把内存暂存的得分批量一次性写入（减少频繁 commit）。"""
    if not rows:
        return
    db.add_all([EvalRunScore(**row) for row in rows])
    db.commit()
    rows.clear()


def _update_progress(db: Session, run_id: int, success: int, processed: int = 0, failed: int = 0) -> None:
    """【新增】实时进度上报：每处理完一个样本即轻量 UPDATE 进度字段，
    前端轮询任务列表即可看到并发执行进度。失败仅告警，不影响评测。

    【v1.13.1】三个计数一起写：只有 success 的话，「跳过」和「失败」的样本在前端
    看来等于没跑，进度条会停在 90% 不动（run 37 的 27/30 就是这个观感）。
    """
    try:
        db.query(EvalRun).filter(EvalRun.id == run_id).update(
            {
                "success_items": success,
                "processed_items": processed,
                "failed_items": failed,
            }
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.warning("评测进度更新失败 run_id=%s", run_id, exc_info=True)


def _metrics_from_scores(db: Session, run_id: int) -> dict | None:
    """【v1.13.1 新增】从已落库的得分反算四项指标均值（无得分返回 None）。

    用途：任务因进程中断/异常被判 failed 时，之前已付费算完的样本成绩仍然有效，
    不能让它在列表里显示成「—」。同一套 `_mean_or_none` 口径，空值不算 0。
    """
    rows = db.query(EvalRunScore).filter(EvalRunScore.run_id == run_id).all()
    if not rows:
        return None
    sums: dict[str, list[float]] = {k: [] for k in _COMPARE_METRICS}
    for r in rows:
        for k in _COMPARE_METRICS:
            v = getattr(r, k, None)
            if v is not None:
                sums[k].append(v)
    return {k: _mean_or_none(v) for k, v in sums.items()}


class _EvalAborted(Exception):
    """内部控制流：配置性故障导致的熔断中止（v1.17）。

    与 `_RunCancelled` 的区别：这是**故障**，不是用户动作，收尾时 status=failed，
    且 error_msg 必须写清原因。与普通异常的区别：它带有「已跑样本全无效」的语义，
    收尾时不能把已有得分当成功（全空得分本来也不会进 success）。
    """


class _RunCancelled(Exception):
    """内部控制流：用户主动停止。

    刻意**不继承 BizError** —— 它不是接口错误，也不该写进 error_msg 的"失败"语义。
    cancelled ≠ failed：前者是人到点刹车，已完成部分指标真实可用；后者是故障，指标不可信。
    """


def _is_cancel_requested(run_id: int) -> bool:
    """采样查询取消标记（每个样本完成时调一次）。

    为什么必须用**独立短会话**：后台线程自己的会话在 MySQL InnoDB 默认的
    REPEATABLE READ 隔离级别下，同一事务内的重复读取的是同一个快照，外部会话
    提交的 cancelled 永远看不见。后台线程虽然频繁 commit，但两次提交之间仍是
    旧快照。独立会话每次开新快照，最稳；开销是一次主键点查，相对单样本的
    LLM 调用完全可以忽略。

    探测失败一律返回 False（按未取消处理）：宁可多跑一会儿，也不因为一次查询
    抖动就把正在正常执行的任务误杀 —— 误杀会直接丢掉用户已经付费的算力。
    """
    probe = None
    try:
        probe = SessionLocal()
        status = probe.query(EvalRun.status).filter(EvalRun.id == run_id).scalar()
        return status == RUN_CANCELLED
    except Exception:  # noqa: BLE001
        logger.warning("取消标记探测失败 run_id=%s（按未取消处理）", run_id, exc_info=True)
        return False
    finally:
        if probe is not None:
            probe.close()


def _run_concurrent(samples: list[_SampleSpec], ctx: dict, handle: Callable) -> None:
    """【新增】asyncio 并发调度样本评测。

    - asyncio 事件循环运行在评测后台线程内，同步的 run_agent/RAGAS 经
      run_in_executor 提交到 ThreadPoolExecutor 执行；
    - max_workers = EVAL_MAX_CONCURRENCY 即并发上限（防止上游 429 限流）；
    - as_completed 逐个回收结果：每完成一个样本立即回调 handle
      （批量落库 + 进度上报），整体耗时 ≈ ceil(N / 并发数) × 单样本耗时。

    【v1.13 取消】这里刻意**不用 `with ThreadPoolExecutor`**：
    with 的 __exit__ 固定等价于 `shutdown(wait=True)`，取消时即便 handle 抛出
    异常，也要等队列里所有已提交样本全部跑完才退出 —— 那等于没取消。
    改为 `shutdown(wait=False, cancel_futures=True)`：队列中尚未开跑的样本
    直接丢弃；已在跑的样本无法安全中断（同步阻塞的 LLM 调用没有协作式中断点，
    CPython 也没有安全杀线程的手段），它们会自然跑完，但结果被丢弃、不落库。
    """

    async def _amain() -> None:
        loop = asyncio.get_running_loop()
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=settings.eval_max_concurrency,
            thread_name_prefix="eval-worker",
        )
        futures: list[asyncio.Future] = []
        try:
            futures = [loop.run_in_executor(pool, _eval_sample_with_retry, s, ctx) for s in samples]
            for done in asyncio.as_completed(futures):
                handle(*(await done))
        finally:
            # 先断开 asyncio↔concurrent 的桥接再关池：目标 future 已 cancelled，
            # _chain_future 会直接忽略迟到的结果，避免事件循环关闭后
            # 「Event loop is closed」的噪音日志。
            for fut in futures:
                fut.cancel()
            pool.shutdown(wait=False, cancel_futures=True)

    asyncio.run(_amain())


def _run_eval_background(run_id: int) -> None:
    """后台评测主流程（独立线程 + 独立会话）。

    【新增】并发/批量改造（结果口径不变）：
    - EVAL_CONCURRENT_ENABLE=true 时样本级并发执行（EVAL_MAX_CONCURRENCY 上限保护）；
      false 时回退原串行逐条执行，逐字保持原行为；
    - 得分内存暂存、按 EVAL_SCORE_BATCH_SIZE 批量落库（原为逐条 commit）；
    - 每完成一个样本实时更新 success_items（前端轮询即可见并发进度）；
    - 单样本失败自动重试 EVAL_RETRY_ATTEMPTS 次，仍失败则记入 run.error_msg 摘要，
      不打断整体任务；trace 日志沿用 TraceCallbackHandler 的内存暂存 + 单样本
      一次性落库机制（flush），日志内容完整不丢失。
    """
    db = SessionLocal()
    run = None
    # 【v1.13.1】计数器与得分缓冲区提到 try 之外：异常路径（进程没死但主流程抛错）
    # 必须能把「已算完但还没落库」的样本抢救回来。此前它们定义在 try 内，
    # 一抛异常就随栈帧一起消失，9 条已付费得分全丢（真实库 run 33 即如此）。
    success = 0
    processed = 0
    failed = 0
    failures: list[str] = []
    score_buffer: list[dict] = []
    metric_sums: dict[str, list[float]] = {k: [] for k in _COMPARE_METRICS}
    try:
        # 【v1.13 取消】原子认领：只有仍是 pending 的任务才允许转 running。
        # 不能写成「get 出来改字段再 commit」——那与「用户刚创建就点停止」存在竞态：
        # 线程读到 pending 之后、写回之前用户已把它改成 cancelled，再写 running 就把
        # 取消覆盖掉了，用户会看到任务明明点了停止却还在跑。
        claimed = (
            db.query(EvalRun)
            .filter(EvalRun.id == run_id, EvalRun.status == RUN_PENDING)
            .update(
                {EvalRun.status: RUN_RUNNING, EvalRun.started_at: datetime.now()},
                synchronize_session=False,
            )
        )
        db.commit()
        if not claimed:
            # 任务不存在，或 pending 阶段就被取消掉了 —— 无需执行
            logger.info("评测任务未认领（不存在或已取消）run_id=%s", run_id)
            return

        run = db.get(EvalRun, run_id)
        if not run:
            return

        # Embedding 提供方预警：hash 提供方不具备语义相似度（仅离线联调用），
        # 会导致向量检索失效（context_recall/precision 趋近 0），且 RAGAS
        # answer_relevancy 同样使用该嵌入，分值为测量失真而非真实质量。
        if settings.embedding_provider == "hash":
            logger.warning(
                "评测 run_id=%s 使用 EMBEDDING_PROVIDER=hash（无语义能力）："
                "context_recall/context_precision/answer_relevancy 将不具参考性。"
                "请切换为语义 Embedding 并重建知识库向量后重评。",
                run_id,
            )

        # 【新增】预加载为纯值/脱离对象：工作线程绝不触碰主会话（跨线程会话不安全）
        items = (
            db.query(EvalDatasetItem)
            .filter(EvalDatasetItem.dataset_id == run.dataset_id)
            .order_by(EvalDatasetItem.id.asc())
            .all()
        )
        samples = [
            _SampleSpec(id=i.id, question=i.question, reference_answer=i.reference_answer)
            for i in items
        ]

        # 【B1-6 BUG-1 修复】空数据集不允许产出 done(0/0) 的"伪成功"记录：
        # 直接置为 failed 并写明原因，避免前端把 0/0 误读为评测通过。
        if not samples:
            run.status = RUN_FAILED
            run.error_msg = f"数据集为空（0 条样本），无法执行评测 dataset_id={run.dataset_id}"
            run.finished_at = datetime.now()
            db.commit()
            logger.warning(
                "评测任务因空数据集终止 run_id=%s dataset_id=%s", run_id, run.dataset_id
            )
            return

        kb = _load_kb_detached(run.kb_id)

        # 读取本次运行持久化的 rag_config；None 时 options 为 None，
        # run_agent 内部 (options or {}) 自然退化为知识库原生配置，兼容旧任务。
        agent_options = _build_agent_options(run.rag_config)
        # 【v1.17】开跑前探活判分模型。不可用就立刻带着可读原因失败。
        # 不探活的代价是真实的：run 63/64/65 判分模型 403（免费额度耗尽），
        # 每个样本仍完整跑完检索 + 生成 + 4 次判分调用（全部 403 被 ragas 吞成 NaN），
        # 最终 30/30 显示 done、指标列全「—」—— 用户花的钱和等的时间全白费，
        # 而界面上没有任何一处能看出是故障。一次探活几十 token，换掉这个黑洞。
        evaluator = RagasEvaluator()
        if settings.eval_preflight_enable:
            try:
                evaluator.preflight()
            except RagasScoringUnavailable as exc:
                run.status = RUN_FAILED
                run.success_items = 0
                run.processed_items = 0
                run.failed_items = 0
                run.metrics = None
                run.error_msg = f"评测未启动（判分链路不可用）：{exc}"[:1000]
                run.finished_at = datetime.now()
                db.commit()
                logger.error("评测未启动 run_id=%s: %s", run_id, exc)
                return

        ctx: dict = {
            "run_id": run_id,
            "kb_id": run.kb_id,
            "user_id": run.created_by,
            "kb": kb,
            "agent_options": agent_options,
            "rag_top_k": (agent_options or {}).get("top_k"),
            "rag_search_mode": (agent_options or {}).get("search_mode"),
            "evaluator": evaluator,
            "retry_attempts": settings.eval_retry_attempts,
        }

        batch_size = settings.eval_score_batch_size
        # 【v1.17】连续失败计数：用于「配置性故障」熔断（见 handle 末尾）
        consecutive_failures = 0

        def handle(kind: str, payload: object) -> None:
            """单样本结果回收（串行/并发统一入口）：计数、暂存得分、批量落库、进度上报。"""
            nonlocal success, processed, failed, consecutive_failures
            processed += 1
            if kind == "ok":
                row = payload
                assert isinstance(row, dict)
                score_buffer.append(row)
                success += 1
                consecutive_failures = 0
                for k in metric_sums:
                    if row[k] is not None:
                        metric_sums[k].append(row[k])
            elif kind == "failed":
                failed += 1
                consecutive_failures += 1
                failures.append(str(payload))
            # kind == "skipped"（direct/report 路由，不参与评测）：只计入 processed，
            # 既不算成功也不算失败 —— 否则进度条会漏掉这部分，任务跑完也差几条。

            # 【新增】得分达到批量阈值即一次性落库；每样本完成即上报实时进度
            if len(score_buffer) >= batch_size:
                _flush_scores(db, score_buffer)
            _update_progress(db, run_id, success, processed, failed)

            # 【v1.17】熔断：连续 N 个样本失败且一条都没成功 → 判定为配置性故障，立即中止。
            # 为什么是「连续 + 零成功」两个条件：偶发上游抖动不会连续命中，且一旦有过成功
            # 样本就证明判分链路是通的，此时继续跑完对用户仍有价值，不该被误杀。
            # 没有这道熔断，判分模型挂掉时用户会看着进度条走完 30 条样本才被告知失败。
            if success == 0 and consecutive_failures >= _EVAL_ABORT_CONSECUTIVE_FAILURES:
                raise _EvalAborted(
                    f"连续 {consecutive_failures} 条样本判分失败且无一成功，已中止以避免继续空跑。"
                    f"首个失败原因：{failures[0][:300]}"
                )

            # 【v1.13 取消】检查点放在**回收之后**而不是之前：
            # 样本的算力已经花掉了，先把它计入 success/score_buffer 再判断要不要停，
            # 这样收尾时 _flush_scores 能把已完成的结果保住，不会让用户白付钱。
            if _is_cancel_requested(run_id):
                raise _RunCancelled()

        cancelled = False
        aborted_reason: str | None = None
        try:
            if settings.eval_concurrent_enable and len(samples) > 1:
                _run_concurrent(samples, ctx, handle)
            else:
                # 串行模式（EVAL_CONCURRENT_ENABLE=false 或单样本）：与原实现等价的
                # 顺序执行，同样享有重试/批量落库/进度上报能力。
                # 取消由 handle 统一检查，这里不需要重复判断。
                for s in samples:
                    handle(*_eval_sample_with_retry(s, ctx))
        except _RunCancelled:
            cancelled = True
            logger.info(
                "评测被用户取消 run_id=%s 已完成=%s/%s", run_id, success, len(samples)
            )
        except _EvalAborted as exc:
            # 熔断：已完成部分可能为零（配置故障场景），收尾按 failed 处理并写清原因
            aborted_reason = str(exc)
            logger.error("评测熔断中止 run_id=%s 已处理=%s/%s: %s", run_id, processed, len(samples), exc)

        # 收尾：剩余得分批量落库 + 汇总落状态
        _flush_scores(db, score_buffer)
        run = db.get(EvalRun, run_id)
        if run is None:
            # 任务被删了的兜底（delete_run 已禁止删除运行中任务，正常走不到这里）
            logger.warning("评测任务已被删除，跳过收尾 run_id=%s", run_id)
            return
        if cancelled:
            # 已完成部分的指标仍然真实可用（用户可能就想要前 N 条），照常落库；
            # 但样本数不全，必须在 error_msg 里说清，避免被误当成完整运行做 A/B。
            run.status = RUN_CANCELLED
            run.success_items = success
            run.processed_items = processed
            run.failed_items = failed
            run.metrics = _aggregate_metrics(metric_sums, success)
            run.finished_at = datetime.now()
            run.error_msg = (
                f"用户取消：已完成 {success}/{len(samples)} 条，"
                f"指标仅代表已完成样本，不可与完整运行做 A/B 对比"
            )[:1000]
            db.commit()
            return
        if aborted_reason:
            run.status = RUN_FAILED
            run.success_items = success
            run.processed_items = processed
            run.failed_items = failed
            # 零成功时必须写 None 而不是 {"faithfulness": None, ...}：后者是
            # 「有数据但全空」的假象，会让前端/回填逻辑误判为已有指标（同一口径
            # 见下方异常收尾路径）。有成功样本时照常汇总，只有部分样本失败。
            run.metrics = (
                None if success == 0 else _aggregate_metrics(metric_sums, success)
            )
            run.finished_at = datetime.now()
            run.error_msg = (
                f"评测已熔断中止（已完成 {processed}/{len(samples)} 条）：{aborted_reason}"
            )[:1000]
            db.commit()
            return
        # 全部样本失败时判定为 failed（部分失败仍为 done + error_msg，原语义不变）
        run.status = RUN_FAILED if (failures and success == 0) else RUN_DONE
        run.success_items = success
        run.processed_items = processed
        run.failed_items = failed
        run.metrics = _aggregate_metrics(metric_sums, success)
        run.finished_at = datetime.now()
        if failures:
            # 【新增】失败样本错误明细摘要（不打断任务，供前端/排查查看）
            run.error_msg = (
                f"{len(failures)}/{len(samples)} 条样本评测失败（已自动重试"
                f" {settings.eval_retry_attempts} 次）: " + "; ".join(failures)
            )[:1000]
        db.commit()
    except Exception as exc:
        logger.exception("评测运行失败 run_id=%s", run_id)
        db.rollback()
        try:
            if run is not None:
                # 【v1.13.1 关键修复】先把缓冲区里的得分落库再写失败状态。
                # 原来这里只 rollback + 改状态，缓冲区里「已算完但没攒够一批」的样本
                # 直接蒸发：真实库 run 33 成功 9 条、库里 0 条得分，就是这条路径。
                try:
                    _flush_scores(db, score_buffer)
                except Exception:
                    logger.exception("异常收尾时落库得分失败 run_id=%s", run_id)
                run = db.get(EvalRun, run_id)
                if run:
                    run.status = RUN_FAILED
                    # 已付费算完的指标照样汇总出来：failed 只说明「没跑全」，
                    # 不代表「一条都没算出来」。列表里显示 — 会让用户以为白花钱。
                    run.success_items = success
                    run.processed_items = processed
                    run.failed_items = failed
                    # 一条都没算完时必须写 None 而不是 {"faithfulness": None, ...}：
                    # 后者是「有数据但全空」的假象，也会让后续回填逻辑误判为已有指标。
                    partial = _metrics_from_scores(db, run_id) or {
                        k: _mean_or_none(v) for k, v in metric_sums.items()
                    }
                    run.metrics = None if all(v is None for v in partial.values()) else partial
                    run.error_msg = (
                        f"任务中断：已完成 {success}/{run.total_items or processed} 条（失败 {failed} 条），"
                        f"指标仅代表已完成样本，不可做 A/B 对比；原因: {str(exc)[:400]}"
                    )[:1000]
                    run.finished_at = datetime.now()
                    db.commit()
        except Exception:
            logger.exception("写评测失败状态也失败 run_id=%s", run_id)
    finally:
        db.close()
