"""上下文工程服务（PRD FR-010 / 第 9 章）：Token 预算、工具结果瘦身、上下文压缩。

三个能力，边界清晰：
1. **预算**：``plan_context`` 逐组件预估 Token，超节点预算 80% 触发压缩；
2. **瘦身**：``slim_tool_result`` 在工具结果进入上下文**之前**执行（原始结果只落库）；
3. **压缩**：``compress`` 保留最近 3 轮原文，更早的工具结果替换为"结论 + 关键数字"模板摘要，
   关键数值按白名单正则保留，最多压 2 层（防信息塌缩不可控）。

所有动作都返回可写入 ``token_ledger`` / ``context_meta`` 的度量，供 trace 与压缩 A/B 实验统计。
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.core.logging import get_logger
from app.core.w2_config import w2
from app.schemas.context import (
    CompressionConfig,
    CompressionResult,
    ContextBuildResult,
    SlimmingConfig,
    SlimResult,
    TokenEstimate,
)
from app.utils.tokens import estimate_json_tokens, estimate_messages_tokens, estimate_tokens, message_rounds

logger = get_logger(__name__)

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?%?")
_JSON_KW = dict(ensure_ascii=False, default=str)


class ContextService:
    """无状态服务（策略可注入，便于测试与后续 A/B）。"""

    def __init__(
        self,
        *,
        compression: CompressionConfig | None = None,
        slimming: SlimmingConfig | None = None,
        budgets: dict[str, int] | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.compression = compression or CompressionConfig(
            enabled=w2.COMPRESS_ENABLED if enabled is None else enabled,
            trigger_ratio=w2.COMPRESS_TRIGGER_RATIO,
            message_rounds_threshold=w2.COMPRESS_MESSAGE_ROUNDS_THRESHOLD,
            keep_recent_rounds=w2.COMPRESS_KEEP_RECENT_ROUNDS,
            max_layers=w2.COMPRESS_MAX_LAYERS,
        )
        self.slimming = slimming or SlimmingConfig(
            kb_top_k=w2.KB_DEFAULT_TOP_K,
            max_kb_chunks=w2.KB_MAX_TOP_K,
        )
        self.budgets = budgets or w2.token_budgets

    # ------------------------------------------------------------------
    # 1) 预算
    # ------------------------------------------------------------------
    def budget(self, node: str) -> int:
        return self.budgets.get(node, w2.TOKEN_BUDGET_EXECUTOR)

    def plan_context(
        self,
        node: str,
        components: dict[str, str],
        *,
        messages: list[dict[str, Any]] | None = None,
        reserved_output: int = 0,
    ) -> ContextBuildResult:
        """构造上下文并判定是否压缩。

        ``components``：组件名 -> 文本（如 system / goal / memory / tools / history / evidence）。
        返回的 ``content`` 已按组件顺序拼接，可直接作为 LLM 输入正文。
        """
        budget = self.budget(node)
        texts = dict(components)
        if reserved_output > 0:
            # 输出预留只参与估算、不参与拼接：用空格占位（4 字符 ≈ 1 Token 的口径）
            texts["reserved_output"] = " " * (reserved_output * 4)

        estimate_before = self._estimate(texts, budget)
        rounds = message_rounds(messages or [])
        need_compress = self.compression.enabled and (
            estimate_before.over_threshold or rounds > self.compression.message_rounds_threshold
        )

        content_texts = {k: v for k, v in texts.items() if k != "reserved_output"}
        compressed = False
        layers = 0
        before_tokens = estimate_before.total
        after_tokens = before_tokens
        kept_rounds = rounds
        history_digest = ""

        if need_compress and messages:
            result = self.compress(messages, budget=budget)
            compressed = result.compressed
            layers = result.layers
            # 统一口径：压缩前后都按「组件 + 历史消息」合计，否则压缩比不可比
            before_tokens = estimate_before.total + result.before_tokens
            after_tokens = estimate_before.total + result.after_tokens
            kept_rounds = result.kept_rounds
            if result.digest:
                history_digest = result.digest
                content_texts["history"] = result.digest

        content = "\n\n".join(text for text in content_texts.values() if text)
        estimate_after = TokenEstimate.of(
            {k: estimate_tokens(v) for k, v in content_texts.items()}, budget, self.compression.trigger_ratio
        )

        return ContextBuildResult(
            node=node,
            content=content,
            budget=budget,
            estimate=estimate_after,
            compressed=compressed,
            compress_layers=layers,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            kept_rounds=kept_rounds,
            history_digest=history_digest,
        )

    def _estimate(self, texts: dict[str, str], budget: int) -> TokenEstimate:
        return TokenEstimate.of(
            {name: estimate_tokens(text) for name, text in texts.items()},
            budget,
            self.compression.trigger_ratio,
        )

    # ------------------------------------------------------------------
    # 2) 压缩
    # ------------------------------------------------------------------
    def compress(self, messages: list[dict[str, Any]], *, budget: int | None = None) -> CompressionResult:
        """保留最近 N 轮原文，更早消息压缩为模板摘要；最多压 2 层。"""
        before_tokens = estimate_messages_tokens(messages)
        if not messages:
            return CompressionResult(compressed=False, before_tokens=0, after_tokens=0)

        keep_rounds = max(1, self.compression.keep_recent_rounds)
        boundary = self._recent_boundary(messages, keep_rounds)
        older = messages[:boundary]
        recent = messages[boundary:]

        if not older:
            return CompressionResult(
                digest=self._render(recent),
                compressed=False,
                before_tokens=before_tokens,
                after_tokens=estimate_messages_tokens(recent),
                kept_rounds=keep_rounds,
            )

        preserved: list[str] = []
        summary_lines: list[str] = []
        for index, message in enumerate(older):
            line, numbers = self._summarize_message(message, index)
            summary_lines.append(line)
            preserved.extend(numbers)

        layers = 1
        digest = "\n".join(summary_lines)
        after_tokens = estimate_tokens(digest) + estimate_messages_tokens(recent)

        # 第二层：仍超阈值时只保留"结论 + 数字"，丢弃叙述性文字
        while (
            budget is not None
            and layers < self.compression.max_layers
            and after_tokens > budget * self.compression.trigger_ratio
        ):
            summary_lines = [self._aggressive_line(line) for line in summary_lines]
            layers += 1
            digest = "\n".join(summary_lines)
            after_tokens = estimate_tokens(digest) + estimate_messages_tokens(recent)

        digest_full = digest + "\n\n[最近消息原文]\n" + self._render(recent)

        if after_tokens >= before_tokens:
            # 压缩无收益（历史本身很短，模板摘要反而更长）→ 放弃压缩，保留原文
            logger.debug(
                "compress_no_gain",
                extra={"before": before_tokens, "after": after_tokens, "messages": len(messages)},
            )
            return CompressionResult(
                compressed=False,
                layers=0,
                before_tokens=before_tokens,
                after_tokens=before_tokens,
                kept_rounds=len(messages),
            )

        return CompressionResult(
            digest=digest_full,
            compressed=True,
            layers=layers,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            kept_rounds=keep_rounds,
            preserved_numbers=list(dict.fromkeys(preserved))[:40],
        )

    @staticmethod
    def _is_round_marker(message: dict[str, Any]) -> bool:
        return message.get("role") in {"user", "assistant"}

    def _recent_boundary(self, messages: list[dict[str, Any]], keep_rounds: int) -> int:
        """从后往前数第 N 个 round 的起始下标。"""
        seen = 0
        for index in range(len(messages) - 1, -1, -1):
            if self._is_round_marker(messages[index]):
                seen += 1
                if seen >= keep_rounds:
                    return index
        return 0

    def _summarize_message(self, message: dict[str, Any], index: int) -> tuple[str, list[str]]:
        role = str(message.get("role", "unknown"))
        content = str(message.get("content") or "")
        numbers = _NUMBER_RE.findall(content)
        keep_numbers = self._filter_numbers(numbers)
        head = content.strip().replace("\n", " ")[:120]
        if role == "tool":
            tool_name = message.get("name") or "tool"
            line = f"[压缩#{index}] {tool_name} 结果：{head}"
        else:
            line = f"[压缩#{index}] {role}：{head}"
        if keep_numbers:
            line += f" | 关键数值: {', '.join(keep_numbers[:8])}"
        return line, keep_numbers

    def _filter_numbers(self, numbers: list[str]) -> list[str]:
        """保留带 % 或含小数 / 大整数的数值，丢弃无信息量的短数字。"""
        kept: list[str] = []
        for token in numbers:
            if token.endswith("%") or "." in token or len(token) >= 4:
                kept.append(token)
        return kept

    @staticmethod
    def _aggressive_line(line: str) -> str:
        """第二层压缩：只保留到"关键数值"为止，丢弃叙述。"""
        marker = "| 关键数值:"
        if marker in line:
            head = line.split("：", 1)[0]
            return f"{head}：{line.split(marker, 1)[1].strip()}"
        return line[:80]

    @staticmethod
    def _render(messages: list[dict[str, Any]]) -> str:
        lines = []
        for message in messages:
            role = message.get("role", "unknown")
            content = str(message.get("content") or "").strip()
            lines.append(f"{role}: {content}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 3) 工具结果瘦身
    # ------------------------------------------------------------------
    def slim_tool_result(self, tool: str, payload: dict[str, Any]) -> SlimResult:
        """按 PRD 9.3 分工具处理；返回的 digest 才会进入上下文。"""
        normalised = (tool or "").replace("_", "-").lower()
        before = estimate_json_tokens(payload)
        if normalised == "sql-query":
            return self._slim_sql(payload, before)
        if normalised == "kb-search":
            return self._slim_kb(payload, before)
        if normalised == "chart-render":
            return self._slim_chart(payload, before)
        if normalised == "notify":
            return self._slim_notify(payload, before)

        digest = json.dumps(payload, **_JSON_KW)[: self.slimming.max_digest_chars]
        return SlimResult(
            tool=normalised,
            digest=digest,
            payload=payload,
            before_tokens=before,
            after_tokens=estimate_tokens(digest),
        )

    def _slim_sql(self, payload: dict[str, Any], before: int) -> SlimResult:
        columns: list[str] = list(payload.get("columns") or [])
        rows: list[list[Any]] = [list(r) for r in payload.get("rows") or []]
        row_count = int(payload.get("row_count") or len(rows))
        truncated = bool(payload.get("truncated"))

        preview = rows[: self.slimming.sql_preview_rows]
        rounded = [[self._round(value) for value in row] for row in preview]
        stats = self._numeric_stats(columns, rows)

        parts = [
            f"列: {columns}",
            f"行数: {row_count}{'（已截断）' if truncated or row_count > len(preview) else ''}",
            f"前 {len(rounded)} 行: {json.dumps(rounded, **_JSON_KW)}",
        ]
        if stats:
            parts.append(f"统计: {json.dumps(stats, **_JSON_KW)}")
        digest = "\n".join(parts)[: self.slimming.max_digest_chars]

        slim_payload = {
            "columns": columns,
            "row_count": row_count,
            "truncated": truncated or row_count > len(preview),
            "preview_rows": rounded,
            "stats": stats,
        }
        dropped = [] if row_count <= self.slimming.sql_preview_rows else [f"rows[{len(preview)}:]"]
        return SlimResult(
            tool="sql-query",
            digest=digest,
            payload=slim_payload,
            before_tokens=before,
            after_tokens=estimate_tokens(digest),
            truncated=bool(slim_payload["truncated"]),
            dropped_fields=dropped,
        )

    def _slim_kb(self, payload: dict[str, Any], before: int) -> SlimResult:
        raw_chunks = list(payload.get("chunks") or [])
        limit = min(self.slimming.kb_top_k, self.slimming.max_kb_chunks)
        chunks = []
        for item in raw_chunks[:limit]:
            chunks.append(
                {
                    "doc_id": item.get("doc_id"),
                    "title": item.get("title"),
                    "content": str(item.get("content") or "")[: self.slimming.kb_content_chars],
                    "score": item.get("score"),
                    "source": item.get("source"),
                }
            )
        degraded = bool(payload.get("degraded"))
        digest_obj: dict[str, Any] = {"chunks": chunks, "degraded": degraded}
        if degraded:
            digest_obj["note"] = "历史周报引用缺失（知识库不可达，已降级继续）"
        digest = json.dumps(digest_obj, **_JSON_KW)[: self.slimming.max_digest_chars]
        return SlimResult(
            tool="kb-search",
            digest=digest,
            payload=digest_obj,
            before_tokens=before,
            after_tokens=estimate_tokens(digest),
            truncated=len(raw_chunks) > limit,
            dropped_fields=[] if len(raw_chunks) <= limit else [f"chunks[{limit}:]"],
        )

    def _slim_chart(self, payload: dict[str, Any], before: int) -> SlimResult:
        kept = {field: payload.get(field) for field in self.slimming.chart_fields if field in payload}
        digest = json.dumps(kept, **_JSON_KW)
        return SlimResult(
            tool="chart-render",
            digest=digest,
            payload=kept,
            before_tokens=before,
            after_tokens=estimate_tokens(digest),
            dropped_fields=[k for k in payload if k not in kept],
        )

    def _slim_notify(self, payload: dict[str, Any], before: int) -> SlimResult:
        kept = {field: payload.get(field) for field in self.slimming.notify_fields if field in payload}
        digest = json.dumps(kept, **_JSON_KW)
        return SlimResult(
            tool="notify",
            digest=digest,
            payload=kept,
            before_tokens=before,
            after_tokens=estimate_tokens(digest),
            dropped_fields=[k for k in payload if k not in kept],
        )

    def _numeric_stats(self, columns: list[str], rows: list[list[Any]]) -> dict[str, dict[str, float]]:
        """全量行（非仅前 20 行）计算统计，保证关键数字不因截断丢失。"""
        stats: dict[str, dict[str, float]] = {}
        if not rows:
            return stats
        for index, name in enumerate(columns):
            values: list[float] = []
            for row in rows:
                if index >= len(row):
                    continue
                value = row[index]
                if isinstance(value, bool) or value is None:
                    continue
                if isinstance(value, (int, float)):
                    values.append(float(value))
                elif isinstance(value, str):
                    try:
                        values.append(float(value))
                    except ValueError:
                        continue
            if values and len(values) == len(rows):
                stats[name] = {
                    "sum": round(sum(values), self.slimming.sql_float_digits),
                    "avg": round(sum(values) / len(values), self.slimming.sql_float_digits),
                    "max": round(max(values), self.slimming.sql_float_digits),
                    "min": round(min(values), self.slimming.sql_float_digits),
                }
        return stats

    def _round(self, value: Any) -> Any:
        if isinstance(value, bool):
            return value
        if isinstance(value, float):
            return round(value, self.slimming.sql_float_digits)
        return value

    # ------------------------------------------------------------------
    # 4) 记账辅助
    # ------------------------------------------------------------------
    @staticmethod
    def ledger_entry(
        *,
        input_tokens: int,
        output_tokens: int,
        build: ContextBuildResult | None = None,
    ) -> dict[str, Any]:
        """生成可直接并入 ``state.token_ledger`` 的记账条目。"""
        entry: dict[str, Any] = {
            "input": int(input_tokens),
            "output": int(output_tokens),
            "calls": 1,
        }
        if build is not None:
            entry.update(
                {
                    "compress_before": build.before_tokens,
                    "compress_after": build.after_tokens,
                    "compressed": build.compressed,
                }
            )
        return entry


__all__ = ["ContextService"]
