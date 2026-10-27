"""评测任务「得分不丢 + 进度可信」测试（v1.13.1，离线 sqlite）。

背景（真实库实测出来的 bug，不是假设）：
    run 33 failed 成功 9 条 / 库里 0 条得分
    run 41 failed 成功 8 条 / 库里 0 条得分
    run 51 failed 成功 13 条 / 库里 10 条得分
    run 37 done   成功 27 条 / 库里 27 条得分   ← 唯一完整的

`scores` 恒为 EVAL_SCORE_BATCH_SIZE(10) 的整数倍，且永远 ≤ success_items。根因是
得分先进内存缓冲、攒够 10 条才 flush，而异常/中断路径既不 flush 也不汇总 metrics：
用户已经为这 1~9 条样本付过 LLM 费用，结果连记录都没留下。

本文件锁死四件事：
  1. 主流程抛异常时，缓冲区里的得分必须被抢救落库，且四项指标按已有得分汇总；
  2. 「跳过」（direct/report 路由）必须计入 processed_items，否则进度条永远到不了 100%；
  3. 失败样本计入 failed_items / processed_items，与 success 分开统计；
  4. 进程中断留下的历史脏数据（有得分、metrics 为空）可被回填修复。
"""
import pytest

import app.services.eval_service as eval_service
from app.config import settings
from app.core.database import Base, SessionLocal, engine
from app.models.eval_dataset import EvalDataset, EvalDatasetItem
from app.models.eval_run import RUN_DONE, RUN_FAILED, RUN_RUNNING, EvalRun, EvalRunScore
from app.schemas.eval import RunCreate
from datetime import datetime, timedelta

N_ITEMS = 3
_FAKE = {
    "faithfulness": 0.8,
    "answer_relevancy": 0.6,
    "context_precision": 0.7,
    "context_recall": 0.9,
}

# 模块导入期固化真实后台函数（create_run 里会被 patch 成 no-op 拦截自动线程）
_REAL_BG = eval_service._run_eval_background


@pytest.fixture()
def env3(monkeypatch):
    """3 条样本的数据集 + 内存库四表；外部依赖（问答/检索/RAGAS）全部替换为 fake。"""
    eval_service._tables_ready = False
    Base.metadata.create_all(bind=engine, checkfirst=True)

    from app.models.knowledge_base import KnowledgeBase

    db = SessionLocal()
    try:
        db.add(KnowledgeBase(
            id=998, name="落库测试知识库", owner_id=1, embedding_model="mock",
            chunk_size=500, chunk_overlap=50, top_k=3, rerank_threshold=0.0,
            status="ready", doc_count=0,
        ))
        ds = EvalDataset(kb_id=998, name="落库测试集", item_count=N_ITEMS, created_by=1)
        db.add(ds)
        db.flush()
        dataset_id = ds.id
        for i in range(N_ITEMS):
            db.add(EvalDatasetItem(
                dataset_id=dataset_id, question=f"问题{i}", reference_answer=f"答案{i}",
            ))
        db.commit()
        yield {"dataset_id": dataset_id}
    finally:
        db.rollback()
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session(env3):
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _admin():
    from app.api.deps import CurrentUser

    return CurrentUser(id=1, username="admin", display_name=None, role="admin", status="active")


def _score_row(item, run_id):
    return {
        "run_id": run_id,
        "item_id": item.id,
        "question": item.question,
        "answer": f"回答[{item.question}]",
        "retrieved_contexts": [f"召回[{item.question}]"],
        "latency_ms": 5,
        **_FAKE,
    }


def _start(env3, monkeypatch, db, **kwargs):
    """建 run（拦截自动线程）→ 返回 run_id，由用例自己决定怎么跑。"""
    monkeypatch.setattr(eval_service, "_run_eval_background", lambda run_id: None)
    data = eval_service.create_run(
        db, _admin(), RunCreate(dataset_id=env3["dataset_id"], **kwargs)
    )
    return data["id"]


