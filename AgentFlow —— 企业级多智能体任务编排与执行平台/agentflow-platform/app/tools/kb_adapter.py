"""W4：kb-search MCP **适配层**（不改 MCP 外层框架：mcp_stdio / mcp_client 零改动）。

W2 已交付 ``kb_search.HttpKbProvider``（超时 8s + 重试 2 次 + 降级空结果），本文件在它之上补齐
W4 要求的三件事，且**只做适配、不动协议层**：

1. **熔断**：连续失败达阈值后打开断路器，在冷却窗口内直接跳过 HTTP，避免每个子任务都等满超时；
2. **兜底数据源**：HTTP 不可用（或熔断打开）时，退到 mall 只读库直查「断货/SKU」证据，
   保证非 Mock 模式仍能拿到可引用的真实数据，而不是空 chunks；
3. **开关语义不变**：``MOCK_EXTERNAL_RAG=true`` 依旧走 ``MockKbProvider``，一行逻辑都不改。

装配位置：``kb_search.KbSearchTool.__init__`` 的 provider 选择改为调用本文件的
``build_kb_provider()``（仅此一处，其余 MCP 外层框架与工具签名完全不变）。

数据源说明（不硬编码列名）：
- 项目一 mall 库的表结构由项目一维护，这里通过 ``inspect`` 探测 ``skus`` 表的实际列名，
  按「候选名列表」映射出 SKU 编码 / 名称 / 库存 / 周转天数 / 状态；
- 列名全部探测不到时**不猜**，直接降级并返回 ``KB_UNAVAILABLE``，避免拿错列产生假证据；
- 支持 ``MALL_RO_DSN`` 覆盖只读 DSN；未配置时用 W2 的 ``SQL_RO_*`` 拼 ``mysql+aiomysql`` DSN。

安全边界：
- 只执行**参数化 SELECT**，语句由本文件拼装，不接受任何外部 SQL 文本；
- 只读账号（``SQL_RO_USER``）连接，与 sql-query 工具共用同一套凭证口径。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import settings
from app.core.logging import get_logger
from app.core.w2_config import w2
from app.schemas.mcp import KbChunk, KbSearchOutput, McpErrorCode
from app.tools.mcp_servers.kb_search import HttpKbProvider, KbProvider, MockKbProvider

logger = get_logger(__name__)

#: 从 query 里提取 SKU 编码（如 SKU-10086 / sku_10086）
_SKU_RE = re.compile(r"sku[\s\-_]?(\d{3,})", re.IGNORECASE)
#: 从 query 里提取中文/英文关键词（用于名称模糊匹配）
_TOKEN_RE = re.compile(r"[\u4e00-\u9fa5]{2,}|[A-Za-z]{3,}")
#: 领域词表：长中文串直接 LIKE 会全部落空（"库存周转风险" ≠ "库存"），先按词表切
_DOMAIN_KEYWORDS: tuple[str, ...] = (
    "断货",
    "缺货",
    "库存",
    "周转",
    "补货",
    "下架",
    "供应链",
    "转化率",
    "流量",
    "预算",
    "投放",
    "付费",
    "自然流量",
    "数码配件",
    "服饰鞋包",
    "家居日用",
    "食品生鲜",
    "美妆个护",
    "客单价",
    "订单量",
    "退款",
    "爆款",
)


# ---------------------------------------------------------------------------
# 1. 熔断器
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class BreakerSnapshot:
    name: str
    state: str
    failures: int
    opened_at: float | None
    remaining_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "failures": self.failures,
            "opened_at": self.opened_at,
            "remaining_seconds": round(self.remaining_seconds, 2),
        }


class CircuitBreaker:
    """三态熔断（closed / open / half_open），线程内单实例，按 asyncio 单线程语义安全。

    参数刻意保守：3 次连续失败即打开，30s 后允许一次半开试探——
    kb-search 是**旁路证据**，宁可少查也不能让每个子任务都白等 8s×3 次。
    """

    def __init__(self, *, name: str = "kb-search", failure_threshold: int = 3, recovery_seconds: float = 30.0) -> None:
        self.name = name
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_seconds = max(1.0, recovery_seconds)
        self._failures = 0
        self._opened_at: float | None = None
        self._half_open = False
        self._success = 0

    # ---------------- 状态 ----------------
    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if (time.monotonic() - self._opened_at) >= self.recovery_seconds:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        """open 拒绝；half_open 允许一次试探。"""
        state = self.state
        if state == "closed":
            return True
        if state == "half_open":
            if not self._half_open:
                self._half_open = True
                return True
            return False
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._half_open = False
        self._success += 1

    def record_failure(self) -> None:
        self._failures += 1
        if self.state == "half_open" or self._failures >= self.failure_threshold:
            self._opened_at = time.monotonic()
            self._half_open = False
            logger.warning(
                "kb_circuit_open",
                extra={"failures": self._failures, "recovery_seconds": self.recovery_seconds},
            )

    def reset(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._half_open = False

    def snapshot(self) -> BreakerSnapshot:
        remaining = 0.0
        if self._opened_at is not None:
            remaining = max(0.0, self.recovery_seconds - (time.monotonic() - self._opened_at))
        return BreakerSnapshot(
            name=self.name, state=self.state, failures=self._failures, opened_at=self._opened_at, remaining_seconds=remaining
        )


_GLOBAL_BREAKER = CircuitBreaker(name="kb-search-http")


def get_breaker() -> CircuitBreaker:
    return _GLOBAL_BREAKER


def reset_breaker() -> None:
    _GLOBAL_BREAKER.reset()


# ---------------------------------------------------------------------------
# 2. mall 只读库兜底 Provider
# ---------------------------------------------------------------------------
#: 候选列名（按优先级）—— 探测不到就降级，绝不猜测
_COLUMN_CANDIDATES: dict[str, tuple[str, ...]] = {
    "code": ("sku_code", "sku_no", "sku_id", "code", "id"),
    "name": ("sku_name", "name", "title", "product_name"),
    "category": ("category_name", "category", "category_id", "cate_name"),
    "stock": ("stock", "stock_qty", "inventory", "inventory_qty", "qty"),
    "turnover": ("turnover_days", "stock_days", "turnover", "stock_turnover_days"),
    "status": ("status", "state", "is_active"),
}


def default_mall_dsn() -> str:
    """mall 只读 DSN：优先 ``MALL_RO_DSN``，否则用 W2 的 SQL_RO_* 拼 MySQL DSN。"""
    import os

    override = os.environ.get("MALL_RO_DSN", "").strip()
    if override:
        return override
    return (
        f"mysql+aiomysql://{w2.SQL_RO_USER}:{w2.SQL_RO_PASSWORD}"
        f"@{w2.SQL_RO_HOST}:{w2.SQL_RO_PORT}/{w2.SQL_RO_DB}?charset=utf8mb4"
    )


class MallKbProvider(KbProvider):
    """直查 mall 只读库生成证据片段（非 Mock 模式的兜底路径）。"""

    def __init__(self, *, dsn: str | None = None, timeout: float | None = None, table: str = "skus") -> None:
        self.dsn = dsn or default_mall_dsn()
        self.timeout = float(timeout or w2.KB_TIMEOUT_SECONDS)
        self.table = table
        self._engine: AsyncEngine | None = None
        self._columns: dict[str, str] | None = None

    # ---------------- 连接 ----------------
    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(self.dsn, future=True, pool_pre_ping=True)
        return self._engine

    async def aclose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    async def _columns_safe(self) -> dict[str, str]:
        """按候选名探测实际列名；结果缓存（运行期表结构不变）。"""
        if self._columns is not None:
            return self._columns

        def _read(sync_conn: Any) -> dict[str, str]:
            inspector = sa_inspect(sync_conn)
            if self.table not in inspector.get_table_names():
                return {}
            actual = {str(col["name"]).lower(): str(col["name"]) for col in inspector.get_columns(self.table)}
            resolved: dict[str, str] = {}
            for logical, candidates in _COLUMN_CANDIDATES.items():
                for candidate in candidates:
                    if candidate in actual:
                        resolved[logical] = actual[candidate]
                        break
            return resolved

        async with self.engine.connect() as conn:
            self._columns = await conn.run_sync(_read)
        return self._columns

    # ---------------- 查询 ----------------
    @staticmethod
    def _extract_terms(query: str) -> list[str]:
        terms: list[str] = []
        for match in _SKU_RE.finditer(query):
            terms.append(f"SKU-{match.group(1)}")
            terms.append(match.group(1))
        # 领域词优先：长中文串整串 LIKE 命中率极低，必须切出"库存/断货/周转"这类可匹配词
        for word in _DOMAIN_KEYWORDS:
            if word in query:
                terms.append(word)
        for token in _TOKEN_RE.findall(query):
            if 2 <= len(token) <= 6:
                terms.append(token)
        # 去重保序，最多 6 个词，避免 LIKE 组合爆炸
        seen: set[str] = set()
        ordered: list[str] = []
        for term in terms:
            if term.lower() not in seen:
                seen.add(term.lower())
                ordered.append(term)
        return ordered[:6]

    async def search(self, query: str, top_k: int, tenant: str) -> KbSearchOutput:
        started = time.perf_counter()
        try:
            columns = await asyncio.wait_for(self._columns_safe(), timeout=self.timeout)
        except asyncio.TimeoutError:
            return self._degraded(started, McpErrorCode.KB_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - 库不可用一律降级
            logger.warning("mall_kb_schema_failed", extra={"error": str(exc)[:200]})
            return self._degraded(started, McpErrorCode.KB_UNAVAILABLE)

        if not columns.get("code") and not columns.get("name"):
            logger.warning("mall_kb_columns_unresolved", extra={"table": self.table, "resolved": list(columns)})
            return self._degraded(started, McpErrorCode.KB_UNAVAILABLE)

        terms = self._extract_terms(query)
        try:
            rows = await asyncio.wait_for(self._query_rows(columns, terms, top_k), timeout=self.timeout)
            if not rows:
                # 关键词命不中 SKU 列（如"库存周转风险"）时，退化为「周转天数最高的 top-N」——
                # 这正是用户问"有没有断货风险"时想看的候选清单，而不是空结果
                rows = await asyncio.wait_for(
                    self._query_rows(columns, terms, top_k, relaxed=True), timeout=self.timeout
                )
        except asyncio.TimeoutError:
            return self._degraded(started, McpErrorCode.KB_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mall_kb_query_failed", extra={"error": str(exc)[:200]})
            return self._degraded(started, McpErrorCode.KB_UNAVAILABLE)

        chunks = [self._to_chunk(row, columns, query) for row in rows]
        chunks = [c for c in chunks if c is not None][:top_k]
        return KbSearchOutput(
            chunks=chunks,
            latency_ms=int((time.perf_counter() - started) * 1000),
            degraded=not chunks,
            error_code=None if chunks else McpErrorCode.KB_UNAVAILABLE.value,
        )

    async def _query_rows(
        self, columns: dict[str, str], terms: Sequence[str], top_k: int, *, relaxed: bool = False
    ) -> list[dict[str, Any]]:
        code = columns.get("code")
        name = columns.get("name")
        category = columns.get("category")
        turnover = columns.get("turnover")
        selected = ", ".join(dict.fromkeys(columns.values()))
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if terms and not relaxed:
            for index, term in enumerate(terms):
                if code:
                    clauses.append(f"CAST({code} AS CHAR) LIKE :t{index}")
                if name:
                    clauses.append(f"{name} LIKE :t{index}")
                if category:
                    clauses.append(f"{category} LIKE :t{index}")
                params[f"t{index}"] = f"%{term}%"
        where = f" WHERE ({' OR '.join(clauses)})" if clauses else ""
        order = f" ORDER BY {turnover} DESC" if turnover else ""
        sql = f"SELECT {selected} FROM {self.table}{where}{order} LIMIT :limit"
        params["limit"] = max(1, min(top_k * 3, 20))
        async with self.engine.connect() as conn:
            result = await conn.execute(text(sql), params)
            return [dict(row._mapping) for row in result.fetchall()]

    def _to_chunk(self, row: dict[str, Any], columns: dict[str, str], query: str) -> KbChunk | None:
        by_lower = {str(k).lower(): v for k, v in row.items()}
        code = str(by_lower.get(str(columns.get("code", "")).lower()) or "")
        name = str(by_lower.get(str(columns.get("name", "")).lower()) or "")
        category = str(by_lower.get(str(columns.get("category", "")).lower()) or "")
        stock = by_lower.get(str(columns.get("stock", "")).lower())
        turnover = by_lower.get(str(columns.get("turnover", "")).lower())
        status = by_lower.get(str(columns.get("status", "")).lower())

        stock_num = _to_float(stock)
        parts: list[str] = []
        if category:
            parts.append(f"品类 {category}")
        if stock is not None:
            parts.append(f"库存 {stock}")
        if turnover is not None:
            parts.append(f"库存周转天数 {turnover} 天")
        if status is not None:
            parts.append(f"状态 {status}")
        if stock_num is not None and stock_num <= 0:
            parts.append("已断货")

        content = f"{name}（{code}）：" + "，".join(parts) if parts else f"{name}（{code}）"
        if not content.strip("（）："):
            return None

        matched = any(token.lower() in f"{code}{name}{category}".lower() for token in self._extract_terms(query))
        score = 0.95 if matched else 0.7
        return KbChunk(
            doc_id=f"mall:skus:{code or name}",
            title=f"{name}｜SKU 库存明细",
            content=content[:300],
            score=score,
            source=f"mall:{self.table}",
        )

    @staticmethod
    def _degraded(started: float, code: McpErrorCode) -> KbSearchOutput:
        return KbSearchOutput(
            chunks=[],
            latency_ms=int((time.perf_counter() - started) * 1000),
            degraded=True,
            error_code=code.value,
        )


def _to_float(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 3. 韧性 Provider（熔断 + 兜底链）
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ResilientKbProvider(KbProvider):
    """primary（HTTP）→ fallbacks（mall 只读库）→ 空结果降级。

    与 W2 的差别只有两点：**熔断打开时不再白等超时**、**HTTP 失败后还有真实数据兜底**。
    返回类型与字段完全沿用 ``KbSearchOutput``，Executor/Reviewer 无需任何改动。
    """

    primary: KbProvider | None = None
    fallbacks: list[KbProvider] = field(default_factory=list)
    breaker: CircuitBreaker = field(default_factory=lambda: _GLOBAL_BREAKER)
    timeout: float = field(default_factory=lambda: float(w2.KB_TIMEOUT_SECONDS) + 1.0)

    async def search(self, query: str, top_k: int, tenant: str) -> KbSearchOutput:
        started = time.perf_counter()
        skipped_primary = False

        if self.primary is not None:
            if self.breaker.allow():
                try:
                    result = await asyncio.wait_for(self.primary.search(query, top_k, tenant), timeout=self.timeout)
                except asyncio.TimeoutError:
                    self.breaker.record_failure()
                    logger.warning("kb_primary_timeout", extra={"timeout": self.timeout})
                except Exception as exc:  # noqa: BLE001 - 适配层必须吞掉，不阻塞编排
                    self.breaker.record_failure()
                    logger.warning("kb_primary_failed", extra={"error": str(exc)[:200]})
                else:
                    if result.degraded:
                        # 只有"降级/失败"才算失败；空 chunks 是"库中确实没有相关内容"，
                        # 属于正常业务结果，不能计熔断次数，也不该再去兜底数据源拿假证据。
                        self.breaker.record_failure()
                    else:
                        self.breaker.record_success()
                        return result
            else:
                skipped_primary = True
                logger.info("kb_circuit_skipped", extra={"state": self.breaker.state})

        for index, fallback in enumerate(self.fallbacks):
            try:
                result = await fallback.search(query, top_k, tenant)
            except Exception as exc:  # noqa: BLE001
                logger.warning("kb_fallback_failed", extra={"index": index, "error": str(exc)[:200]})
                continue
            if result.chunks:
                logger.info("kb_fallback_used", extra={"index": index, "chunks": len(result.chunks)})
                return result

        error_code = McpErrorCode.KB_UNAVAILABLE.value
        return KbSearchOutput(
            chunks=[],
            latency_ms=int((time.perf_counter() - started) * 1000),
            degraded=True,
            error_code=error_code,
        )


# ---------------------------------------------------------------------------
# 4. 工厂（唯一的装配入口）
# ---------------------------------------------------------------------------
def build_kb_provider(*, mock: bool | None = None, mall_dsn: str | None = None) -> KbProvider:
    """按开关返回 provider。

    - ``MOCK_EXTERNAL_RAG=true``（默认）：**原样返回** ``MockKbProvider()``，W2 行为不变；
    - ``false``：按 ``EXTERNAL_RAG_CONTRACT`` 选择项目一真实契约
      （``project1_kb_query`` 默认 / ``legacy_kb_search`` / ``mall_only``），
      外层统一套熔断，失败后按 fallbacks 顺序兜底。

    说明：项目一真实检索入口是 ``POST /api/v1/kbs/{kb_id}/query``（见 ``app/tools/project1_kb.py``），
    PRD A-01 假设的 ``POST /api/v1/kb/search`` 在项目一并不存在，只在显式选择
    ``legacy_kb_search`` 时才作为主路（用于项目一后续补齐该接口的情况）。
    """
    is_mock = settings.MOCK_EXTERNAL_RAG if mock is None else mock
    if is_mock:
        return MockKbProvider()

    contract = (settings.EXTERNAL_RAG_CONTRACT or "project1_kb_query").strip().lower()
    fallbacks: list[KbProvider] = []
    dsn = mall_dsn or default_mall_dsn()
    if dsn:
        fallbacks.append(MallKbProvider(dsn=dsn))

    if contract == "mall_only":
        # 纯兜底数据源模式（项目一不可用时的降级演示）
        return ResilientKbProvider(primary=None, fallbacks=fallbacks)

    if contract == "legacy_kb_search":
        primary: KbProvider = HttpKbProvider()
    else:
        # 延迟 import：project1_kb 依赖 kb_search，避免模块级循环
        from app.tools.project1_kb import ProjectOneKbProvider

        primary = ProjectOneKbProvider()
        # 项目一若未来补上 A-01 的 /api/v1/kb/search，可把开关切成 legacy 直接复用
        fallbacks.insert(0, HttpKbProvider())

    return ResilientKbProvider(primary=primary, fallbacks=fallbacks)


__all__ = [
    "BreakerSnapshot",
    "CircuitBreaker",
    "MallKbProvider",
    "ResilientKbProvider",
    "build_kb_provider",
    "default_mall_dsn",
    "get_breaker",
    "reset_breaker",
]
