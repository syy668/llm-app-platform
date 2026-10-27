"""报表删除接口 + 会话首问自动标题单元测试（离线运行，不依赖 MySQL / Redis / DeepSeek）。

覆盖：
- DELETE /reports/{id}：未登录 401；本人删除成功（物理删除消息并提交）；报表不存在 404；
- build_auto_title：短问原样、长问截断补省略号、空白压缩、空问返回 None；
- _maybe_autotitle：title 为空才填充；已重命名（title 非空）不覆盖；写库失败不外抛。
"""
from types import SimpleNamespace

import pytest

from app.api.v1 import reports as reports_api
from app.services.agent_service import AUTO_TITLE_MAX_LEN, _maybe_autotitle, build_auto_title
from conftest import assert_error, assert_ok


# ---------------- 报表删除 ----------------

def test_delete_report_requires_login(client):
    """未登录删除报表返回 401。"""
    assert_error(client.delete("/api/v1/reports/1"), status=401, code=401)


class _FakeDb:
    """记录 delete/commit 调用的假 DB 会话。"""

    def __init__(self):
        self.deleted = []
        self.commits = 0

    def delete(self, obj):
        self.deleted.append(obj)

    def commit(self):
        self.commits += 1


@pytest.fixture
def fake_report_msg():
    return SimpleNamespace(id=9, conversation_id=3, role="assistant", agent_route="report", content="# 测试报表")


def test_delete_report_success(monkeypatch, admin_client, fake_report_msg):
    """本人删除报表：删除传入的消息对象并提交事务。"""
    fake_db = _FakeDb()
    monkeypatch.setattr(reports_api, "_report_msg", lambda db, user, rid: fake_report_msg)
    from app.core.database import get_db

    admin_client.app.dependency_overrides[get_db] = lambda: fake_db
    data = assert_ok(admin_client.delete("/api/v1/reports/9"))
    assert data == {}
    assert fake_db.deleted == [fake_report_msg]
    assert fake_db.commits == 1


def test_delete_report_not_found(monkeypatch, admin_client):
    """报表不存在（_report_msg 抛 NotFoundError）返回 404。"""
    from app.core.exceptions import NotFoundError

    def _raise(db, user, rid):
        raise NotFoundError("报表不存在")

    monkeypatch.setattr(reports_api, "_report_msg", _raise)
    assert_error(admin_client.delete("/api/v1/reports/999"), status=404, code=404)


# ---------------- 首问自动标题 ----------------

def test_build_auto_title_short():
    """短问题原样作为标题（内部空白压缩为单空格）。"""
    assert build_auto_title("打印机怎么连接") == "打印机怎么连接"
    assert build_auto_title("  库存  查询  ") == "库存 查询"


def test_build_auto_title_long_truncated():
    """超过 30 字的问题截断并补省略号。"""
    long_q = "请帮我查一下最近三个月各部门的差旅费用占比情况并生成对比分析"  # 30 字
    assert len(long_q) == AUTO_TITLE_MAX_LEN
    assert build_auto_title(long_q) == long_q  # 恰好 30 字不截断
    assert build_auto_title(long_q + "再来一份报表") == long_q + "…"


def test_build_auto_title_empty():
    """空问题返回 None（不生成标题）。"""
    assert build_auto_title("") is None
    assert build_auto_title("   \n  ") is None


class _TitleDb:
    """仅提供 commit/rollback 的假 DB；rollback 模拟 SQLAlchemy 还原会话属性。"""

    def __init__(self, conv=None, fail=False):
        self.conv = conv
        self.fail = fail
        self.commits = 0

    def commit(self):
        if self.fail:
            raise RuntimeError("db down")
        self.commits += 1

    def rollback(self):
        # ORM 会话 rollback 会还原未提交的属性修改，这里模拟同款行为
        if self.conv is not None:
            self.conv.title = self.conv._orig_title


def test_autotitle_fills_empty_title():
    """title 为空时用首问摘要填充。"""
    conv = SimpleNamespace(id=1, title=None)
    _maybe_autotitle(_TitleDb(), conv, "怎么申请年假")
    assert conv.title == "怎么申请年假"


def test_autotitle_skips_renamed():
    """已重命名（title 非空）的会话不被覆盖。"""
    conv = SimpleNamespace(id=1, title="用例")
    _maybe_autotitle(_TitleDb(), conv, "怎么申请年假")
    assert conv.title == "用例"


def test_autotitle_swallow_db_error():
    """写库失败仅回滚还原标题，不外抛、不影响问答主链路。"""
    conv = SimpleNamespace(id=1, title=None)
    conv._orig_title = conv.title
    _maybe_autotitle(_TitleDb(conv=conv, fail=True), conv, "怎么申请年假")
    assert conv.title is None
