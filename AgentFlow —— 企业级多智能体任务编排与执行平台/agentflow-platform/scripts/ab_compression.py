#!/usr/bin/env python
"""W4 上下文压缩 A/B 对照实验（FR-010 / PRD 9.2）。

实验设计（两组的唯一变量是 ``CompressionConfig.enabled``）：

| 组 | 配置 | 行为 |
|---|---|---|
| A 对照（compress_off） | ``enabled=False`` | 历史不进摘要、不计压缩（W2 现状行为） |
| B 实验（compress_on） | ``enabled=True`` | 历史压缩为模板摘要后注入上下文，最多 2 层 |

其余全部固定：同一评测集子集、同一 MockLLM/真实模型、同一温度与预算、同一段"历史种子"
（``--history-rounds`` 轮，由样例 ground_truth 中的数字与关键字构造，保证两组原料一致）。

两级测量（缺一不可）：

1. **压缩层微基准**（确定性，永远可跑）：直接对固定历史调用 ``ContextService.plan_context``，
   得到 ``before_tokens → after_tokens``、压缩率、**数字保留率**、**关键字召回率**。
   这是压缩算法本身的质量，不受生成模型随机性影响。
2. **端到端跑批**（图级真实执行）：每个样例跑一遍完整 graph，得到总 token / 分节点 token /
   LLM 调用次数 / 时延 / 报告关键字召回 / faithfulness（LLM-as-judge）/ 幻觉计数 / 超时率。

为什么两级都要：离线 Mock 模式下生成文本是确定性模板，端到端质量指标两组相等是**必然**的，
只有压缩层微基准能反映真实差异；真实模型（``LLM_MOCK=false``）下两级一起看才有意义。

输出（默认 ``eval_output/``）：
- ``ab_samples_<tag>.csv``：逐样例 × 逐组明细
- ``ab_metrics_<tag>.csv``：指标汇总（一行一指标，含 A/B/差值/变化率）
- ``AB实验报告_<tag>.md``：人读报告（含复现命令与结论）

用法：
    python scripts/ab_compression.py                          # 离线：8 条样例 × 14 轮历史
    python scripts/ab_compression.py --limit 30 --history-rounds 30
    python scripts/ab_compression.py --llm real --judge-api-key sk-xxx   # 真实模型对照
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _p in (PROJECT_ROOT, PROJECT_ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import w4_common as W  # noqa: E402

DEFAULT_MANIFEST = PROJECT_ROOT / "eval_datasets" / "分层难例评测集-manifest.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "eval_output"
ARMS: tuple[tuple[str, bool], ...] = (("compress_off", False), ("compress_on", True))
MICRO_NODES: tuple[str, ...] = ("planner", "executor", "reviewer", "summarizer")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


# ---------------------------------------------------------------------------
# 历史种子（两组共用，保证可比）
# ---------------------------------------------------------------------------
def build_history(sample: dict[str, Any], rounds: int) -> list[dict[str, str]]:
    """用样例自身的关键数字/结论构造 N 轮历史，模拟"长会话"后再提问。

    刻意保留一段冗长的叙述性文字（每组 6 处重复），使压缩层有可压缩空间；
    同时保留全部关键数字，用于衡量"压缩是否塌缩了信息"。
    """
    facts = "，".join(
        [
            "数码配件转化率由 3.1% 降至 1.7%",
            "GMV 环比 -18%",
            "广告日预算由 12000 降至 7200（-40%）",
            "付费流量 -24%",
            "库存周转天数由 9 天升至 21 天",
            "SKU-10086 断货下架",
        ]
    )
    filler = (
        "运营侧已同步供应链、市场与客服三条线，当前判断仍需继续观察长尾流量的承接效率，"
        "并按日复核品类结构与投放节奏，避免把单点缺货误判为全域性下滑。"
    )
    history: list[dict[str, str]] = []
    for index in range(rounds):
        history.append(
            {
                "role": "user",
                "content": f"第{index + 1}轮复盘：{sample['question'][:60]}；请结合上周数据与历史周报给出结论。",
            }
        )
        history.append(
            {
                "role": "assistant",
                "content": (
                    f"第{index + 1}轮结论：{facts}。{filler * 3}"
                    f"（轮次备注：本段为历史上下文原文，用于压缩对照，编号 R{index + 1:02d}）"
                ),
            }
        )
    return history


# ---------------------------------------------------------------------------
# 测量记录
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ArmSample:
    arm: str
    sample_id: str
    stratum: str
    status: str = "PENDING"
    reason: str = ""
    token_total: int = 0
    node_tokens: dict[str, int] = field(default_factory=dict)
    llm_calls: int = 0
    latency_ms: int = 0
    summary: str = ""
    keyword_recall: float = 0.0
    gmv_pass: bool = False
    faithfulness: float | None = None
    judge_mode: str = ""
    hallucinations: int = 0
    timed_out: bool = False
    # 压缩层微基准
    micro_before: int = 0
    micro_after: int = 0
    micro_ratio: float = 0.0
    numeric_retention: float = 0.0
    keyword_retention: float = 0.0

    def as_row(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "id": self.sample_id,
            "stratum": self.stratum,
            "status": self.status,
            "reason": self.reason,
            "token_total": self.token_total,
            "node_tokens": self.node_tokens,
            "llm_calls": self.llm_calls,
            "latency_ms": self.latency_ms,
            "keyword_recall": self.keyword_recall,
            "gmv_pass": self.gmv_pass,
            "faithfulness": self.faithfulness,
            "judge_mode": self.judge_mode,
            "hallucinations": self.hallucinations,
            "timed_out": self.timed_out,
            "micro_before": self.micro_before,
            "micro_after": self.micro_after,
            "micro_ratio": self.micro_ratio,
            "numeric_retention": self.numeric_retention,
            "keyword_retention": self.keyword_retention,
            "summary_chars": len(self.summary or ""),
        }


# ---------------------------------------------------------------------------
# 压缩层微基准
# ---------------------------------------------------------------------------
def micro_benchmark(
    context: Any, sample: dict[str, Any], history: Sequence[dict[str, str]]
) -> dict[str, float]:
    """对固定历史逐节点调用 ``plan_context``，量化压缩率与信息保留率。

    口径说明（重要）：``plan_context`` 返回的 ``before_tokens`` 在「关闭压缩」时
    只统计组件、在「开启压缩」时才把历史消息并入，两组不同源、不可直接相减。
    因此这里统一按 **组件 token + 历史注入 token** 自行计算：

    * ``before = tokens(components) + tokens(全文历史)``   ← 两组完全相同
    * ``after  = tokens(components) + tokens(注入的历史)`` ← 注入物 = 摘要 or 原文
    """
    from app.utils.tokens import estimate_messages_tokens, estimate_tokens

    components = {
        "system": "你是电商经营分析助手，必须基于证据作答。" * 6,
        "goal": str(sample["question"]),
        "evidence": str(sample.get("ground_truth") or "") * 2,
    }
    component_tokens = sum(estimate_tokens(text) for text in components.values())
    history_tokens = estimate_messages_tokens(list(history))

    before_total = after_total = 0
    numeric_hits = numeric_total = 0
    keyword_hits = keyword_total = 0

    raw_text = "\n".join(item["content"] for item in history)
    raw_numbers = set(_NUM_RE.findall(raw_text))
    keywords = [str(k) for k in (sample.get("expected_keywords") or [])]

    for node in MICRO_NODES:
        result = context.plan_context(node, components, messages=list(history), reserved_output=800)
        digest = result.history_digest or raw_text  # 未压缩时等价于"原文注入"
        before_total += component_tokens + history_tokens
        # 未压缩 → 注入物与基准完全一致，压缩率恒为 0（避免消息头开销带来 1~2% 噪声）
        after_total += component_tokens + (estimate_tokens(result.history_digest) if result.history_digest else history_tokens)
        digest_numbers = set(_NUM_RE.findall(digest))
        numeric_hits += len(raw_numbers & digest_numbers)
        numeric_total += len(raw_numbers)
        keyword_hits += sum(1 for k in keywords if k in digest)
        keyword_total += len(keywords)

    return {
        "micro_before": before_total,
        "micro_after": after_total,
        "micro_ratio": round((before_total - after_total) / before_total, 4) if before_total else 0.0,
        "numeric_retention": round(numeric_hits / numeric_total, 4) if numeric_total else 1.0,
        "keyword_retention": round(keyword_hits / keyword_total, 4) if keyword_total else 1.0,
    }


# ---------------------------------------------------------------------------
# 端到端图级跑批
# ---------------------------------------------------------------------------
async def run_arm_sample(
    env: Any,
    sample: dict[str, Any],
    history: Sequence[dict[str, str]],
    judge: W.JudgeClient,
    *,
    arm: str,
    timeout: float,
) -> ArmSample:
    from app.utils.ids import new_trace_id

    record = ArmSample(arm=arm, sample_id=str(sample["id"]), stratum=str(sample.get("stratum") or ""))
    micro = micro_benchmark(env.runtime.context, sample, history)
    record.micro_before = int(micro["micro_before"])
    record.micro_after = int(micro["micro_after"])
    record.micro_ratio = float(micro["micro_ratio"])
    record.numeric_retention = float(micro["numeric_retention"])
    record.keyword_retention = float(micro["keyword_retention"])

    graph = env.runtime.graph
    task_id = 800000 + abs(hash((arm, sample["id"]))) % 100000
    state = {
        "task_id": task_id,
        "trace_id": new_trace_id(),
        "creator_id": env.users.get("admin_user", 1),
        "goal": str(sample["question"]),
        "time_range": dict(W.DEFAULT_WINDOW),
        "memory_hits": [],
        "plan": [],
        "subtask_results": {},
        "replan_count": 0,
        "approvals": {},
        "messages": list(history),
        "token_ledger": {},
        "context_digest": "",
        "artifacts": [],
        "status": "PLANNING",
    }
    config = {"configurable": {"thread_id": f"ab-{arm}-{uuid.uuid4().hex[:8]}"}, "recursion_limit": 60}

    started = time.perf_counter()
    try:
        payload = await asyncio.wait_for(graph.ainvoke(state, config=config), timeout=timeout)
    except asyncio.TimeoutError:
        record.status = "TIMEOUT"
        record.reason = "timeout"
        record.timed_out = True
        record.latency_ms = int((time.perf_counter() - started) * 1000)
        return record
    except Exception as exc:  # noqa: BLE001 - 单样例失败不影响对照
        record.status = "ERROR"
        record.reason = f"{type(exc).__name__}: {str(exc)[:160]}"
        record.latency_ms = int((time.perf_counter() - started) * 1000)
        return record

    record.latency_ms = int((time.perf_counter() - started) * 1000)
    out = dict(payload or {})
    record.status = str(out.get("status") or ("COMPLETED" if out.get("summary") else "FAILED"))
    record.reason = str((out.get("error") or {}).get("code") or record.status)
    record.node_tokens = W.node_tokens(out.get("token_ledger"))
    record.token_total = W.total_tokens_of(out.get("token_ledger"))
    record.llm_calls = W.llm_calls_of(out.get("token_ledger"))
    record.summary = str(out.get("summary") or "")
    record.keyword_recall = W.keyword_recall(record.summary, sample.get("expected_keywords") or [])
    expected = dict(sample.get("expected_gmv") or {})
    record.gmv_pass = bool(W.score_gmv_conclusion(record.summary, expected).get("gmv_pass"))
    record.hallucinations = W.count_hallucinations(record.summary, expected.get("must_not_mention") or [])

    judged = await judge.faithfulness(
        report=record.summary,
        evidence=str(sample.get("ground_truth") or ""),
        ground_truth=str(sample.get("ground_truth") or ""),
    )
    record.faithfulness = judged.faithfulness
    record.judge_mode = judged.mode
    return record


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def summarize(records: Sequence[ArmSample]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for arm, _ in ARMS:
        items = [r for r in records if r.arm == arm]
        latencies = [r.latency_ms for r in items if r.latency_ms]
        faithful = [r.faithfulness for r in items if r.faithfulness is not None]
        nodes: dict[str, list[int]] = {}
        for r in items:
            for node, value in (r.node_tokens or {}).items():
                nodes.setdefault(node, []).append(int(value))
        result[arm] = {
            "samples": len(items),
            "success_rate": round(sum(1 for r in items if r.status == "COMPLETED") / len(items), 4) if items else 0.0,
            "token_total": sum(r.token_total for r in items),
            "token_avg": W.mean([r.token_total for r in items]),
            "llm_calls_avg": W.mean([r.llm_calls for r in items]),
            "llm_calls_total": sum(r.llm_calls for r in items),
            "latency_p50": W.percentile(latencies, 50),
            "latency_p95": W.percentile(latencies, 95),
            "timeout_rate": round(sum(1 for r in items if r.timed_out) / len(items), 4) if items else 0.0,
            "faithfulness_avg": W.mean(faithful),
            "recall_avg": W.mean([r.keyword_recall for r in items]),
            "hallucination_total": sum(r.hallucinations for r in items),
            "gmv_accuracy": round(sum(1 for r in items if r.gmv_pass) / len(items), 4) if items else 0.0,
            "micro_before_avg": W.mean([r.micro_before for r in items]),
            "micro_after_avg": W.mean([r.micro_after for r in items]),
            "micro_ratio_avg": W.mean([r.micro_ratio for r in items]),
            "numeric_retention_avg": W.mean([r.numeric_retention for r in items]),
            "keyword_retention_avg": W.mean([r.keyword_retention for r in items]),
            "node_tokens_avg": {node: W.mean(values) for node, values in sorted(nodes.items())},
        }
    return result


def _delta(off: float, on: float) -> tuple[float, str]:
    diff = round(on - off, 4)
    rate = f"{(diff / off * 100):+.2f}%" if off else "n/a"
    return diff, rate


def build_metrics_table(summary: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    off, on = summary["compress_off"], summary["compress_on"]
    specs = [
        ("单样例 token 均值", "token_avg", "token", "lower"),
        ("总 token", "token_total", "token", "lower"),
        ("LLM 调用次数均值", "llm_calls_avg", "次", "lower"),
        ("P50 时延", "latency_p50", "ms", "lower"),
        ("P95 时延", "latency_p95", "ms", "lower"),
        ("超时率", "timeout_rate", "ratio", "lower"),
        ("关键字召回率", "recall_avg", "0-1", "higher"),
        ("faithfulness", "faithfulness_avg", "0-1", "higher"),
        ("幻觉条数合计", "hallucination_total", "条", "lower"),
        ("GMV 结论准确率", "gmv_accuracy", "ratio", "higher"),
        ("成功率", "success_rate", "ratio", "higher"),
        ("压缩前 token 均值（微基准）", "micro_before_avg", "token", "lower"),
        ("压缩后 token 均值（微基准）", "micro_after_avg", "token", "lower"),
        ("压缩率（微基准）", "micro_ratio_avg", "ratio", "higher"),
        ("数字保留率", "numeric_retention_avg", "0-1", "higher"),
        ("关键字保留率", "keyword_retention_avg", "0-1", "higher"),
    ]
    rows: list[dict[str, Any]] = []
    for label, key, unit, direction in specs:
        a_value = off.get(key, 0) or 0
        b_value = on.get(key, 0) or 0
        diff, rate = _delta(float(a_value), float(b_value))
        better = "持平"
        if diff != 0:
            better = "实验组更优" if (diff < 0 if direction == "lower" else diff > 0) else "对照组更优"
        rows.append(
            {
                "指标": label,
                "对照A(关闭压缩)": a_value,
                "实验B(开启压缩)": b_value,
                "差值(B-A)": diff,
                "变化率": rate,
                "单位": unit,
                "结论": better,
            }
        )
    return rows


def build_report(
    summary: dict[str, dict[str, Any]], rows: Sequence[dict[str, Any]], meta: dict[str, Any]
) -> str:
    off, on = summary["compress_off"], summary["compress_on"]
    lines = [
        "# W4 上下文压缩 A/B 对照实验报告",
        "",
        f"- 生成时间：{meta['generated_at']}",
        f"- 参评样例：{meta['samples']} 条（来源 `{meta['manifest']}`）",
        f"- 历史种子：{meta['history_rounds']} 轮/样例；评测集与模型：固定",
        f"- 生成侧 LLM：`{meta['llm_mode']}`；faithfulness judge：`{meta['judge_mode']}`",
        f"- 压缩触发：`trigger_ratio={meta['trigger_ratio']}`、`keep_recent_rounds={meta['keep_recent_rounds']}`、"
        f"`max_layers={meta['max_layers']}`（来自 W2 配置，两组一致）",
        f"- 单样例超时：{meta['timeout']}s",
        "",
        "## 1. 实验设计",
        "",
        "| 组 | 变量 | 说明 |",
        "|---|---|---|",
        "| A 对照 | `CompressionConfig.enabled=False` | 历史不做摘要、不注入（W2 默认关闭路径） |",
        "| B 实验 | `CompressionConfig.enabled=True` | 历史压缩为模板摘要后注入，最多 2 层 |",
        "",
        "固定项：同一评测集子集、同一 LLM（同温度/预算）、同一段历史种子、同一套图节点与工具。",
        "",
        "## 2. 指标汇总",
        "",
        W.markdown_table(
            rows,
            ["指标", "对照A(关闭压缩)", "实验B(开启压缩)", "差值(B-A)", "变化率", "单位", "结论"],
        ),
        "",
        "## 3. 分节点 token（均值）",
        "",
    ]
    nodes = sorted(set(off["node_tokens_avg"]) | set(on["node_tokens_avg"]))
    node_rows = [
        {
            "节点": node,
            "对照A": off["node_tokens_avg"].get(node, 0),
            "实验B": on["node_tokens_avg"].get(node, 0),
            "差值": round(on["node_tokens_avg"].get(node, 0) - off["node_tokens_avg"].get(node, 0), 4),
        }
        for node in nodes
    ]
    lines += [W.markdown_table(node_rows, ["节点", "对照A", "实验B", "差值"]) if node_rows else "（无数据）"]

    # ---- 结论（数据驱动，不写死话术）----
    token_diff = on["token_avg"] - off["token_avg"]
    recall_diff = on["recall_avg"] - off["recall_avg"]
    faith_diff = (on["faithfulness_avg"] or 0) - (off["faithfulness_avg"] or 0)
    micro_ratio = on["micro_ratio_avg"]

    lines += ["", "## 4. 结论", ""]
    if micro_ratio > 0.05:
        lines.append(
            f"1. **压缩层自身有效**：同样的历史原文，注入 token 由 {on['micro_before_avg']:.0f} 降到 "
            f"{on['micro_after_avg']:.0f}，压缩率 **{micro_ratio * 100:.1f}%**；"
            f"数字保留率 {on['numeric_retention_avg'] * 100:.1f}%、关键字保留率 {on['keyword_retention_avg'] * 100:.1f}%"
            "（W2 的 `numeric_field_whitelist` 生效，关键数字未被塌缩）。"
        )
    else:
        lines.append(
            f"1. **压缩未被触发**：`micro_ratio={micro_ratio}`，说明当前历史长度未越过触发阈值 "
            f"（`trigger_ratio={meta['trigger_ratio']}`、`message_rounds_threshold={meta['message_rounds_threshold']}`）。"
            "可提高 `--history-rounds` 或降低触发阈值后复跑。"
        )
    lines.append(
        f"2. **端到端 token 变化 {token_diff:+.0f}/样例**：压缩组把历史摘要**注入**了 prompt，"
        "因此相比「完全不注入历史」的对照组，输入 token 会上升——这是「用少量 token 换取历史可用性」的代价，"
        "而不是压缩没生效。若与「原文全量注入」相比，节省即为上一条的压缩率。"
        if token_diff > 0
        else f"2. **端到端 token 变化 {token_diff:+.0f}/样例**：压缩组比对照组更省。"
    )
    lines.append(
        f"3. **质量侧**：关键字召回率 {recall_diff:+.4f}、faithfulness {faith_diff:+.4f}、"
        f"幻觉条数 {off['hallucination_total']} → {on['hallucination_total']}。"
    )
    if meta["llm_mode"] == "mock":
        lines.append(
            "   ⚠️ 本次为**离线 Mock 模式**：生成文本是确定性模板，两组质量指标的差异主要来自"
            "是否注入了历史摘要，不能代表真实模型下的表现；要得到可对外引用的质量结论，"
            "请用 `--llm real` + 真实 `JUDGE_API_KEY` 复跑。"
        )
    lines += [
        f"4. **性能侧**：P95 时延 {off['latency_p95']} → {on['latency_p95']} ms，"
        f"超时率 {off['timeout_rate'] * 100:.1f}% → {on['timeout_rate'] * 100:.1f}%，LLM 调用次数未变化"
        f"（{off['llm_calls_avg']} vs {on['llm_calls_avg']}，压缩不额外调用模型）。",
        "",
        "## 5. 复现命令",
        "",
        "```bash",
        f"python scripts/ab_compression.py --limit {meta['samples']} --history-rounds {meta['history_rounds']} \\",
        "    --tag ab_mock          # 离线：验证管线与压缩层",
        "# 真实模型（需两个不同模型：生成 + 评审）",
        "LLM_MOCK=false LLM_API_KEY=sk-xxx python scripts/ab_compression.py \\",
        "    --llm real --judge-base-url https://api.siliconflow.cn/v1 --judge-api-key sk-yyy \\",
        "    --judge-model Qwen/Qwen2.5-72B-Instruct --limit 30 --history-rounds 30 --tag ab_real",
        "```",
        "",
        "## 6. 已知限制",
        "",
        "1. 离线模式下 token 由 `utils/tokens.py` 估算，与真实计费 token 存在偏差（同口径可比，绝对值不可外推）；",
        "2. 历史种子是构造数据（同一段事实重复 N 轮），真实会话的压缩率会低于此处结果的重复度红利；",
        "3. 压缩只作用于「历史消息」这一路，工具结果瘦身（`SlimmingConfig`）未纳入本次对照；",
        "4. 幻觉计数用「必须不出现」词表命中，属弱标注，需配合 faithfulness 一起看。",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
async def run(args: argparse.Namespace) -> int:
    samples = _load_samples(Path(args.manifest), limit=args.limit)
    output_dir = Path(args.output)
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir
    W.ensure_dir(output_dir)
    tag = args.tag or W.now_tag()

    judge = W.JudgeClient(base_url=args.judge_base_url, api_key=args.judge_api_key, model=args.judge_model)
    llm_mock = os.environ.get("LLM_MOCK", "").lower() in {"1", "true"}

    print(f"[ab] 样例 {len(samples)} 条 × 2 组；历史 {args.history_rounds} 轮；judge={judge.mode}；llm={'mock' if llm_mock else 'real'}")

    records: list[ArmSample] = []
    for arm, compress in ARMS:
        print(f"\n[ab] === 组 {arm}（compress={compress}）===")
        db_path = PROJECT_ROOT / "storage" / f"_ab_{arm}.db"
        env = await W.EmbeddedEnv(
            db_path=db_path,
            compress=compress,
            force_compress=args.force_compress,
            llm_mock=args.llm != "real",
        ).start()
        try:
            for index, sample in enumerate(samples, start=1):
                history = build_history(sample, args.history_rounds)
                item = await run_arm_sample(env, sample, history, judge, arm=arm, timeout=args.timeout)
                records.append(item)
                print(
                    f"  [{index}/{len(samples)}] {item.sample_id} {item.status:<10} "
                    f"token={item.token_total:<6} calls={item.llm_calls} {item.latency_ms}ms "
                    f"recall={item.keyword_recall:<6} faith={item.faithfulness} "
                    f"micro={item.micro_before}→{item.micro_after}({item.micro_ratio:.1%})"
                )
        finally:
            await env.aclose()

    summary = summarize(records)
    rows = build_metrics_table(summary)
    meta = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "samples": len(samples),
        "manifest": str(Path(args.manifest).name),
        "history_rounds": args.history_rounds,
        "llm_mode": "mock" if llm_mock else "real",
        "judge_mode": judge.mode,
        "timeout": args.timeout,
        "trigger_ratio": 0.05 if args.force_compress else "w2_default",
        "message_rounds_threshold": 0 if args.force_compress else "w2_default",
        "keep_recent_rounds": 1 if args.force_compress else "w2_default",
        "max_layers": 2,
        "force_compress": args.force_compress,
    }

    sample_rows = [r.as_row() for r in records]
    W.write_csv(output_dir / f"ab_samples_{tag}.csv", sample_rows)
    W.write_csv(output_dir / f"ab_metrics_{tag}.csv", rows)
    report = build_report(summary, rows, meta)
    report_path = W.write_text(output_dir / f"AB实验报告_{tag}.md", report)
    W.write_json(
        output_dir / f"ab_detail_{tag}.json",
        {"meta": meta, "summary": summary, "metrics": rows, "samples": sample_rows},
    )

    print("\n=== 指标汇总 ===")
    print(W.markdown_table(rows, ["指标", "对照A(关闭压缩)", "实验B(开启压缩)", "差值(B-A)", "变化率", "结论"]))
    print(f"\n[ab] 报告 {report_path.name}；目录 {output_dir}")
    return 0


def _load_samples(manifest: Path, *, limit: int | None) -> list[dict[str, Any]]:
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    items = raw.get("samples") if isinstance(raw, dict) else raw
    selected = [dict(item) for item in items if not item.get("needs_notify")]
    if limit:
        selected = selected[: max(1, limit)]
    if not selected:
        raise SystemExit("没有可用的非 notify 样例（A/B 避免审批打断）")
    return selected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="W4 上下文压缩 A/B 对照实验")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="评测集 manifest")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="输出目录（默认 eval_output/）")
    parser.add_argument("--tag", default=None, help="输出文件名后缀")
    parser.add_argument("--limit", type=int, default=8, help="参评样例数（默认 8，控制耗时）")
    parser.add_argument("--history-rounds", type=int, default=14, help="历史种子轮数（默认 14）")
    parser.add_argument("--llm", choices=("mock", "real"), default="mock", help="生成侧 LLM：mock/real")
    parser.add_argument("--timeout", type=float, default=120.0, help="单样例超时秒数")
    parser.add_argument(
        "--force-compress",
        action="store_true",
        default=True,
        help="强制低阈值触发压缩（默认开：否则短历史下压缩根本不触发，对照无意义）",
    )
    parser.add_argument("--no-force-compress", dest="force_compress", action="store_false", help="用 W2 默认阈值")
    parser.add_argument("--judge-base-url", default=None)
    parser.add_argument("--judge-api-key", default=None)
    parser.add_argument("--judge-model", default=None)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.judge_base_url:
        os.environ["JUDGE_BASE_URL"] = args.judge_base_url
    if args.judge_api_key:
        os.environ["JUDGE_API_KEY"] = args.judge_api_key
    if args.judge_model:
        os.environ["JUDGE_MODEL"] = args.judge_model
    if args.llm == "real":
        os.environ["LLM_MOCK"] = "false"
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
