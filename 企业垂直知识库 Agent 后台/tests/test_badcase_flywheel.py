"""v1.9 数据飞轮：点踩样本一键沉淀到「用户反馈Badcase」评测集。

覆盖：
  - 服务层 add_badcase_from_message：自动建/复用集合、写入 question、幂等去重、
    点踩原因落 message_feedbacks.comment、越权与非法消息类型的拒绝；
  - 接口层 POST /api/v1/chat/badcase：成功契约、参数校验(422)、未登录(401)。
"""
import pytest

import app.services.eval_service as eval_service
from app.core.exceptions import BizError, NotFoundError
from app.models import Conversation, Message
from app.models.eval_dataset import EvalDataset, EvalDatasetItem
from app.models.message import Message as MessageModel
from app.models.message_feedback import MessageFeedback
from conftest import assert_error, assert_ok, make_user

API = "/api/v1/chat/badcase"


@pytest.fixture
def chat_db(sqlite_engine):
    """建好会话 / 消息 / 反馈 / 评测四张表的会话（独立 sqlite 文件）。"""
    Conversation.__table__.create(sqlite_engine)
    MessageModel.__table__.create(sqlite_engine)
    MessageFeedback.__table__.create(sqlite_engine)
    EvalDataset.__table__.create(sqlite_engine)
    EvalDatasetItem.__table__.create(sqlite_engine)

    from sqlalchemy.orm import sessionmaker

    db = sessionmaker(bind=sqlite_engine)()
    yield db
    db.close()


@pytest.fixture(autouse=True)
def _no_global_table_setup(monkeypatch):
    """屏蔽 ensure_eval_tables 对全局 engine 的建表动作（测试用自己的 sqlite 库）。"""
    monkeypatch.setattr(eval_service, "ensure_eval_tables", lambda: None)


def _seed_conv(db, question="差旅住宿标准是多少？", answer="每晚 800 元。", kb_id=7, user_id=2):
    conv = Conversation(user_id=user_id, kb_id=kb_id, title="测试会话")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    q = Message(conversation_id=conv.id, user_id=user_id, role="user", content=question, status="done")
    db.add(q)
    db.commit()
    db.refresh(q)
    a = Message(conversation_id=conv.id, user_id=user_id, role="assistant", content=answer, status="done")
    db.add(a)
    db.commit()
    db.refresh(a)
    return conv, q, a


def test_badcase_creates_dataset_and_item(chat_db):
    user = make_user("user")
    _, _, answer = _seed_conv(chat_db)

    data = eval_service.add_badcase_from_message(chat_db, user, answer.id, "金额答错了")

    assert data["dataset_name"] == eval_service.BAD_CASE_DATASET_NAME
    assert data["duplicated"] is False
    assert data["created_dataset"] is True
    assert data["needs_reference"] is True

    ds = chat_db.get(EvalDataset, data["dataset_id"])
    assert ds.name == "用户反馈Badcase"
    assert ds.kb_id == 7
    assert ds.item_count == 1

    item = chat_db.get(EvalDatasetItem, data["item_id"])
    assert item.question == "差旅住宿标准是多少？"
    # 刻意留空：把 AI 的错误答案当参考答案会让 RAGAS 以幻觉为基准
    assert item.reference_answer == ""
    assert item.stratum == eval_service.BAD_CASE_STRATUM


def test_badcase_reuses_same_dataset_per_kb(chat_db):
    user = make_user("user")
    _, _, a1 = _seed_conv(chat_db)
    first = eval_service.add_badcase_from_message(chat_db, user, a1.id)

    # 同一会话追加一问一答
    conv_id = a1.conversation_id
    chat_db.add(Message(conversation_id=conv_id, user_id=2, role="user", content="报销多久到账？", status="done"))
    chat_db.commit()
    a2 = Message(conversation_id=conv_id, user_id=2, role="assistant", content="3 个工作日。", status="done")
    chat_db.add(a2)
    chat_db.commit()
    chat_db.refresh(a2)

    second = eval_service.add_badcase_from_message(chat_db, user, a2.id, "时效说错了")

    assert second["dataset_id"] == first["dataset_id"]
    assert second["created_dataset"] is False
    ds = chat_db.get(EvalDataset, first["dataset_id"])
    assert ds.item_count == 2
    assert chat_db.query(EvalDataset).count() == 1


