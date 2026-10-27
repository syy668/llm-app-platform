"""第十步：向已有评测集「追加样本」。

锁三件事：
1. **seq 必须接着现有最大序号往下排**（不能从 1 重排）—— 历史得分明细里的 item_seq 是旧样本的快照，
   重排会让"当时的第 3 条"和"现在的第 3 条"对不上，A/B 对齐直接错位；
2. `item_count` 必须等于真实条数（不是 max_seq + n，历史上删过样本就会漂）；
3. **总量闸**：单数据集 ≤ DATASET_MAX_ITEMS。没有这道闸，反复追加能把页面承诺的"最多 500 条"变成假话。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import EvalDataset, EvalItem


@pytest.fixture()
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def _seed(count: int = 2) -> dict:
    db = SessionLocal()
    tag = f"T10-{int(time.time() * 1000)}"
    ds = EvalDataset(name=f"追加测试 {tag}", code=tag, target_type="kb", kb_id=6, item_count=count)
    db.add(ds)
    db.flush()
    db.add_all([
        EvalItem(
            dataset_id=ds.id, seq=i, question=f"Q{i}", reference_answer=f"A{i}",
            stratum="easy" if i == 1 else None,
        )
        for i in range(1, count + 1)
    ])
    db.commit()
    ids = {"dataset_id": ds.id}
    db.close()
    return ids


def _cleanup(dataset_id: int) -> None:
    db = SessionLocal()
    db.execute(EvalItem.__table__.delete().where(EvalItem.dataset_id == dataset_id))
    db.execute(EvalDataset.__table__.delete().where(EvalDataset.id == dataset_id))
    db.commit()
    db.close()


def _payload(n: int, tag: str = "N") -> dict:
    return {
        "items": [
            {
                "question": f"{tag}-Q{i}",
                "reference_answer": f"{tag}-A{i}",
                "stratum": "hard",
                "difficulty": 3,
                "reference_contexts": [f"{tag}-ctx{i}"],
            }
            for i in range(1, n + 1)
        ]
    }


def test_append_continues_seq_and_updates_count(client):
    ids = _seed(count=2)
    try:
        res = client.post(f"/api/v1/eval/datasets/{ids['dataset_id']}/items", json=_payload(3))
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["imported"] == 3
        assert data["previous_count"] == 2
        assert data["item_count"] == 5

        db = SessionLocal()
        rows = db.execute(
            select(EvalItem).where(EvalItem.dataset_id == ids["dataset_id"]).order_by(EvalItem.seq)
        ).scalars().all()
        # seq 必须是 1,2,3,4,5 —— 接续而不是重排
        assert [r.seq for r in rows] == [1, 2, 3, 4, 5]
        assert rows[0].question == "Q1" and rows[0].stratum == "easy"  # 老样本原样保留
        assert rows[2].question == "N-Q1" and rows[2].stratum == "hard"
        assert rows[2].difficulty == 3 and rows[2].reference_contexts == ["N-ctx1"]
        # 数据集上的计数与真实条数一致
        assert db.get(EvalDataset, ids["dataset_id"]).item_count == 5
        db.close()
    finally:
        _cleanup(ids["dataset_id"])


def test_append_twice_accumulates(client):
    ids = _seed(count=1)
    try:
        client.post(f"/api/v1/eval/datasets/{ids['dataset_id']}/items", json=_payload(2, "A"))
        res = client.post(f"/api/v1/eval/datasets/{ids['dataset_id']}/items", json=_payload(2, "B"))
        assert res.json()["data"]["item_count"] == 5
        db = SessionLocal()
        seqs = [r for r in db.execute(
            select(EvalItem.seq).where(EvalItem.dataset_id == ids["dataset_id"]).order_by(EvalItem.seq)
        ).scalars()]
        assert seqs == [1, 2, 3, 4, 5]
        db.close()
    finally:
        _cleanup(ids["dataset_id"])


def test_append_rejects_when_total_would_exceed_cap(client):
    ids = _seed(count=3)
    try:
        over = settings.DATASET_MAX_ITEMS - 3 + 1
        # 单次请求上限是 500，超了会先被 pydantic 拦；这里恰好卡在总量闸上
        if over > 500:
            pytest.skip("DATASET_MAX_ITEMS 配置过大，单次请求上限先触发，无法命中总量闸")
        res = client.post(f"/api/v1/eval/datasets/{ids['dataset_id']}/items", json=_payload(over))
        assert res.status_code == 409
        message = res.json()["message"]
        assert "超过单数据集上限" in message
        assert str(settings.DATASET_MAX_ITEMS) in message

        # 失败不能留下半截数据
        db = SessionLocal()
        assert db.execute(
            select(func.count()).select_from(EvalItem).where(EvalItem.dataset_id == ids["dataset_id"])
        ).scalar_one() == 3
        assert db.get(EvalDataset, ids["dataset_id"]).item_count == 3
        db.close()
    finally:
        _cleanup(ids["dataset_id"])


def test_append_to_missing_dataset_returns_404(client):
    res = client.post("/api/v1/eval/datasets/999999999/items", json=_payload(1))
    assert res.status_code == 404


def test_append_accepts_exactly_up_to_cap(client):
    """边界：正好填满上限要放行（off-by-one 最容易在这里翻车）。"""
    ids = _seed(count=settings.DATASET_MAX_ITEMS - 2)
    try:
        res = client.post(f"/api/v1/eval/datasets/{ids['dataset_id']}/items", json=_payload(2))
        assert res.status_code == 200, res.json()
        assert res.json()["data"]["item_count"] == settings.DATASET_MAX_ITEMS
    finally:
        _cleanup(ids["dataset_id"])
