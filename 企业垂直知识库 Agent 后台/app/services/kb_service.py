"""知识库业务编排。

权限模型：管理员可管理全部知识库；普通用户可见自己创建的库，以及被
kb_permissions 授权的库（read/write 皆可）。具体单库鉴权在 api/deps.py 的
get_accessible_kb 依赖中完成，列表接口此前漏认授权（割裂 Bug），现与鉴权口径统一。
"""
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Document, DocumentChunk, KBPermission, KnowledgeBase
from ..rag import retriever, vector_store
from ..schemas.kb import kb_to_dict


def list_kbs(
    db: Session, user, page: int, size: int, keyword: str | None = None
) -> dict:
    """知识库列表：管理员返回全部；普通用户返回自己创建的 + 被授权的库。

    采用 OR + IN 子查询（方案 A）：owner_id == user.id OR id IN (
        SELECT kb_id FROM kb_permissions WHERE user_id == user.id
    )。子查询不产生行放大，分页计数安全，且 kb_permissions 已有
    (kb_id,user_id) 唯一约束与单列索引支撑。
    """
    query = db.query(KnowledgeBase)
    if user.role != "admin":
        query = query.filter(
            or_(
                KnowledgeBase.owner_id == user.id,
                KnowledgeBase.id.in_(
                    select(KBPermission.kb_id).where(KBPermission.user_id == user.id)
                ),
            )
        )
    if keyword:
        query = query.filter(KnowledgeBase.name.like(f"%{keyword}%"))

    total = query.count()
    items = (
        query.order_by(KnowledgeBase.id.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    items_out = []
    for k in items:
        d = kb_to_dict(k)
        # is_shared：非管理员且非本人创建的库（即仅因被授权而可见的库）标记为
        # 「共享」，供前端在列表里区分「我的 / 共享给我」。该字段仅在列表 payload
        # 注入，不污染 KnowledgeBaseOut 核心模型，详见前端方案 2。
        d["is_shared"] = (user.role != "admin") and (k.owner_id != user.id)
        items_out.append(d)
    return {"total": total, "items": items_out}


def create_kb(db: Session, user, data) -> dict:
    """创建知识库，创建者即 owner。"""
    kb = KnowledgeBase(
        name=data.name,
        description=data.description,
        owner_id=user.id,
        embedding_model=data.embedding_model or settings.embedding_model or "default",
        chunk_size=data.chunk_size,
        chunk_overlap=data.chunk_overlap,
        top_k=data.top_k,
        rerank_threshold=data.rerank_threshold,
        status="active",
        doc_count=0,
    )
    db.add(kb)
    db.commit()
    db.refresh(kb)
    return kb_to_dict(kb)


def update_kb(db: Session, kb: KnowledgeBase, data) -> dict:
    """更新知识库配置。"""
    for field in (
        "name", "description", "chunk_size", "chunk_overlap",
        "top_k", "rerank_threshold", "status",
    ):
        value = getattr(data, field)
        if value is not None:
            setattr(kb, field, value)
    db.commit()
    db.refresh(kb)
    return kb_to_dict(kb)


def delete_kb(db: Session, kb: KnowledgeBase) -> dict:
    """删除知识库：先删向量与缓存，再删记录（DB 外键级联删除文档/分块）。"""
    vector_store.delete_collection(kb.id)
    retriever.invalidate_bm25(kb.id)
    db.delete(kb)
    db.commit()
    return {}


def stats(db: Session, kb: KnowledgeBase) -> dict:
    """知识库统计：文档数、分块数、token 总量（以实际数据为准）。"""
    doc_count = (
        db.query(func.count(Document.id)).filter(Document.kb_id == kb.id).scalar() or 0
    )
    chunk_count = (
        db.query(func.count(DocumentChunk.id)).filter(DocumentChunk.kb_id == kb.id).scalar() or 0
    )
    token_sum = (
        db.query(func.coalesce(func.sum(DocumentChunk.token_count), 0))
        .filter(DocumentChunk.kb_id == kb.id)
        .scalar() or 0
    )
    return {"doc_count": int(doc_count), "chunk_count": int(chunk_count), "token_sum": int(token_sum)}