def test_badcase_duplicate_question_is_idempotent(chat_db):
    user = make_user("user")
    _, _, answer = _seed_conv(chat_db)
    first = eval_service.add_badcase_from_message(chat_db, user, answer.id)

    # 另一条 AI 回答对应同一个问题（同会话再问一次）
    chat_db.add(Message(conversation_id=answer.conversation_id, user_id=2, role="user",
                        content="差旅住宿标准是多少？", status="done"))
    chat_db.commit()
    dup_answer = Message(conversation_id=answer.conversation_id, user_id=2, role="assistant",
                         content="每晚 500 元。", status="done")
    chat_db.add(dup_answer)
    chat_db.commit()
    chat_db.refresh(dup_answer)

    second = eval_service.add_badcase_from_message(chat_db, user, dup_answer.id)

    assert second["duplicated"] is True
    assert second["item_id"] == first["item_id"]
    assert chat_db.query(EvalDatasetItem).count() == 1


def test_badcase_reason_saved_to_feedback(chat_db):
    user = make_user("user")
    _, _, answer = _seed_conv(chat_db)

    data = eval_service.add_badcase_from_message(chat_db, user, answer.id, "引用了旧版制度")

    assert data["reason_saved"] is True
    fb = chat_db.query(MessageFeedback).filter(MessageFeedback.message_id == answer.id).one()
    assert fb.rating == -1
    assert fb.comment == "引用了旧版制度"


def test_badcase_reason_optional(chat_db):
    user = make_user("user")
    _, _, answer = _seed_conv(chat_db)

    data = eval_service.add_badcase_from_message(chat_db, user, answer.id)

    assert data["reason_saved"] is False
    fb = chat_db.query(MessageFeedback).filter(MessageFeedback.message_id == answer.id).one()
    assert fb.rating == -1 and fb.comment is None


def test_badcase_rejects_other_users_message(chat_db):
    _, _, answer = _seed_conv(chat_db, user_id=2)
    other = make_user("user")
    other.id = 3  # 换个用户 id，模拟「别人的消息」

    with pytest.raises(NotFoundError):
        eval_service.add_badcase_from_message(chat_db, other, answer.id)


def test_badcase_rejects_user_role_message(chat_db):
    user = make_user("user")
    _, question, _ = _seed_conv(chat_db)

    with pytest.raises(BizError) as exc:
        eval_service.add_badcase_from_message(chat_db, user, question.id)
    assert exc.value.code == 400


def test_badcase_requires_kb_binding(chat_db):
    user = make_user("user")
    _, _, answer = _seed_conv(chat_db, kb_id=None)

    with pytest.raises(BizError) as exc:
        eval_service.add_badcase_from_message(chat_db, user, answer.id)
    assert exc.value.code == 400
    assert "知识库" in exc.value.message


def test_badcase_missing_message(chat_db):
    user = make_user("user")
    with pytest.raises(NotFoundError):
        eval_service.add_badcase_from_message(chat_db, user, 99999)


# --------------------------------------------------------------- 接口契约


def test_badcase_api_ok(user_client, monkeypatch):
    monkeypatch.setattr(
        eval_service, "add_badcase_from_message",
        lambda db, user, message_id, reason=None: {
            "dataset_id": 3, "dataset_name": "用户反馈Badcase", "item_id": 9,
            "duplicated": False, "needs_reference": True,
        },
    )
    data = assert_ok(user_client.post(API, json={"message_id": 9, "reason": "答非所问"}))
    assert data["dataset_id"] == 3
    assert data["item_id"] == 9


def test_badcase_api_requires_message_id(user_client):
    assert_error(user_client.post(API, json={"reason": "缺 message_id"}), 422, 422)


def test_badcase_api_requires_login(client):
    assert_error(client.post(API, json={"message_id": 1}), 401, 401)