def _scores(db, run_id):
    return db.query(EvalRunScore).filter(EvalRunScore.run_id == run_id).all()


class _KeepOpenSession:
    """把服务的 `db.close()` 变成空操作。

    cleanup / backfill 内部走 `db = SessionLocal()` + `finally: db.close()`，
    测试里 SessionLocal 被替换成返回同一个会话，直接 close 会让用例手里的对象
    变成 detached（DetachedInstanceError）。这里只屏蔽 close，其余调用原样透传。
    """

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):  # noqa: D102 - 有意屏蔽
        pass


@pytest.fixture()
def patched_session(db_session, monkeypatch):
    monkeypatch.setattr(eval_service, "SessionLocal", lambda: _KeepOpenSession(db_session))
    return db_session


# ---------------------------------------------------------------------------
# 1) 异常路径：已算完的得分必须被抢救
# ---------------------------------------------------------------------------

def test_abort_path_salvages_buffered_scores(env3, db_session, monkeypatch):
    """主流程抛异常时，缓冲区里没攒够一批的得分不能丢，指标要按已有得分汇总。

    这是本次修复的核心：原实现异常分支只 rollback + 改 failed，score_buffer
    随栈帧消失 —— 真实库 run 33 就是这样丢掉 9 条已付费得分的。
    """
    monkeypatch.setattr(settings, "eval_concurrent_enable", False)
    monkeypatch.setattr(settings, "eval_score_batch_size", 10)  # 复现旧默认：攒批

    calls = {"n": 0}

    def flaky(item, ctx):
        calls["n"] += 1
        if calls["n"] > 2:
            raise RuntimeError("模拟主流程崩溃（如 RAGAS 内部异常/DB 断连）")
        return ("ok", _score_row(item, ctx["run_id"]))

    monkeypatch.setattr(eval_service, "_eval_sample_with_retry", flaky)
    run_id = _start(env3, monkeypatch, db_session)
    _REAL_BG(run_id)

    db_session.expire_all()
    run = db_session.get(EvalRun, run_id)
    rows = _scores(db_session, run_id)

    assert run.status == RUN_FAILED
    assert len(rows) == 2, "缓冲区里 2 条已算完的得分必须落库（batch_size=10 时一条都不该 flush）"
    assert run.success_items == 2
    assert run.processed_items == 2
    assert run.metrics is not None and run.metrics["faithfulness"] == pytest.approx(0.8)
    assert "任务中断" in run.error_msg and "2/3" in run.error_msg


def test_abort_without_any_score_keeps_metrics_empty(env3, db_session, monkeypatch):
    """一条都没算完就崩：metrics 必须是 None（不能编造 0 分，0 分会污染均值）。"""
    monkeypatch.setattr(settings, "eval_concurrent_enable", False)

    def boom(item, ctx):
        raise RuntimeError("第一条就炸")

    monkeypatch.setattr(eval_service, "_eval_sample_with_retry", boom)
    run_id = _start(env3, monkeypatch, db_session)
    _REAL_BG(run_id)

    db_session.expire_all()
    run = db_session.get(EvalRun, run_id)
    assert run.status == RUN_FAILED
    assert run.metrics is None
    assert run.success_items == 0
    assert _scores(db_session, run_id) == []


# ---------------------------------------------------------------------------
# 2) 进度口径：跳过与失败都要计入 processed
# ---------------------------------------------------------------------------

