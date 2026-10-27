"""Chroma 向量库封装（本地持久化）。

每个知识库对应一个 Chroma collection（kb_{id}），向量由上层显式传入，
因此无需配置 Chroma 内置 embedding function。
"""
import logging
import threading
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)

_client = None
# chromadb PersistentClient 非线程安全（并发 get_or_create/query 会触发
# 'RustBindingsAPI' object has no attribute 'bindings' / Could not connect to
# tenant 等竞态错误），所有客户端/集合操作统一串行化；并发评测线程池下必需。
_CHROMA_LOCK = threading.RLock()


def _get_client():
    """懒加载 PersistentClient，chromadb 未安装时只在首次使用时报错。"""
    global _client
    if _client is None:
        import chromadb

        persist_dir = Path(settings.chroma_persist_dir)
        persist_dir.mkdir(parents=True, exist_ok=True)
        _client = chromadb.PersistentClient(path=str(persist_dir))
    return _client


def _collection_name(kb_id: int) -> str:
    return f"kb_{kb_id}"


def _get_collection(kb_id: int):
    with _CHROMA_LOCK:
        return _get_client().get_or_create_collection(
            name=_collection_name(kb_id),
            metadata={"hnsw:space": "cosine"},
        )


def add_chunks(
    kb_id: int,
    ids: list[str],
    embeddings: list[list[float]],
    documents: list[str],
    metadatas: list[dict],
) -> None:
    """批量写入向量。"""
    if not ids:
        return
    with _CHROMA_LOCK:
        _get_collection(kb_id).add(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )


def query(kb_id: int, embedding: list[float], top_k: int) -> list[tuple[str, float]]:
    """向量检索，返回 [(chroma_id, distance)]。"""
    with _CHROMA_LOCK:
        result = _get_collection(kb_id).query(
            query_embeddings=[embedding],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
    ids = result.get("ids", [[]])[0]
    distances = result.get("distances", [[]])[0]
    return list(zip(ids, distances))


def delete_by_ids(kb_id: int, ids: list[str]) -> None:
    """按 id 删除向量。"""
    if not ids:
        return
    with _CHROMA_LOCK:
        _get_collection(kb_id).delete(ids=ids)


def delete_collection(kb_id: int) -> None:
    """删除整个知识库对应的 collection。"""
    try:
        with _CHROMA_LOCK:
            _get_client().delete_collection(name=_collection_name(kb_id))
    except Exception as exc:  # collection 不存在等情况不阻断主流程
        logger.warning("删除 Chroma collection(kb_%s)失败: %s", kb_id, exc)
