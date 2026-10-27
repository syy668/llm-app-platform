"""抽样精测：RAGAS 判分到底烧多少 token（用于给 AgentFlow 评测成本一个真实数字）。

背景 / 为什么需要这个脚本
--------------------------
项目一的 `trace_logs` 只记录**问答链路**的 LLM 调用（检索 → 生成答案）。
RAGAS 的四项指标由 ragas 内部直接调 langchain 判分模型，**不经过**项目一的
trace callback，因此 `trace_logs` 里看不出判分消耗。

后果：任何人问「跑一次 N 条样本的评测花多少 token」，都只能靠拍脑袋。拍脑袋的
数字一旦被写进文档/汇报，就又变成了「看起来精确的假数据」——这正是本项目要
根治的问题。

做法
----
用 langchain 的 callback 给判分模型挂一个 usage 收集器（`on_llm_end` 读
`llm_output.token_usage`），对**已落库的 1 条真实样本**重跑一次四指标判分，
得到「单样本判分 = X 次调用 / Y input / Z output」，再乘以样本数外推。

为什么复用已落库样本而不是重新检索生成：
- 省一次检索 + 生成的钱，只测判分这一项（本脚本唯一目标）；
- answer / contexts / reference 都是当时真实产物，判分输入分布与正式跑批一致。

注意：这是**抽样**，外推值是估算。脚本会同时打印每项的调用次数，便于判断
离散度（claim 数不同会导致 faithfulness 的 NLI 判定次数不同）。

用法
----
    .venv/Scripts/python.exe scripts/probe_judge_token_usage.py --run-id 69 --sample-index 0
    .venv/Scripts/python.exe scripts/probe_judge_token_usage.py --run-id 69 --all
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.callbacks import BaseCallbackHandler  # noqa: E402


class UsageCollector(BaseCallbackHandler):
    """统计判分模型的调用次数与 token 用量。

    `on_llm_end` 是唯一能拿到 usage 的钩子：OpenAI 兼容接口把用量放在
    `response.llm_output["token_usage"]`，不同 langchain 版本键名略有差异，
    这里做兼容读取（prompt_tokens / input_tokens 两种写法都认）。
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.by_metric: dict[str, dict[str, int]] = {}
        self._current = "unknown"

    def set_metric(self, name: str) -> None:
        self._current = name
        self.by_metric.setdefault(name, {"calls": 0, "in": 0, "out": 0})

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:  # noqa: ANN401
        usage = {}
        llm_output = getattr(response, "llm_output", None) or {}
        if isinstance(llm_output, dict):
            usage = llm_output.get("token_usage") or llm_output.get("usage") or {}
        if not usage:
            # 部分版本把 usage 挂在 generation.message.usage_metadata
            try:
                gen = response.generations[0][0]
                usage = getattr(getattr(gen, "message", None), "usage_metadata", None) or {}
            except Exception:  # noqa: BLE001
                usage = {}
        inp = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        out = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        self.calls += 1
        self.input_tokens += inp
        self.output_tokens += out
        slot = self.by_metric[self._current]
        slot["calls"] += 1
        slot["in"] += inp
        slot["out"] += out


def _load_scores(run_id: int) -> list[dict]:
    from app.core.database import SessionLocal
    from app.models.eval_dataset import EvalDatasetItem
    from app.models.eval_run import EvalRunScore

    db = SessionLocal()
    try:
        rows = (
            db.query(EvalRunScore)
            .filter(EvalRunScore.run_id == run_id)
            .order_by(EvalRunScore.id.asc())
            .all()
        )
        # reference 不在 eval_run_scores 里（得分表只存模型产物），要回样本表取 gold
        item_ids = [r.item_id for r in rows]
        gold = {
            it.id: it.reference_answer
            for it in db.query(EvalDatasetItem).filter(EvalDatasetItem.id.in_(item_ids)).all()
        } if item_ids else {}
        return [
            {
                "item_id": r.item_id,
                "question": r.question,
                "answer": r.answer,
                "contexts": list(r.retrieved_contexts or []),
                "reference": gold.get(r.item_id) or "",
                "stored": {
                    "faithfulness": r.faithfulness,
                    "answer_relevancy": r.answer_relevancy,
                    "context_precision": r.context_precision,
                    "context_recall": r.context_recall,
                },
            }
            for r in rows
        ]
    finally:
        db.close()


