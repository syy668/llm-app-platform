"""P2 收尾 · 任务 1 单元测试：RAG 检索字段对齐（``app/memory/rag_schema.py``）。

覆盖任务书要求的两类用例：
1. **字段缺失校验**——逐字段定位、一次报全、严格/宽松两条路径；
2. **相似度打分对齐**——百分制 / 余弦 / 越界 / 非法值统一收敛到 ``[0, 1]``。

另外覆盖：别名归一化（旧字段无损映射）、``spans`` 三种上游形态、``MOCK_EXTERNAL_RAG``
开关（**mock 下必须零网络请求**）、HTTP 失败降级、以及与 W2 ``KbSearchOutput`` 的桥接。

全部离线：需要「真实 HTTP」的用例用 ``httpx.MockTransport``，不发真实请求。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.memory import rag_schema
from app.memory.rag_schema import (
    MOCK_RAG_CHUNKS,
    RAG_SCHEMA_VERSION,
    REQUIRED_CHUNK_FIELDS,
    RagChunk,
    RagContractClient,
    RagFieldIssue,
    RagQuery,
    RagSchemaError,
    RagSpan,
    align_score,
    build_mock_result,
    build_rag_client,
    check_kb_contract,
    extract_chunks,
    mock_enabled,
    normalize_chunk,
    parse_spans,
    rank_chunks,
    validate_chunk,
    validate_response,
)

FULL_CHUNK = {
    "chunk_id": "kb-w42-001#0",
    "doc_id": "kb-w42-001",
    "score": 0.88,
    "text": "W42 数码配件品类 GMV 环比下滑 18%，转化率降至 1.7%。",
    "spans": [{"start": 0, "end": 12}],
    "source": "kb:weekly_report:W42",
}


# ---------------------------------------------------------------------------
# 1) 契约字段与缺失校验
# ---------------------------------------------------------------------------
def test_required_fields_are_exactly_six() -> None:
    assert REQUIRED_CHUNK_FIELDS == ("chunk_id", "doc_id", "score", "text", "spans", "source")
    assert RAG_SCHEMA_VERSION.startswith("p2.")


def test_validate_chunk_accepts_full_payload() -> None:
    chunk = validate_chunk(FULL_CHUNK)
    assert chunk.chunk_id == "kb-w42-001#0"
    assert chunk.doc_id == "kb-w42-001"
    assert chunk.score == 0.88
    assert chunk.spans[0].length == 12
    assert chunk.source == "kb:weekly_report:W42"


def test_validate_chunk_reports_missing_chunk_id() -> None:
    payload = {key: value for key, value in FULL_CHUNK.items() if key != "chunk_id"}
    with pytest.raises(RagSchemaError) as excinfo:
        validate_chunk(payload)
    assert [issue.field_name for issue in excinfo.value.issues] == ["chunk_id"]
    assert "字段缺失" in excinfo.value.issues[0].reason
    assert "chunk[0].chunk_id" in str(excinfo.value)


def test_validate_chunk_reports_all_missing_fields_at_once() -> None:
    payload = {"chunk_id": "c1", "doc_id": "d1"}
    with pytest.raises(RagSchemaError) as excinfo:
        validate_chunk(payload, index=3)
    missing = {issue.field_name for issue in excinfo.value.issues}
    assert missing == {"score", "text", "spans", "source"}
    assert all(issue.index == 3 for issue in excinfo.value.issues)


def test_validate_chunk_rejects_blank_text_and_bad_spans_type() -> None:
    payload = dict(FULL_CHUNK, text="   ", spans=123)
    with pytest.raises(RagSchemaError) as excinfo:
        validate_chunk(payload)
    reasons = {issue.field_name: issue.reason for issue in excinfo.value.issues}
    assert "为空" in reasons["text"]
    assert "类型不支持" in reasons["spans"]


def test_validate_chunk_rejects_non_mapping() -> None:
    with pytest.raises(RagSchemaError) as excinfo:
        validate_chunk(["not-a-dict"], index=0)
    assert excinfo.value.issues[0].field_name == "chunk"


def test_validate_chunk_prefers_contract_fields_over_aliases() -> None:
    payload = dict(FULL_CHUNK, content="旧字段正文（应被 text 覆盖）")
    assert validate_chunk(payload).text.startswith("W42 数码配件")


def test_issue_describe_and_to_dict() -> None:
    issue = RagFieldIssue("text", "字段缺失", 2)
    assert issue.describe() == "chunk[2].text: 字段缺失"
    assert issue.to_dict() == {"field": "text", "reason": "字段缺失", "index": 2}


# ---------------------------------------------------------------------------
# 2) 别名归一化（旧上游零改动接入）
# ---------------------------------------------------------------------------
def test_normalize_chunk_maps_legacy_aliases() -> None:
    legacy = {
        "id": "d1#3",
        "content": "数码配件 SKU-10086 断货，库存周转 21 天。",
        "similarity": 93,
        "highlight": "0-3",
        "origin": "kb:legacy",
    }
    chunk = normalize_chunk(legacy, index=0)
    assert chunk.chunk_id == "d1#3"
    assert chunk.doc_id == "d1"  # 从 chunk_id 前缀还原
    assert chunk.score == 0.93  # 百分制对齐
    assert chunk.source == "kb:legacy"
    assert chunk.span_texts() == ["数码配"]


def test_normalize_chunk_derives_ids_when_absent() -> None:
    chunk = normalize_chunk({"doc_id": "kb-1", "content": "正文"}, index=2)
    assert chunk.chunk_id == "kb-1#2"  # 幂等派生规则
    assert chunk.source == "project1"  # 默认来源


def test_normalize_chunk_rejects_blank_text() -> None:
    with pytest.raises(RagSchemaError) as excinfo:
        normalize_chunk({"doc_id": "kb-1", "content": "  "})
    assert excinfo.value.issues[0].field_name == "text"


def test_normalize_chunk_rejects_missing_doc_id() -> None:
    with pytest.raises(RagSchemaError) as excinfo:
        normalize_chunk({"content": "只有正文"})
    assert excinfo.value.issues[0].field_name == "doc_id"


# ---------------------------------------------------------------------------
# 3) spans 三种形态
# ---------------------------------------------------------------------------
def test_parse_spans_accepts_three_shapes() -> None:
    assert [s.to_dict() for s in parse_spans([{"start": 0, "end": 5}])] == [{"start": 0, "end": 5, "text": ""}]
    assert [s.to_dict() for s in parse_spans([[2, 4]])] == [{"start": 2, "end": 4, "text": ""}]
    assert [s.to_dict() for s in parse_spans("0-3, 5:9")] == [
        {"start": 0, "end": 3, "text": ""},
        {"start": 5, "end": 9, "text": ""},
    ]


def test_parse_spans_supports_offset_length_and_skips_invalid() -> None:
    spans = parse_spans(
        [
            {"offset": 4, "length": 6},
            {"start": 9, "end": 3},  # 倒序 → 跳过
            {"start": -1, "end": 2},  # 负数 → 跳过
            "not-a-range",  # 非法串 → 跳过
            [7, 7],  # 空区间合法
        ]
    )
    assert [(s.start, s.end) for s in spans] == [(4, 10), (7, 7)]


def test_parse_spans_empty_inputs() -> None:
    assert parse_spans(None) == []
    assert parse_spans("") == []
    assert parse_spans(123) == []


def test_span_rejects_end_before_start() -> None:
    with pytest.raises(ValueError):
        RagSpan(start=5, end=2)


# ---------------------------------------------------------------------------
# 4) 相似度打分对齐
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0.88, 0.88),
        ("0.5", 0.5),
        (93, 0.93),
        ("87%", 0.87),
        (-0.2, 0.4),
        (1.5, 1.0),
        (250, 1.0),
        (-3.0, 0.0),
        (None, 0.0),
        ("abc", 0.0),
        (float("nan"), 0.0),
        (float("inf"), 0.0),
    ],
)
def test_align_score_matrix(raw: object, expected: float) -> None:
    assert align_score(raw) == expected


def test_chunk_model_aligns_score_on_construction() -> None:
    chunk = RagChunk(chunk_id="c", doc_id="d", score=93, text="正文")
    assert chunk.score == 0.93
    payload = dict(FULL_CHUNK, score=93)
    assert validate_chunk(payload).score == 0.93


def test_rank_chunks_orders_desc_and_dedupes_by_chunk_id() -> None:
    duplicated = [
        RagChunk(chunk_id="a", doc_id="d1", score=0.4, text="x"),
        RagChunk(chunk_id="a", doc_id="d1", score=0.7, text="x"),
        RagChunk(chunk_id="b", doc_id="d2", score=0.6, text="y"),
    ]
    ranked = rank_chunks(duplicated)
    assert [chunk.chunk_id for chunk in ranked] == ["a", "b"]
    assert ranked[0].score == 0.7  # 同 id 保留最高分
    assert [chunk.chunk_id for chunk in rank_chunks(duplicated, top_k=1)] == ["a"]


def test_query_payload_and_min_score_alignment() -> None:
    query = RagQuery(query="  数码配件 GMV  ", top_k=5, min_score=75)
    assert query.query == "数码配件 GMV"
    assert query.min_score == 0.75
    assert query.to_payload() == {"tenant_id": "agentflow", "query": "数码配件 GMV", "top_k": 5}
    filtered = RagQuery(query="x", filters={"week": "W42"})
    assert filtered.to_payload()["filters"] == {"week": "W42"}


# ---------------------------------------------------------------------------
# 5) 响应体解析与契约体检
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        {"data": {"chunks": [FULL_CHUNK]}},
        {"chunks": [FULL_CHUNK]},
        [FULL_CHUNK],
        {"data": {"results": [FULL_CHUNK]}},
        {"results": [FULL_CHUNK]},
    ],
)
def test_extract_chunks_container_shapes(payload: object) -> None:
    assert extract_chunks(payload) == [FULL_CHUNK]


def test_extract_chunks_returns_empty_for_unknown_shape() -> None:
    assert extract_chunks({"data": {"items_total": 3}}) == []
    assert extract_chunks(None) == []
    assert extract_chunks("x") == []


def test_check_kb_contract_ok() -> None:
    report = check_kb_contract({"data": {"chunks": [FULL_CHUNK]}})
    assert report.ok is True
    assert report.total == 1
    assert len(report.chunks) == 1
    assert report.issues == []
    assert report.to_dict()["parsed"] == 1


def test_check_kb_contract_lenient_collects_issues() -> None:
    broken = {"chunk_id": "c1", "doc_id": "d1", "score": 0.5}  # 缺 text/spans/source
    report = check_kb_contract({"data": {"chunks": [FULL_CHUNK, broken]}})
    assert report.ok is False
    assert report.total == 2
    assert len(report.chunks) == 1  # 好的一条仍然保留
    assert {"text", "spans", "source"} <= set(report.missing_fields)
    assert report.warnings


def test_check_kb_contract_reports_empty_response() -> None:
    report = check_kb_contract({"data": {"chunks": []}})
    assert report.ok is False
    assert report.to_dict()["parsed"] == 0
    assert "没有可解析的 chunks" in report.warnings[0]


def test_validate_response_lenient_marks_partial() -> None:
    broken = {"chunk_id": "c1", "doc_id": "d1", "score": 0.5}
    result = validate_response({"chunks": [FULL_CHUNK, broken]}, query="q")
    assert result.degraded is False
    assert result.error_code == "KB_PARTIAL"
    assert len(result.chunks) == 1
    assert result.query == "q"


def test_validate_response_strict_raises() -> None:
    with pytest.raises(RagSchemaError):
        validate_response({"chunks": [{"doc_id": "d1", "content": "x"}]}, strict=True)


def test_validate_response_truncates_to_top_k() -> None:
    payload = {"chunks": [dict(FULL_CHUNK, chunk_id=f"c{index}", score=score) for index, score in enumerate([0.2, 0.9, 0.5])]}
    result = validate_response(payload, top_k=2)
    assert [chunk.chunk_id for chunk in result.chunks] == ["c1", "c2"]


# ---------------------------------------------------------------------------
# 6) 与 W2 既有契约的桥接
# ---------------------------------------------------------------------------
def test_to_kb_output_bridges_mcp_contract() -> None:
    chunk = validate_chunk(FULL_CHUNK)
    result = validate_response({"data": {"chunks": [FULL_CHUNK]}}, latency_ms=12)
    output = result.to_kb_output()
    assert output.chunks[0].doc_id == chunk.doc_id
    assert output.chunks[0].content == chunk.text[:1200]
    assert output.chunks[0].score == chunk.score
    assert output.chunks[0].source == chunk.source
    assert output.latency_ms == 12
    assert output.degraded is False


def test_chunk_to_dict_roundtrip() -> None:
    payload = validate_chunk(FULL_CHUNK).to_dict()
    assert payload["spans"] == [{"start": 0, "end": 12, "text": ""}]
    assert RagChunk(**payload).chunk_id == FULL_CHUNK["chunk_id"]


# ---------------------------------------------------------------------------
# 7) MOCK_EXTERNAL_RAG 开关
# ---------------------------------------------------------------------------
def _explode(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - 命中即失败
    raise AssertionError("mock 模式下不应发起任何网络请求")


async def test_mock_mode_makes_no_network_request() -> None:
    client = RagContractClient(mock=True, transport=httpx.MockTransport(_explode))
    assert client.mock_active is True
    result = await client.search("数码配件 GMV 下滑", top_k=2)
    assert len(result.chunks) == 2
    assert all(chunk.chunk_id and chunk.doc_id and chunk.text for chunk in result.chunks)
    assert any("mock-external-rag" in item for item in result.warnings)


def test_mock_enabled_reads_global_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rag_schema.settings, "MOCK_EXTERNAL_RAG", True)
    assert mock_enabled() is True
    assert mock_enabled(False) is False
    monkeypatch.setattr(rag_schema.settings, "MOCK_EXTERNAL_RAG", False)
    assert mock_enabled() is False
    assert RagContractClient().mock_active is False


def test_build_mock_result_scores_are_aligned() -> None:
    result = build_mock_result("W42 数码配件 GMV 环比下滑 转化率", top_k=3)
    assert result.chunks
    assert all(0.0 <= chunk.score <= 1.0 for chunk in result.chunks)
    scores = [chunk.score for chunk in result.chunks]
    assert scores == sorted(scores, reverse=True)
    assert all(chunk.spans or chunk.chunk_id for chunk in result.chunks)


def test_mock_corpus_matches_contract() -> None:
    for raw in MOCK_RAG_CHUNKS:
        payload = {key: value for key, value in raw.items() if key != "keywords"}
        chunk = normalize_chunk(payload, index=0)
        assert chunk.chunk_id and chunk.doc_id and chunk.text
        assert 0.0 <= chunk.score <= 1.0


def test_build_rag_client_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    assert build_rag_client(mock=True).mock_active is True
    client = build_rag_client()
    assert client.mock_active is mock_enabled()


# ---------------------------------------------------------------------------
# 8) 真实 HTTP 路径（httpx.MockTransport，离线）
# ---------------------------------------------------------------------------
def _handler(payload: dict[str, object], status: int = 200):
    def _inner(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/kb/search"
        body = json.loads(request.content)
        assert body["query"]
        assert body["tenant_id"]  # 显式 RagQuery 用自身 tenant，字符串入参用客户端默认值
        return httpx.Response(status, json=payload)

    return _inner


async def test_http_path_normalizes_response() -> None:
    body = {"data": {"chunks": [dict(FULL_CHUNK, score=93)]}}
    client = RagContractClient(mock=False, transport=httpx.MockTransport(_handler(body)))
    result = await client.search(RagQuery(query="数码配件 GMV", top_k=1))
    assert result.degraded is False
    assert result.chunks[0].score == 0.93
    assert result.latency_ms >= 0


async def test_http_auth_failure_degrades() -> None:
    client = RagContractClient(mock=False, transport=httpx.MockTransport(_handler({}, status=401)))
    result = await client.search("数码配件")
    assert result.degraded is True
    assert result.error_code == "KB_AUTH_FAILED"
    assert result.chunks == []


async def test_http_server_error_degrades() -> None:
    client = RagContractClient(mock=False, transport=httpx.MockTransport(_handler({}, status=500)))
    result = await client.search("数码配件")
    assert result.degraded is True
    assert result.error_code == "KB_UNAVAILABLE"
    assert "HTTP 500" in result.warnings[0]


async def test_http_timeout_degrades() -> None:
    def _timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated timeout")

    client = RagContractClient(mock=False, transport=httpx.MockTransport(_timeout))
    result = await client.search("数码配件")
    assert result.degraded is True
    assert result.error_code == "KB_TIMEOUT"


async def test_http_non_json_degrades() -> None:
    def _plain(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway error</html>")

    client = RagContractClient(mock=False, transport=httpx.MockTransport(_plain))
    result = await client.search("数码配件")
    assert result.degraded is True
    assert "不是合法 JSON" in result.warnings[0]


async def test_http_strict_contract_violation_degrades() -> None:
    body = {"chunks": [{"doc_id": "d1", "content": "缺 chunk_id/score/spans/source"}]}
    client = RagContractClient(mock=False, strict=True, transport=httpx.MockTransport(_handler(body)))
    result = await client.search("数码配件")
    assert result.degraded is True
    assert result.error_code == "KB_UNAVAILABLE"


async def test_http_min_score_filters_results() -> None:
    body = {
        "chunks": [
            dict(FULL_CHUNK, chunk_id="c1", score=0.2),
            dict(FULL_CHUNK, chunk_id="c2", score=0.9),
        ]
    }
    client = RagContractClient(mock=False, transport=httpx.MockTransport(_handler(body)))
    result = await client.search("数码配件", min_score=0.5)
    assert [chunk.chunk_id for chunk in result.chunks] == ["c2"]
    assert any("min_score" in item for item in result.warnings)


# ---------------------------------------------------------------------------
# 9) 自检
# ---------------------------------------------------------------------------
def test_describe_reports_schema_and_switch() -> None:
    info = rag_schema.describe()
    assert info["schema_version"] == RAG_SCHEMA_VERSION
    assert info["required_fields"] == list(REQUIRED_CHUNK_FIELDS)
    assert isinstance(info["mock_external_rag"], bool)


def test_client_describe_hides_token() -> None:
    info = RagContractClient(mock=False, token="secret-token").describe()
    assert "secret-token" not in str(info)
    assert info["search_path"] == "/api/v1/kb/search"
