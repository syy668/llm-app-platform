"""P2 收尾 · 任务 1：RAG 检索字段对齐（``app/memory/rag_schema.py``）。

## 为什么需要这一层

项目一 RAG 与 AgentFlow 之间此前只有「隐式契约」：``layered_memory.HttpRagVectorStore``
读 ``data.chunks[].doc_id/score``，``kb_search.HttpKbProvider`` 读 ``doc_id/title/content/score/source``，
两处字段名、打分口径、缺失容忍度**各写各的**。结果是同一份上游响应在两个通道里
打分不同（一个是百分制 93，一个是 0~1 的 0.93）、缺字段时一个静默补空一个抛异常。

本模块提供**唯一契约**，不替换上面任何既有实现（W1~W5 全部只读）：

1. **统一入参**：``RagQuery``（query / top_k / tenant / min_score / filters）；
2. **统一出参**：``RagChunk``（``chunk_id`` ``doc_id`` ``score`` ``text`` ``spans`` ``source``）；
3. **契约校验**：``check_kb_contract`` 逐字段定位缺失原因，``strict`` 下直接拒绝；
4. **打分对齐**：``align_score`` 把百分制 / 余弦距离 / 越界值统一收敛到 ``[0, 1]``；
5. **兼容层**：``normalize_chunk`` 用别名字典把旧字段（``content``/``id``/``similarity``）
   无损映射到新契约，因此**上游不用改一行**；
6. **离线兜底**：``MOCK_EXTERNAL_RAG=true`` 时 ``RagContractClient`` 直接返回预置片段，
   不发起任何网络请求（单测与 CI 全离线）。

## 设计取舍

- **校验与归一化分离**：``validate_chunk(strict=True)`` 只判断不修补（用于契约测试、
  联调断言），``normalize_chunk`` 尽力修补（用于生产链路），二者共用同一份字段表；
- **``spans`` 允许三种上游形态**：``[{"start","end"}]`` / ``[[s, e]]`` / ``"0-12,20-30"``，
  因为项目一不同版本分别用过这三种，统一在 ``parse_spans`` 里吃掉差异；
- **不抛异常的默认路径**：``validate_response`` 默认非严格，缺字段降级为空 ``text`` +
  记录 ``warnings``，与 PRD FR-007「检索失败不阻塞主流程」保持一致。
"""

from __future__ import annotations

import math
import os
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import settings
from app.core.logging import get_logger
from app.core.w5_config import w5
from app.schemas.mcp import KbChunk, KbSearchOutput, McpErrorCode

logger = get_logger(__name__)

#: 契约版本：字段顺序/语义发生不兼容变更时递增，便于上下游对齐
RAG_SCHEMA_VERSION = "p2.1"

#: 必填字段（顺序即文档顺序，测试直接断言该元组）
REQUIRED_CHUNK_FIELDS: tuple[str, ...] = ("chunk_id", "doc_id", "score", "text", "spans", "source")

#: 上游别名表：新契约字段 -> 可接受的旧字段名（按优先级）
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "chunk_id": ("chunk_id", "chunkId", "chunk", "id"),
    "doc_id": ("doc_id", "docId", "document_id", "doc"),
    "score": ("score", "similarity", "relevance", "distance_score"),
    "text": ("text", "content", "chunk_text", "body"),
    "spans": ("spans", "highlight", "offsets", "positions"),
    "source": ("source", "source_name", "origin"),
}

#: 响应体里可能承载 chunks 的三层路径（项目一 v1 / v2 / Chroma 直出）
_CHUNK_CONTAINERS: tuple[tuple[str, ...], ...] = (
    ("data", "chunks"),
    ("data", "results"),
    ("data", "items"),
    ("chunks",),
    ("results",),
)

_RANGE_RE = re.compile(r"^\s*(\d+)\s*[-:]\s*(\d+)\s*$")


