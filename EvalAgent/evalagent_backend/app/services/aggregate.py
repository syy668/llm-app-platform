"""指标聚合（纯计算，无 IO）。

统一口径：
  - 均值**只对非 NULL 求**，全部为 NULL 时返回 None（不返回 0，0 是"真实得零分"）；
  - 保留 6 位小数，避免前端 0.6000000000000001 这类浮点噪声。
"""

from collections import defaultdict
from typing import Any, Iterable

from app.models.eval_run import CORE_METRICS


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def mean_metrics(rows: Iterable[dict[str, Any]]) -> dict[str, float | None] | None:
    """四项指标均值；四项全空时返回 None（表示"这次没算出分"）。"""
    rows = list(rows)
    out: dict[str, float | None] = {}
    has_any = False
    for metric in CORE_METRICS:
        vals = [r[metric] for r in rows if r.get(metric) is not None]
        out[metric] = _avg(vals)
        has_any = has_any or bool(vals)
    return out if has_any else None


def stratum_metrics(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """按 stratum 分层聚合。未分层（None）单独归一组，与前端 `stratum: null` 对齐。"""
    groups: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[r.get("stratum")].append(r)

    items = []
    for stratum in sorted(groups, key=lambda s: (s is None, s or "")):
        group = groups[stratum]
        metrics = mean_metrics(group) or {m: None for m in CORE_METRICS}
        items.append({"stratum": stratum, "sample_count": len(group), "metrics": metrics})

    return {
        "total": sum(i["sample_count"] for i in items),
        "items": items,
    }


def compare_scores(
    run_a: dict[str, Any],
    run_b: dict[str, Any],
    scores_a: list[dict[str, Any]],
    scores_b: list[dict[str, Any]],
    diff_threshold: float,
) -> dict[str, Any]:
    """A/B 对比：按 question 对齐，逐指标算 Δ = b - a。

    只在一侧出现的样本，另一侧给 None、Δ 也给 None（不拿 0 顶替，
    否则"少跑了几条"会被算成"指标暴跌"）。
    """
    by_q_a = {s["question"]: s for s in scores_a}
    by_q_b = {s["question"]: s for s in scores_b}
    # 保持 A 的顺序，B 独有的追加在后
    questions = list(by_q_a.keys()) + [q for q in by_q_b if q not in by_q_a]

    rows = []
    for question in questions:
        a, b = by_q_a.get(question), by_q_b.get(question)
        diffs: dict[str, float | None] = {}
        for metric in CORE_METRICS:
            va = a.get(metric) if a else None
            vb = b.get(metric) if b else None
            diffs[metric] = round(vb - va, 6) if (va is not None and vb is not None) else None
        is_diff = any(d is not None and abs(d) > diff_threshold for d in diffs.values())
        rows.append({"question": question, "a": a, "b": b, "diffs": diffs, "is_diff": is_diff})

    summary = []
    for metric in CORE_METRICS:
        # 落库的 run.metrics 优先，缺失才现算（与项目一口径一致）
        avg_a = (run_a.get("metrics") or {}).get(metric)
        avg_b = (run_b.get("metrics") or {}).get(metric)
        if avg_a is None:
            avg_a = mean_metrics(scores_a) or {}
            avg_a = avg_a.get(metric)
        if avg_b is None:
            avg_b = mean_metrics(scores_b) or {}
            avg_b = avg_b.get(metric)
        diff = round(avg_b - avg_a, 6) if (avg_a is not None and avg_b is not None) else None
        summary.append({"metric": metric, "avg_a": avg_a, "avg_b": avg_b, "diff": diff})

    return {
        "run_a": run_a,
        "run_b": run_b,
        "diff_threshold": diff_threshold,
        "summary": summary,
        "rows": rows,
    }
