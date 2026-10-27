"""v1.13 样本修正：黄金集补参考答案 / 删脏样本。

为什么单独一个用例集：v1.9 的「点踩 → 评测集」把 badcase 的 reference_answer
刻意写成空串（填 AI 的错误答案会让 RAGAS 把幻觉当标准答案），但旧实现只有
「追加」没有「修改」，这批样本的 context_recall 永远算不出来，前端
「可在评测页补充参考答案」的提示无法兑现。本文件锁死这两个补丁的行为。

覆盖：
  - 服务层 update_item：补答案、只传答案不清 stratum/contexts、空答案拒绝、
    跨数据集归属校验、空请求体拒绝、审计落库；
  - 服务层 delete_item：item_count 按实际行数重算（顺带修历史漂移）、跨集拒绝；
  - 服务层 list_datasets：missing_reference 聚合（空串与纯空白都算缺）；
  - schema 层：空串走 422、显式 null 放行到服务层给可读 400；
  - 接口层：成功契约、普通用户 403、未登录 401。
"""
import pytest
from pydantic import ValidationError

import app.services.eval_service as eval_service
from app.core.exceptions import BizError, NotFoundError
from app.models.eval_dataset import EvalDataset, EvalDatasetItem
from app.models.operation_log import OperationLog
from app.schemas.eval import ItemUpdate
from conftest import assert_error, assert_ok, make_user

API = "/api/v1/eval"


@pytest.fixture(autouse=True)
def _no_global_table_setup(monkeypatch):
    """屏蔽 ensure_eval_tables 对全局 engine（真实 MySQL）的建表动作。"""
    monkeypatch.setattr(eval_service, "ensure_eval_tables", lambda: None)


def _make_dataset(db, name="测试集", kb_id=6, count=0):
    ds = EvalDataset(kb_id=kb_id, name=name, description=None, item_count=count, created_by=1)
    db.add(ds)
    db.commit()
    db.refresh(ds)
    return ds


def _make_item(db, ds, question="差旅住宿标准是多少？", answer="", stratum=None):
    it = EvalDatasetItem(
        dataset_id=ds.id,
        question=question,
        reference_answer=answer,
        reference_contexts=None,
        stratum=stratum,
    )
    db.add(it)
    db.commit()
    db.refresh(it)
    return it


# ---------------------------------------------------------------------------
# 服务层：update_item
# ---------------------------------------------------------------------------

def test_update_item_fills_reference_answer(eval_db):
    """核心场景：badcase 的空答案被人工补上。"""
    ds = _make_dataset(eval_db, count=1)
    item = _make_item(eval_db, ds, answer="")

    data = eval_service.update_item(
        eval_db, ds.id, item.id, ItemUpdate(reference_answer="住宿上限 500 元/晚"), make_user("admin")
    )

    assert data["reference_answer"] == "住宿上限 500 元/晚"
    assert eval_db.get(EvalDatasetItem, item.id).reference_answer == "住宿上限 500 元/晚"


def test_update_item_trims_answer(eval_db):
    """首尾空白入库前被裁掉（避免「看起来有答案」实际是空格）。"""
    ds = _make_dataset(eval_db, count=1)
    item = _make_item(eval_db, ds, answer="")

    eval_service.update_item(
        eval_db, ds.id, item.id, ItemUpdate(reference_answer="  按 500 元/晚执行  "), make_user("admin")
    )

    assert eval_db.get(EvalDatasetItem, item.id).reference_answer == "按 500 元/晚执行"


def test_update_item_keeps_untouched_fields(eval_db):
    """PATCH 语义：只传 reference_answer 不得清空 stratum / reference_contexts。"""
    ds = _make_dataset(eval_db, count=1)
    item = _make_item(eval_db, ds, answer="旧答案", stratum="hard")
    item.reference_contexts = ["《差旅制度》第2块"]
    eval_db.commit()

    eval_service.update_item(
        eval_db, ds.id, item.id, ItemUpdate(reference_answer="新答案"), make_user("admin")
    )

    row = eval_db.get(EvalDatasetItem, item.id)
    assert row.reference_answer == "新答案"
    assert row.stratum == "hard"
    assert row.reference_contexts == ["《差旅制度》第2块"]


def test_update_item_can_set_stratum(eval_db):
    """分层标签可单独补（历史样本 stratum 全为 NULL，分层聚合才有意义）。"""
    ds = _make_dataset(eval_db, count=1)
    item = _make_item(eval_db, ds, answer="答案", stratum=None)

    eval_service.update_item(eval_db, ds.id, item.id, ItemUpdate(stratum="easy"), make_user("admin"))

    row = eval_db.get(EvalDatasetItem, item.id)
    assert row.stratum == "easy"
    assert row.reference_answer == "答案"


