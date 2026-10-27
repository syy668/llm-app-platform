"""RAGAS 评测接口单元测试骨架（仅新增模块）。

覆盖接口：
  POST /api/v1/eval/datasets                 创建数据集
  GET  /api/v1/eval/datasets                 数据集列表
  POST /api/v1/eval/datasets/{id}/items      导入样本
  GET  /api/v1/eval/datasets/{id}/items      样本列表
  POST /api/v1/eval/runs                     触发评测运行
  GET  /api/v1/eval/runs                     运行列表
  GET  /api/v1/eval/runs/{id}                运行详情
  GET  /api/v1/eval/runs/{id}/scores         单条得分

权限：全部 require_admin。验证点：统一响应体、成功契约、Pydantic 校验(422)、越权。
"""
import app.services.eval_service as eval_service
from conftest import assert_error, assert_ok

API = "/api/v1/eval"


def test_create_dataset_ok(admin_client, monkeypatch):
    monkeypatch.setattr(
        eval_service, "create_dataset",
        lambda db, user, data: {"id": 1, "kb_id": data.kb_id, "name": data.name},
    )
    data = assert_ok(admin_client.post(f"{API}/datasets", json={"kb_id": 10, "name": "冒烟数据集"}))
    assert data["id"] == 1
    assert data["kb_id"] == 10


def test_list_datasets_ok(admin_client, monkeypatch):
    monkeypatch.setattr(
        eval_service, "list_datasets",
        lambda db, page, size, kb_id=None: {"total": 1, "items": [{"id": 1}]},
    )
    data = assert_ok(admin_client.get(f"{API}/datasets?page=1&size=20"))
    assert data["total"] == 1


def test_add_items_ok(admin_client, monkeypatch):
    monkeypatch.setattr(eval_service, "add_items", lambda db, did, data: {"imported": 2, "item_count": 2})
    payload = {
        "items": [
            {"question": "查询所有工单", "reference_answer": "应包含多条工单", "reference_contexts": ["工单表"]},
            {"question": "查询所有告警", "reference_answer": "应包含多条告警"},
        ]
    }
    data = assert_ok(admin_client.post(f"{API}/datasets/1/items", json=payload))
    assert data["imported"] == 2


def test_list_items_ok(admin_client, monkeypatch):
    monkeypatch.setattr(
        eval_service, "list_items",
        lambda db, did, page, size: {"total": 2, "items": [{"question": "查询所有工单"}]},
    )
    data = assert_ok(admin_client.get(f"{API}/datasets/1/items?page=1&size=20"))
    assert data["total"] == 2


def test_create_run_ok(admin_client, monkeypatch):
    monkeypatch.setattr(
        eval_service, "create_run",
        lambda db, user, data: {"id": 5, "dataset_id": data.dataset_id, "status": "pending"},
    )
    data = assert_ok(admin_client.post(f"{API}/runs", json={"dataset_id": 1}))
    assert data["id"] == 5
    assert data["status"] == "pending"


def test_list_runs_ok(admin_client, monkeypatch):
    monkeypatch.setattr(
        eval_service, "list_runs",
        lambda db, page, size, status=None: {"total": 1, "items": [{"id": 5, "status": "done"}]},
    )
    data = assert_ok(admin_client.get(f"{API}/runs?status=done"))
    assert data["items"][0]["status"] == "done"


def test_get_run_ok(admin_client, monkeypatch):
    monkeypatch.setattr(
        eval_service, "get_run",
        lambda db, rid: {"id": rid, "status": "done", "metrics": {"faithfulness": 0.9}},
    )
    data = assert_ok(admin_client.get(f"{API}/runs/5"))
    assert data["metrics"]["faithfulness"] == 0.9


def test_list_scores_ok(admin_client, monkeypatch):
    monkeypatch.setattr(
        eval_service, "list_scores",
        lambda db, rid, page, size: {"total": 1, "items": [{"faithfulness": 0.9, "answer_relevancy": 0.8}]},
    )
    data = assert_ok(admin_client.get(f"{API}/runs/5/scores?page=1&size=20"))
    assert data["items"][0]["faithfulness"] == 0.9


# ---------- Pydantic 校验：422 ----------

def test_add_items_empty_returns_422(admin_client):
    """items 为空数组 -> 422（ItemsImport.items min_length=1）。"""
    assert_error(admin_client.post(f"{API}/datasets/1/items", json={"items": []}), status=422, code=422)


def test_create_dataset_invalid_kb_returns_422(admin_client):
    """kb_id=0 -> 422（Field ge=1）。"""
    assert_error(admin_client.post(f"{API}/datasets", json={"kb_id": 0, "name": "x"}), status=422, code=422)


# ---------- 权限：401 / 403 ----------

def test_eval_read_open_to_login_user(user_client, monkeypatch):
    """【可选2调整】GET 查询接口放开为登录可读：普通用户 GET 200（原为 403）。"""
    monkeypatch.setattr(
        eval_service, "list_datasets",
        lambda db, page, size, kb_id=None: {"total": 0, "items": []},
    )
    data = assert_ok(user_client.get(f"{API}/datasets?page=1&size=20"))
    assert data["total"] == 0


def test_eval_write_requires_admin(user_client):
    """写操作仍仅管理员：普通用户 POST -> 403。"""
    assert_error(user_client.post(f"{API}/datasets", json={"kb_id": 1, "name": "x"}), status=403, code=403)


def test_eval_delete_requires_admin(user_client):
    """【新增】DELETE 接口仅管理员：普通用户 DELETE -> 403。"""
    assert_error(user_client.delete(f"{API}/datasets/1"), status=403, code=403)
    assert_error(user_client.delete(f"{API}/runs/1"), status=403, code=403)


def test_eval_requires_auth(client):
    assert_error(client.get(f"{API}/datasets"), status=401, code=401)
    assert_error(client.post(f"{API}/runs", json={"dataset_id": 1}), status=401, code=401)
