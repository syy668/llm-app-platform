"""文档业务编排：上传、解析、分块、向量化、删除。"""
import hashlib
import logging
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import settings
from ..core.exceptions import BizError, ConflictError
from ..models import Document, DocumentChunk, KnowledgeBase
from ..models.document import PARSE_DONE, PARSE_FAILED, PARSE_PENDING
from ..rag import embeddings, loader, retriever, splitter, vector_store
from ..schemas.kb import chunk_to_dict, doc_to_dict

logger = logging.getLogger(__name__)

_EMBED_BATCH = 16


def _save_upload(kb_id: int, filename: str, content: bytes) -> str:
    """保存上传文件到 uploads/{kb_id}/，返回绝对路径。仅取文件名避免路径穿越。"""
    upload_dir = Path(settings.upload_dir) / str(kb_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(filename).name
    dest = upload_dir / f"{uuid.uuid4().hex}_{safe_name}"
    dest.write_bytes(content)
    return str(dest)


def upload_document(db: Session, kb: KnowledgeBase, user, file) -> dict:
    """上传并同步完成 解析 -> 分块 -> 向量化 全链路。"""
    filename = file.filename or "unnamed"
    file_type = loader.detect_file_type(filename)
    if file_type is None:
        raise BizError(400, "不支持的文件类型，仅支持 PDF / Word(docx) / Excel(xlsx) / txt / md")

    content = file.file.read()
    file_hash = hashlib.sha256(content).hexdigest()

    # 去重：同一知识库内相同内容只允许上传一次
    exists = (
        db.query(Document)
        .filter(Document.kb_id == kb.id, Document.file_hash == file_hash)
        .first()
    )
    if exists:
        raise ConflictError("该文档已存在于当前知识库（内容重复）")

    file_path = _save_upload(kb.id, filename, content)

    doc = Document(
        kb_id=kb.id,
        title=filename,
        file_type=file_type,
        file_path=file_path,
        file_size=len(content),
        file_hash=file_hash,
        parse_status=PARSE_PENDING,
        chunk_count=0,
        uploaded_by=user.id,
    )
    db.add(doc)
    kb.doc_count = (kb.doc_count or 0) + 1
    db.commit()
    db.refresh(doc)

    try:
        _process_document(db, kb, doc)
    except Exception as exc:
        logger.exception("文档解析失败 doc_id=%s", doc.id)
        db.rollback()
        doc = db.get(Document, doc.id)
        if doc:
            doc.parse_status = PARSE_FAILED
            doc.error_msg = str(exc)[:500]
            db.commit()

    return doc_to_dict(doc)


def _process_document(db: Session, kb: KnowledgeBase, doc: Document) -> None:
    """解析 -> 分块 -> 向量化 -> 入库。"""
    text = loader.extract_text(doc.file_path, doc.file_type)
    if not text or not text.strip():
        raise ValueError("未能从文件中提取到文本内容")

    chunks = splitter.split_text(text, kb.chunk_size, kb.chunk_overlap)
    if not chunks:
        raise ValueError("文档分块结果为空")

    # 分批向量化，避免单次请求过大
    all_embeddings: list[list[float]] = []
    for i in range(0, len(chunks), _EMBED_BATCH):
        all_embeddings.extend(embeddings.embed_texts(chunks[i:i + _EMBED_BATCH]))

    ids = [f"{doc.id}:{i}" for i in range(len(chunks))]
    metadatas = [
        {"doc_id": doc.id, "title": doc.title, "chunk_index": i}
        for i in range(len(chunks))
    ]
    chunk_rows = [
        DocumentChunk(
            doc_id=doc.id,
            kb_id=kb.id,
            chunk_index=i,
            content=chunks[i],
            token_count=splitter.estimate_tokens(chunks[i]),
            chunk_metadata=metadatas[i],
            chroma_id=ids[i],
        )
        for i in range(len(chunks))
    ]

    # 先写向量库，再写数据库，避免出现“有分块无向量”的脏数据
    vector_store.add_chunks(kb.id, ids, all_embeddings, chunks, metadatas)
    try:
        db.add_all(chunk_rows)
        doc.parse_status = PARSE_DONE
        doc.chunk_count = len(chunks)
        doc.error_msg = None
        db.commit()
    except Exception:
        db.rollback()
        vector_store.delete_by_ids(kb.id, ids)  # 回滚向量，避免脏数据
        raise

    retriever.invalidate_bm25(kb.id)


def list_documents(
    db: Session,
    kb: KnowledgeBase,
    page: int,
    size: int,
    parse_status: str | None = None,
    keyword: str | None = None,
) -> dict:
    """分页文档列表。"""
    query = db.query(Document).filter(Document.kb_id == kb.id)
    if parse_status:
        query = query.filter(Document.parse_status == parse_status)
    if keyword:
        query = query.filter(Document.title.like(f"%{keyword}%"))

    total = query.count()
    items = query.order_by(Document.id.desc()).offset((page - 1) * size).limit(size).all()
    return {"total": total, "items": [doc_to_dict(d) for d in items]}


def delete_document(db: Session, doc: Document) -> dict:
    """删除文档：清理向量与缓存，记录级联删除分块。"""
    chunk_rows = (
        db.query(DocumentChunk).filter(DocumentChunk.doc_id == doc.id).all()
    )
    chroma_ids = [c.chroma_id for c in chunk_rows if c.chroma_id]
    vector_store.delete_by_ids(doc.kb_id, chroma_ids)
    retriever.invalidate_bm25(doc.kb_id)

    kb = db.get(KnowledgeBase, doc.kb_id)
    if kb:
        kb.doc_count = max(0, (kb.doc_count or 0) - 1)
    db.delete(doc)  # DB 外键 ON DELETE CASCADE 删除分块
    db.commit()
    return {}


def list_chunks(db: Session, doc: Document, page: int, size: int) -> dict:
    """分页分块列表。"""
    query = db.query(DocumentChunk).filter(DocumentChunk.doc_id == doc.id)
    total = query.count()
    items = (
        query.order_by(DocumentChunk.chunk_index.asc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    return {"total": total, "items": [chunk_to_dict(c) for c in items]}


def reprocess_document(db: Session, doc: Document) -> dict:
    """重新解析文档（用于解析失败恢复或参数变更后重建索引）：

    清理旧分块与向量 -> 重跑 解析/分块/向量化；失败时置 PARSE_FAILED 并抛出业务错误。
    """
    kb = db.get(KnowledgeBase, doc.kb_id)
    if not kb:
        raise BizError(404, "所属知识库不存在，无法重新解析")
    if not Path(doc.file_path).exists():
        raise BizError(400, "源文件已丢失，无法重新解析，请重新上传")

    # 清理旧分块与向量，避免重复入库
    chunk_rows = db.query(DocumentChunk).filter(DocumentChunk.doc_id == doc.id).all()
    chroma_ids = [c.chroma_id for c in chunk_rows if c.chroma_id]
    vector_store.delete_by_ids(doc.kb_id, chroma_ids)
    for c in chunk_rows:
        db.delete(c)
    doc.parse_status = PARSE_PENDING
    doc.error_msg = None
    doc.chunk_count = 0
    db.commit()
    retriever.invalidate_bm25(doc.kb_id)

    try:
        _process_document(db, kb, doc)
    except Exception as exc:
        logger.exception("重新解析失败 doc_id=%s", doc.id)
        db.rollback()
        doc = db.get(Document, doc.id)
        if doc:
            doc.parse_status = PARSE_FAILED
            doc.error_msg = str(exc)[:500]
            db.commit()
        raise BizError(500, f"重新解析失败：{doc.error_msg if doc else '未知错误'}")
    return doc_to_dict(doc)
