"""ORM → 响应字典。

带 `__compat__` 注释的键是「前端 v1 视图仍在读取的旧字段别名」：
后端表结构已演进（kb_id 可空、无 model 列），但第四步只改 axios 基址、
不改前端视图，故在序列化层补齐别名，避免 UI 出现空白。
前端类型定义对齐后（第五步）可一次性移除。
"""

from typing import Any

from app.models import EvalDataset, EvalItem, EvalRun, EvalScore


def dataset_to_dict(ds: EvalDataset) -> dict[str, Any]:
    return {
        "id": ds.id,
        "code": ds.code,
        "name": ds.name,
        "description": ds.description,
        "target_type": ds.target_type,
        "target_ref": ds.target_ref,
        "kb_id": ds.kb_id,  # __compat__ 前端 DatasetView 直接展示该列
        "agent_scope": ds.agent_scope,
        "scenario": ds.scenario,
        "tags": ds.tags,
        "version": ds.version,
        "item_count": ds.item_count,
        "is_active": ds.is_active,
        "created_by": ds.created_by,
        "created_at": ds.created_at,
        "updated_at": ds.updated_at,
    }


def item_to_dict(item: EvalItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "dataset_id": item.dataset_id,
        "seq": item.seq,
        "question": item.question,
        "reference_answer": item.reference_answer,
        "reference_contexts": item.reference_contexts,
        "stratum": item.stratum,
        "difficulty": item.difficulty,
        "item_type": item.item_type,
        "expected_tools": item.expected_tools,
        "expected_agents": item.expected_agents,
        "max_latency_ms": item.max_latency_ms,
        "created_at": item.created_at,
    }


def run_to_dict(run: EvalRun, dataset: EvalDataset | None = None) -> dict[str, Any]:
    evaluator = run.evaluator_config if isinstance(run.evaluator_config, dict) else {}
    judge = evaluator.get("judge_model")
    return {
        "id": run.id,
        "run_no": run.run_no,
        "dataset_id": run.dataset_id,
        "dataset_version": run.dataset_version,
        "target": run.target,
        "target_ref": run.target_ref,
        "variant": run.variant,
        "status": run.status,
        "total_items": run.total_items,
        "processed_items": run.processed_items,
        "success_items": run.success_items,
        "failed_items": run.failed_items,
        "skipped_items": run.skipped_items,
        "metrics": run.metrics,
        "metric_detail": run.metric_detail,
        "rag_config": run.rag_config,
        "evaluator_config": run.evaluator_config,
        "agent_config": run.agent_config,
        "total_tokens": run.total_tokens,
        "cost_usd": float(run.cost_usd) if run.cost_usd is not None else 0.0,
        "trace_id": run.trace_id,
        "ext_run_ref": run.ext_run_ref,
        "error_msg": run.error_msg,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "created_at": run.created_at,
        # __compat__ TaskView 详情页读 model、列表页读 kb_id
        "model": judge or "",
        # 本次打分用的向量模型（answer_relevancy 依赖）；换 embedding 前后 AR 不可直接比，
        # 历史 run 必须能回答"当时用的哪个向量模型"
        "judge_embedding_model": evaluator.get("judge_embedding_model") or "",
        "kb_id": dataset.kb_id if dataset is not None else None,
        # __compat__ 项目三 _poll_upstream 读 data["detail"]["samples_count"]
        "detail": {
            "samples_count": run.success_items,
            "processed_items": run.processed_items,
            "total_items": run.total_items,
        },
    }


def score_to_dict(score: EvalScore) -> dict[str, Any]:
    return {
        "id": score.id,
        "run_id": score.run_id,
        "item_id": score.item_id,
        "item_seq": score.item_seq,
        "stratum": score.stratum,
        "question": score.question,
        "answer": score.answer,
        "reference_answer": score.reference_answer,
        # 与 GET /eval/runs/{id}/badcases 的 ground_truth 同名同义：
        # 打分时已把 item.reference_answer 冗余落库（runner 写入），本身就是「数据集里的标准答案」，
        # 没必要让明细接口缺这一列、只有 BadCase 接口去 JOIN 数据集才有 —— 那会让详情页
        # 同一行「BadCase 模式有答案、普通明细模式是 —」，被当成数据丢了（v1.24 修）。
        "ground_truth": score.reference_answer,
        "retrieved_contexts": score.retrieved_contexts,
        "faithfulness": score.faithfulness,
        "answer_relevancy": score.answer_relevancy,
        "context_precision": score.context_precision,
        "context_recall": score.context_recall,
        "metric_scores": score.metric_scores,
        "agent_role": score.agent_role,
        "latency_ms": score.latency_ms,
        "tokens": score.tokens,
        "suspect": score.suspect,
        "status": score.status,
        "error_msg": score.error_msg,
        "scored_at": score.scored_at,
        "created_at": score.created_at,
    }
