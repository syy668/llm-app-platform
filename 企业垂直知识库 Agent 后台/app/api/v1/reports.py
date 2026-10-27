"""报表接口：会话内生成的 Markdown 报表的列表 / 详情 / 下载。

报表 = agent_route='report' 的 AI 回复消息（report 节点直出，不落独立表），
仅返回当前登录用户本人会话中的报表。
"""
from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from ..deps import CurrentUser, get_current_user
from ...core.database import get_db
from ...core.exceptions import NotFoundError
from ...core.response import ok
from ...models import Conversation, Message

router = APIRouter(prefix="/reports", tags=["报表"])


def _report_msg(db: Session, user, report_id: int) -> Message:
    """按 ID 取本人的报表消息（report 路由的 assistant 消息）。"""
    msg = (
        db.query(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .filter(
            Message.id == report_id,
            Message.role == "assistant",
            Message.agent_route == "report",
            Conversation.user_id == user.id,
        )
        .first()
    )
    if not msg:
        raise NotFoundError("报表不存在")
    return msg


@router.get("")
def list_reports(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """本人已生成的报表列表（时间倒序）。"""
    q = (
        db.query(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .filter(
            Message.role == "assistant",
            Message.agent_route == "report",
            Conversation.user_id == user.id,
        )
    )
    total = q.count()
    items = q.order_by(Message.id.desc()).offset((page - 1) * size).limit(size).all()
    return ok({
        "total": total,
        "items": [
            {
                "id": m.id,
                "conversation_id": m.conversation_id,
                "title": (m.content.splitlines()[0].lstrip("# ").strip() if m.content else "") or f"报表 #{m.id}",
                "preview": m.content[:120],
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in items
        ],
    })


@router.get("/{report_id}")
def get_report(
    report_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """报表内容（Markdown 文本）。"""
    msg = _report_msg(db, user, report_id)
    return ok({"id": msg.id, "conversation_id": msg.conversation_id, "content": msg.content})


@router.get("/{report_id}/download")
def download_report(
    report_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """报表下载（.md 文件流，文件名 报表_{id}.md）。"""
    msg = _report_msg(db, user, report_id)
    return Response(
        content=msg.content or "",
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="report_{msg.id}.md"'},
    )


@router.delete("/{report_id}")
def delete_report(
    report_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除报表（物理删除该 report 消息；消息反馈表外键 CASCADE 一并清理）。"""
    msg = _report_msg(db, user, report_id)
    db.delete(msg)
    db.commit()
    return ok({})
