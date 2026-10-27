"""v1.1 新增能力的单元测试（不依赖 MySQL / Redis / 外部 HTTP）。

覆盖三块：
1. Reviewer 可信度与三态判定（``compute_confidence`` / ``decide_verdict``）；
2. ``route_after_review`` 的拒绝重规划分支（PASS / REVISE / REJECT × 预算）；
3. 项目一知识库 Provider 的响应映射与未配置降级（不打真实网络）。

设计原则：只测纯函数与数据映射，集成行为（真实 HTTP、导出下载）由联调验收覆盖，
避免把单测变成需要外部服务的集成测试。
"""

from __future__ import annotations

import pytest

from app.agents.graph import route_after_review
from app.agents.reviewer import compute_confidence, decide_verdict
from app.core.w2_config import w2
from app.routers.task_export import SCHEMA_TASK, SCHEMA_TRACE, _duration_seconds
from app.schemas.graph_state import ReviewScores
from app.schemas.mcp import McpErrorCode
from app.tools.project1_kb import ProjectOneKbProvider


def _scores(value: int) -> ReviewScores:
    return ReviewScores(evidence=value, faithfulness=value, consistency=value, completeness=value, format=value)


# ---------------------------------------------------------------------------
# 1) 可信度
# ---------------------------------------------------------------------------
def test_confidence_full_evidence_external_is_high() -> None:
    confidence, reason = compute_confidence(_scores(5), evidence_coverage=1.0, source="external")
    assert confidence >= 0.9
    assert "证据覆盖 100%" in reason


def test_confidence_drops_when_evidence_missing() -> None:
    """分数很高但几乎没有证据时，可信度必须明显下降（防"分高但没证据"）。"""
    high_score = compute_confidence(_scores(5), evidence_coverage=1.0, source="external")[0]
    no_evidence = compute_confidence(_scores(5), evidence_coverage=0.0, source="local", degraded=True)[0]
    assert no_evidence < high_score
    assert no_evidence < w2.REVIEW_CONFIDENCE_FLOOR


def test_confidence_bounded_and_skipped_capped() -> None:
    for coverage in (0.0, 0.5, 1.0):
        for source in ("external", "local", "local_fallback", "unknown"):
            confidence, _ = compute_confidence(
                _scores(3), evidence_coverage=coverage, source=source, degraded=True, skipped=True
            )
            assert 0.05 <= confidence <= 0.99
    capped, _ = compute_confidence(_scores(5), evidence_coverage=1.0, source="skipped", skipped=True)
    assert capped <= 0.4


# ---------------------------------------------------------------------------
# 2) 三态判定 + 拒绝重规划分支
# ---------------------------------------------------------------------------
def test_verdict_three_states() -> None:
    assert decide_verdict(4.2, 0.9, passed=True) == "PASS"
    assert decide_verdict(3.0, 0.9, passed=False) == "REVISE"
    assert decide_verdict(2.0, 0.9, passed=False) == "REJECT"
    # 分数达标但可信度不足 → 仍要重规划
    assert decide_verdict(4.0, 0.3, passed=True) == "REVISE"
    # 跳过评审按放行处理
    assert decide_verdict(3.5, 0.3, passed=True, skipped=True) == "PASS"


@pytest.mark.parametrize(
    ("verdict", "total", "replan_count", "expected"),
    [
        ("PASS", 4.2, 0, "summarizer"),
        ("PASS", 4.2, 2, "summarizer"),
        ("REVISE", 3.0, 0, "replan"),
        ("REVISE", 3.0, 1, "replan"),
        ("REVISE", 3.0, w2.REPLAN_MAX_COUNT, "summarizer"),
        ("REJECT", 2.0, 0, "replan"),
        ("REJECT", 2.0, w2.REPLAN_MAX_COUNT, "summarizer"),
    ],
)
def test_route_after_review_verdict_branches(verdict: str, total: float, replan_count: int, expected: str) -> None:
    state = {"review": {"verdict": verdict, "total": total}, "replan_count": replan_count}
    assert route_after_review(state) == expected  # type: ignore[arg-type]


