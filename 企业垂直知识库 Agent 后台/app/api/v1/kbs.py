"""知识库与文档上传接口。"""
from fastapi import APIRouter, Depends, File, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..deps import (
    CurrentUser,
    get_accessible_kb,
    get_current_user,
    get_writable_kb,
    require_admin,
)
from ..rate_limit import rate_limit_ai
from ...core.database import get_db
from ...config import settings
from ...core.exceptions import BizError, NotFoundError
from ...core.response import ok
from ...models import KBPermission, KnowledgeBase, User
from ...schemas.kb import KnowledgeBaseCreate, KnowledgeBaseUpdate, QueryRequest, kb_to_dict
from ...services import document_service, kb_service, rag_service

router = APIRouter(prefix="/kbs", tags=["知识库"])


class PermissionGrant(BaseModel):
    """授权请求：user_id + 权限级别（read/write）。"""

    user_id: int = Field(..., ge=1)
    permission: str = Field(..., pattern="^(read|write)$")


@router.get("")
def list_kbs(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    keyword: str | None = None,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """知识库列表：管理员全部，普通用户仅自己创建的。"""
    return ok(kb_service.list_kbs(db, user, page, size, keyword))


@router.post("")
def create_kb(
    payload: KnowledgeBaseCreate,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """创建知识库（创建者即 owner）。"""
    return ok(kb_service.create_kb(db, user, payload))


@router.get("/{kb_id}")
def get_kb(kb: KnowledgeBase = Depends(get_accessible_kb)):
    """知识库详情。"""
    return ok(kb_to_dict(kb))


@router.put("/{kb_id}")
def update_kb(
    payload: KnowledgeBaseUpdate,
    kb: KnowledgeBase = Depends(get_writable_kb),
    db: Session = Depends(get_db),
):
    """更新知识库（需要 write 权限）。"""
    return ok(kb_service.update_kb(db, kb, payload))


@router.delete("/{kb_id}")
def delete_kb(
    kb: KnowledgeBase = Depends(get_writable_kb),
    db: Session = Depends(get_db),
):
    """删除知识库（级联清理文档、分块、向量）。"""
    return ok(kb_service.delete_kb(db, kb))


@router.get("/{kb_id}/stats")
def kb_stats(
    kb: KnowledgeBase = Depends(get_accessible_kb),
    db: Session = Depends(get_db),
):
    """知识库统计。"""
    return ok(kb_service.stats(db, kb))


# ---------------- 知识库级用户授权（kb_permissions） ----------------


@router.get("/{kb_id}/permissions")
def list_permissions(
    kb: KnowledgeBase = Depends(get_accessible_kb),
    db: Session = Depends(get_db),
):
    """授权用户列表（库主与管理员可管理，被授权者可查看）。"""
    rows = (
        db.query(KBPermission, User)
        .join(User, KBPermission.user_id == User.id)
        .filter(KBPermission.kb_id == kb.id)
        .order_by(KBPermission.id.asc())
        .all()
    )
    return ok([
        {
            "user_id": p.user_id,
            "username": u.username,
            "display_name": u.display_name,
            "permission": p.permission,
            "granted_by": p.granted_by,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p, u in rows
    ])


@router.post("/{kb_id}/permissions")
def grant_permission(
    kb_id: int,
    payload: PermissionGrant,
    kb: KnowledgeBase = Depends(get_accessible_kb),
    user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """授予用户知识库访问权限（admin/owner，upsert 重复授权更新级别）。"""
    target = db.get(User, payload.user_id)
    if not target:
        raise NotFoundError("被授权用户不存在")
    if kb.owner_id != user.id and target.id == kb.owner_id:
        raise BizError(400, "创建者无需授权")
    row = (
        db.query(KBPermission)
        .filter(KBPermission.kb_id == kb.id, KBPermission.user_id == payload.user_id)
        .first()
    )
    if row:
        row.permission = payload.permission
    else:
        row = KBPermission(
            kb_id=kb.id, user_id=payload.user_id,
            permission=payload.permission, granted_by=user.id,
        )
        db.add(row)
    db.commit()
    return ok({"kb_id": kb.id, "user_id": payload.user_id, "permission": payload.permission})


@router.delete("/{kb_id}/permissions/{user_id}")
def revoke_permission(
    kb_id: int,
    user_id: int,
    kb: KnowledgeBase = Depends(get_accessible_kb),
    user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """撤销用户的知识库访问授权。"""
    row = (
        db.query(KBPermission)
        .filter(KBPermission.kb_id == kb.id, KBPermission.user_id == user_id)
        .first()
    )
    if not row:
        raise NotFoundError("授权记录不存在")
    db.delete(row)
    db.commit()
    return ok({})


@router.post("/{kb_id}/documents")
def upload_document(
    kb: KnowledgeBase = Depends(get_writable_kb),
    file: UploadFile = File(...),
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """上传文档并完成解析、分块、向量化（需要 write 权限）。"""
    # 大小限制：在进入解析/向量化流程前拦截，避免无谓的磁盘与计算开销
    max_mb = settings.max_file_size_mb
    if file.size is not None and file.size > max_mb * 1024 * 1024:
        raise BizError(400, f"文件大小超过限制（最大 {max_mb}MB），请压缩或拆分后上传")
    return ok(document_service.upload_document(db, kb, user, file))


@router.get("/{kb_id}/documents")
def list_documents(
    kb: KnowledgeBase = Depends(get_accessible_kb),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    parse_status: str | None = None,
    keyword: str | None = None,
    db: Session = Depends(get_db),
):
    """文档列表。"""
    return ok(document_service.list_documents(db, kb, page, size, parse_status, keyword))


@router.post("/{kb_id}/query")
def query_kb(
    payload: QueryRequest,
    kb: KnowledgeBase = Depends(get_accessible_kb),
    db: Session = Depends(get_db),
    _rate: None = Depends(rate_limit_ai),
):
    """RAG 问答：混合检索 + LLM，返回带引用来源的回答。

    【批次3 新增】挂载 AI 限流：本接口是显式消耗「向量检索 + LLM 生成」的高成本入口，
    此前只有会话问答受限、知识库问答裸奔，是最容易被刷的缺口。
    """
    return ok(rag_service.query_kb(db, kb, payload.question, payload.top_k))
