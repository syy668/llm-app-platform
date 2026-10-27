"""v1.9 引用卡片（溯源 UI）后端契约用例。

覆盖：
  - rag_tool.format_context / to_sources：标号与结构化来源严格一一对应；
  - rag_retrieve_with_sources：命中 / 空召回 / 异常三种情况的返回形状；
  - rag_retrieve：旧签名兼容（仍返回纯文本）；
  - graph.rag_node：state 里带回 sources；
  - graph.parallel_compare_node：两路来源合并后统一重编号，B 路文本标号顺延。
"""
import app.agent.graph as graph
import app.agent.tools.rag_tool as rag_tool
from app.rag import retriever

HITS = [
    {
        "chunk_id": 11, "document_id": 3, "document_title": "公司差旅报销制度",
        "chunk_index": 0, "content": "部门经理住宿标准 800 元/晚", "score": 0.032,
    },
    {
        "chunk_id": 12, "document_id": 3, "document_title": "公司差旅报销制度",
        "chunk_index": 1, "content": "报销时限 30 天内", "score": 0.028,
    },
]


class _FakeKB:
    top_k = 5


class _FakeSession:
    def get(self, model, pk):
        return _FakeKB()

    def close(self):
        pass


def _patch_session(monkeypatch):
    monkeypatch.setattr(rag_tool, "SessionLocal", lambda: _FakeSession())


# ------------------------------------------------------------------ 工具层


def test_format_context_labels_are_sequential():
    text = rag_tool.format_context(HITS)
    # 块号是 1-based：库里 chunk_index=0 的第一块，对外必须叫「第1块」，
    # 否则模型会原样抄出「第0块」，用户看到就以为是 bug。
    assert "[来源1] 文档《公司差旅报销制度》 第1块：" in text
    assert "[来源2] 文档《公司差旅报销制度》 第2块：" in text
    assert "第0块" not in text
    assert text.index("[来源1]") < text.index("[来源2]")


def test_to_sources_exposes_display_chunk_no():
    sources = rag_tool.to_sources(HITS)
    assert [s["index"] for s in sources] == [1, 2]
    assert sources[0]["document_title"] == "公司差旅报销制度"
    assert sources[0]["chunk_index"] == 0      # 存储原值保留，便于排查
    assert sources[0]["chunk_no"] == 1         # 对外展示号 = 存储序号 + 1
    assert sources[1]["chunk_no"] == 2
    assert sources[0]["score"] == 0.032
    assert sources[0]["content"].startswith("部门经理住宿标准")


def test_retrieve_with_sources_returns_text_and_hits(monkeypatch):
    _patch_session(monkeypatch)
    monkeypatch.setattr(retriever, "hybrid_retrieve", lambda *a, **kw: HITS)

    text, sources = rag_tool.rag_retrieve_with_sources("住宿标准？", 7)

    assert "[来源1]" in text and "[来源2]" in text
    assert len(sources) == 2
    assert sources[1]["index"] == 2


def test_retrieve_empty_hits_gives_fixed_text(monkeypatch):
    _patch_session(monkeypatch)
    monkeypatch.setattr(retriever, "hybrid_retrieve", lambda *a, **kw: [])

    text, sources = rag_tool.rag_retrieve_with_sources("无关问题", 7)

    assert text == rag_tool.NO_HIT_TEXT
    assert sources == []


def test_retrieve_exception_degrades_without_raising(monkeypatch):
    _patch_session(monkeypatch)

    def _boom(*a, **kw):
        raise RuntimeError("chroma 挂了")

    monkeypatch.setattr(retriever, "hybrid_retrieve", _boom)

    text, sources = rag_tool.rag_retrieve_with_sources("问题", 7)

    assert "RAG 检索失败" in text
    assert sources == []


def test_legacy_rag_retrieve_still_returns_text(monkeypatch):
    _patch_session(monkeypatch)
    monkeypatch.setattr(retriever, "hybrid_retrieve", lambda *a, **kw: HITS)

    out = rag_tool.rag_retrieve("住宿标准？", 7)
    assert isinstance(out, str)
    assert "[来源1]" in out


# -------------------------------------------------------------------- 图节点


def test_rag_node_passes_sources_into_state(monkeypatch):
    monkeypatch.setattr(
        graph, "rag_retrieve_with_sources",
        lambda q, kb_id, top_k=None, search_mode=None: ("上下文文本", HITS),
    )
    out = graph.rag_node({"question": "住宿标准？", "kb_id": 7})
    assert out["tool_output"] == "上下文文本"
    assert out["sources"] == HITS


def test_rag_node_without_kb_returns_empty_sources(monkeypatch):
    out = graph.rag_node({"question": "住宿标准？", "kb_id": None})
    assert out["sources"] == []
    assert "未绑定知识库" in out["tool_output"]


def test_parallel_compare_renumbers_sources(monkeypatch):
    monkeypatch.setattr(graph, "_extract_compare_queries", lambda q: ("旧制度", "新制度"))

    def _fake_retrieve(question, kb_id, top_k=None, search_mode=None):
        base = "[来源1] 文档《制度》 第0块：\n内容-" + question
        if question == "旧制度":
            return base + "\n\n[来源2] 文档《制度》 第1块：\n更多", [
                {"index": 1, "document_title": "制度", "chunk_index": 0, "content": "内容-旧制度"},
                {"index": 2, "document_title": "制度", "chunk_index": 1, "content": "更多"},
            ]
        return base, [
            {"index": 1, "document_title": "制度", "chunk_index": 9, "content": "内容-新制度"},
        ]

    monkeypatch.setattr(graph, "rag_retrieve_with_sources", _fake_retrieve)

    out = graph.parallel_compare_node({"question": "对比新旧制度", "kb_id": 7})

    sources = out["sources"]
    assert [s["index"] for s in sources] == [1, 2, 3]
    assert [s.get("group") for s in sources] == ["A", "A", "B"]
    # B 路原始 [来源1] 必须顺延成 [来源3]，否则前端会串到 A 路的片段
    assert "[来源3]" in out["tool_output"]
    assert out["tool_output"].count("[来源1]") == 1
