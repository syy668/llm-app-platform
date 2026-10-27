"""第三步测试：聚合口径 + 4 个聚合读接口契约（不调真 LLM，用合成数据）。

真链路（项目一取数 + RAGAS 打分）由 scripts/e2e_eval.py 覆盖，
这里只锁"算得对不对、字段对不对、边界怎么处理"。
"""

import time

import pytest
from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.main import app
from app.models import EvalDataset, EvalItem, EvalRun, EvalScore
from app.services.aggregate import compare_scores, mean_metrics, stratum_metrics

CORE = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")


# ============================ 纯计算 ============================
def test_mean_metrics_ignores_null_and_returns_none_when_all_null():
    rows = [
        {"faithfulness": 1.0, "answer_relevancy": None, "context_precision": 0.5, "context_recall": None},
        {"faithfulness": 0.0, "answer_relevancy": None, "context_precision": 1.0, "context_recall": None},
    ]
    m = mean_metrics(rows)
    assert m["faithfulness"] == 0.5  # 0.0 是真实得分，必须参与均值
    assert m["context_precision"] == 0.75
    assert m["answer_relevancy"] is None  # 全空 -> None，不是 0

    assert mean_metrics([{k: None for k in CORE}]) is None


def test_stratum_metrics_groups_unlabeled_separately():
    rows = [
        {"stratum": "easy", "faithfulness": 1.0, "answer_relevancy": 1.0,
         "context_precision": 1.0, "context_recall": 1.0},
        {"stratum": "easy", "faithfulness": 0.0, "answer_relevancy": 0.0,
         "context_precision": 0.0, "context_recall": 0.0},
        {"stratum": None, "faithfulness": 0.5, "answer_relevancy": None,
         "context_precision": None, "context_recall": None},
    ]
    agg = stratum_metrics(rows)
    assert agg["total"] == 3
    easy = next(i for i in agg["items"] if i["stratum"] == "easy")
    assert easy["sample_count"] == 2
    assert easy["metrics"]["faithfulness"] == 0.5
    unlabeled = next(i for i in agg["items"] if i["stratum"] is None)
    assert unlabeled["metrics"]["answer_relevancy"] is None


def test_compare_aligns_by_question_and_keeps_missing_side_null():
    a = [{"question": "Q1", "faithfulness": 0.9}, {"question": "Q2", "faithfulness": 0.4}]
    b = [{"question": "Q1", "faithfulness": 0.5}, {"question": "Q3", "faithfulness": 0.8}]
    for row in a + b:
        for k in CORE:
            row.setdefault(k, None)
    res = compare_scores({"metrics": None}, {"metrics": None}, a, b, 0.1)

    q1 = next(r for r in res["rows"] if r["question"] == "Q1")
    assert q1["diffs"]["faithfulness"] == pytest.approx(-0.4)
    assert q1["is_diff"] is True  # |Δ| > 0.1

    q3 = next(r for r in res["rows"] if r["question"] == "Q3")
    assert q3["a"] is None and q3["diffs"]["faithfulness"] is None  # 单边样本不给 Δ

    summary = {s["metric"]: s for s in res["summary"]}
    assert summary["faithfulness"]["avg_a"] == pytest.approx(0.65)
    assert summary["faithfulness"]["avg_b"] == pytest.approx(0.65)


# ============================ 接口契约（合成数据）============================
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def seeded():
    """造 1 个评测集 + 1 个 done 的 run + 3 条明细（含 NULL 与失败行）。"""
    db = SessionLocal()
    tag = f"T3-{int(time.time())}"
    ds = EvalDataset(name=f"契约测试 {tag}", code=tag, target_type="kb", kb_id=6, item_count=3)
    db.add(ds)
    db.flush()

    items = [
        EvalItem(dataset_id=ds.id, seq=1, question="Q-easy", reference_answer="A1", stratum="easy"),
        EvalItem(dataset_id=ds.id, seq=2, question="Q-hard", reference_answer="A2", stratum="hard"),
        EvalItem(dataset_id=ds.id, seq=3, question="Q-null", reference_answer="A3", stratum=None),
    ]
    db.add_all(items)
    db.flush()

    run = EvalRun(
        run_no=f"EV-T3-{int(time.time())}", dataset_id=ds.id, target="kb", status="done",
        total_items=3, processed_items=3, success_items=2, failed_items=1,
        metrics={"faithfulness": 0.5, "answer_relevancy": 0.6,
                 "context_precision": 0.7, "context_recall": 0.8},
        variant="baseline", rag_config={"top_k": 3},
    )
    db.add(run)
    db.flush()

    db.add_all([
        EvalScore(run_id=run.id, item_id=items[0].id, item_seq=1, stratum="easy",
                  question="Q-easy", answer="答1", reference_answer="A1",
                  faithfulness=0.9, answer_relevancy=0.9, context_precision=0.9, context_recall=0.9,
                  latency_ms=120, status="ok"),
        EvalScore(run_id=run.id, item_id=items[1].id, item_seq=2, stratum="hard",
                  question="Q-hard", answer="答2", reference_answer="A2",
                  faithfulness=0.1, answer_relevancy=0.2, context_precision=0.3, context_recall=0.4,
                  latency_ms=200, suspect=True, status="ok"),
        EvalScore(run_id=run.id, item_id=items[2].id, item_seq=3, stratum=None,
                  question="Q-null", answer=None, reference_answer="A3",
                  latency_ms=0, status="failed", error_msg="上游未召回任何上下文"),
    ])
    db.commit()

    ids = {"dataset_id": ds.id, "run_id": run.id}
    yield ids

    db.execute(EvalScore.__table__.delete().where(EvalScore.run_id == run.id))
    db.delete(run)
    db.delete(ds)
    db.commit()
    db.close()


