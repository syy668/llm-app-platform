"""RAG 问答业务编排：检索 -> 拼接上下文 -> 调用 LLM -> 返回带引用来源的回答。"""
from sqlalchemy.orm import Session

from ..models import KnowledgeBase
from ..rag import llm, retriever


def query_kb(db: Session, kb: KnowledgeBase, question: str, top_k: int | None = None) -> dict:
    """对指定知识库执行混合检索并生成回答。"""
    k = top_k or kb.top_k or 5
    hits = retriever.hybrid_retrieve(db, kb, question, k)

    contexts = [h["content"] for h in hits]
    answer_text = llm.answer(question, contexts, kb.name)
    return {"answer": answer_text, "sources": hits}
