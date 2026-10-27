"""导出第三步落库实据（runs + scores），供截图脚本渲染。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.core.database import engine  # noqa: E402

RUNS_SQL = text(
    """
    SELECT r.id, r.run_no, r.dataset_id, r.variant, r.status,
           r.total_items, r.success_items, r.failed_items,
           r.rag_config, r.metrics, r.total_tokens, r.started_at, r.finished_at
    FROM eval_runs r ORDER BY r.id
    """
)

SCORES_SQL = text(
    """
    SELECT s.run_id, s.item_seq, s.stratum, s.question,
           s.faithfulness, s.answer_relevancy, s.context_precision, s.context_recall,
           s.latency_ms, s.suspect, s.status, s.error_msg
    FROM eval_scores s ORDER BY s.run_id, s.item_seq
    """
)


def _json(value):
    """JSON 列用 text() 读出来是字符串，必须显式反序列化。

    （否则截图里 metrics 全是 NULL —— 踩过一次，别再用 str 直接塞给前端。）
    """
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def main() -> None:
    out = {"runs": [], "scores": []}
    with engine.connect() as conn:
        for r in conn.execute(RUNS_SQL).fetchall():
            out["runs"].append(
                {
                    "id": r[0], "run_no": r[1], "dataset_id": r[2], "variant": r[3],
                    "status": r[4], "total_items": r[5], "success_items": r[6],
                    "failed_items": r[7],
                    "rag_config": _json(r[8]), "metrics": _json(r[9]), "total_tokens": r[10],
                    "started_at": str(r[11]), "finished_at": str(r[12]),
                }
            )
        for r in conn.execute(SCORES_SQL).fetchall():
            out["scores"].append(
                {
                    "run_id": r[0], "item_seq": r[1], "stratum": r[2], "question": r[3],
                    "faithfulness": r[4], "answer_relevancy": r[5],
                    "context_precision": r[6], "context_recall": r[7],
                    "latency_ms": r[8], "suspect": bool(r[9]), "status": r[10],
                    "error_msg": r[11],
                }
            )

    target = Path(__file__).resolve().parent.parent / "logs" / "stage3_db.json"
    target.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"导出 runs={len(out['runs'])} scores={len(out['scores'])} -> {target}")


if __name__ == "__main__":
    main()
