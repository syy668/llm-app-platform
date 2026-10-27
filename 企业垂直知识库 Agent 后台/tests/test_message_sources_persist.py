"""v1.10 消息级引用来源快照：让历史消息也能溯源。

背景：v1.9 的 sources 只在 SSE done 事件里随回答一次性下发，页面刷新/重开会话
后重新 GET /conversations/{id}/messages 拿到的消息没有 sources，于是答案里的
「《文档》第N块」点了永远是空的 —— 用户看到的就是「暂无可展开的原文」。

覆盖：
  - Message.sources 落库后能被 msg_to_dict 原样返回（含 chunk_no）；
  - 未写 sources 的老消息返回 None（前端据此降级为不可点击样式）；
  - 落库内容是创建时的快照，不随文档重新分块变化。
"""
import pytest
from sqlalchemy.orm import sessionmaker

from app.models import Conversation, Message
from app.schemas.conversation import msg_to_dict
import app.services.agent_service as agent_service


@pytest.fixture
def msg_db(sqlite_engine):
    """建好会话 / 消息两张表的会话（独立 sqlite 文件）。"""
    Conversation.__table__.create(sqlite_engine)
    Message.__table__.create(sqlite_engine)
    db = sessionmaker(bind=sqlite_engine)()
    yield db
    db.close()


class _User:
    id = 2


SOURCES = [
    {
        "index": 1,
        "chunk_id": 11,
        "document_id": 3,
        "document_title": "公司差旅报销制度.md",
        "chunk_index": 0,
        "chunk_no": 1,
        "score": 0.032,
        "content": "部门经理住宿标准 800 元/晚",
    },
    {
        "index": 2,
        "chunk_id": 12,
        "document_id": 3,
        "document_title": "公司差旅报销制度.md",
        "chunk_index": 1,
        "chunk_no": 2,
        "score": 0.028,
        "content": "报销时限 30 天内",
    },
]


def _seed_conv(db, user_id=2):
    conv = Conversation(user_id=user_id, kb_id=7, title="测试会话")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def test_write_message_persists_sources(msg_db):
    conv = _seed_conv(msg_db)

    msg = agent_service._write_message(
        msg_db, conv, _User(), "assistant", "住宿 800 元（来源1：《公司差旅报销制度.md》第1块）",
        "done", "trace-x", agent_route="rag", sources=SOURCES,
    )

    out = msg_to_dict(msg)
    assert out["sources"] is not None
    assert [s["index"] for s in out["sources"]] == [1, 2]
    # 展示号随快照一起落库，历史消息不需要再回表算
    assert [s["chunk_no"] for s in out["sources"]] == [1, 2]
    assert out["sources"][0]["document_title"] == "公司差旅报销制度.md"
    assert out["sources"][0]["content"].startswith("部门经理住宿标准")


def test_write_message_without_sources_returns_none(msg_db):
    """老消息 / 非检索路由（direct）不写 sources，返回 None。"""
    conv = _seed_conv(msg_db)

    msg = agent_service._write_message(
        msg_db, conv, _User(), "assistant", "你好", "done", "trace-y", agent_route="direct",
    )

    assert msg_to_dict(msg)["sources"] is None


def test_empty_sources_list_is_stored_as_null(msg_db):
    """空列表统一存 NULL：前端只需判 `!sources?.length` 一种情况。"""
    conv = _seed_conv(msg_db)

    msg = agent_service._write_message(
        msg_db, conv, _User(), "assistant", "知识库中未找到相关信息", "done", "trace-z",
        agent_route="rag", sources=[],
    )

    assert msg.sources is None
    assert msg_to_dict(msg)["sources"] is None


def test_sources_snapshot_survives_document_rechunk(msg_db):
    """快照语义：文档重新分块后，历史回答仍指向当时的块内容。"""
    import copy

    conv = _seed_conv(msg_db)
    passed_in = copy.deepcopy(SOURCES)
    msg = agent_service._write_message(
        msg_db, conv, _User(), "assistant", "答案", "done", "trace-s",
        agent_route="rag", sources=passed_in,
    )
    msg_id = msg.id

    # 模拟「文档已重新分块」：调用方手里的列表被改写，落库快照不应受影响
    passed_in[0]["content"] = "（已被重新分块覆盖的内容）"
    msg_db.expire_all()

    again = msg_db.get(Message, msg_id)
    assert "800 元/晚" in again.sources[0]["content"]
