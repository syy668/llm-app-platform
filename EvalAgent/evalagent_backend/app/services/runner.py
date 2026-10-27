"""评测执行编排：取答案 → 打分 → 落库 → 聚合。

三段式刻意分开，且**打分在项目二自己的进程里跑**（算力隔离）：
    项目一：只负责答题（被测系统）
    项目二：负责取数、裁判、落库、聚合（评测中台）

失败语义（对齐项目一 v1.17 的教训）：
  - 单条样本失败 → 该行 status=failed、指标留 NULL，其余样本照跑；
  - 全部样本零成功 → run.status=failed 且 metrics=NULL（绝不写全 null 的 dict 冒充成功）；
  - 裁判配置性错误 → preflight 阶段直接失败，不浪费上游调用。
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import (
    CORE_METRICS,
    RUN_CANCELLED,
    RUN_DONE,
    RUN_FAILED,
    RUN_PENDING,
    RUN_RUNNING,
    SCORE_FAILED,
    SCORE_OK,
    SCORE_SKIPPED,
    EvalDataset,
    EvalItem,
    EvalRun,
    EvalScore,
)
from app.services.aggregate import mean_metrics, stratum_metrics
from app.services.judge import RagasScoringUnavailable, judge
from app.services.rag_client import UpstreamError, rag_client
from app.services.runtime_config import build_judge_config

logger = logging.getLogger("evalagent.runner")

# 裁判兼容性失败的统一文案：探活（judge.STRUCTURED_OUTPUT_UNSUPPORTED）、
# 空指标熔断（本文件）与整轮零成功（_finalize）三处共用同一句话，
# 避免「同一个病三种说法」让运维以为是三个不同问题。
EMPTY_METRIC_ABORT_MSG = "裁判模型未返回有效解析结果，请检查模型兼容性"


def now() -> datetime:
    return datetime.now()


def next_run_no(db) -> str:  # noqa: ANN001
    prefix = f"EV{now():%Y%m%d}-"
    last = db.execute(
        select(EvalRun.run_no)
        .where(EvalRun.run_no.like(f"{prefix}%"))
        .order_by(EvalRun.run_no.desc())
        .limit(1)
    ).scalar_one_or_none()
    seq = int(last.rsplit("-", 1)[-1]) + 1 if last else 1
    return f"{prefix}{seq:04d}"


def _mark_failed(db, run: EvalRun, message: str) -> None:  # noqa: ANN001
    run.status = RUN_FAILED
    run.error_msg = message[:1000]
    run.finished_at = now()
    db.commit()
    logger.error("run %s 失败：%s", run.run_no, message[:200])


def _current_status(db, run_id: int) -> str:  # noqa: ANN001
    """读任务最新状态（取消接口写的那个值）。

    为什么**另开一个会话**读，而不是用 ``db.execute``：
      1. 绕过 identity map —— 本地 ORM 对象里的 status 是几分钟前的快照；
      2. 更要命的是 MySQL 默认 REPEATABLE READ —— 同一个会话的只读事务会一直用
         第一次读时的快照。执行器在轮询循环里连续调用本函数时（中间没有 commit），
         复用 ``db`` 会**永远读不到取消标记**，表现为「点了停止没反应」。
    每次 poll 新建会话 = 新事务 = 新快照，代价是一条连接，换的是取消能真正生效。
    """
    probe = SessionLocal()
    try:
        return probe.execute(select(EvalRun.status).where(EvalRun.id == run_id)).scalar_one()
    finally:
        probe.close()


def _settle_cancelled(db, run: EvalRun, note: str | None = None) -> None:  # noqa: ANN001
    """把任务落定为 cancelled。

    刻意**不写 metrics**：半轮样本的均值不能冒充整轮结论（与「零成功必须 NULL」同一口径）。
    已经算出来的单样本得分保留在 eval_scores 里 —— 那是真实花过钱的数据，删了才是浪费。
    """
    run.status = RUN_CANCELLED
    run.finished_at = now()
    run.error_msg = (note or "用户手动停止")[:1000]
    db.commit()
    logger.warning("run %s 已停止：%s", run.run_no, note or "")


async def _cancel_tasks(tasks: set[asyncio.Task]) -> None:  # type: ignore[type-arg]
    """取消还在排队/打分的子任务并回收，避免停止后继续烧裁判额度。"""
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    tasks.clear()


async def _wait_or_cancelled(db, run_id: int, task: asyncio.Task):  # noqa: ANN001, ANN201
    """等一个协程完成，期间按 RUN_CANCEL_POLL_SECONDS 轮询取消标记。

    返回 ``(结果, 是否被取消)``。为什么不用裸 await：
    取数阶段可能持续几十秒（多样本并发打上游），期间用户点停止必须能立刻响。
    """
    while True:
        done, _ = await asyncio.wait({task}, timeout=settings.RUN_CANCEL_POLL_SECONDS)
        if done:
            return task.result(), False
        if _current_status(db, run_id) == RUN_CANCELLED:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return None, True


async def execute_run(run_id: int) -> None:
    """执行一个评测任务。由 POST /eval/runs 以后台任务方式触发。"""
    db = SessionLocal()
    answers: dict[int, Any] = {}
    try:
        run = db.get(EvalRun, run_id)
        if run is None:
            logger.warning("run %s 不存在，跳过", run_id)
            return
        if run.status != RUN_PENDING:
            logger.warning("run %s 状态为 %s，非 pending，跳过", run.run_no, run.status)
            return

        dataset = db.get(EvalDataset, run.dataset_id)
        if dataset is None:
            _mark_failed(db, run, f"评测集 {run.dataset_id} 不存在")
            return

        items = (
            db.execute(
                select(EvalItem).where(EvalItem.dataset_id == dataset.id).order_by(EvalItem.seq.asc())
            )
            .scalars()
            .all()
        )
        if not items:
            _mark_failed(db, run, "评测集为空，请先导入样本")
            return

        kb_id = dataset.kb_id
        if kb_id is None and dataset.target_ref and str(dataset.target_ref).isdigit():
            kb_id = int(dataset.target_ref)
        if kb_id is None:
            _mark_failed(db, run, "评测集未绑定知识库（kb_id 与 target_ref 均为空），无法取答案")
            return

        run.status = RUN_RUNNING
        run.started_at = now()
        run.total_items = len(items)
        run.processed_items = 0
        run.success_items = run.failed_items = run.skipped_items = 0
        db.commit()
        logger.info(
            "run %s 开始：dataset=%s kb=%s items=%s variant=%s rag_config=%s",
            run.run_no, dataset.name, kb_id, len(items), run.variant, run.rag_config,
        )

        # ---- 1) 裁判探活：配置错就立刻失败，不浪费上游调用 ----
        try:
            await asyncio.to_thread(judge.preflight)
        except RagasScoringUnavailable as exc:
            _mark_failed(db, run, f"裁判不可用：{exc}")
            return

        # ---- 2) 取答案（并发，受 UPSTREAM_RAG_CONCURRENCY 约束）----
        top_k = (run.rag_config or {}).get("top_k")
        fetch_task = asyncio.create_task(_fetch_all(items, kb_id, top_k))
        fetched, stopped = await _wait_or_cancelled(db, run_id, fetch_task)
        if stopped:
            _settle_cancelled(db, run, f"用户手动停止（取数阶段，已处理 0/{len(items)}）")
            return
        answers = {item_id: result for item_id, result in fetched}
        logger.info("run %s 取数完成：%s/%s 成功", run.run_no, sum(1 for r in answers.values() if r[0] is not None), len(items))

        # ---- 3) 打分 + 落库（逐条提交，前端进度条能实时走）----
        score_ctxs = []
        for item in items:
            result, err = answers.get(item.id, (None, "取数缺失"))
            score_ctxs.append((item, result, err))

        sem = asyncio.Semaphore(settings.RUN_ITEM_CONCURRENCY)
        total_tokens = 0
        empty_streak = 0
        pending = {item.id for item, _answer, _err in score_ctxs}
        tasks: set[asyncio.Task] = {asyncio.create_task(_score_one(c, sem)) for c in score_ctxs}
        while tasks:
            # timeout 是「取消响应窗口」：即便一条样本都还没算完，也能在
            # RUN_CANCEL_POLL_SECONDS 内感知到停止请求（见 config 注释）。
            done, tasks = await asyncio.wait(
                tasks,
                timeout=settings.RUN_CANCEL_POLL_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for finished in done:
                item, outcome, err, tokens = finished.result()
                total_tokens += tokens
                pending.discard(item.id)

                row = _build_score_row(run_id, item, answers.get(item.id, (None, None))[0], outcome, err)
                db.add(row)
                run.processed_items += 1
                if row.status == SCORE_OK:
                    run.success_items += 1
                elif row.status == SCORE_SKIPPED:
                    run.skipped_items += 1
                else:
                    run.failed_items += 1
                run.total_tokens = total_tokens
                db.commit()
                logger.info(
                    "run %s 进度 %s/%s  seq=%s status=%s F=%s AR=%s",
                    run.run_no, run.processed_items, run.total_items, item.seq, row.status,
                    row.faithfulness, row.answer_relevancy,
                )

                # ---- 配置性致命错误熔断 ----
                # 额度耗尽 / key 失效 / 模型名不存在：这类错误重试多少次都一样，
                # 继续跑只会让每条样本各耗 90s×3 次重试（实测 7 条拖到 5 分钟，
                # 项目三 300s 轮询直接判超时）。立即终止并写清根因。
                if err and _is_fatal_judge_error(err) and pending:
                    logger.error("run %s 裁判配置失效，提前终止：%s", run.run_no, err[:200])
                    await _cancel_tasks(tasks)
                    for rest in items:
                        if rest.id in pending:
                            db.add(
                                _build_score_row(
                                    run_id, rest, answers.get(rest.id, (None, None))[0], None,
                                    "裁判配置失效，任务提前终止（未重试）",
                                )
                            )
                            run.skipped_items += 1
                            run.processed_items += 1
                    _mark_failed(db, run, f"裁判不可用，已提前终止：{err[:300]}")
                    return

                # ---- 空指标熔断（裁判兼容性）----
                # 「能聊天但吐不出 RAGAS 要的 JSON」的模型（如 kimi-k3）会让每条样本都
                # 返回空指标。不拦的话 N 条样本各耗 5 次 LLM 调用、几分钟后才在末尾判失败，
                # 钱白烧、人也白等。连续 N 条全空即认定模型不兼容，立刻收尾。
                if row.status == SCORE_FAILED and (row.error_msg or "").startswith("指标全空"):
                    empty_streak += 1
                    if empty_streak >= settings.RUN_EMPTY_METRIC_STREAK_LIMIT and pending:
                        logger.error(
                            "run %s 连续 %s 条样本指标全空，判定裁判不兼容，提前终止",
                            run.run_no, empty_streak,
                        )
                        await _cancel_tasks(tasks)
                        for rest in items:
                            if rest.id in pending:
                                db.add(
                                    _build_score_row(
                                        run_id, rest, answers.get(rest.id, (None, None))[0], None,
                                        "裁判模型未返回有效解析结果，任务提前终止",
                                    )
                                )
                                run.skipped_items += 1
                                run.processed_items += 1
                        _mark_failed(db, run, EMPTY_METRIC_ABORT_MSG)
                        return
                else:
                    empty_streak = 0

            # ---- 取消标记检查（每轮都查，包括 wait 超时的空轮）----
            # 注意顺序：先落库刚算完的那条，再判取消 —— 已经花钱算出来的得分不能扔。
            if _current_status(db, run_id) == RUN_CANCELLED:
                await _cancel_tasks(tasks)
                _settle_cancelled(
                    db, run,
                    f"用户手动停止（已处理 {run.processed_items}/{run.total_items}，"
                    "剩余样本不再打分，已产出得分予以保留）",
                )
                return

        # ---- 3.5) 打分超时样本补跑 ----
        await _rescue_timeouts(db, run, items, answers)

        # ---- 4) 聚合（从库里读，单一事实来源）----
        _finalize(db, run)
    except Exception as exc:  # noqa: BLE001 - 后台任务兜底，绝不静默
        logger.exception("run %s 执行异常", run_id)
        try:
            run = db.get(EvalRun, run_id)
            if run is not None and run.status == RUN_RUNNING:
                _mark_failed(db, run, f"执行异常：{type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            logger.exception("回写失败状态也失败了")
    finally:
        db.close()


async def _fetch_all(items, kb_id: int, top_k: Any) -> list[tuple[int, tuple[Any, str | None]]]:
    """并发取答案。返回 [(item_id, (RagAnswer|None, error|None))]。"""
    sem = asyncio.Semaphore(settings.RUN_ITEM_CONCURRENCY * 2)

    async def one(item: EvalItem):
        async with sem:
            try:
                ans = await rag_client.query(kb_id, item.question, top_k)
                if not ans.contexts:
                    return item.id, (None, "上游未召回任何上下文")
                return item.id, (ans, None)
            except UpstreamError as exc:
                return item.id, (None, f"上游调用失败：{exc}")
            except Exception as exc:  # noqa: BLE001
                return item.id, (None, f"上游未知错误：{type(exc).__name__}: {exc}")

    results = await asyncio.gather(*[one(i) for i in items])
    return list(results)


# 配置性致命错误特征：重试无意义，必须立刻熔断（写清根因，别让整轮空转）
_FATAL_JUDGE_PATTERNS = (
    "AllocationQuota",        # dashscope 免费额度耗尽（FreeTierOnly）
    "FreeTierOnly",
    "insufficient_quota",     # OpenAI 额度不足
    "PermissionDenied",       # openai SDK 对 403 的异常类型名（额度耗尽/key 无权限）
    "invalid_api_key",        # key 失效
    "Error code: 401",
    "Error code: 403",
    "does not exist",         # 模型名写错
    "model_not_found",
    "NotFoundError",
)


def _is_fatal_judge_error(message: str) -> bool:
    """判断打分错误是否属于「配置坏了」而非「这条样本难」。

    前者继续跑只是重复失败（每条还要吃满 RAGAS 的重试退避），
    后者应当逐条记录、其余继续。
    """
    text = message or ""
    return any(pat.lower() in text.lower() for pat in _FATAL_JUDGE_PATTERNS)


async def _score_one(ctx, sem: asyncio.Semaphore):  # noqa: ANN001
    item, answer, fetch_err = ctx
    if answer is None:
        return item, None, fetch_err or "取数失败", 0
    async with sem:
        try:
            outcome = await asyncio.wait_for(
                asyncio.to_thread(
                    judge.score,
                    item.question,
                    answer.answer,
                    answer.contexts,
                    item.reference_answer,
                    item.reference_contexts,
                ),
                timeout=settings.JUDGE_ITEM_TIMEOUT,
            )
            return item, outcome, None, outcome.tokens
        except asyncio.TimeoutError:
            # 单条样本卡死不能拖垮整个 run（RAGAS 内部重试退避可能静默空转数分钟）
            return item, None, f"打分超时（>{settings.JUDGE_ITEM_TIMEOUT:.0f}s）", 0
        except RagasScoringUnavailable as exc:
            return item, None, f"打分失败：{exc}", 0
        except Exception as exc:  # noqa: BLE001
            return item, None, f"打分异常：{type(exc).__name__}: {exc}", 0


async def _rescue_timeouts(db, run: EvalRun, items: list[EvalItem], answers: dict) -> None:  # noqa: ANN001
    """把「打分超时」的样本补跑一次。

    为什么值得单独补跑：实测这些样本单独重打只要 20~40s 就能出满分
    （300s 超时 = 瞬时网络抖动 × 客户端重试叠加，不是样本本身算不动，
    见 config.py 里 JUDGE_TIMEOUT / JUDGE_ITEM_TIMEOUT 的预算注释）。
    不补跑一次的话，一次网络抖动就让 10% 样本零指标，用户只能手工重跑整个任务。

    约束：只在 run 仍 running 时补；总预算 RUN_RESCUE_BUDGET_SECONDS，
    超预算就放弃剩余样本（宁可这条 failed，也不能让烂网络把 run 拖成小时级）。
    """
    if not settings.RUN_RESCUE_TIMEOUTS:
        return
    if _current_status(db, run.id) != RUN_RUNNING:
        return

    rows = (
        db.execute(
            select(EvalScore).where(
                EvalScore.run_id == run.id, EvalScore.status == SCORE_FAILED
            )
        )
        .scalars()
        .all()
    )
    targets = [r for r in rows if (r.error_msg or "").startswith("打分超时")]
    if not targets:
        return

    item_by_id = {i.id: i for i in items}
    deadline = time.monotonic() + settings.RUN_RESCUE_BUDGET_SECONDS
    sem = asyncio.Semaphore(2)
    logger.warning(
        "run %s 有 %s 条打分超时，开始补跑（预算 %.0fs）",
        run.run_no, len(targets), settings.RUN_RESCUE_BUDGET_SECONDS,
    )

    for row in targets:
        item = item_by_id.get(row.item_id)
        answer = answers.get(row.item_id, (None, None))[0]
        if item is None or answer is None:
            continue
        remain = deadline - time.monotonic()
        if remain <= 1:
            logger.warning("run %s 补跑预算用尽，剩余超时样本保持 failed", run.run_no)
            break
        async with sem:
            try:
                outcome = await asyncio.wait_for(
                    asyncio.to_thread(
                        judge.score,
                        item.question,
                        answer.answer,
                        answer.contexts,
                        item.reference_answer,
                        item.reference_contexts,
                    ),
                    timeout=min(settings.JUDGE_ITEM_TIMEOUT, remain),
                )
            except asyncio.TimeoutError:
                logger.warning("run %s seq=%s 补跑仍超时，保持 failed", run.run_no, item.seq)
                continue
            except Exception as exc:  # noqa: BLE001 - 补跑失败不影响主流程
                logger.warning("run %s seq=%s 补跑异常：%s", run.run_no, item.seq, str(exc)[:200])
                continue

        fresh = _build_score_row(run.id, item, answer, outcome, None)
        # 原地覆盖旧行（保持 item_seq 顺序与行 id 不变，前端分页不会错位）
        for field in (
            "status", "faithfulness", "answer_relevancy", "context_precision", "context_recall",
            "metric_scores", "tokens", "error_msg", "suspect", "scored_at",
        ):
            setattr(row, field, getattr(fresh, field))
        if row.status == SCORE_OK:
            run.failed_items = max(0, run.failed_items - 1)
            run.success_items += 1
        run.total_tokens = (run.total_tokens or 0) + (outcome.tokens or 0)
        db.commit()
        logger.info(
            "run %s seq=%s 补跑成功 F=%s AR=%s",
            run.run_no, item.seq, row.faithfulness, row.answer_relevancy,
        )


def _build_score_row(run_id: int, item: EvalItem, answer, outcome, err: str | None) -> EvalScore:  # noqa: ANN001
    row = EvalScore(
        run_id=run_id,
        item_id=item.id,
        item_seq=item.seq,
        stratum=item.stratum,
        question=item.question,
        reference_answer=item.reference_answer,
        answer=(answer.answer if answer is not None else None),
        retrieved_contexts=(answer.contexts if answer is not None else None),
        latency_ms=(answer.latency_ms if answer is not None else 0),
        tokens=(outcome.tokens if outcome is not None else 0),
        scored_at=now(),
    )
    if outcome is None:
        row.status = SCORE_SKIPPED if answer is None else SCORE_FAILED
        row.error_msg = (err or "未知原因")[:500]
        return row

    row.status = SCORE_OK
    for metric, value in outcome.scores.items():
        setattr(row, metric, value)

    # 四项全空 = RAGAS 把异常吞成了 NaN（典型的静默空分）。
    # 这种行绝不能挂 ok：前端会把它当"已评分"渲染，聚合时又被 mean 忽略，
    # 最后一个 7 样本的 run 只剩 5 条有分却看不出问题。
    if all(v is None for v in outcome.scores.values()):
        row.status = SCORE_FAILED
        row.error_msg = "指标全空：裁判未返回可解析结果（疑似解析失败或输出被截断）"
        row.metric_scores = outcome.raw
        return row

    row.metric_scores = outcome.raw
    row.suspect = _is_suspect(answer, outcome)

    # ---- 单指标缺失要留痕 ----
    # 四项里坏一项时 ragas 只把那一项吞成 NaN，status 仍是 ok —— 不写清楚的话
    # 前端只看到空单元格，运维无法区分「这条没算」和「这条真的是 0 分」。
    missing = (outcome.raw or {}).get("missing") or []
    if missing:
        row.error_msg = f"指标缺失：{'/'.join(missing)}（已定向补跑一次仍为空）"[:500]

    # ---- 拒答样本不参与 answer_relevancy ----
    # AR = 由答案反推问题、再与原问题比相似度。拒答话术（"资料中未找到相关信息"）
    # 没有任何主题内容，反推出的问题必然与原问题不相似 → AR 恒 0，
    # 而这类样本的标准答案本身就是「答不出来」，AR=0 被当成"答得不好"是口径错误。
    if settings.REFUSAL_SKIP_AR and _is_refusal_item(item) and row.answer_relevancy is not None:
        row.answer_relevancy = None
        row.metric_scores = {
            **(outcome.raw or {}),
            "answer_relevancy_skipped": "拒答样本不适用（不计入 AR 聚合）",
        }
    return row


# 拒答型样本的话术标记：标准答案本身就是「知识库里没有」这个结论。
# 刻意写得具体（"知识库未包含" 而不是 "未包含"）—— 后者会误伤
# "含税、未包含运费" 这类正常口径描述，把有效样本的 AR 也抹掉。
_REFUSAL_MARKERS = (
    "未找到相关信息", "未找到相关", "未找到该信息", "没有相关信息",
    "无法回答", "知识库未包含", "知识库中不包含", "资料中没有",
)


def _is_refusal_item(item: EvalItem) -> bool:  # noqa: ANN001
    """是不是「拒答型」样本。

    两级判定：分层标签优先（数据集导入时带 stratum，最可靠），
    其次看标准答案话术 —— 手工建的评测集常常没有分层标签。
    """
    if (item.stratum or "").strip() in ("拒答陷阱", "拒答"):
        return True
    return any(k in (item.reference_answer or "") for k in _REFUSAL_MARKERS)


def _is_suspect(answer, outcome) -> bool:  # noqa: ANN001
    """可疑标记（沿用项目一 citation_guard「只标不拦」的思路）。

    这里只做最保守的一条：有上下文却答"资料中未找到"类话术，
    或者 faithfulness/answer_relevancy 同时极低 —— 大概率是引用断裂。
    """
    if answer is None:
        return False
    text = (answer.answer or "").strip()
    if answer.contexts and any(k in text for k in ("未找到", "无法回答", "没有相关信息")):
        return True
    f = outcome.scores.get("faithfulness")
    ar = outcome.scores.get("answer_relevancy")
    # AR 被 ragas 的 `cosine × int(not committal)` 直接归零：答案明明有内容、
    # 忠实度也不低，却因为裁判把回答判成 noncommittal（"回避/不承诺"）而 0 分。
    # 实测弱裁判（qwen3.6-flash）对"打印机卡纸第一步"这种明确答案也会标 noncommittal=1。
    # 不涂改分数，只打可疑标记（沿用项目一 citation_guard「只标不拦」的口径）。
    if ar == 0 and f is not None and f >= 0.5:
        return True
    return bool(f is not None and ar is not None and f < 0.2 and ar < 0.2)


def _finalize(db, run: EvalRun) -> None:  # noqa: ANN001
    rows = db.execute(select(EvalScore).where(EvalScore.run_id == run.id)).scalars().all()
    as_dicts = [
        {
            "question": r.question,
            "stratum": r.stratum,
            "status": r.status,
            "faithfulness": r.faithfulness,
            "answer_relevancy": r.answer_relevancy,
            "context_precision": r.context_precision,
            "context_recall": r.context_recall,
        }
        for r in rows
    ]
    ok_rows = [r for r in as_dicts if r["status"] == SCORE_OK]
    metrics = mean_metrics(ok_rows)

    if metrics is None:
        # 零成功：metrics 必须是 NULL，不能写全 null 的 dict
        run.metrics = None
        run.metric_detail = {"by_stratum": stratum_metrics(ok_rows), "reason": "零成功样本"}
        run.status = RUN_FAILED
        # 零成功有三种根因，文案必须指向正确的排查方向：
        #   ① 裁判吐不出结构化 JSON（模型不兼容）→ 固定文案，与探活/熔断统一；
        #   ② 上游被测系统没给出答案（401/404/超时/知识库不存在）→ 这是上游问题，
        #      绝不能说"请查裁判配置"（2026-10-27 实测踩过：项目一账号 401，
        #      界面却提示查裁判，排查方向直接被带偏 20 分钟）；
        #   ③ 其他（打分全异常）。
        judge_incompatible = any(
            r.status == SCORE_FAILED and (r.error_msg or "").startswith("指标全空") for r in rows
        )
        upstream_errors = [
            (r.error_msg or "") for r in rows
            if r.status == SCORE_SKIPPED and (r.error_msg or "").startswith(("上游调用失败", "取数失败"))
        ]
        if judge_incompatible:
            run.error_msg = EMPTY_METRIC_ABORT_MSG
        elif upstream_errors:
            first = upstream_errors[0][:160]
            run.error_msg = (
                f"上游被测系统未返回答案，{len(upstream_errors)}/{len(rows)} 条样本取数失败：{first}"
            )[:1000]
        else:
            run.error_msg = "全部样本未产出有效指标（请查裁判配置与上游可用性）"
        run.finished_at = now()
        db.commit()
        logger.error(
            "run %s 零成功样本，置为 failed（裁判兼容性=%s，上游取数失败=%s 条）",
            run.run_no, judge_incompatible, len(upstream_errors),
        )
        return

    run.metrics = metrics
    _cfg = build_judge_config()
    # 指标缺失率：单条样本里某个指标被 ragas 吞成 NaN 时，聚合会静默把它从分母里剔掉，
    # 于是「30 条样本、F 只算了 24 条」和「30 条全算」在界面上长得一模一样。
    # 缺失率过高时必须显式告警：这时的均值只能当参考，不能当结论（也解释了
    # 「换个裁判模型分数突然变好」—— 有时是坏指标被剔得更多）。
    ok_total = len(ok_rows)
    # 注意区分「算空了」和「有意不算」：拒答样本的 answer_relevancy 是我们主动置空的
    # （AR 对拒答无意义），把它算成缺失会让任何含拒答题的数据集都误报警。
    skipped = {
        metric: sum(
            1 for r in rows
            if r.status == SCORE_OK and (r.metric_scores or {}).get(f"{metric}_skipped")
        )
        for metric in CORE_METRICS
    }
    missing_rates: dict[str, float] = {}
    for metric in CORE_METRICS if ok_total else ():
        skip = skipped[metric]
        denom = ok_total - skip
        numer = sum(1 for r in ok_rows if r[metric] is None) - skip
        missing_rates[metric] = round(max(0, numer) / denom, 4) if denom > 0 else 0.0
    warnings = [
        f"{metric} 缺失率 {rate:.0%}（{ok_total} 条成功样本）"
        for metric, rate in missing_rates.items()
        if rate >= settings.METRIC_MISSING_WARN_RATE
    ]
    run.metric_detail = {
        "by_stratum": stratum_metrics(ok_rows),
        "scored_items": len(ok_rows),
        # 同样取生效配置，而不是 .env 默认值（界面改过模型后 settings 是旧的）
        "judge_model": _cfg.model,
        # 向量模型一并留痕：answer_relevancy 依赖它，换模型前后分数不可比
        "judge_embedding_model": _cfg.embedding_model,
        "missing_rates": missing_rates,
    }
    if warnings:
        run.metric_detail["warnings"] = warnings
        logger.warning("run %s 指标缺失率偏高，结论仅供参考：%s", run.run_no, warnings)
    if settings.JUDGE_PRICE_PER_1M:
        run.cost_usd = round(run.total_tokens / 1_000_000 * settings.JUDGE_PRICE_PER_1M, 6)
    run.status = RUN_DONE
    run.finished_at = now()
    db.commit()
    logger.info("run %s 完成：metrics=%s", run.run_no, metrics)


def recover_stale_runs(max_age_seconds: int | None = None) -> int:
    """启动时回收僵死任务。

    进程被杀（或我这边的后台任务被系统回收）后，eval_runs.status 会永久停在 running，
    占住并发闸、前端也永远转圈。这里按 started_at 超时统一判死。
    """
    limit = max_age_seconds or settings.RUN_STALE_SECONDS
    db = SessionLocal()
    try:
        cutoff = now().timestamp() - limit
        rows = db.execute(
            select(EvalRun).where(EvalRun.status.in_((RUN_PENDING, RUN_RUNNING)))
        ).scalars().all()
        count = 0
        for run in rows:
            started = run.started_at or run.created_at
            if started is None or started.timestamp() > cutoff:
                continue
            run.status = RUN_FAILED
            run.error_msg = f"任务僵死：超过 {limit}s 未完成（进程中断）"
            run.finished_at = now()
            count += 1
        if count:
            db.commit()
            logger.warning("回收僵死任务 %s 条", count)
        return count
    finally:
        db.close()
