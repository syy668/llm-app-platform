"""第四步契约测试：跨系统触发的 dataset_id 解析（主键 / 业务编码）。

为什么单独锁这一条：
项目三 AgentFlow 的 `EvalRunCreateRequest.dataset_id` 是**字符串语义**
（默认 "agentflow-30"），而本平台早期只接受 int 主键 → 联调时 422，
项目三会把它包装成 502「项目二 eval API 不可达」，排障方向直接跑偏。
所以这里用测试把「字符串编码能解析到评测集」这件事钉死。
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def _create_dataset(client, code: str, with_items: bool = False) -> int:
    ds = client.post(
        "/api/v1/eval/datasets",
        json={"name": f"契约测试集 {code}", "code": code, "target_type": "agentflow", "kb_id": 6},
    ).json()["data"]
    if with_items:
        client.post(
            f"/api/v1/eval/datasets/{ds['id']}/items",
            json={"items": [{"question": "契约测试问题？", "reference_answer": "契约测试答案"}]},
        )
    return ds["id"]


def test_create_run_accepts_dataset_code(client):
    """字符串业务编码（项目三的载荷形状）必须能解析到评测集，且失败信息要区分「无样本」与「不存在」。"""
    ds_id = _create_dataset(client, "EXT-CONTRACT-EMPTY", with_items=False)
    try:
        res = client.post("/api/v1/eval/runs", json={"dataset_id": "EXT-CONTRACT-EMPTY"})
        # 解析成功才会走到「没有样本」这条业务校验；若解析失败会是 404 不存在
        assert res.status_code == 409, res.text
        assert "没有样本" in res.json()["message"]
    finally:
        client.delete(f"/api/v1/eval/datasets/{ds_id}")


def test_create_run_accepts_int_primary_key(client):
    """前端 evalagent_vue 传的是 int 主键，必须继续兼容。"""
    ds_id = _create_dataset(client, "EXT-CONTRACT-INT", with_items=False)
    try:
        res = client.post("/api/v1/eval/runs", json={"dataset_id": ds_id})
        assert res.status_code == 409, res.text
        assert "没有样本" in res.json()["message"]
    finally:
        client.delete(f"/api/v1/eval/datasets/{ds_id}")


def test_unknown_dataset_code_404_with_actionable_message(client):
    res = client.post("/api/v1/eval/runs", json={"dataset_id": "NO-SUCH-CODE"})
    assert res.status_code == 404
    msg = res.json()["message"]
    assert "NO-SUCH-CODE" in msg
    assert "code" in msg  # 文案要告诉调用方「用 id 或 code 触发」，别让人去猜


def test_agentflow_payload_shape_is_accepted(client):
    """完整模拟项目三 create_run 发的 payload：target / variant / callback_run_no 全带。"""
    ds_id = _create_dataset(client, "EXT-CONTRACT-FULL", with_items=True)
    try:
        res = client.post(
            "/api/v1/eval/runs",
            json={
                "dataset_id": "EXT-CONTRACT-FULL",
                "target": "agentflow",
                "variant": None,
                "callback_run_no": "AFRUN-CONTRACT-001",
            },
        )
        assert res.status_code == 200, res.text
        data = res.json()["data"]
        assert data["target"] == "agentflow"
        assert data["ext_run_ref"] == "AFRUN-CONTRACT-001"
        # 2026-10 起 dataset_id 返回的是解析后的本地主键，便于前端回查
        assert data["dataset_id"] == ds_id
    finally:
        client.delete(f"/api/v1/eval/datasets/{ds_id}")
