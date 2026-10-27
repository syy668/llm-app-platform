#!/usr/bin/env python
"""W4 评测跑批（FR-013 / FR-016）：30 条分层难例 → 提交 → 轮询 → 指标 → csv/json。

指标口径（与 W4 任务书逐条对应）：

| 指标 | 计算方式 |
|---|---|
| 任务成功率 | ``status == COMPLETED`` 的比例（分母 = 全部样例，超时与失败都算不成功） |
| 重规划次数 | ``detail.replan_count`` 的均值 / 总和 / 发生重规划的样例数 |
| GMV 结论准确率 | 趋势 + 幅度区间 + 必提词 + 禁提词 四项全过才计 1（见 ``w4_common.score_gmv_conclusion``） |
| 分节点 token 消耗 | ``detail.token_ledger`` 逐节点 input+output，聚合成均值表 |
| 总 token | 全部样例 token 之和，以及单样例均值 |
| P95 时延 | 端到端（提交 → 终态）耗时的 nearest-rank P95 |
| faithfulness | LLM-as-judge（评测模型与生成模型分离），无 key 时退化为本地 2-gram 支撑率并显式标注 |
| 上下文召回率 | ``summary`` 对 ``expected_keywords`` 的覆盖率（关键字召回） |

超时重试：
- 单样例最多 ``--retries`` 次重试（默认 2 → 最多 3 次尝试），每次重试**新建任务**（旧任务可能卡在
  断点，复用会污染时延口径）；
- 三次都超时 → 该样例记 ``timeout``，批量继续，不影响其余样例。

输出（``--output`` 默认 ``eval_output/``）：
- ``eval_detail_<tag>.json``：逐样例明细 + 元信息 + 汇总（机器可读）
- ``eval_summary_<tag>.csv``：逐样例扁平表（Excel 可直接打开）
- ``eval_metrics_<tag>.csv``：指标汇总表（一行一指标）
- ``eval_report_<tag>.md``：人读报告

用法：
    python scripts/eval_runner.py                          # 嵌入式离线跑全部 30 条
    python scripts/eval_runner.py --limit 3                # 冒烟：只跑前 3 条
    python scripts/eval_runner.py --base-url http://127.0.0.1:8000 --judge-api-key sk-xxx
    python scripts/eval_runner.py --ids AF-EVAL-001,AF-EVAL-015 --concurrency 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _p in (PROJECT_ROOT, PROJECT_ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import w4_common as W  # noqa: E402  - 必须在 app.* 之前导入（铺环境变量）

DEFAULT_MANIFEST = PROJECT_ROOT / "eval_datasets" / "分层难例评测集-manifest.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "eval_output"


# ---------------------------------------------------------------------------
# 数据集加载
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class EvalSample:
    idx: int
    sample_id: str
    stratum: str
    case_type: str
    risk_level: str
    question: str
    ground_truth: str
    expected_gmv: dict[str, Any] = field(default_factory=dict)
    expected_keywords: list[str] = field(default_factory=list)
    needs_notify: bool = False
    window: dict[str, str] = field(default_factory=lambda: dict(W.DEFAULT_WINDOW))

    @property
    def risky(self) -> bool:
        return self.risk_level.lower() == "high" or self.needs_notify


def load_samples(path: Path, *, limit: int | None = None, ids: Sequence[str] | None = None) -> list[EvalSample]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get("samples") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise SystemExit(f"评测集格式不对：{path} 既不是数组也不是 {{'samples': [...]}}")
    window = raw.get("window") if isinstance(raw, dict) else None

    samples: list[EvalSample] = []
    wanted = {x.strip() for x in ids} if ids else None
    for position, item in enumerate(items, start=1):
        sample_id = str(item.get("id") or f"AF-EVAL-{position:03d}")
        if wanted and sample_id not in wanted:
            continue
        samples.append(
            EvalSample(
                idx=int(item.get("idx") or position),
                sample_id=sample_id,
                stratum=str(item.get("stratum") or ""),
                case_type=str(item.get("case_type") or ""),
                risk_level=str(item.get("risk_level") or "normal"),
                question=str(item.get("question") or "").strip(),
                ground_truth=str(item.get("ground_truth") or "").strip(),
                expected_gmv=dict(item.get("expected_gmv") or {}),
                expected_keywords=[str(k) for k in (item.get("expected_keywords") or [])],
                needs_notify=bool(item.get("needs_notify")),
                window=dict(window or W.DEFAULT_WINDOW),
            )
        )
    if limit:
        samples = samples[: max(1, limit)]
    if not samples:
        raise SystemExit("没有可跑的样例（检查 --ids / --limit）")
    return samples


# ---------------------------------------------------------------------------
# 单样例执行
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class SampleResult:
    sample: EvalSample
    attempts: int = 0
    task_ids: list[int] = field(default_factory=list)
    status: str = "PENDING"
    terminal_reason: str = "PENDING"
    timed_out: bool = False
    error_code: str | None = None
    error_message: str | None = None
    replan_count: int = 0
    token_total: int = 0
    node_tokens: dict[str, int] = field(default_factory=dict)
    llm_calls: int = 0
    latency_ms: int = 0
    summary: str = ""
    keyword_recall: float = 0.0
    recall_hit: list[str] = field(default_factory=list)
    recall_missing: list[str] = field(default_factory=list)
    gmv: dict[str, Any] = field(default_factory=dict)
    faithfulness: float | None = None
    judge_mode: str = ""
    judge_model: str = ""
    judge_unsupported: list[str] = field(default_factory=list)
    subtasks_total: int = 0
    subtasks_done: int = 0
    artifacts: int = 0

    @property
    def ok(self) -> bool:
        return self.status == W.STATUS_COMPLETED

    def as_row(self) -> dict[str, Any]:
        gmv = self.gmv or {}
        return {
            "idx": self.sample.idx,
            "id": self.sample.sample_id,
            "stratum": self.sample.stratum,
            "case_type": self.sample.case_type,
            "risk_level": self.sample.risk_level,
            "needs_notify": self.sample.needs_notify,
            "question": self.sample.question,
            "task_ids": ",".join(str(x) for x in self.task_ids),
            "attempts": self.attempts,
            "status": self.status,
            "terminal_reason": self.terminal_reason,
            "ok": self.ok,
            "timed_out": self.timed_out,
            "error_code": self.error_code or "",
            "replan_count": self.replan_count,
            "token_total": self.token_total,
            "node_tokens": self.node_tokens,
            "llm_calls": self.llm_calls,
            "latency_ms": self.latency_ms,
            "keyword_recall": self.keyword_recall,
            "recall_missing": self.recall_missing,
            "gmv_pass": bool(gmv.get("gmv_pass")),
            "gmv_trend": gmv.get("trend_detected", ""),
            "gmv_pct_matched": gmv.get("pct_matched"),
            "gmv_must_missing": gmv.get("must_missing", []),
            "gmv_forbidden_hit": gmv.get("forbidden_hit", []),
            "faithfulness": self.faithfulness,
            "judge_mode": self.judge_mode,
            "subtasks_total": self.subtasks_total,
            "subtasks_done": self.subtasks_done,
            "artifacts": self.artifacts,
            "summary_chars": len(self.summary or ""),
        }


def _evidence_of(detail: dict[str, Any], sample: EvalSample) -> str:
    """judge 用的「证据」：参考答案 + 各子任务结果摘要（工具产物的落地形态）。"""
    parts = [f"[参考答案]\n{sample.ground_truth}"]
    for item in detail.get("subtasks") or []:
        digest = item.get("result_summary") or ""
        if digest:
            parts.append(f"[子任务 {item.get('seq')} {item.get('name')}] {digest}")
    for artifact in detail.get("artifacts") or []:
        title = artifact.get("title") or artifact.get("type") or ""
        if title:
            parts.append(f"[产物] {title}")
    return "\n".join(parts)[:6000]


async def run_sample(
    api: W.TaskApi,
    sample: EvalSample,
    judge: W.JudgeClient,
    *,
    timeout: float,
    retries: int,
    reject: bool,
    interval: float,
) -> SampleResult:
    result = SampleResult(sample=sample)
    terminal: W.TerminalResult | None = None

    for attempt in range(retries + 1):
        result.attempts = attempt + 1
        started = time.perf_counter()
        try:
            submitted = await api.submit(sample.question, sample.window)
            task_id = int(submitted["task_id"])
            result.task_ids.append(task_id)
            terminal = await W.wait_terminal(
                api, task_id, timeout=timeout, interval=interval, auto_approve=True, reject=reject
            )
        except Exception as exc:  # noqa: BLE001 - 提交/轮询异常也走重试
            result.status = W.STATUS_FAILED
            result.terminal_reason = f"CLIENT_ERROR:{type(exc).__name__}"
            result.error_message = str(exc)[:300]
            if attempt < retries:
                await asyncio.sleep(0.5)
                continue
            result.latency_ms = int((time.perf_counter() - started) * 1000)
            return result

        if not terminal.timed_out:
            break
        result.timed_out = True
        if attempt < retries:
            await asyncio.sleep(0.5)
            continue

    assert terminal is not None
    detail = terminal.detail or {}
    result.status = terminal.status
    result.terminal_reason = "timeout" if terminal.timed_out else terminal.terminal_reason
    result.latency_ms = terminal.waited_ms
    result.replan_count = int(detail.get("replan_count") or 0)
    result.node_tokens = W.node_tokens(detail.get("token_ledger"))
    result.token_total = W.total_tokens_of(detail.get("token_ledger"), int(detail.get("token_usage") or 0))
    result.llm_calls = W.llm_calls_of(detail.get("token_ledger"))
    result.summary = str(detail.get("summary") or "")
    subtasks = detail.get("subtasks") or []
    result.subtasks_total = len(subtasks)
    result.subtasks_done = sum(1 for s in subtasks if str(s.get("status")) == "DONE")
    result.artifacts = len(detail.get("artifacts") or [])
    error = detail.get("error") or {}
    result.error_code = error.get("code")
    result.error_message = (error.get("message") or "")[:300] or None

    # 召回：期望关键字在 summary 中的覆盖率
    result.keyword_recall = W.keyword_recall(result.summary, sample.expected_keywords)
    result.recall_hit = [k for k in sample.expected_keywords if k in result.summary]
    result.recall_missing = [k for k in sample.expected_keywords if k not in result.summary]

    # GMV 结论判定
    result.gmv = W.score_gmv_conclusion(result.summary, sample.expected_gmv)

    # faithfulness（LLM-as-judge；失败自动降级，不影响批量）
    judged = await judge.faithfulness(
        report=result.summary, evidence=_evidence_of(detail, sample), ground_truth=sample.ground_truth
    )
    result.faithfulness = judged.faithfulness
    result.judge_mode = judged.mode
    result.judge_model = judged.model
    result.judge_unsupported = judged.unsupported
    return result


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def aggregate(results: Sequence[SampleResult], meta: dict[str, Any]) -> dict[str, Any]:
    total = len(results)
    ok = [r for r in results if r.ok]
    timeouts = [r for r in results if r.timed_out]
    latencies = [r.latency_ms for r in results if r.latency_ms]
    tokens = [r.token_total for r in results]
    faithful = [r.faithfulness for r in results if r.faithfulness is not None]
    recalls = [r.keyword_recall for r in results]
    gmv_pass = [r for r in results if (r.gmv or {}).get("gmv_pass")]

    nodes: dict[str, list[int]] = {}
    for r in results:
        for node, value in (r.node_tokens or {}).items():
            nodes.setdefault(node, []).append(int(value))

    by_stratum: dict[str, dict[str, Any]] = {}
    for r in results:
        bucket = by_stratum.setdefault(
            r.sample.stratum or "未分层", {"count": 0, "success": 0, "gmv_pass": 0, "latency": [], "tokens": []}
        )
        bucket["count"] += 1
        bucket["success"] += 1 if r.ok else 0
        bucket["gmv_pass"] += 1 if (r.gmv or {}).get("gmv_pass") else 0
        bucket["latency"].append(r.latency_ms)
        bucket["tokens"].append(r.token_total)

    return {
        "meta": meta,
        "samples": total,
        "success_count": len(ok),
        "success_rate": round(len(ok) / total, 4) if total else 0.0,
        "timeout_count": len(timeouts),
        "timeout_rate": round(len(timeouts) / total, 4) if total else 0.0,
        "failed_count": total - len(ok) - len(timeouts),
        "replan_total": sum(r.replan_count for r in results),
        "replan_avg": W.mean([r.replan_count for r in results]),
        "replan_samples": sum(1 for r in results if r.replan_count > 0),
        "gmv_accuracy": round(len(gmv_pass) / total, 4) if total else 0.0,
        "gmv_pass_count": len(gmv_pass),
        # 分项准确率：便于看出"判对了趋势但没说对幅度"这类部分正确
        "gmv_trend_accuracy": round(sum(1 for r in results if (r.gmv or {}).get("trend_ok")) / total, 4) if total else 0.0,
        "gmv_pct_accuracy": round(sum(1 for r in results if (r.gmv or {}).get("pct_ok")) / total, 4) if total else 0.0,
        "gmv_cause_accuracy": round(sum(1 for r in results if (r.gmv or {}).get("cause_ok")) / total, 4) if total else 0.0,
        "gmv_clean_rate": round(sum(1 for r in results if (r.gmv or {}).get("clean_ok")) / total, 4) if total else 0.0,
        "token_total": sum(tokens),
        "token_avg": W.mean(tokens),
        "token_max": max(tokens) if tokens else 0,
        "node_tokens_avg": {node: W.mean(values) for node, values in sorted(nodes.items())},
        "node_tokens_sum": {node: sum(values) for node, values in sorted(nodes.items())},
        "llm_calls_total": sum(r.llm_calls for r in results),
        "llm_calls_avg": W.mean([r.llm_calls for r in results]),
        "latency_p50": W.percentile(latencies, 50),
        "latency_p95": W.percentile(latencies, 95),
        "latency_avg": W.mean(latencies),
        "latency_max": max(latencies) if latencies else 0,
        "faithfulness_avg": W.mean(faithful),
        "faithfulness_count": len(faithful),
        "recall_avg": W.mean(recalls),
        "judge_mode": results[0].judge_mode if results else "",
        "by_stratum": {
            k: {
                "count": v["count"],
                "success_rate": round(v["success"] / v["count"], 4) if v["count"] else 0.0,
                "gmv_accuracy": round(v["gmv_pass"] / v["count"], 4) if v["count"] else 0.0,
                "latency_p95": W.percentile(v["latency"], 95),
                "token_avg": W.mean(v["tokens"]),
            }
            for k, v in by_stratum.items()
        },
    }


def build_report(agg: dict[str, Any], results: Sequence[SampleResult]) -> str:
    meta = agg["meta"]
    head = [
        "# AgentFlow W4 评测报告",
        "",
        f"- 生成时间：{meta['generated_at']}",
        f"- 评测集：`{meta['manifest']}`（{agg['samples']} 条）",
        f"- 运行目标：{meta['target']}",
        f"- LLM 模式：{meta['llm_mode']}；judge 模式：{agg['judge_mode']}",
        f"- 超时/重试：timeout={meta['timeout']}s，retries={meta['retries']}",
        "",
        "> 离线（LLM_MOCK=true）时生成侧输出是确定性 Mock 文本，**质量类指标只能验证管线是否通**，",
        "> 不能代表真实模型效果；token 类指标同样来自估算器。真实结论请用 `--base-url` + 真实 LLM 复跑。",
        "",
        "## 1. 总览",
        "",
    ]
    overview = [
        {"指标": "任务成功率", "值": f"{agg['success_rate'] * 100:.2f}%（{agg['success_count']}/{agg['samples']}）"},
        {"指标": "超时率", "值": f"{agg['timeout_rate'] * 100:.2f}%（{agg['timeout_count']} 条）"},
        {"指标": "GMV 结论准确率（四项全过）", "值": f"{agg['gmv_accuracy'] * 100:.2f}%（{agg['gmv_pass_count']}/{agg['samples']}）"},
        {"指标": "GMV 分项：趋势 / 幅度 / 必提词 / 无禁用论断",
         "值": f"{agg['gmv_trend_accuracy'] * 100:.1f}% / {agg['gmv_pct_accuracy'] * 100:.1f}% / "
               f"{agg['gmv_cause_accuracy'] * 100:.1f}% / {agg['gmv_clean_rate'] * 100:.1f}%"},
        {"指标": "重规划次数（均值/总/涉及样例）", "值": f"{agg['replan_avg']} / {agg['replan_total']} / {agg['replan_samples']}"},
        {"指标": "总 token / 单样例均值", "值": f"{agg['token_total']} / {agg['token_avg']}"},
        {"指标": "LLM 调用次数（均值）", "值": f"{agg['llm_calls_total']} / {agg['llm_calls_avg']}"},
        {"指标": "P50 / P95 时延", "值": f"{agg['latency_p50']} ms / {agg['latency_p95']} ms"},
        {"指标": "faithfulness 均值", "值": f"{agg['faithfulness_avg']}（{agg['faithfulness_count']} 条有效）"},
        {"指标": "上下文召回率均值", "值": f"{agg['recall_avg']}"},
    ]
    body = [W.markdown_table(overview, ["指标", "值"]), "", "## 2. 分节点 token 消耗（均值）", ""]
    node_rows = [{"节点": node, "均值": avg, "合计": agg["node_tokens_sum"].get(node, 0)} for node, avg in agg["node_tokens_avg"].items()]
    body += [W.markdown_table(node_rows, ["节点", "均值", "合计"]) if node_rows else "（无数据）"]

    body += ["", "## 3. 分层表现", ""]
    stratum_rows = [{"分层": k, **v} for k, v in agg["by_stratum"].items()]
    body += [W.markdown_table(stratum_rows, ["分层", "count", "success_rate", "gmv_accuracy", "latency_p95", "token_avg"])]

    body += ["", "## 4. 逐样例明细", ""]
    rows = [
        {
            "id": r.sample.sample_id,
            "status": r.status,
            "reason": r.terminal_reason,
            "replan": r.replan_count,
            "token": r.token_total,
            "ms": r.latency_ms,
            "recall": r.keyword_recall,
            "gmv": "✅" if (r.gmv or {}).get("gmv_pass") else "❌",
            "faith": r.faithfulness,
        }
        for r in results
    ]
    body += [W.markdown_table(rows, ["id", "status", "reason", "replan", "token", "ms", "recall", "gmv", "faith"])]

    failures = [r for r in results if not r.ok]
    if failures:
        body += ["", "## 5. 失败样例归因", ""]
        for r in failures:
            body.append(
                f"- `{r.sample.sample_id}` status={r.status} reason={r.terminal_reason} "
                f"error={r.error_code or '-'} msg={(r.error_message or '')[:120]}"
            )
    return "\n".join(head + body) + "\n"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
async def run(args: argparse.Namespace) -> int:
    manifest = Path(args.manifest)
    if not manifest.is_absolute():
        manifest = PROJECT_ROOT / manifest
    samples = load_samples(manifest, limit=args.limit, ids=(args.ids or "").split(",") if args.ids else None)

    output_dir = Path(args.output)
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir
    W.ensure_dir(output_dir)
    tag = args.tag or W.now_tag()

    judge = W.JudgeClient(
        base_url=args.judge_base_url,
        api_key=args.judge_api_key,
        model=args.judge_model,
        mock=args.judge_mock,
    )

    print(f"[eval] 样例 {len(samples)} 条；目标={'HTTP ' + args.base_url if args.base_url else '嵌入式'}；judge={judge.mode}")
    api, handle = await W.build_target(args)
    results: list[SampleResult] = []
    semaphore = asyncio.Semaphore(max(1, args.concurrency))

    async def _one(index: int, sample: EvalSample) -> None:
        async with semaphore:
            print(f"  [{index}/{len(samples)}] {sample.sample_id} {sample.question[:34]}…")
            try:
                item = await run_sample(
                    api,
                    sample,
                    judge,
                    timeout=args.timeout,
                    retries=args.retries,
                    reject=args.reject,
                    interval=args.interval,
                )
            except Exception as exc:  # noqa: BLE001 - 单条异常不得中断整批
                item = SampleResult(sample=sample, status=W.STATUS_FAILED)
                item.terminal_reason = f"RUNNER_ERROR:{type(exc).__name__}"
                item.error_message = str(exc)[:300]
            results.append(item)
            print(
                f"      → {item.status} {item.terminal_reason} replan={item.replan_count} "
                f"token={item.token_total} {item.latency_ms}ms gmv={'✅' if (item.gmv or {}).get('gmv_pass') else '❌'} "
                f"recall={item.keyword_recall} faith={item.faithfulness}"
            )

    started = time.perf_counter()
    try:
        await asyncio.gather(*(_one(i + 1, s) for i, s in enumerate(samples)))
    finally:
        await W.close_target(handle)
    elapsed = round(time.perf_counter() - started, 2)

    results.sort(key=lambda r: r.sample.idx)
    meta = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "manifest": str(manifest.relative_to(PROJECT_ROOT)) if str(manifest).startswith(str(PROJECT_ROOT)) else str(manifest),
        "target": f"http {args.base_url}" if args.base_url else "embedded(sqlite+fakeredis+InMemorySaver)",
        "llm_mode": "mock" if (os.environ.get("LLM_MOCK", "").lower() in {"1", "true"}) else "real",
        "judge_mode": judge.mode,
        "judge_model": judge.model or "local-rule",
        "timeout": args.timeout,
        "retries": args.retries,
        "concurrency": args.concurrency,
        "elapsed_seconds": elapsed,
        "offline_flags": {
            "LLM_MOCK": os.environ.get("LLM_MOCK"),
            "MOCK_EXTERNAL_RAG": os.environ.get("MOCK_EXTERNAL_RAG"),
            "MOCK_EXTERNAL_EVAL": os.environ.get("MOCK_EXTERNAL_EVAL"),
            "CHECKPOINTER_BACKEND": os.environ.get("CHECKPOINTER_BACKEND"),
        },
    }
    agg = aggregate(results, meta)
    rows = [r.as_row() for r in results]

    detail_path = W.write_json(output_dir / f"eval_detail_{tag}.json", {"meta": meta, "aggregate": agg, "samples": rows})
    summary_path = W.write_csv(output_dir / f"eval_summary_{tag}.csv", rows)
    metric_rows = [
        {"metric": "success_rate", "value": agg["success_rate"], "unit": "ratio"},
        {"metric": "timeout_rate", "value": agg["timeout_rate"], "unit": "ratio"},
        {"metric": "gmv_accuracy", "value": agg["gmv_accuracy"], "unit": "ratio"},
        {"metric": "gmv_trend_accuracy", "value": agg["gmv_trend_accuracy"], "unit": "ratio"},
        {"metric": "gmv_pct_accuracy", "value": agg["gmv_pct_accuracy"], "unit": "ratio"},
        {"metric": "gmv_cause_accuracy", "value": agg["gmv_cause_accuracy"], "unit": "ratio"},
        {"metric": "gmv_clean_rate", "value": agg["gmv_clean_rate"], "unit": "ratio"},
        {"metric": "replan_avg", "value": agg["replan_avg"], "unit": "count"},
        {"metric": "token_avg", "value": agg["token_avg"], "unit": "token"},
        {"metric": "token_total", "value": agg["token_total"], "unit": "token"},
        {"metric": "llm_calls_avg", "value": agg["llm_calls_avg"], "unit": "count"},
        {"metric": "latency_p50", "value": agg["latency_p50"], "unit": "ms"},
        {"metric": "latency_p95", "value": agg["latency_p95"], "unit": "ms"},
        {"metric": "faithfulness_avg", "value": agg["faithfulness_avg"], "unit": "0-1"},
        {"metric": "recall_avg", "value": agg["recall_avg"], "unit": "0-1"},
    ]
    for node, value in agg["node_tokens_avg"].items():
        metric_rows.append({"metric": f"token_avg.{node}", "value": value, "unit": "token"})
    metrics_path = W.write_csv(output_dir / f"eval_metrics_{tag}.csv", metric_rows, ["metric", "value", "unit"])
    report_path = W.write_text(output_dir / f"eval_report_{tag}.md", build_report(agg, results))

    print("\n=== 汇总 ===")
    print(f"成功率        {agg['success_rate'] * 100:.2f}%  ({agg['success_count']}/{agg['samples']})")
    print(f"GMV 准确率    {agg['gmv_accuracy'] * 100:.2f}%  "
          f"（趋势 {agg['gmv_trend_accuracy'] * 100:.0f}% / 幅度 {agg['gmv_pct_accuracy'] * 100:.0f}% / "
          f"必提词 {agg['gmv_cause_accuracy'] * 100:.0f}%）")
    print(f"重规划        均值 {agg['replan_avg']} / 涉及 {agg['replan_samples']} 条")
    print(f"token         总 {agg['token_total']} / 均值 {agg['token_avg']}")
    print(f"P95 时延      {agg['latency_p95']} ms")
    print(f"faithfulness  {agg['faithfulness_avg']}（{agg['judge_mode']}）")
    print(f"召回率均值    {agg['recall_avg']}")
    print(f"\n[eval] 明细 {detail_path.name} / 汇总 {summary_path.name} / 指标 {metrics_path.name} / 报告 {report_path.name}")
    print(f"[eval] 目录 {output_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="W4 评测跑批（分层难例 30 条）")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="评测集 manifest 路径")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="输出目录（默认 eval_output/）")
    parser.add_argument("--tag", default=None, help="输出文件名后缀（默认时间戳）")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（冒烟用）")
    parser.add_argument("--ids", default=None, help="只跑指定 ID，逗号分隔")
    parser.add_argument("--concurrency", type=int, default=1, help="并发提交数（默认 1，保证时延口径稳定）")
    parser.add_argument("--retries", type=int, default=2, help="超时重试次数（默认 2）")
    parser.add_argument("--reject", action="store_true", help="挂起时自动驳回（验证 replan 路径）")
    parser.add_argument("--judge-base-url", default=None, help="评测模型 base_url（默认读 JUDGE_BASE_URL）")
    parser.add_argument("--judge-api-key", default=None, help="评测模型 api_key（默认读 JUDGE_API_KEY）")
    parser.add_argument("--judge-model", default=None, help="评测模型名（默认读 JUDGE_MODEL）")
    parser.add_argument("--judge-mock", action="store_true", help="强制本地规则评审（不打外部 LLM）")
    return W.add_target_args(parser)


def main() -> int:
    args = build_parser().parse_args()
    if args.judge_base_url:
        os.environ["JUDGE_BASE_URL"] = args.judge_base_url
    if args.judge_api_key:
        os.environ["JUDGE_API_KEY"] = args.judge_api_key
    if args.judge_model:
        os.environ["JUDGE_MODEL"] = args.judge_model
    if args.judge_mock:
        os.environ["JUDGE_MOCK"] = "true"
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