@pytest.mark.parametrize("blank", ["   ", "\n\t", " \n "])
def test_update_item_rejects_blank_answer(eval_db, blank):
    """纯空白参考答案必须拒绝：留空样本等于让 context_recall 永久失效。"""
    ds = _make_dataset(eval_db, count=1)
    item = _make_item(eval_db, ds, answer="")

    with pytest.raises(BizError) as exc:
        eval_service.update_item(
            eval_db, ds.id, item.id, ItemUpdate(reference_answer=blank), make_user("admin")
        )

    assert "context_recall" in str(exc.value)
    assert eval_db.get(EvalDatasetItem, item.id).reference_answer == ""


def test_update_item_rejects_explicit_null(eval_db):
    """显式 null 由服务层拒绝（给可读原因，而不是 pydantic 的 422 泛化报错）。"""
    ds = _make_dataset(eval_db, count=1)
    item = _make_item(eval_db, ds, answer="原答案")

    with pytest.raises(BizError):
        eval_service.update_item(
            eval_db, ds.id, item.id, ItemUpdate(reference_answer=None), make_user("admin")
        )

    assert eval_db.get(EvalDatasetItem, item.id).reference_answer == "原答案"


def test_update_item_rejects_empty_payload(eval_db):
    """一个字段都没传 -> 400，避免静默成功却什么都没改。"""
    ds = _make_dataset(eval_db, count=1)
    item = _make_item(eval_db, ds, answer="")

    with pytest.raises(BizError):
        eval_service.update_item(eval_db, ds.id, item.id, ItemUpdate(), make_user("admin"))


def test_update_item_rejects_cross_dataset(eval_db):
    """用 B 数据集的路径改 A 数据集的样本 -> 404（防跨集篡改黄金集）。"""
    ds_a = _make_dataset(eval_db, name="A")
    ds_b = _make_dataset(eval_db, name="B")
    item = _make_item(eval_db, ds_a, answer="")

    with pytest.raises(NotFoundError):
        eval_service.update_item(
            eval_db, ds_b.id, item.id, ItemUpdate(reference_answer="越权写入"), make_user("admin")
        )

    assert eval_db.get(EvalDatasetItem, item.id).reference_answer == ""


def test_update_item_writes_audit_log(eval_db):
    """参考答案是黄金集基准，改写必须留痕（含新旧值）。"""
    ds = _make_dataset(eval_db, count=1)
    item = _make_item(eval_db, ds, answer="")

    eval_service.update_item(
        eval_db, ds.id, item.id, ItemUpdate(reference_answer="住宿上限 500 元/晚"), make_user("admin")
    )

    logs = eval_db.query(OperationLog).filter(OperationLog.action == "update_eval_item").all()
    assert len(logs) == 1
    log = logs[0]
    assert log.target_type == "eval_dataset_item"
    assert log.target_id == str(item.id)
    assert log.user_id == 1
    assert log.detail["dataset_id"] == ds.id
    assert log.detail["old_len"] == 0
    assert log.detail["new_reference_answer"] == "住宿上限 500 元/晚"


# ---------------------------------------------------------------------------
# 服务层：delete_item
# ---------------------------------------------------------------------------

def test_delete_item_recounts_item_count(eval_db):
    """item_count 按实际行数重算：故意写入漂移值 99，删一条后应为 1。"""
    ds = _make_dataset(eval_db, count=99)
    first = _make_item(eval_db, ds, question="q1", answer="a1")
    _make_item(eval_db, ds, question="q2", answer="a2")

    data = eval_service.delete_item(eval_db, ds.id, first.id, make_user("admin"))

    assert data["deleted_item_id"] == first.id
    assert data["item_count"] == 1
    assert eval_db.get(EvalDatasetItem, first.id) is None
    assert eval_db.get(EvalDataset, ds.id).item_count == 1


def test_delete_item_rejects_cross_dataset(eval_db):
    ds_a = _make_dataset(eval_db, name="A")
    ds_b = _make_dataset(eval_db, name="B")
    item = _make_item(eval_db, ds_a, answer="a1")

    with pytest.raises(NotFoundError):
        eval_service.delete_item(eval_db, ds_b.id, item.id, make_user("admin"))

    assert eval_db.get(EvalDatasetItem, item.id) is not None


def test_delete_item_writes_audit_log(eval_db):
    ds = _make_dataset(eval_db, count=1)
    item = _make_item(eval_db, ds, question="要删掉的问题", answer="答案")

    eval_service.delete_item(eval_db, ds.id, item.id, make_user("admin"))

    log = eval_db.query(OperationLog).filter(OperationLog.action == "delete_eval_item").one()
    assert log.detail["question"] == "要删掉的问题"
    assert log.detail["dataset_id"] == ds.id


# ---------------------------------------------------------------------------
# 服务层：list_datasets 的参考答案覆盖率
# ---------------------------------------------------------------------------

def test_list_datasets_counts_missing_reference(eval_db):
    """空串与纯空白都计入 missing_reference；答案齐全的数据集为 0。"""
    ds_half = _make_dataset(eval_db, name="缺一半", count=3)
    _make_item(eval_db, ds_half, question="q1", answer="")
    _make_item(eval_db, ds_half, question="q2", answer="   ")
    _make_item(eval_db, ds_half, question="q3", answer="有答案")

    ds_full = _make_dataset(eval_db, name="全齐", count=1)
    _make_item(eval_db, ds_full, question="q4", answer="ok")

    data = eval_service.list_datasets(eval_db, 1, 20)
    by_id = {row["id"]: row for row in data["items"]}

    assert by_id[ds_half.id]["missing_reference"] == 2
    assert by_id[ds_full.id]["missing_reference"] == 0