# ---------------------------------------------------------------------------
# 1) 错误与问题描述
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class RagFieldIssue:
    """单个字段的契约问题（用于联调时一眼定位是哪条 chunk 的哪个字段）。"""

    field_name: str
    reason: str
    index: int | None = None

    def describe(self) -> str:
        prefix = f"chunk[{self.index}]." if self.index is not None else ""
        return f"{prefix}{self.field_name}: {self.reason}"

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field_name, "reason": self.reason, "index": self.index}


class RagSchemaError(ValueError):
    """契约校验失败。``issues`` 保留全部问题，而不是只报第一个。"""

    def __init__(self, issues: Sequence[RagFieldIssue], *, where: str = "chunk") -> None:
        self.issues = list(issues)
        detail = "; ".join(issue.describe() for issue in self.issues) or "未知字段问题"
        super().__init__(f"RAG 契约校验失败（{where}）：{detail}")


# ---------------------------------------------------------------------------
# 2) 打分对齐
# ---------------------------------------------------------------------------
def align_score(raw: Any, *, decimals: int = 4) -> float:
    """把各种上游打分口径统一收敛到 ``[0, 1]``。

    | 上游形态 | 典型值 | 处理 |
    |---|---|---|
    | 归一化相似度 | ``0.93`` | 原样 |
    | 百分制 | ``93`` / ``"93%"`` | ``/100`` |
    | 余弦相似度 | ``-0.2`` | ``(x + 1) / 2`` |
    | 略微越界 | ``1.5`` | 裁到 ``1.0``（不是百分制的 1.5 分） |
    | 非法值 | ``None`` / ``"abc"`` / ``nan`` | ``0.0`` |
    """
    if raw is None:
        return 0.0
    if isinstance(raw, str):
        stripped = raw.strip().rstrip("%")
        try:
            value = float(stripped)
        except (TypeError, ValueError):
            return 0.0
        if raw.strip().endswith("%"):
            return round(min(1.0, max(0.0, value / 100.0)), decimals)
    else:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 0.0
    if math.isnan(value) or math.isinf(value):
        return 0.0
    if 0.0 <= value <= 1.0:
        aligned = value
    elif 1.0 < value < 2.0:
        # 刚过 1 的分数是「越界」而不是百分制（真正的百分制不会给 1.5 分）
        aligned = 1.0
    elif 2.0 <= value <= 100.0:
        aligned = value / 100.0
    elif -1.0 <= value < 0.0:
        aligned = (value + 1.0) / 2.0
    else:
        aligned = min(1.0, max(0.0, value))
    return round(aligned, decimals)


def rank_chunks(chunks: Sequence["RagChunk"], *, top_k: int | None = None) -> list["RagChunk"]:
    """按对齐后的 ``score`` 降序，并对同一 ``chunk_id`` 去重（保留最高分那条）。

    去重是必要的：项目一同一 chunk 可能因多租户/多索引被返回两次，直接拼进提示词
    会造成重复占位（PRD「引用片段总长 ≤ N Token」）。
    """
    best: dict[str, RagChunk] = {}
    for chunk in chunks:
        current = best.get(chunk.chunk_id)
        if current is None or chunk.score > current.score:
            best[chunk.chunk_id] = chunk
    ordered = sorted(best.values(), key=lambda item: (-item.score, item.doc_id, item.chunk_id))
    if top_k is not None:
        return ordered[: max(0, int(top_k))]
    return ordered


