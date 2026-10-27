"""知识库 / 文档 / RAG 问答相关的请求/响应模型。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str | None = Field(None, max_length=512)
    chunk_size: int = Field(500, ge=100, le=4000)
    chunk_overlap: int = Field(50, ge=0, le=1000)
    top_k: int = Field(5, ge=1, le=50)
    rerank_threshold: float = Field(0.0, ge=0.0, le=1.0)
    embedding_model: str | None = Field(None, max_length=64)


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    description: str | None = Field(None, max_length=512)
    chunk_size: int | None = Field(None, ge=100, le=4000)
    chunk_overlap: int | None = Field(None, ge=0, le=1000)
    top_k: int | None = Field(None, ge=1, le=50)
    rerank_threshold: float | None = Field(None, ge=0.0, le=1.0)
    status: Literal["active", "archived"] | None = None


class KnowledgeBaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    owner_id: int
    embedding_model: str
    chunk_size: int
    chunk_overlap: int
    top_k: int
    rerank_threshold: float
    status: str
    doc_count: int
    created_at: datetime


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kb_id: int
    title: str
    file_type: str
    file_size: int
    file_hash: str | None
    parse_status: str
    chunk_count: int
    error_msg: str | None
    uploaded_by: int
    created_at: datetime


class ChunkOut(BaseModel):
    id: int
    doc_id: int
    kb_id: int
    chunk_index: int
    content: str
    token_count: int
    metadata: dict | None
    chroma_id: str | None


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    top_k: int | None = Field(None, ge=1, le=50)


def kb_to_dict(kb) -> dict:
    return KnowledgeBaseOut.model_validate(kb).model_dump(mode="json")


def doc_to_dict(doc) -> dict:
    return DocumentOut.model_validate(doc).model_dump(mode="json")


def chunk_to_dict(chunk) -> dict:
    """分块转字典（ORM 属性为 chunk_metadata，对外仍输出 metadata 键）。"""
    return {
        "id": chunk.id,
        "doc_id": chunk.doc_id,
        "kb_id": chunk.kb_id,
        "chunk_index": chunk.chunk_index,
        "content": chunk.content,
        "token_count": chunk.token_count,
        "metadata": chunk.chunk_metadata,
        "chroma_id": chunk.chroma_id,
    }
