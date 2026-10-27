"""评测集与样本接口。"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.response import ok, paged
from app.models import EvalDataset, EvalItem
from app.schemas.eval import DatasetCreate, ItemsImport
from app.services.serialize import dataset_to_dict, item_to_dict

router = APIRouter(prefix="/eval/datasets", tags=["评测集"])


def _get_dataset(db: Session, dataset_id: int) -> EvalDataset:
    ds = db.get(EvalDataset, dataset_id)
    if ds is None or not ds.is_active:
        raise HTTPException(status_code=404, detail=f"评测集 {dataset_id} 不存在或已归档")
    return ds


@router.get("", summary="评测集分页列表")
def list_datasets(
    page: int = Query(1, ge=1),
    size: int = Query(100, ge=1, le=200),
    target_type: str | None = None,
    db: Session = Depends(get_db),
):
    stmt = select(EvalDataset).where(EvalDataset.is_active.is_(True))
    if target_type:
        stmt = stmt.where(EvalDataset.target_type == target_type)
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = (
        db.execute(stmt.order_by(EvalDataset.id.desc()).offset((page - 1) * size).limit(size))
        .scalars()
        .all()
    )
    return paged([dataset_to_dict(r) for r in rows], total, page, size)


@router.post("", summary="创建评测集")
def create_dataset(payload: DatasetCreate, db: Session = Depends(get_db)):
    if payload.code:
        exists = db.execute(
            select(EvalDataset.id).where(EvalDataset.code == payload.code)
        ).scalar_one_or_none()
        if exists is not None:
            raise HTTPException(status_code=409, detail=f"评测集编码 {payload.code} 已存在")

    ds = EvalDataset(
        code=payload.code,
        name=payload.name,
        description=payload.description,
        target_type=payload.target_type,
        target_ref=payload.target_ref or (str(payload.kb_id) if payload.kb_id else None),
        kb_id=payload.kb_id,
        agent_scope=[a.model_dump() for a in payload.agent_scope] if payload.agent_scope else None,
        scenario=payload.scenario,
        tags=payload.tags,
        version=payload.version,
        created_by=payload.created_by,
    )
    db.add(ds)
    db.commit()
    db.refresh(ds)
    return ok(dataset_to_dict(ds))


@router.get("/{dataset_id}", summary="评测集详情")
def get_dataset(dataset_id: int, db: Session = Depends(get_db)):
    return ok(dataset_to_dict(_get_dataset(db, dataset_id)))


@router.delete("/{dataset_id}", summary="删除评测集（级联删除样本）")
def delete_dataset(dataset_id: int, db: Session = Depends(get_db)):
    ds = _get_dataset(db, dataset_id)
    deleted_items = db.execute(
        select(func.count()).select_from(EvalItem).where(EvalItem.dataset_id == dataset_id)
    ).scalar_one()
    db.delete(ds)  # ORM 级联 delete-orphan + DB 侧 ondelete=CASCADE 双保险
    db.commit()
    return ok({"deleted_dataset_id": dataset_id, "deleted_items": int(deleted_items)})


@router.get("/{dataset_id}/items", summary="样本分页列表")
def list_items(
    dataset_id: int,
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=200),
    stratum: str | None = None,
    db: Session = Depends(get_db),
):
    _get_dataset(db, dataset_id)
    stmt = select(EvalItem).where(EvalItem.dataset_id == dataset_id)
    if stratum:
        stmt = stmt.where(EvalItem.stratum == stratum)
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = (
        db.execute(stmt.order_by(EvalItem.seq.asc()).offset((page - 1) * size).limit(size))
        .scalars()
        .all()
    )
    return paged([item_to_dict(r) for r in rows], total, page, size)


@router.post("/{dataset_id}/items", summary="批量导入/追加样本（单次≤500，单数据集总量≤500）")
def add_items(dataset_id: int, payload: ItemsImport, db: Session = Depends(get_db)):
    """既用于「新建评测集时导入」，也用于「向已有评测集追加样本」。

    两道上限要分清：
      - **单次请求** ≤ 500：由 `ItemsImport.max_length` 拦（pydantic 422）；
      - **单数据集总量** ≤ `DATASET_MAX_ITEMS`：在这里拦（409）。
        支持追加之后要是没有总量闸，反复追加就能无限撑大，页面写的"最多 500 条"是假话，
        而且一轮评测的裁判开销没有上限。
    """
    ds = _get_dataset(db, dataset_id)
    existing = int(
        db.execute(
            select(func.count()).select_from(EvalItem).where(EvalItem.dataset_id == dataset_id)
        ).scalar_one()
    )
    incoming = len(payload.items)
    if existing + incoming > settings.DATASET_MAX_ITEMS:
        raise HTTPException(
            status_code=409,
            detail=(
                f"追加后样本数 {existing + incoming} 超过单数据集上限 "
                f"{settings.DATASET_MAX_ITEMS} 条（当前 {existing} 条，本次 {incoming} 条）"
            ),
        )

    current_max = db.execute(
        select(func.coalesce(func.max(EvalItem.seq), 0)).where(EvalItem.dataset_id == dataset_id)
    ).scalar_one()

    rows = []
    for offset, item in enumerate(payload.items, start=1):
        rows.append(
            EvalItem(
                dataset_id=dataset_id,
                # seq 从现有最大序号继续（不是从 1 重排）——历史得分明细里的 item_seq
                # 是对旧样本的快照，重排会让"当时的第 3 条"和"现在的第 3 条"对不上
                seq=int(current_max) + offset,
                question=item.question,
                reference_answer=item.reference_answer,
                reference_contexts=item.reference_contexts,
                stratum=item.stratum,
                difficulty=item.difficulty,
                item_type=item.item_type,
                expected_tools=item.expected_tools,
                expected_agents=item.expected_agents,
                max_latency_ms=item.max_latency_ms,
                meta=item.meta,
            )
        )
    db.add_all(rows)
    ds.item_count = existing + len(rows)
    db.commit()
    return ok({"imported": len(rows), "item_count": ds.item_count, "previous_count": existing})
