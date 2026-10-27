"""会话重命名接口单元测试（离线运行，不依赖 MySQL / Redis / DeepSeek）。

覆盖：
- PUT /conversations/{id}：仅本人可重命名（越权 403 / 未登录 401）；
- 标题校验（空标题 422、超长 422），保存时去首尾空白；
- 删除接口维持原语义（本文件仅验证 401 场景，业务行为由既有链路保障）。
"""
from types import SimpleNamespace
from datetime import datetime

import pytest

from conftest import assert_error, assert_ok, make_user
from app.api.deps import get_current_user, get_own_conversation
from app.core.exceptions import ForbiddenError

API = "/api/v1/conversations/1"


def _fake_conversation(conv_id: int = 1, user_id: int = 1):
    """构造带完整字段的假会话对象（不落库，仅用于依赖注入与响应序列化）。"""
    return SimpleNamespace(
        id=conv_id,
        user_id=user_id,
        kb_id=None,
        title=None,
        created_at=datetime(2026, 10, 18, 10, 0, 0),
        updated_at=datetime(2026, 10, 18, 10, 0, 0),
    )


@pytest.fixture
def own_client(client):
    """当前用户持有会话 #1 的客户端。"""
    client.app.dependency_overrides[get_current_user] = lambda: make_user("user")
    client.app.dependency_overrides[get_own_conversation] = _fake_conversation
    yield client


def test_rename_requires_login(client):
    """未登录重命名返回 401。"""
    resp = client.put(API, json={"title": "新标题"})
    assert resp.status_code == 401


def test_rename_success(own_client):
    """本人重命名成功，返回更新后的会话（标题去首尾空白）。"""
    data = assert_ok(own_client.put(API, json={"title": "  打印机问题排查  "}))
    assert data["id"] == 1
    assert data["title"] == "打印机问题排查"


def test_rename_empty_title_422(own_client):
    """空标题触发参数校验 422。"""
    assert_error(own_client.put(API, json={"title": ""}), status=422, code=422)
    assert_error(own_client.put(API, json={"title": "   "}), status=422, code=422)


def test_rename_too_long_title_422(own_client):
    """标题超过 100 字触发参数校验 422。"""
    assert_error(own_client.put(API, json={"title": "标" * 101}), status=422, code=422)


def test_rename_cross_conversation_403(client):
    """越权访问他人会话：get_own_conversation 抛 ForbiddenError -> 403。"""
    client.app.dependency_overrides[get_current_user] = lambda: make_user("user")

    def _forbidden():
        raise ForbiddenError("无权访问该会话")

    client.app.dependency_overrides[get_own_conversation] = _forbidden
    assert_error(client.put(API, json={"title": "改你的会话"}), status=403, code=403)


def test_delete_requires_login(client):
    """未登录删除会话返回 401。"""
    resp = client.delete(API)
    assert resp.status_code == 401