def test_list_datasets_missing_reference_zero_when_empty(eval_db):
    """空数据集不应报错（无样本时聚合结果为空，回落 0）。"""
    ds = _make_dataset(eval_db, name="空集", count=0)

    data = eval_service.list_datasets(eval_db, 1, 20)

    assert data["items"][0]["id"] == ds.id
    assert data["items"][0]["missing_reference"] == 0


# ---------------------------------------------------------------------------
# schema 层：空串 422 vs 显式 null 放行
# ---------------------------------------------------------------------------

def test_schema_rejects_empty_string():
    """空串在 pydantic 层就被拦（min_length=1），与批量导入的 ItemInput 口径一致。"""
    with pytest.raises(ValidationError):
        ItemUpdate(reference_answer="")


def test_schema_allows_explicit_null():
    """显式 null 必须放行到服务层：否则前端只能拿到 422 泛化报错，看不懂原因。"""
    payload = ItemUpdate(reference_answer=None)
    assert payload.model_dump(exclude_unset=True) == {"reference_answer": None}


def test_schema_exclude_unset_omits_absent_fields():
    """未出现的字段不得参与更新（否则覆盖式更新会清空已有值）。"""
    assert ItemUpdate().model_dump(exclude_unset=True) == {}


# ---------------------------------------------------------------------------
# 接口层
# ---------------------------------------------------------------------------

def test_update_item_api_ok(admin_client, monkeypatch):
    monkeypatch.setattr(
        eval_service,
        "update_item",
        lambda db, did, iid, data, user: {"id": iid, "dataset_id": did, "reference_answer": data.reference_answer},
    )
    data = assert_ok(
        admin_client.put(f"{API}/datasets/3/items/57", json={"reference_answer": "差旅住宿上限 500 元/晚"})
    )
    assert data["id"] == 57
    assert data["reference_answer"] == "差旅住宿上限 500 元/晚"


def test_update_item_api_empty_string_422(admin_client):
    """空串 -> 422（schema 层拦截，不落到服务层）。"""
    assert_error(
        admin_client.put(f"{API}/datasets/3/items/57", json={"reference_answer": ""}),
        status=422,
        code=422,
    )


def test_update_item_api_null_reaches_service(admin_client, monkeypatch):
    """显式 null 不是 422：能透传到服务层，由服务层给 400 + 可读原因。"""
    seen: dict = {}

    def fake(db, did, iid, data, user):
        seen.update(data.model_dump(exclude_unset=True))
        raise BizError(400, "参考答案不能置空：留空会导致该样本的 context_recall 无法计算")

    monkeypatch.setattr(eval_service, "update_item", fake)
    assert_error(
        admin_client.put(f"{API}/datasets/3/items/57", json={"reference_answer": None}),
        status=400,
        code=400,
    )
    assert seen == {"reference_answer": None}


def test_update_item_api_requires_admin(user_client):
    """写操作仍仅管理员：普通用户 PUT -> 403。"""
    assert_error(
        user_client.put(f"{API}/datasets/3/items/57", json={"reference_answer": "x"}),
        status=403,
        code=403,
    )


def test_update_item_api_requires_auth(client):
    assert_error(
        client.put(f"{API}/datasets/3/items/57", json={"reference_answer": "x"}),
        status=401,
        code=401,
    )


def test_delete_item_api_ok(admin_client, monkeypatch):
    monkeypatch.setattr(
        eval_service,
        "delete_item",
        lambda db, did, iid, user: {"deleted_item_id": iid, "item_count": 4},
    )
    data = assert_ok(admin_client.delete(f"{API}/datasets/3/items/57"))
    assert data["deleted_item_id"] == 57
    assert data["item_count"] == 4


def test_delete_item_api_requires_admin(user_client):
    assert_error(user_client.delete(f"{API}/datasets/3/items/57"), status=403, code=403)


def test_delete_item_api_requires_auth(client):
    assert_error(client.delete(f"{API}/datasets/3/items/57"), status=401, code=401)


def test_dataset_list_returns_missing_reference(admin_client, monkeypatch):
    """列表接口回显 missing_reference，前端据此提示「待补 N 条」。"""
    monkeypatch.setattr(
        eval_service,
        "list_datasets",
        lambda db, page, size, kb_id=None: {
            "total": 1,
            "items": [{"id": 3, "kb_id": 6, "name": "用户反馈Badcase", "description": None,
                       "item_count": 5, "missing_reference": 5, "created_by": 1,
                       "created_at": "2026-10-24T10:00:00"}],
        },
    )
    data = assert_ok(admin_client.get(f"{API}/datasets?page=1&size=20"))
    assert data["items"][0]["missing_reference"] == 5