def test_skipped_samples_count_as_processed(env3, db_session, monkeypatch):
    """direct/report 路由的样本不计分，但必须计入 processed —— 否则进度条差几条不达标。"""
    monkeypatch.setattr(settings, "eval_concurrent_enable", False)
    calls = {"n": 0}

    def mixed(item, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            return ("ok", _score_row(item, ctx["run_id"]))
        return ("skipped", None)

    monkeypatch.setattr(eval_service, "_eval_sample_with_retry", mixed)
    run_id = _start(env3, monkeypatch, db_session)
    _REAL_BG(run_id)

    db_session.expire_all()
    run = db_session.get(EvalRun, run_id)
    assert run.status == RUN_DONE
    assert run.success_items == 1
    assert run.processed_items == N_ITEMS, "跳过的样本也算处理过，进度必须是 3/3"
    assert run.failed_items == 0
    assert run.error_msg is None  # 跳过不是错误


def test_failed_samples_tracked_separately(env3, db_session, monkeypatch):
    """失败样本单独计数：success=1 / failed=2 / processed=3，状态仍为 done（部分失败）。"""
    monkeypatch.setattr(settings, "eval_concurrent_enable", False)
    calls = {"n": 0}

    def mixed(item, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            return ("ok", _score_row(item, ctx["run_id"]))
        return ("failed", f"item#{item.id}: 上游 429")

    monkeypatch.setattr(eval_service, "_eval_sample_with_retry", mixed)
    run_id = _start(env3, monkeypatch, db_session)
    _REAL_BG(run_id)

    db_session.expire_all()
    run = db_session.get(EvalRun, run_id)
    assert run.status == RUN_DONE  # 有成功样本即 done，与原语义一致
    assert (run.success_items, run.failed_items, run.processed_items) == (1, 2, 3)
    assert "2/3 条样本评测失败" in run.error_msg


# ---------------------------------------------------------------------------
# 3) 历史脏数据回填
# ---------------------------------------------------------------------------

def _make_run(db, env3, status=RUN_RUNNING, metrics=None, success=0, **extra):
    run = EvalRun(
        dataset_id=env3["dataset_id"], kb_id=998, model="deepseek-chat",
        status=status, total_items=N_ITEMS, success_items=success, metrics=metrics,
        created_by=1, **extra,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _make_scores(db, run_id, n=2):
    for i in range(n):
        db.add(EvalRunScore(
            run_id=run_id, item_id=i + 1, question=f"问题{i}", answer="a",
            retrieved_contexts=[], latency_ms=1, **_FAKE,
        ))
    db.commit()


def test_cleanup_backfills_metrics_for_interrupted_run(env3, patched_session, monkeypatch):
    """进程重启回收僵死任务时，按已落库的得分回填指标（否则列表四项全「—」）。"""
    db_session = patched_session
    run = _make_run(
        db_session, env3, status=RUN_RUNNING, success=2,
        started_at=datetime.now() - timedelta(seconds=7200),
    )
    _make_scores(db_session, run.id, n=2)

    cleaned = eval_service.cleanup_stale_running_runs(600)

    assert cleaned == 1
    db_session.expire_all()
    fresh = db_session.get(EvalRun, run.id)
    assert fresh.status == RUN_FAILED
    assert fresh.metrics is not None
    assert fresh.metrics["context_recall"] == pytest.approx(0.9)


def test_backfill_terminal_metrics_repairs_legacy_rows(env3, patched_session, monkeypatch):
    """历史 failed 任务有得分无指标 → 回填；没得分的任务保持 None（不编造）。"""
    db_session = patched_session
    with_scores = _make_run(db_session, env3, status=RUN_FAILED)
    _make_scores(db_session, with_scores.id, n=2)
    without_scores = _make_run(db_session, env3, status=RUN_FAILED)

    repaired = eval_service.backfill_terminal_metrics()

    assert repaired == 1
    db_session.expire_all()
    assert db_session.get(EvalRun, with_scores.id).metrics is not None
    assert db_session.get(EvalRun, without_scores.id).metrics is None
    # 幂等：再跑一次不再重复修
    assert eval_service.backfill_terminal_metrics() == 0


def test_run_out_exposes_progress_fields(env3, db_session, monkeypatch):
    """RunOut 必须带上 processed_items / failed_items，否则前端进度条拿不到值。"""
    from app.schemas.eval import RunOut

    run = _make_run(db_session, env3, status=RUN_DONE, metrics=dict(_FAKE))
    run.processed_items = 3
    run.failed_items = 1
    db_session.commit()

    out = RunOut.model_validate(run).model_dump()
    assert out["processed_items"] == 3
    assert out["failed_items"] == 1