# ---------------------------------------------------------------------------
# 3) spans 解析（三种上游形态）
# ---------------------------------------------------------------------------
class RagSpan(BaseModel):
    """文本高亮区间：``text[start:end]``（左闭右开，与 Python 切片一致）。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str = ""

    @model_validator(mode="after")
    def _check_order(self) -> "RagSpan":
        if self.end < self.start:
            raise ValueError(f"span 结束位 {self.end} 小于起始位 {self.start}")
        return self

    @property
    def length(self) -> int:
        return self.end - self.start

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "text": self.text}


def parse_spans(raw: Any) -> list[RagSpan]:
    """尽力解析 spans；非法条目**跳过而不是报错**（引用高亮不值得阻塞检索）。"""
    if raw is None or raw == "":
        return []
    items: list[Any]
    if isinstance(raw, str):
        items = [part for part in raw.split(",") if part.strip()]
    elif isinstance(raw, Mapping):
        items = [raw]
    elif isinstance(raw, Iterable):
        items = list(raw)
    else:
        return []

    spans: list[RagSpan] = []
    for item in items:
        span = _coerce_span(item)
        if span is not None:
            spans.append(span)
    spans.sort(key=lambda s: (s.start, s.end))
    return spans


def _coerce_span(item: Any) -> RagSpan | None:
    if isinstance(item, RagSpan):
        return item
    if isinstance(item, str):
        match = _RANGE_RE.match(item)
        if not match:
            return None
        start, end = int(match.group(1)), int(match.group(2))
        return _safe_span(start, end)
    if isinstance(item, Mapping):
        start = item.get("start", item.get("begin", item.get("offset")))
        end = item.get("end")
        if end is None and start is not None and item.get("length") is not None:
            end = int(start) + int(item["length"])
        if start is None or end is None:
            return None
        return _safe_span(int(start), int(end), text=str(item.get("text") or ""))
    if isinstance(item, Sequence) and len(item) == 2:
        return _safe_span(int(item[0]), int(item[1]))
    return None


def _safe_span(start: int, end: int, *, text: str = "") -> RagSpan | None:
    if start < 0 or end < start:
        return None
    try:
        return RagSpan(start=start, end=end, text=text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 4) 出参模型
# ---------------------------------------------------------------------------
class RagChunk(BaseModel):
    """统一检索片段（契约唯一载体）。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    chunk_id: str = Field(min_length=1, max_length=128)
    doc_id: str = Field(min_length=1, max_length=128)
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    text: str = Field(min_length=1, max_length=12000)
    spans: list[RagSpan] = Field(default_factory=list)
    source: str = Field(default="project1", max_length=200)
    title: str = Field(default="", max_length=300)
    tenant: str = Field(default="", max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("score", mode="before")
    @classmethod
    def _align_score(cls, value: Any) -> float:
        return align_score(value)

    @field_validator("chunk_id", "doc_id", "text", "source", "title", "tenant", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    # ---------------- 兼容既有 MCP 契约 ----------------
    def to_kb_chunk(self) -> KbChunk:
        """转成 W2 的 ``KbChunk``（``content`` 上限 1200，与既有工具一致）。"""
        return KbChunk(
            doc_id=self.doc_id,
            title=self.title or self.doc_id,
            content=self.text[:1200],
            score=self.score,
            source=self.source,
        )

    def span_texts(self) -> list[str]:
        """按区间回切文本（区间越界时跳过），用于前端高亮或提示词加粗。"""
        result: list[str] = []
        for span in self.spans:
            piece = self.text[span.start : min(span.end, len(self.text))]
            if piece:
                result.append(piece)
            elif span.text:
                result.append(span.text)
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "score": self.score,
            "text": self.text,
            "spans": [span.to_dict() for span in self.spans],
            "source": self.source,
            "title": self.title,
            "tenant": self.tenant,
            "metadata": dict(self.metadata),
        }


class RagQuery(BaseModel):
    """统一检索入参（对应项目一 ``POST /api/v1/kb/search`` 的 body 超集）。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=3, ge=1, le=20)
    tenant: str = Field(default="agentflow", max_length=64)
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)
    filters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("query", mode="before")
    @classmethod
    def _strip_query(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("min_score", mode="before")
    @classmethod
    def _align_min(cls, value: Any) -> float:
        return align_score(value) if value is not None else 0.0

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tenant_id": self.tenant,
            "query": self.query,
            "top_k": self.top_k,
        }
        if self.filters:
            payload["filters"] = dict(self.filters)
        return payload


class RagResult(BaseModel):
    """统一检索出参（``degraded`` 语义与 FR-007 一致：失败不抛异常）。"""

    model_config = ConfigDict(extra="ignore")

    chunks: list[RagChunk] = Field(default_factory=list)
    degraded: bool = False
    error_code: str | None = None
    latency_ms: int = 0
    warnings: list[str] = Field(default_factory=list)
    query: str = ""

    def to_kb_output(self) -> KbSearchOutput:
        """回填 W2 既有契约，供现有 ``kb-search`` 工具直接复用。"""
        return KbSearchOutput(
            chunks=[chunk.to_kb_chunk() for chunk in self.chunks],
            latency_ms=self.latency_ms,
            degraded=self.degraded,
            error_code=self.error_code,
        )

    def texts(self) -> list[str]:
        return [chunk.text for chunk in self.chunks]

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunks": [chunk.to_dict() for chunk in self.chunks],
            "degraded": self.degraded,
            "error_code": self.error_code,
            "latency_ms": self.latency_ms,
            "warnings": list(self.warnings),
            "query": self.query,
        }


@dataclass(slots=True)
class RagContractReport:
    """契约体检报告（联调用：既能一次看全所有问题，也能断言整体是否通过）。"""

    ok: bool
    chunks: list[RagChunk] = field(default_factory=list)
    issues: list[RagFieldIssue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    total: int = 0
    missing_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "total": self.total,
            "parsed": len(self.chunks),
            "issues": [issue.to_dict() for issue in self.issues],
            "warnings": list(self.warnings),
            "missing_fields": list(self.missing_fields),
        }


# ---------------------------------------------------------------------------
# 5) 归一化与校验
# ---------------------------------------------------------------------------
def _pick(payload: Mapping[str, Any], field_name: str) -> tuple[bool, Any]:
    """按别名优先级取值；返回 ``(是否命中, 值)``。"""
    for alias in _FIELD_ALIASES.get(field_name, (field_name,)):
        if alias in payload and payload[alias] is not None:
            return True, payload[alias]
    return False, None


def _strict_default() -> bool:
    """``RAG_CONTRACT_STRICT=1`` 让全局默认走严格模式（联调期用，无需改代码）。"""
    return os.environ.get("RAG_CONTRACT_STRICT", "").strip().lower() in {"1", "true", "yes", "on"}


def validate_chunk(payload: Any, *, index: int | None = 0, strict: bool = True) -> RagChunk:
    """**只校验不修补**：缺字段/字段非法时抛 ``RagSchemaError``（一次报全）。"""
    if not isinstance(payload, Mapping):
        raise RagSchemaError([RagFieldIssue("chunk", f"期望对象，实际 {type(payload).__name__}", index)], where="chunk")
    issues: list[RagFieldIssue] = []
    for field_name in REQUIRED_CHUNK_FIELDS:
        found, value = _pick(payload, field_name)
        if not found:
            issues.append(RagFieldIssue(field_name, "字段缺失", index))
            continue
        if field_name in {"chunk_id", "doc_id", "source"} and not str(value).strip():
            issues.append(RagFieldIssue(field_name, "字段为空", index))
        elif field_name == "text" and not str(value).strip():
            issues.append(RagFieldIssue(field_name, "字段为空（片段正文不可为空）", index))
        elif field_name == "spans" and not isinstance(value, (list, tuple, str, Mapping)):
            issues.append(RagFieldIssue(field_name, f"类型不支持：{type(value).__name__}", index))
    if issues:
        raise RagSchemaError(issues, where="chunk")
    return _build_chunk(payload, index=index or 0, strict=True)


def normalize_chunk(
    payload: Any,
    *,
    index: int = 0,
    default_source: str = "project1",
    strict: bool | None = None,
) -> RagChunk:
    """**尽力修补**：别名映射 + ``chunk_id`` 派生 + 打分对齐 + spans 容错。

    修补不了的（没有 ``doc_id`` 或正文为空）仍然抛错——这种片段拿进来也无用，
    与其让它污染提示词，不如让调用方看到明确原因。
    """
    mode = _strict_default() if strict is None else bool(strict)
    if mode:
        return validate_chunk(payload, index=index, strict=True)
    if not isinstance(payload, Mapping):
        raise RagSchemaError([RagFieldIssue("chunk", f"期望对象，实际 {type(payload).__name__}", index)], where="chunk")
    return _build_chunk(payload, index=index, strict=False, default_source=default_source)


def _build_chunk(
    payload: Mapping[str, Any],
    *,
    index: int,
    strict: bool,
    default_source: str = "project1",
) -> RagChunk:
    issues: list[RagFieldIssue] = []

    has_chunk_id, chunk_raw = _pick(payload, "chunk_id")
    chunk_id = str(chunk_raw or "").strip() if has_chunk_id else ""

    _, doc_raw = _pick(payload, "doc_id")
    doc_id = str(doc_raw or "").strip()
    if not doc_id and chunk_id:
        # 兼容 Chroma 直出（只有 ``id``，文档号藏在 ``id`` 前缀里：``doc#0``）
        doc_id = chunk_id.split("#", 1)[0].strip()
    if not doc_id:
        issues.append(RagFieldIssue("doc_id", "缺失或为空（无法定位来源文档）", index))

    _, text_raw = _pick(payload, "text")
    text = str(text_raw or "").strip()
    if not text:
        issues.append(RagFieldIssue("text", "缺失或为空（片段正文不可为空）", index))

    if issues:
        raise RagSchemaError(issues, where="chunk")

    if not chunk_id:
        # 派生规则固定：同一 doc 的第 N 条 → ``doc_id#N``，保证幂等（重复解析不漂移）
        chunk_id = f"{doc_id}#{index}"

    _, score_raw = _pick(payload, "score")
    _, spans_raw = _pick(payload, "spans")
    _, source_raw = _pick(payload, "source")
    source = str(source_raw or "").strip() or default_source
    title_raw = payload.get("title") or payload.get("doc_title") or ""
    tenant_raw = payload.get("tenant_id") or payload.get("tenant") or ""
    metadata = payload.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}

    try:
        return RagChunk(
            chunk_id=chunk_id,
            doc_id=doc_id,
            score=align_score(score_raw),
            text=text,
            spans=parse_spans(spans_raw),
            source=source,
            title=str(title_raw)[:300],
            tenant=str(tenant_raw)[:64],
            metadata=metadata,
        )
    except ValueError as exc:  # pydantic ValidationError 也是 ValueError
        raise RagSchemaError([RagFieldIssue("chunk_id", str(exc)[:200], index)], where="chunk") from exc


def extract_chunks(payload: Any) -> list[Any]:
    """从任意常见响应形态里取出原始 chunks 列表（取不到就是空列表）。"""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, Mapping):
        return []
    for path in _CHUNK_CONTAINERS:
        node: Any = payload
        for key in path:
            node = node.get(key) if isinstance(node, Mapping) else None
            if node is None:
                break
        if isinstance(node, list):
            return node
    return []


def check_kb_contract(
    payload: Any,
    *,
    strict: bool | None = None,
    default_source: str = "project1",
    top_k: int | None = None,
) -> RagContractReport:
    """对项目一知识库响应做一次完整体检（不抛异常，问题全量收集）。"""
    mode = _strict_default() if strict is None else bool(strict)
    raw_chunks = extract_chunks(payload)
    chunks: list[RagChunk] = []
    issues: list[RagFieldIssue] = []
    warnings: list[str] = []
    missing: set[str] = set()

    for index, raw in enumerate(raw_chunks):
        if isinstance(raw, Mapping):
            # 契约体检口径：**字段在不在**与「能不能补」是两件事，缺了就记账
            for field_name in REQUIRED_CHUNK_FIELDS:
                found, _ = _pick(raw, field_name)
                if not found:
                    missing.add(field_name)
        try:
            chunks.append(normalize_chunk(raw, index=index, default_source=default_source, strict=mode))
        except RagSchemaError as exc:
            issues.extend(exc.issues)
            warnings.append(str(exc))
            for issue in exc.issues:
                if "缺失" in issue.reason or "为空" in issue.reason:
                    missing.add(issue.field_name)

    if not raw_chunks:
        warnings.append("响应中没有可解析的 chunks（字段缺失或检索为空）")

    if top_k is not None and len(chunks) > top_k:
        chunks = rank_chunks(chunks, top_k=top_k)

    return RagContractReport(
        ok=bool(chunks) and not issues and not missing,
        chunks=chunks,
        issues=issues,
        warnings=warnings,
        total=len(raw_chunks),
        missing_fields=sorted(missing),
    )


def validate_response(
    payload: Any,
    *,
    strict: bool | None = None,
    default_source: str = "project1",
    top_k: int | None = None,
    query: str = "",
    latency_ms: int = 0,
) -> RagResult:
    """契约校验 + 归一化 → ``RagResult``；``strict`` 下有任何问题即抛错。"""
    mode = _strict_default() if strict is None else bool(strict)
    report = check_kb_contract(payload, strict=mode, default_source=default_source, top_k=top_k)
    if mode and report.issues:
        raise RagSchemaError(report.issues, where="response")
    degraded = bool(report.issues) and not report.chunks
    if degraded:
        error_code = McpErrorCode.KB_UNAVAILABLE.value
    elif report.issues or report.missing_fields:
        error_code = "KB_PARTIAL"
    else:
        error_code = None
    logger.debug(
        "rag_contract_checked",
        extra={
            "total": report.total,
            "parsed": len(report.chunks),
            "issues": len(report.issues),
            "missing_fields": report.missing_fields,
        },
    )
    return RagResult(
        chunks=report.chunks,
        degraded=degraded,
        error_code=error_code,
        latency_ms=latency_ms,
        warnings=report.warnings,
        query=query,
    )


# ---------------------------------------------------------------------------
# 6) Mock 兜底（MOCK_EXTERNAL_RAG=true）
# ---------------------------------------------------------------------------
#: 预置片段：与 ``kb_search.MOCK_CHUNKS`` 同源（W41/W42 周报），此处补齐契约六字段
MOCK_RAG_CHUNKS: list[dict[str, Any]] = [
    {
        "chunk_id": "kb-w41-001#0",
        "doc_id": "kb-w41-001",
        "score": 0.93,
        "text": (
            "数码配件爆款 SKU-10086 库存周转天数由 9 天升至 21 天，存在断货风险，已同步供应链；"
            "该 SKU 贡献数码配件品类 31% 自然流量，若断货将导致流量向低转化长尾 SKU 分散。"
        ),
        "spans": [[0, 30]],
        "source": "kb:weekly_report:W41",
        "title": "W41 电商经营周报｜风险与异常",
        "keywords": ["库存", "断货", "sku-10086", "数码配件", "风险", "周转"],
    },
    {
        "chunk_id": "kb-w42-001#0",
        "doc_id": "kb-w42-001",
        "score": 0.88,
        "text": (
            "W42 数码配件品类 GMV 环比下滑 18%，转化率降至 1.7%，主因爆款 SKU 断货后流量结构恶化；" "其余 7 个品类环比波动均在 ±3% 以内。"
        ),
        "spans": [{"start": 0, "end": 26}],
        "source": "kb:weekly_report:W42",
        "title": "W42 电商经营周报｜品类明细",
        "keywords": ["gmv", "下滑", "转化率", "数码配件", "品类", "环比"],
    },
    {
        "chunk_id": "kb-w40-003#0",
        "doc_id": "kb-w40-003",
        "score": 0.76,
        "text": ("数码配件品类 W41 起日预算由 12,000 元下调至 7,200 元（-40%），付费流量预计减少约 24%，" "建议同步评估自然流量承接能力。"),
        "spans": "0-24,25-60",
        "source": "kb:weekly_report:W40",
        "title": "W40 电商经营周报｜投放复盘",
        "keywords": ["投放", "预算", "付费流量", "数码配件", "复盘"],
    },
    {
        "chunk_id": "kb-w41-002#0",
        "doc_id": "kb-w41-002",
        "score": 0.81,
        "text": (
            "W41（10-05~10-11）全店 GMV 3,842 万元，环比 +1.2%；数码配件品类 GMV 占比 27%，" "客单价 268 元，转化率 3.1%，为近 8 周峰值。"
        ),
        "spans": [],
        "source": "kb:weekly_report:W41",
        "title": "W41 电商经营周报｜经营概览",
        "keywords": ["gmv", "经营概览", "数码配件", "转化率", "客单价"],
    },
]


def mock_enabled(override: bool | None = None) -> bool:
    """``MOCK_EXTERNAL_RAG`` 运行期读取（允许测试/调用方显式覆盖）。"""
    return bool(settings.MOCK_EXTERNAL_RAG) if override is None else bool(override)


def build_mock_result(
    query: str,
    *,
    top_k: int = 3,
    tenant: str = "agentflow",
    min_score: float = 0.0,
    latency_ms: int = 0,
) -> RagResult:
    """离线预置检索：按关键词命中数微调分数，保证 demo 与单测可复现。"""
    lowered = (query or "").lower()
    scored: list[tuple[float, dict[str, Any]]] = []
    for chunk in MOCK_RAG_CHUNKS:
        hits = sum(1 for kw in chunk["keywords"] if kw.lower() in lowered)
        scored.append((min(0.99, chunk["score"] + 0.02 * hits), chunk))
    scored.sort(key=lambda item: item[0], reverse=True)
    picked = scored[: max(1, min(top_k, len(scored)))]
    chunks: list[RagChunk] = []
    for score, chunk in picked:
        if score < min_score:
            continue
        payload = {key: value for key, value in chunk.items() if key != "keywords"}
        payload["score"] = score
        payload["tenant"] = tenant
        chunks.append(normalize_chunk(payload, index=len(chunks), default_source="mock-rag", strict=False))
    return RagResult(
        chunks=chunks,
        degraded=False,
        error_code=None,
        latency_ms=latency_ms,
        warnings=[] if chunks else ["mock 无命中（query 与预置语料无交集）"],
        query=query,
    )


# ---------------------------------------------------------------------------
# 7) 契约客户端（mock / 真实 HTTP 一个入口）
# ---------------------------------------------------------------------------
class RagContractClient:
    """项目一知识库的统一调用入口：mock 分流 + 契约校验 + 降级不抛异常。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        search_path: str | None = None,
        tenant: str | None = None,
        timeout: float | None = None,
        mock: bool | None = None,
        strict: bool | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or settings.EXTERNAL_RAG_BASE_URL).rstrip("/")
        self.token = settings.EXTERNAL_RAG_TOKEN if token is None else token
        self.search_path = search_path or w5.MEMORY_RAG_SEARCH_PATH
        self.tenant = tenant or w5.MEMORY_RAG_TENANT
        self.timeout = float(timeout or settings.EXTERNAL_RAG_TIMEOUT_SECONDS)
        self.mock = mock
        self.strict = strict
        self.transport = transport

    @property
    def mock_active(self) -> bool:
        return mock_enabled(self.mock)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def search(
        self,
        query: RagQuery | str,
        *,
        top_k: int | None = None,
        min_score: float | None = None,
        strict: bool | None = None,
    ) -> RagResult:
        started = time.perf_counter()
        request = query if isinstance(query, RagQuery) else RagQuery(query=query, top_k=top_k or 3, tenant=self.tenant)
        if top_k is not None or min_score is not None:
            request = RagQuery(
                query=request.query,
                top_k=top_k or request.top_k,
                tenant=request.tenant,
                min_score=min_score if min_score is not None else request.min_score,
                filters=request.filters,
            )
        mode = self.strict if strict is None else strict

        if self.mock_active:
            result = build_mock_result(
                request.query,
                top_k=request.top_k,
                tenant=request.tenant,
                min_score=request.min_score,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            result.warnings.append("mock-external-rag：未发起真实网络请求")
            return _apply_min_score(result, request.min_score)

        payload = request.to_payload()
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.post(f"{self.base_url}{self.search_path}", json=payload, headers=self._headers())
        except httpx.TimeoutException:
            return self._degraded(request, McpErrorCode.KB_TIMEOUT, started, "请求超时")
        except httpx.HTTPError as exc:
            return self._degraded(request, McpErrorCode.KB_UNAVAILABLE, started, str(exc)[:200])

        if response.status_code in (401, 403):
            return self._degraded(request, McpErrorCode.KB_AUTH_FAILED, started, f"鉴权失败（{response.status_code}）")
        if response.status_code >= 400:
            return self._degraded(request, McpErrorCode.KB_UNAVAILABLE, started, f"HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            return self._degraded(request, McpErrorCode.KB_UNAVAILABLE, started, "响应不是合法 JSON")

        try:
            result = validate_response(
                body,
                strict=mode,
                default_source="project1",
                top_k=request.top_k,
                query=request.query,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except RagSchemaError as exc:
            logger.warning("rag_contract_violation", extra={"error": str(exc)[:300]})
            return self._degraded(request, McpErrorCode.KB_UNAVAILABLE, started, str(exc)[:200])
        return _apply_min_score(result, request.min_score)

    def _degraded(self, request: RagQuery, code: McpErrorCode, started: float, reason: str) -> RagResult:
        logger.warning("rag_contract_degraded", extra={"error_code": code.value, "reason": reason})
        return RagResult(
            chunks=[],
            degraded=True,
            error_code=code.value,
            latency_ms=int((time.perf_counter() - started) * 1000),
            warnings=[f"{code.value}: {reason}"],
            query=request.query,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "schema_version": RAG_SCHEMA_VERSION,
            "required_fields": list(REQUIRED_CHUNK_FIELDS),
            "mock": self.mock_active,
            "base_url": self.base_url if not self.mock_active else "mock",
            "search_path": self.search_path,
            "tenant": self.tenant,
            "strict": _strict_default() if self.strict is None else bool(self.strict),
        }


def _apply_min_score(result: RagResult, min_score: float) -> RagResult:
    if min_score <= 0.0 or not result.chunks:
        return result
    kept = [chunk for chunk in result.chunks if chunk.score >= min_score]
    removed = len(result.chunks) - len(kept)
    if removed:
        result.chunks = rank_chunks(kept)
        result.warnings.append(f"min_score={min_score} 过滤掉 {removed} 条低分片段")
    return result


def build_rag_client(*, mock: bool | None = None, **kwargs: Any) -> RagContractClient:
    """按 ``MOCK_EXTERNAL_RAG`` 选择实现（mock=True 时**永不发网络请求**）。"""
    return RagContractClient(mock=mock, **kwargs)


def describe() -> dict[str, Any]:
    """自检用（挂到 ``/memories/meta`` 之类的地方也安全，不含密钥）。"""
    return {
        "schema_version": RAG_SCHEMA_VERSION,
        "required_fields": list(REQUIRED_CHUNK_FIELDS),
        "mock_external_rag": mock_enabled(),
        "strict_default": _strict_default(),
        "contract_version_note": "chunk_id/doc_id/score/text/spans/source 六字段为唯一契约",
    }


__all__ = [
    "MOCK_RAG_CHUNKS",
    "REQUIRED_CHUNK_FIELDS",
    "RAG_SCHEMA_VERSION",
    "RagChunk",
    "RagContractClient",
    "RagContractReport",
    "RagFieldIssue",
    "RagQuery",
    "RagResult",
    "RagSchemaError",
    "RagSpan",
    "align_score",
    "build_mock_result",
    "build_rag_client",
    "check_kb_contract",
    "describe",
    "extract_chunks",
    "mock_enabled",
    "normalize_chunk",
    "parse_spans",
    "rank_chunks",
    "validate_chunk",
    "validate_response",
]
