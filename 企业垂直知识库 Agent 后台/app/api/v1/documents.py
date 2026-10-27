"""文档详情 / 删除 / 分块 / 下载 / 重新解析接口。"""
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..deps import get_accessible_document, get_writable_document
from ...core.database import get_db
from ...core.exceptions import BizError
from ...core.response import ok
from ...models import Document
from ...schemas.kb import doc_to_dict
from ...services import document_service

router = APIRouter(prefix="/documents", tags=["文档"])

# 文件类型 -> (媒体类型, 扩展名)
_FILE_MEDIA = {
    "pdf": ("application/pdf", ".pdf"),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "txt": ("text/plain", ".txt"),
    "md": ("text/markdown", ".md"),
}


@router.get("/{doc_id}")
def get_document(doc: Document = Depends(get_accessible_document)):
    """文档详情。"""
    return ok(doc_to_dict(doc))


@router.delete("/{doc_id}")
def delete_document(
    doc: Document = Depends(get_writable_document),
    db: Session = Depends(get_db),
):
    """删除文档（级联清理分块与向量，需要 write 权限）。"""
    return ok(document_service.delete_document(db, doc))


@router.get("/{doc_id}/chunks")
def list_chunks(
    doc: Document = Depends(get_accessible_document),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """文档分块列表。"""
    return ok(document_service.list_chunks(db, doc, page, size))


@router.get("/{doc_id}/download")
def download_document(doc: Document = Depends(get_accessible_document)):
    """下载文档原文（文件流，文件名使用上传时的原始标题）。"""
    path = Path(doc.file_path)
    if not path.exists():
        raise BizError(404, "源文件已丢失，无法下载")
    media_type, ext = _FILE_MEDIA.get(doc.file_type, ("application/octet-stream", ""))
    filename = doc.title if Path(doc.title).suffix else (doc.title + ext)
    return FileResponse(path, media_type=media_type, filename=filename)


@router.post("/{doc_id}/reprocess")
def reprocess_document(
    doc: Document = Depends(get_writable_document),
    db: Session = Depends(get_db),
):
    """重新解析文档（解析/分块/向量化），用于失败恢复或参数变更后重建索引。"""
    return ok(document_service.reprocess_document(db, doc))
