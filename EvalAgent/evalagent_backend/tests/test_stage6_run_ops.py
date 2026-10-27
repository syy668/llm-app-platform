"""第六步：任务大盘统计 + 停止任务（止损）。

锁四件容易做错的事：
1. 统计口径与列表页一致（成功率 done/total，平均忠实度按**样本**加权）；
2. 取消是个状态机：只有 pending / running 能停，done/failed/cancelled 必须 409；
3. **停止后已算出的得分必须保留**（那是真实花过钱的数据），但 metrics 不写半轮结论；
4. 取消要能真正打断执行器：剩余样本不再打分，且不抛异常把整轮搞红。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.core.auth import CurrentUser, get_current_user, require_admin
from app.core.config import settings
from app.core.database import SessionLocal
from app.models import EvalDataset, EvalItem, EvalRun, EvalScore
from app.services import runner as runner_mod
from app.services.judge import JudgeOutcome
from app.services.rag_client import RagAnswer


@pytest.fixture()
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def as_admin(client):
    """取消接口要求管理员：身份来自上游 /auth/me，测试里直接覆盖依赖。"""
    from app.main import app

    app.dependency_overrides[require_admin] = lambda: CurrentUser(
        id=1, username="qa_admin", role="admin"
    )
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=1, username="qa_admin", role="admin"
    )
    yield client
    app.dependency_overrides.pop(require_admin, None)
    app.dependency_overrides.pop(get_current_user, None)


# ============================ 造数与清理 ============================
def _seed_dataset(db, items: int = 2) -> tuple[EvalDataset, list[EvalItem]]:
    tag = f"T6-{int(time.time() * 1000)}"
    ds = EvalDataset(
        name=f"第六步测试集 {tag}", code=tag, target_type="kb", kb_id=6, item_count=items
    )
    db.add(ds)
    db.flush()
    rows = [
        EvalItem(
            dataset_id=ds.id, seq=i, question=f"Q{i}", reference_answer=f"A{i}",
            stratum="easy" if i == 1 else None,
        )
        for i in range(1, items + 1)
    ]
    db.add_all(rows)
    db.flush()
    return ds, rows


def _drop(db, dataset_id: int) -> None:
    run_ids = [
        r for r in db.execute(select(EvalRun.id).where(EvalRun.dataset_id == dataset_id)).scalars()
    ]
    for run_id in run_ids:
        db.execute(EvalScore.__table__.delete().where(EvalScore.run_id == run_id))
    db.execute(EvalRun.__table__.delete().where(EvalRun.dataset_id == dataset_id))
    db.execute(EvalItem.__table__.delete().where(EvalItem.dataset_id == dataset_id))
    db.execute(EvalDataset.__table__.delete().where(EvalDataset.id == dataset_id))
    db.commit()


# ============================ 大盘统计 ============================
@pytest.fixture()
def stats_seed():
    """1 done + 1 failed + 1 cancelled，done 的两条样本 faithfulness 为 0.8 / 0.4。"""
    db = SessionLocal()
    ds, items = _seed_dataset(db, items=2)
    tag = int(time.time() * 1000)

    done = EvalRun(
        run_no=f"EV-T6D-{tag}", dataset_id=ds.id, target="kb", status="done",
        total_items=2, processed_items=2, success_items=2, total_tokens=1000, cost_usd=0.5,
        metrics={"faithfulness": 0.6, "answer_relevancy": 0.5,
                 "context_precision": 0.5, "context_recall": 0.5},
    )
    failed = EvalRun(
        run_no=f"EV-T6F-{tag}", dataset_id=ds.id, target="kb", status="failed",
        total_items=2, processed_items=2, failed_items=2, total_tokens=500,
    )
    cancelled = EvalRun(
        run_no=f"EV-T6C-{tag}", dataset_id=ds.id, target="kb", status="cancelled",
        total_items=2, processed_items=1, success_items=1, total_tokens=200,
    )
    db.add_all([done, failed, cancelled])
    db.flush()

    db.add_all([
        EvalScore(run_id=done.id, item_id=items[0].id, item_seq=1, question="Q1",
                  status="ok", faithfulness=0.8, latency_ms=10),
        EvalScore(run_id=done.id, item_id=items[1].id, item_seq=2, question="Q2",
                  status="ok", faithfulness=0.4, latency_ms=10),
        # 失败行的 faithfulness 必须 NULL：卡片均值不能把「没算出来」当 0 分
        EvalScore(run_id=failed.id, item_id=items[0].id, item_seq=1, question="Q1",
                  status="failed", faithfulness=None, latency_ms=0),
    ])
    db.commit()
    ids = {"dataset_id": ds.id, "done_run_id": done.id}
    db.close()

    yield ids

    db = SessionLocal()
    _drop(db, ids["dataset_id"])
    db.close()


def test_stats_counts_and_sample_weighted_faithfulness(as_admin, stats_seed):
    data = as_admin.get(
        f"/api/v1/eval/runs/stats?dataset_id={stats_seed['dataset_id']}"
    ).json()["data"]

    assert data["total_runs"] == 3
    assert data["done_runs"] == 1
    assert data["failed_runs"] == 1
    assert data["cancelled_runs"] == 1
    assert data["running_runs"] == 0
    # 成功率 = done / total，cancelled 与 failed 都算未成功
    assert data["success_rate"] == pytest.approx(1 / 3, abs=1e-4)
    # 样本级均值：(0.8 + 0.4) / 2，失败行的 NULL 不参与
    assert data["avg_faithfulness"] == pytest.approx(0.6, abs=1e-6)
    assert data["scored_samples"] == 2
    # 累计 Token 含已取消任务烧掉的那部分（成本要算真账）
    assert data["total_tokens"] == 1700
    assert data["total_cost_usd"] == pytest.approx(0.5, abs=1e-6)


def test_stats_empty_dataset_is_zero_not_null(as_admin):
    db = SessionLocal()
    ds, _ = _seed_dataset(db, items=1)
    ds_id = ds.id
    db.close()
    try:
        data = as_admin.get(f"/api/v1/eval/runs/stats?dataset_id={ds_id}").json()["data"]
        assert data["total_runs"] == 0
        assert data["success_rate"] == 0.0       # 不是 None，前端直接乘 100
        assert data["avg_faithfulness"] is None  # 没有样本就没有均值，不能填 0
        assert data["total_tokens"] == 0
    finally:
        db = SessionLocal()
        _drop(db, ds_id)
        db.close()


# ============================ 停止任务 ============================
def _make_run(dataset_id: int, status: str = "running", **kw) -> int:
    db = SessionLocal()
    tag = int(time.time() * 1000)
    run = EvalRun(
        run_no=f"EV-T6X-{tag}", dataset_id=dataset_id, target="kb", status=status,
        total_items=kw.pop("total_items", 5), processed_items=kw.pop("processed_items", 2), **kw,
    )
    db.add(run)
    db.commit()
    run_id = run.id
    db.close()
    return run_id


@pytest.fixture()
def dataset_id():
    db = SessionLocal()
    ds, _ = _seed_dataset(db, items=1)
    ds_id = ds.id
    db.close()
    yield ds_id
    db = SessionLocal()
    _drop(db, ds_id)
    db.close()


@pytest.mark.parametrize("status", ["running", "pending"])
def test_cancel_moves_active_run_to_cancelled(as_admin, dataset_id, status):
    run_id = _make_run(dataset_id, status)
    res = as_admin.post(f"/api/v1/eval/runs/{run_id}/cancel")
    assert res.status_code == 200

    data = res.json()["data"]
    assert data["status"] == "cancelled"
    assert data["previous_status"] == status
    assert data["finished_at"] is not None
    assert "用户手动停止" in (data["error_msg"] or "")

    db = SessionLocal()
    fresh = db.get(EvalRun, run_id)
    assert fresh.status == "cancelled"
    db.close()


@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
def test_cancel_rejects_finished_run_with_409(as_admin, dataset_id, status):
    run_id = _make_run(dataset_id, status)
    res = as_admin.post(f"/api/v1/eval/runs/{run_id}/cancel")
    assert res.status_code == 409
    assert "只有 pending / running 可以停止" in res.json()["message"]

    db = SessionLocal()
    assert db.get(EvalRun, run_id).status == status  # 状态没被改坏
    db.close()


def test_cancel_missing_run_returns_404(as_admin):
    res = as_admin.post("/api/v1/eval/runs/999999999/cancel")
    assert res.status_code == 404


def test_cancel_requires_admin(client, dataset_id):
    """不带 token 直接打必须 401，不能让人随便停任务。"""
    if not settings.AUTH_ENFORCE:
        pytest.skip("AUTH_ENFORCE=false（本地调试模式视为管理员），401 断言无意义")
    run_id = _make_run(dataset_id, "running")
    res = client.post(f"/api/v1/eval/runs/{run_id}/cancel")
    assert res.status_code == 401


# ============================ 执行器：取消能真正打断 ============================
@pytest.fixture()
def runner_dataset():
    db = SessionLocal()
    ds, items = _seed_dataset(db, items=3)
    tag = int(time.time() * 1000)
    run = EvalRun(
        run_no=f"EV-T6R-{tag}", dataset_id=ds.id, target="kb", status="pending", total_items=3,
    )
    db.add(run)
    db.commit()
    ids = {"dataset_id": ds.id, "run_id": run.id}
    db.close()
    yield ids
    db = SessionLocal()
    _drop(db, ids["dataset_id"])
    db.close()


def _patch_pipeline(monkeypatch, on_scored=None) -> None:
    """把上游取数与 RAGAS 打分替换成确定性桩，避免真打 8010 / dashscope。"""

    async def fake_query(kb_id, question, top_k=None):  # noqa: ANN001
        return RagAnswer(question=question, answer=f"答-{question}", contexts=["ctx-1"], latency_ms=5)

    def fake_score(question, answer, contexts, reference_answer, reference_contexts):  # noqa: ANN001
        if on_scored is not None:
            on_scored()
        return JudgeOutcome(
            scores={
                "faithfulness": 0.9,
                "answer_relevancy": 0.8,
                "context_precision": 0.7,
                "context_recall": 0.6,
            },
            raw={"stub": True},
            tokens=10,
        )

    monkeypatch.setattr(runner_mod.rag_client, "query", fake_query)
    monkeypatch.setattr(runner_mod.judge, "preflight", lambda *a, **k: "stub-ok")
    monkeypatch.setattr(runner_mod.judge, "score", fake_score)


def test_execute_run_processes_every_item_then_done(monkeypatch, runner_dataset):
    """回归：打分循环重构后仍要把**每一条**样本都跑完（历史上曾因缩进只跑 1 条）。"""
    _patch_pipeline(monkeypatch)
    asyncio.run(runner_mod.execute_run(runner_dataset["run_id"]))

    db = SessionLocal()
    run = db.get(EvalRun, runner_dataset["run_id"])
    rows = db.execute(
        select(EvalScore).where(EvalScore.run_id == run.id)
    ).scalars().all()
    assert run.status == "done"
    assert run.processed_items == 3 == len(rows)
    assert run.metrics is not None and run.metrics["faithfulness"] == pytest.approx(0.9)
    assert run.total_tokens == 30
    db.close()


def test_execute_run_stops_on_cancel_and_keeps_partial_scores(monkeypatch, runner_dataset):
    """跑到第一条后「用户点了停止」：已产出的得分保留，剩余样本不再打分，且不抛异常。"""
    monkeypatch.setattr(settings, "RUN_ITEM_CONCURRENCY", 1)      # 串行，取消时机确定
    monkeypatch.setattr(settings, "RUN_CANCEL_POLL_SECONDS", 0.05)
    run_id = runner_dataset["run_id"]
    state = {"flipped": False}

    def flip_to_cancelled() -> None:
        if state["flipped"]:
            return
        state["flipped"] = True
        side = SessionLocal()
        side.execute(update(EvalRun).where(EvalRun.id == run_id).values(status="cancelled"))
        side.commit()
        side.close()

    _patch_pipeline(monkeypatch, on_scored=flip_to_cancelled)
    asyncio.run(runner_mod.execute_run(run_id))  # 不得抛异常

    db = SessionLocal()
    run = db.get(EvalRun, run_id)
    rows = db.execute(select(EvalScore).where(EvalScore.run_id == run.id)).scalars().all()

    assert run.status == "cancelled"
    assert run.finished_at is not None
    assert "用户手动停止" in (run.error_msg or "")
    assert run.metrics is None, "半轮均值不能冒充整轮结论"
    assert len(rows) == 1, "已算出的那条必须保留，其余不再打分"
    assert rows[0].status == "ok" and rows[0].faithfulness == pytest.approx(0.9)
    assert run.processed_items == 1
    db.close()