def test_run_detail_has_compat_fields(client, seeded):
    data = client.get(f"/api/v1/eval/runs/{seeded['run_id']}").json()["data"]
    assert data["status"] == "done"
    assert data["kb_id"] == 6           # 兼容字段：前端列表页读它
    assert data["detail"]["samples_count"] == 2  # 兼容字段：项目三轮询读它


def test_scores_pagination_and_status_filter(client, seeded):
    all_rows = client.get(f"/api/v1/eval/runs/{seeded['run_id']}/scores?page=1&size=10").json()["data"]
    assert all_rows["total"] == 3

    only_failed = client.get(
        f"/api/v1/eval/runs/{seeded['run_id']}/scores?status=failed"
    ).json()["data"]
    assert only_failed["total"] == 1
    assert only_failed["items"][0]["faithfulness"] is None


def test_badcases_filters_nulls_out(client, seeded):
    data = client.get(
        f"/api/v1/eval/runs/{seeded['run_id']}/badcases?metric=faithfulness&threshold=0.5"
    ).json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["question"] == "Q-hard"
    assert data["items"][0]["ground_truth"] == "A2"  # 来自 eval_items，不是 eval_scores
    assert data["metric"] == "faithfulness"

    # 失败行的 faithfulness 是 NULL，不能被当成 0 分混进 BadCase
    assert all(row["faithfulness"] is not None for row in data["items"])


def test_badcases_rejects_unknown_metric(client, seeded):
    res = client.get(
        f"/api/v1/eval/runs/{seeded['run_id']}/badcases?metric=drop_table"
    )
    assert res.status_code == 422
    assert "metric 非法" in res.json()["message"]


def test_stratum_metrics_endpoint(client, seeded):
    data = client.get(f"/api/v1/eval/runs/{seeded['run_id']}/stratum_metrics").json()["data"]
    assert data["run_id"] == seeded["run_id"]
    assert data["total"] == 2  # 只统计 status=ok 的两条
    strata = {i["stratum"]: i for i in data["items"]}
    assert strata["easy"]["metrics"]["faithfulness"] == 0.9
    assert strata["hard"]["sample_count"] == 1


def test_export_csv_has_bom_and_header(client, seeded):
    res = client.get(f"/api/v1/eval/runs/{seeded['run_id']}/export")
    assert res.status_code == 200
    assert res.content.startswith("\ufeff".encode("utf-8"))  # Excel 中文不乱码
    text = res.content.decode("utf-8-sig")
    assert "忠实度" in text.splitlines()[0]
    assert len(text.splitlines()) == 4  # 表头 + 3 行


def test_compare_requires_same_dataset(client, seeded):
    db = SessionLocal()
    other = EvalDataset(name="另一个集", code=f"OTHER-{int(time.time())}", target_type="kb", kb_id=6)
    db.add(other)
    db.flush()
    other_run = EvalRun(run_no=f"EV-T3B-{int(time.time())}", dataset_id=other.id, status="done")
    db.add(other_run)
    db.commit()
    other_run_id, other_id = other_run.id, other.id
    db.close()

    try:
        res = client.post(
            "/api/v1/eval/runs/compare",
            json={"run_id_a": seeded["run_id"], "run_id_b": other_run_id},
        )
        assert res.status_code == 400
        assert "同一评测集" in res.json()["message"]
    finally:
        db = SessionLocal()
        db.execute(EvalScore.__table__.delete().where(EvalScore.run_id == other_run_id))
        db.delete(db.get(EvalRun, other_run_id))
        db.delete(db.get(EvalDataset, other_id))
        db.commit()
        db.close()


def test_compare_returns_summary_rows(client, seeded):
    res = client.post(
        "/api/v1/eval/runs/compare",
        json={"run_id_a": seeded["run_id"], "run_id_b": seeded["run_id"]},
    ).json()["data"]
    assert len(res["summary"]) == 4
    assert res["diff_threshold"] == 0.1
    # 自己比自己：Δ 必须全为 0
    assert all(row["diff"] == 0 for row in res["summary"])


def test_create_run_validates_dataset(client):
    res = client.post("/api/v1/eval/runs", json={"dataset_id": 999999})
    assert res.status_code == 404
    assert "不存在" in res.json()["message"]
