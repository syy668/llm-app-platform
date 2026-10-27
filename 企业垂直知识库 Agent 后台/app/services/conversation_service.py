"""对话会话业务编排：创建、列表、消息列表、重命名、删除。"""
from sqlalchemy.orm import Session

from ..core.exceptions import NotFoundError
from ..models import Conversation, Message
from ..schemas.conversation import conv_to_dict, msg_to_dict


def create_conversation(db: Session, user, kb_id: int | None = None) -> dict:
    """新建会话，归属当前登录用户。"""
    conv = Conversation(user_id=user.id, kb_id=kb_id, title=None)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv_to_dict(conv)


def list_conversations(db: Session, user, page: int, size: int) -> dict:
    """当前用户的会话列表，按最近更新倒序。"""
    query = db.query(Conversation).filter(Conversation.user_id == user.id)
    total = query.count()
    items = (
        query.order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    return {"total": total, "items": [conv_to_dict(c) for c in items]}


def list_messages(db: Session, conv: Conversation, page: int, size: int) -> dict:
    """会话消息列表，按时间正序（保证多轮对话顺序），附带本人反馈状态。"""
    from ..models import MessageFeedback

    query = db.query(Message).filter(Message.conversation_id == conv.id)
    total = query.count()
    items = query.order_by(Message.id.asc()).offset((page - 1) * size).limit(size).all()
    feedback_map: dict[int, int] = {}
    if items:
        rows = (
            db.query(MessageFeedback.message_id, MessageFeedback.rating)
            .filter(
                MessageFeedback.user_id == conv.user_id,
                MessageFeedback.message_id.in_([m.id for m in items]),
            )
            .all()
        )
        feedback_map = {mid: rating for mid, rating in rows}
    result = []
    for m in items:
        d = msg_to_dict(m)
        d["feedback"] = feedback_map.get(m.id)
        result.append(d)
    return {"total": total, "items": result}


def rename_conversation(db: Session, conv: Conversation, title: str) -> dict:
    """重命名会话；标题已由 schema 层校验并去首尾空白，返回更新后的会话。"""
    conv.title = title
    db.commit()
    return conv_to_dict(conv)


def delete_conversation(db: Session, conv: Conversation) -> dict:
    """删除会话；消息表外键 ON DELETE CASCADE 级联删除。"""
    db.delete(conv)
    db.commit()
    return {}


def set_feedback(db: Session, user, message_id: int, rating: int, comment: str | None = None) -> dict:
    """对本人会话内的消息做点赞/点踩反馈（upsert；重复提交更新评分与备注）。"""
    from ..models import MessageFeedback

    msg = db.get(Message, message_id)
    if not msg or msg.user_id != user.id:
        raise NotFoundError("消息不存在")
    fb = (
        db.query(MessageFeedback)
        .filter(MessageFeedback.message_id == message_id, MessageFeedback.user_id == user.id)
        .first()
    )
    if fb:
        fb.rating = rating
        fb.comment = comment
    else:
        fb = MessageFeedback(message_id=message_id, user_id=user.id, rating=rating, comment=comment)
        db.add(fb)
    db.commit()
    return {"message_id": message_id, "rating": rating}