def test_route_after_review_legacy_state_without_verdict() -> None:
    """老 checkpoint 没有 verdict 字段时，仍按总分阈值判断（向后兼容）。"""
    assert route_after_review({"review": {"total": 4.0, "pass": True}}) == "summarizer"  # type: ignore[arg-type]
    assert route_after_review({"review": {"total": 2.0, "pass": False}, "replan_count": 0}) == "replan"  # type: ignore[arg-type]


def test_route_after_review_failed_state_ends() -> None:
    from langgraph.graph import END

    assert route_after_review({"status": "FAILED", "review": {"verdict": "PASS"}}) == END  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3) 项目一 Provider
# ---------------------------------------------------------------------------
def test_project1_provider_maps_sources_to_chunks() -> None:
    provider = ProjectOneKbProvider(base_url="http://127.0.0.1:8010", kb_id=7, token="t")
    body = {
        "code": 0,
        "message": "ok",
        "data": {
            "answer": "（项目一 LLM 回答，本适配器丢弃）",
            "sources": [
                {
                    "chunk_id": 101,
                    "document_id": 3,
                    "document_title": "W41 电商经营周报",
                    "chunk_index": 12,
                    "content": "数码配件爆款 SKU-10086 库存周转天数升至 21 天，存在断货风险。",
                    "score": 0.0163,
                },
                {"chunk_id": 102, "document_id": 3, "content": "", "score": 0.01},
            ],
        },
    }
    chunks = provider._to_chunks(body, top_k=3)
    assert len(chunks) == 1, "空 content 的片段必须被丢弃"
    chunk = chunks[0]
    assert chunk.doc_id == "101"
    assert "W41" in chunk.title and "第12块" in chunk.title
    assert chunk.source == "project1:kb7:doc3"
    # 分值保持原始 RRF 量纲，不做放大
    assert chunk.score == pytest.approx(0.0163)


def test_project1_provider_respects_top_k() -> None:
    provider = ProjectOneKbProvider(base_url="http://127.0.0.1:8010", kb_id=1, token="t")
    body = {"data": {"sources": [{"chunk_id": i, "content": f"片段{i}"} for i in range(10)]}}
    assert len(provider._to_chunks(body, top_k=2)) == 2
    assert len(provider._to_chunks(body, top_k=99)) == 5, "KbChunk 上限 5 条，超出要截断"


async def test_project1_provider_not_configured_degrades() -> None:
    provider = ProjectOneKbProvider(base_url="http://127.0.0.1:8010", kb_id=1, token="", username="", password="")
    assert provider.configured is False
    result = await provider.search("库存风险", 3, "agentflow")
    assert result.degraded is True
    assert result.chunks == []
    assert result.error_code == McpErrorCode.KB_AUTH_FAILED.value


async def test_project1_provider_unreachable_degrades() -> None:
    """端口没人监听时必须是降级而不是抛异常（编排不能被外部依赖拖死）。"""
    provider = ProjectOneKbProvider(base_url="http://127.0.0.1:9", kb_id=1, token="x", timeout=1)
    result = await provider.search("库存风险", 3, "agentflow")
    assert result.degraded is True
    assert result.error_code in {McpErrorCode.KB_UNAVAILABLE.value, McpErrorCode.KB_TIMEOUT.value}


# ---------------------------------------------------------------------------
# 4) 导出契约
# ---------------------------------------------------------------------------
def test_export_schema_versions_are_stable() -> None:
    assert SCHEMA_TASK == "agentflow.task_export.v1"
    assert SCHEMA_TRACE == "agentflow.trace_export.v1"


def test_export_duration_uses_utc_naive() -> None:
    assert _duration_seconds("2026-10-22T03:14:58", "2026-10-22T03:15:11") == 13.0
    assert _duration_seconds("2026-10-22T03:14:58+00:00", "2026-10-22T03:15:11Z") == 13.0
    assert _duration_seconds(None, "2026-10-22T03:15:11") is None
    assert _duration_seconds("bad", "worse") is None
