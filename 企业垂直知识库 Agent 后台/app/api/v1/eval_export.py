"""RAGAS 评测 CSV 导出接口（仅管理员）。"""
import csv
import io

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..deps import CurrentUser, require_admin
from ...core.database import get_db
from ...core.exceptions import NotFoundError
from ...models.eval_run import EvalRun, EvalRunScore

router = APIRouter(prefix="/eval", tags=["RAGAS 评测"])


def scores_to_csv(rows) -> str:
    """把 eval_run_scores 行序列化为 CSV 文本（不含 BOM）。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "item_id", "question", "answer", "faithfulness",
        "answer_relevancy", "context_precision", "context_recall",
        "retrieved_contexts", "latency_ms",
    ])
    for s in rows:
        writer.writerow([
            s.item_id, s.question, s.answer,
            s.faithfulness, s.answer_relevancy, s.context_precision, s.context_recall,
            " || ".join(s.retrieved_contexts) if s.retrieved_contexts else "",
            s.latency_ms,
        ])
    return buf.getvalue()


@router.get("/runs/{run_id}/export")
def export_run_csv(
    run_id: int,
    _: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """导出某次评测的得分数据集为 CSV 文件下载（管理员）。"""
    if not db.get(EvalRun, run_id):
        raise NotFoundError("评测运行不存在")
    rows = (
        db.query(EvalRunScore)
        .filter(EvalRunScore.run_id == run_id)
        .order_by(EvalRunScore.id.asc())
        .all()
    )

    # 前置 UTF-8 BOM，避免 Excel 打开中文乱码
    content = "﻿" + scores_to_csv(rows)
    return Response(
        content=content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="eval_run_{run_id}.csv"'},
    )