def _build_judge_with_callbacks(collector: "UsageCollector"):
    """构造与「项目一判分模型」等价、但带 usage callback 的 ChatOpenAI 实例。

    为什么不直接用 ``get_eval_chat_model``：该工厂不接受 callbacks 参数，而
    ``model.with_config(callbacks=[...])`` 返回的是 ``RunnableBinding``，
    ragas 的 ``LangchainLLMWrapper`` 会读 ``.temperature`` 属性 → 抛
    ``ValueError: "RunnableBinding" object has no field "temperature"``（实测踩过）。

    所以这里按**完全相同的取值口径**（runtime_config 覆盖 > .env 回退）自己构造
    一个 ChatOpenAI，并把 callbacks 作为构造参数传入 —— 这样得到的就是原始
    ChatOpenAI 实例，属性齐全，ragas 正常包装，usage 也能被 callback 收到。
    """
    from langchain_openai import ChatOpenAI

    from app.config import settings
    from app.core import runtime_config

    model = runtime_config.get_value("eval_llm_model") or runtime_config.get_value("llm_model")
    api_base = (
        runtime_config.get_value("eval_llm_api_base")
        or runtime_config.get_value("llm_api_base")
        or settings.llm_api_base
    )
    api_key = (
        runtime_config.get_value("eval_llm_api_key")
        or runtime_config.get_value("llm_api_key")
        or settings.llm_api_key
    )
    if not (model and api_base and api_key):
        raise SystemExit("判分模型配置不完整（model / api_base / api_key 缺一）")
    return ChatOpenAI(
        model=str(model),
        base_url=str(api_base),
        api_key=str(api_key),
        temperature=0.0,  # 与项目一 metrics.py 硬编码值保持一致
        timeout=float(settings.eval_llm_timeout),
        max_retries=int(settings.eval_llm_max_retries),
        callbacks=[collector],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="抽样精测 RAGAS 判分 token 用量")
    parser.add_argument("--run-id", type=int, required=True, help="项目一 eval_runs.id")
    parser.add_argument("--sample-index", type=int, default=0, help="第几条样本（从 0 起）")
    parser.add_argument("--all", action="store_true", help="对全部样本各测一遍（更准，但更贵）")
    args = parser.parse_args()

    from app.core import runtime_config
    from app.eval.metrics import _AppEmbeddings, _eval_model_label
    from ragas import EvaluationDataset, evaluate
    from ragas.dataset_schema import SingleTurnSample
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import AnswerRelevancy, ContextPrecision, ContextRecall, Faithfulness
    from ragas.run_config import RunConfig

    from app.config import settings

    scores = _load_scores(args.run_id)
    if not scores:
        print(f"run {args.run_id} 没有任何已落库得分，无法抽样")
        return 1

    targets = scores if args.all else [scores[args.sample_index]]
    print(f"run={args.run_id} 样本总数={len(scores)} 本次精测={len(targets)} 条")
    print(f"判分模型={_eval_model_label()}（temperature=0.0，项目一硬编码）")

    collector = UsageCollector()
    judge = _build_judge_with_callbacks(collector)
    # 关键：把 callback 绑到**原始 ChatOpenAI 实例**上，ragas 内部调用才能被统计到。
    # 不能用 model.with_config(...)：它返回 RunnableBinding，而 ragas 的
    # LangchainLLMWrapper 会读取 .temperature 属性 → ValueError（实测踩过）。
    llm = LangchainLLMWrapper(judge)
    emb = LangchainEmbeddingsWrapper(_AppEmbeddings())

    results: list[dict] = []
    for sample in targets:
        # 每项指标分开跑，才能把 token 归因到具体指标
        per_metric = {
            "faithfulness": [Faithfulness(llm=llm)],
            "answer_relevancy": [AnswerRelevancy(llm=llm, embeddings=emb)],
            "context_precision": [ContextPrecision(llm=llm)],
            "context_recall": [ContextRecall(llm=llm)],
        }
        row: dict[str, Any] = {"item_id": sample["item_id"], "metrics": {}}
        for name, metrics in per_metric.items():
            collector.set_metric(name)
            dataset = EvaluationDataset(
                samples=[
                    SingleTurnSample(
                        user_input=sample["question"],
                        response=sample["answer"],
                        retrieved_contexts=sample["contexts"],
                        reference=sample["reference"],
                    )
                ]
            )
            try:
                out = evaluate(
                    dataset=dataset,
                    metrics=metrics,
                    run_config=RunConfig(timeout=settings.eval_ragas_timeout),
                )
                df = out.to_pandas()
                value = float(df.iloc[0][name])
                row["metrics"][name] = None if value != value else round(value, 6)
            except Exception as exc:  # noqa: BLE001
                row["metrics"][name] = f"ERROR: {str(exc)[:120]}"
        row["stored"] = sample["stored"]
        results.append(row)
        print(f"  item={sample['item_id']} 复测={json.dumps(row['metrics'], ensure_ascii=False)}")
        print(f"    落库值={json.dumps(sample['stored'], ensure_ascii=False)}")

    print()
    print("=== 判分 token 用量 ===")
    print(f"  调用次数 = {collector.calls}")
    print(f"  input    = {collector.input_tokens:,}")
    print(f"  output   = {collector.output_tokens:,}")
    total = collector.input_tokens + collector.output_tokens
    print(f"  合计     = {total:,} token（{len(targets)} 条样本）")
    if targets:
        print(f"  单样本均值 = {total / len(targets):,.0f} token")
    print()
    print("  按指标归因：")
    for name, slot in collector.by_metric.items():
        subtotal = slot["in"] + slot["out"]
        print(
            f"    {name:20s} 调用={slot['calls']:3d}  in={slot['in']:7,}  out={slot['out']:7,}  合计={subtotal:7,}"
        )

    report = {
        "run_id": args.run_id,
        "samples_measured": len(targets),
        "calls": collector.calls,
        "input_tokens": collector.input_tokens,
        "output_tokens": collector.output_tokens,
        "total_tokens": total,
        "per_sample_avg": round(total / len(targets), 1) if targets else None,
        "by_metric": collector.by_metric,
        "results": results,
    }
    out_path = Path("data") / f"judge_usage_run{args.run_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"报告已写入 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
