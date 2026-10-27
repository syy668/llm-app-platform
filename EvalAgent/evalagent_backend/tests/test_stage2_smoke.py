"""第二阶段冒烟测试：服务可启动、信封正确、四表可写可读。

跑法（真实 MySQL eval_agent_db）：
    .venv/Scripts/python.exe -m pytest tests -q
"""

import time

import pytest
from fastapi.testclient import TestClient

from app import __version__
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_version_single_source_of_truth():
    """版本号只认 app/__init__.py，OpenAPI 与之一致（防止改文档忘改代码）。"""
    assert app.version == __version__
    assert app.openapi()["info"]["version"] == __version__


def test_health_ok(client):
    res = client.get("/healthz")
    assert res.status_code == 200
    body = res.json()
    assert body["code"] == 0
    assert body["data"]["database"].startswith("MySQL")


def test_root_and_api_health(client):
    root = client.get("/").json()
    assert root["code"] == 0
    assert root["data"]["database"]["healthy"] is True

    api = client.get("/api/v1/health").json()
    assert api["data"]["database_healthy"] is True


def test_error_envelope_is_always_wrapped(client):
    """错误也必须是 {code, message, data}，否则前端拦截器拿不到 message。"""
    res = client.get("/api/v1/eval/runs/999999")
    assert res.status_code == 404
    body = res.json()
    assert body["code"] == 404
    assert "不存在" in body["message"]
    assert body["data"] is None


def test_compat_prefix_mount(client):
    """/eval/* 与 /api/v1/eval/* 必须指向同一实现（前端与项目三两套习惯）。"""
    a = client.get("/eval/runs?page=1&size=1")
    b = client.get("/api/v1/eval/runs?page=1&size=1")
    assert a.status_code == b.status_code == 200
    assert a.json()["code"] == b.json()["code"] == 0


def test_dataset_and_items_roundtrip(client):
    """建集 → 导样本 → 查明细 → 删集，验证四表里两张（datasets/items）真能写。"""
    code = f"DS-SMOKE-{int(time.time())}"
    created = client.post(
        "/api/v1/eval/datasets",
        json={
            "name": "冒烟测试集",
            "code": code,
            "target_type": "kb",
            "kb_id": 1,
            "agent_scope": [
                {"role": "retriever", "weight": 0.4},
                {"role": "summarizer", "weight": 0.6},
            ],
        },
    )
    assert created.status_code == 200, created.text
    ds = created.json()["data"]
    assert ds["agent_scope"][0]["role"] == "retriever"

    try:
        imported = client.post(
            f"/api/v1/eval/datasets/{ds['id']}/items",
            json={
                "items": [
                    {"question": "Q1", "reference_answer": "A1", "stratum": "easy"},
                    {"question": "Q2", "reference_answer": "A2", "stratum": "hard"},
                ]
            },
        )
        # 只断言契约字段，不做整体字典相等：接口后续可能追加字段（如 previous_count），
        # 严格相等会让"加了兼容字段"被误判成破坏性变更
        payload = imported.json()["data"]
        assert payload["imported"] == 2
        assert payload["item_count"] == 2

        listed = client.get(f"/api/v1/eval/datasets/{ds['id']}/items?page=1&size=10").json()
        assert listed["data"]["total"] == 2
        assert listed["data"]["items"][0]["seq"] == 1  # seq 必须 1-based

        # 重复 code 必须 409，而不是静默建出第二条
        dup = client.post(
            "/api/v1/eval/datasets", json={"name": "重复", "code": code, "target_type": "kb"}
        )
        assert dup.status_code == 409
    finally:
        removed = client.delete(f"/api/v1/eval/datasets/{ds['id']}").json()
        assert removed["data"]["deleted_dataset_id"] == ds["id"]


def test_post_runs_rejects_empty_dataset(client):
    """第三步起 POST /eval/runs 已实现；空评测集必须 409，不能发起"空跑"。

    （第二阶段此断言是 501「引擎未启用」，第三步落地后改为锁 409 的前置校验。）
    """
    ds = client.post(
        "/api/v1/eval/datasets", json={"name": "空集验证", "target_type": "kb"}
    ).json()["data"]
    try:
        res = client.post("/api/v1/eval/runs", json={"dataset_id": ds["id"]})
        assert res.status_code == 409
        assert "没有样本" in res.json()["message"]
    finally:
        client.delete(f"/api/v1/eval/datasets/{ds['id']}")
