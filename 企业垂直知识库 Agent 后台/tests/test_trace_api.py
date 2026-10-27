"""Trace 日志查询接口单元测试骨架（仅新增模块）。

覆盖接口：
  GET /api/v1/traces                      列表（含过滤参数）
  GET /api/v1/traces/stats/summary        汇总
  GET /api/v1/traces/{trace_id}           详情（含工具调用）
  GET /api/v1/traces/{trace_id}/tool-calls 工具调用明细

权限：全部 require_admin。验证点：统一响应体、成功契约、401/403 越权。
"""
import app.services.trace_service as trace_service
from conftest import assert_error, assert_ok

API = "/api/v1/traces"


def _patch_list(monkeypatch, result):
    monkeypatch.setattr(trace_service, "list_traces", lambda *a, **k: result)


def test_list_traces_ok(admin_client, monkeypatch):
    _patch_list(monkeypatch, {"total": 1, "items": [{"trace_id": "t-1", "agent_route": "db_query"}]})
    data = assert_ok(admin_client.get(f"{API}?page=1&size=20"))
    assert data["total"] == 1
    assert data["items"][0]["agent_route"] == "db_query"


def test_list_traces_supports_filters(admin_client, monkeypatch):
    """列表接口应把 user_id/route/status/start/end 透传给服务层。"""
    captured = {}

    def fake(db, page, size, user_id=None, route=None, status=None, start=None, end=None):
        captured.update(user_id=user_id, route=route, status=status)
        return {"total": 0, "items": []}

    monkeypatch.setattr(trace_service, "list_traces", fake)
    assert_ok(admin_client.get(f"{API}?page=1&size=10&user_id=3&route=rag&status=success"))
    assert captured == {"user_id": 3, "route": "rag", "status": "success"}


def test_get_trace_ok(admin_client, monkeypatch):
    monkeypatch.setattr(
        trace_service, "get_trace",
        lambda db, tid: {"trace_id": tid, "agent_route": "report", "tool_calls": [{"step_name": "report_tool"}]},
    )
    data = assert_ok(admin_client.get(f"{API}/t-1"))
    assert data["trace_id"] == "t-1"
    assert data["tool_calls"][0]["step_name"] == "report_tool"


def test_list_tool_calls_ok(admin_client, monkeypatch):
    monkeypatch.setattr(
        trace_service, "list_tool_calls",
        lambda db, tid: {"total": 2, "items": [{"tool_name": "db_query_tool"}, {"tool_name": "rag_tool"}]},
    )
    data = assert_ok(admin_client.get(f"{API}/t-1/tool-calls"))
    assert data["total"] == 2


def test_summary_ok(admin_client, monkeypatch):
    monkeypatch.setattr(
        trace_service, "summary",
        lambda db, start=None, end=None: {
            "total": 10, "avg_latency_ms": 123.45, "token_sum": 999,
            "llm_call_sum": 20, "route_distribution": {"rag": 6, "db_query": 4},
        },
    )
    data = assert_ok(admin_client.get(f"{API}/stats/summary"))
    assert data["route_distribution"]["rag"] == 6


def test_traces_requires_admin(user_client):
    """普通用户访问 Trace 接口 -> 403。"""
    assert_error(user_client.get(API), status=403, code=403)


def test_traces_requires_auth(client):
    """未登录访问 Trace 接口 -> 401。"""
    assert_error(client.get(API), status=401, code=401)
