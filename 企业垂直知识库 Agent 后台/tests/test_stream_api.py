"""SSE 流式问答接口单元测试骨架（仅新增模块）。

覆盖接口：POST /api/v1/conversations/{id}/chat/stream

事件协议：route / delta / report / done / error（text/event-stream）。
验证点：媒体类型、SSE 事件格式、report 直出 Markdown、异常事件透传、鉴权越权、参数校验。
"""
import json

import app.services.stream_service as stream_service
from conftest import assert_error, make_user
from app.api.deps import get_current_user, get_own_conversation
from app.core.exceptions import ForbiddenError

API = "/api/v1/conversations/1/chat/stream"


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _fake_stream(user, conversation_id, question, kb_id=None):
    yield _sse("route", {"route": "db_query"})
    yield _sse("delta", {"content": "工单"})
    yield _sse("done", {"trace_id": "t-1", "conversation_id": 1, "message_id": 1, "agent_route": "db_query"})


def test_stream_returns_sse_events(stream_client, monkeypatch):
    monkeypatch.setattr(stream_service, "stream_chat", _fake_stream)
    resp = stream_client.post(API, json={"question": "查询所有工单"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "event: route" in resp.text
    assert "event: delta" in resp.text
    assert "event: done" in resp.text
    assert '"route": "db_query"' in resp.text


async def _report_stream(user, conversation_id, question, kb_id=None):
    yield _sse("route", {"route": "report"})
    yield _sse("report", {"content": "# 工单汇总报表\n\n> 报表由系统自动生成"})
    yield _sse("done", {"trace_id": "t-2", "conversation_id": 1, "message_id": 2, "agent_route": "report"})


def test_stream_report_outputs_markdown(stream_client, monkeypatch):
    """report 路由：SSE 应含 report 事件且内容为 Markdown（不走 LLM 合成）。"""
    monkeypatch.setattr(stream_service, "stream_chat", _report_stream)
    resp = stream_client.post(API, json={"question": "生成本月工单汇总报表"})
    assert resp.status_code == 200
    assert "event: report" in resp.text
    assert "报表由系统自动生成" in resp.text


async def _error_stream(user, conversation_id, question, kb_id=None):
    yield _sse("error", {"message": "AI 服务调用失败，请稍后重试"})


def test_stream_propagates_error_event(stream_client, monkeypatch):
    """服务内部异常时向前端推送 error 事件，连接正常关闭。"""
    monkeypatch.setattr(stream_service, "stream_chat", _error_stream)
    resp = stream_client.post(API, json={"question": "查询所有工单"})
    assert resp.status_code == 200
    assert "event: error" in resp.text


def test_stream_empty_question_returns_422(stream_client):
    """空提问 -> 422（ChatRequest.question min_length=1）。"""
    assert_error(stream_client.post(API, json={"question": ""}), status=422, code=422)


def test_stream_question_too_long_returns_422(stream_client):
    """超长提问(>4000) -> 422（ChatRequest.question max_length=4000）。"""
    assert_error(stream_client.post(API, json={"question": "x" * 4001}), status=422, code=422)


def test_stream_cross_conversation_returns_403(client):
    """跨会话越权：get_own_conversation 抛 ForbiddenError -> 403。"""
    app = client.app
    app.dependency_overrides[get_current_user] = lambda: make_user("user")

    def _forbidden():
        raise ForbiddenError("无权访问该会话")

    app.dependency_overrides[get_own_conversation] = _forbidden
    assert_error(client.post(API, json={"question": "你好"}), status=403, code=403)


def test_stream_requires_auth(client):
    """未登录 -> 401。"""
    assert_error(client.post(API, json={"question": "你好"}), status=401, code=401)
