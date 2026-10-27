"""v1.18 知识库列表权限（方案 A，OR + IN 子查询）回归。

背景：list_kbs 此前只认 owner_id，漏认 kb_permissions 授权 —— 导致「管理员给
普通用户授权后，用户在列表里看不见该库」的割裂 Bug（鉴权接口认授权、列表不认）。
本文件把 list_kbs 当作纯服务函数，用真实 SQLAlchemy 会话（sqlite_engine）跑通
其 OR + IN 子查询 SQL，不 mock 任何逻辑。SQL 方言无关，真实 MySQL 管线同样适用。

覆盖（5 例，对应拍板方案）：
  - 授权可见：普通用户 A 自建 1 库 + 被授权 1 库 → 列表 total=2 且两库都在；
  - 反向隐藏：未授权用户 C 库列表为空（total=0）；
  - owner 回归：A 仅自建 2 库、无任何授权 → 只见到这 2 库；
  - 不重复计数：A 自建的库再被冗余授予 write → OR 不产生行放大，total=1；
  - admin 全量：管理员列表绕过过滤，看到全部（自建 + 他人 + 已授权）。
"""
import pytest
from sqlalchemy.orm import sessionmaker

from app.api.deps import CurrentUser
from app.models import KBPermission, KnowledgeBase
from app.services import kb_service


def _user(uid: int, role: str = "user") -> CurrentUser:
    """构造带指定 id 的登录用户（make_user 仅支持 1/2，这里放开任意 id）。"""
    return CurrentUser(id=uid, username=f"u{uid}", display_name=f"U{uid}", role=role, status="active")


def _mk_kb(db, owner_id: int, name: str) -> KnowledgeBase:
    kb = KnowledgeBase(
        name=name,
        description=None,
        owner_id=owner_id,
        embedding_model="default",
        chunk_size=500,
        chunk_overlap=50,
        top_k=5,
        rerank_threshold=0.0,
        status="active",
        doc_count=0,
    )
    db.add(kb)
    db.commit()
    db.refresh(kb)
    return kb


def _grant(db, kb_id: int, user_id: int, permission: str = "read") -> None:
    perm = KBPermission(kb_id=kb_id, user_id=user_id, permission=permission, granted_by=1)
    db.add(perm)
    db.commit()


@pytest.fixture
def kb_db(sqlite_engine):
    """建 knowledge_bases + kb_permissions 两张表，返回真实会话（用例结束关闭）。"""
    KnowledgeBase.__table__.create(sqlite_engine)
    KBPermission.__table__.create(sqlite_engine)
    db = sessionmaker(bind=sqlite_engine)()
    yield db
    db.close()


# ---------------------------------------------------------------------------
# 1) 授权可见：自建 1 + 被授权 1 → total=2
# ---------------------------------------------------------------------------
def test_list_kbs_granted_visible(kb_db):
    admin = _user(1, "admin")
    kb_admin = _mk_kb(kb_db, owner_id=1, name="管理员库")      # 管理员自建
    kb_a_own = _mk_kb(kb_db, owner_id=2, name="A自建库")        # A 自建
    _grant(kb_db, kb_admin.id, user_id=2, permission="read")    # 授权 A 读管理员库

    data = kb_service.list_kbs(kb_db, _user(2, "user"), 1, 100)
    ids = {k["id"] for k in data["items"]}
    assert data["total"] == 2
    assert kb_a_own.id in ids
    assert kb_admin.id in ids
    # 列表 DTO 新增 is_shared：自建库=False，被授权库=True
    by_id = {k["id"]: k for k in data["items"]}
    assert by_id[kb_a_own.id]["is_shared"] is False
    assert by_id[kb_admin.id]["is_shared"] is True


# ---------------------------------------------------------------------------
# 2) 反向：未授权用户 C 列表为空
# ---------------------------------------------------------------------------
def test_list_kbs_unauthorized_hidden(kb_db):
    _mk_kb(kb_db, owner_id=1, name="管理员库")     # 不属于 C
    _mk_kb(kb_db, owner_id=2, name="A自建库")       # 不属于 C

    data = kb_service.list_kbs(kb_db, _user(3, "user"), 1, 100)
    assert data["total"] == 0
    assert data["items"] == []


# ---------------------------------------------------------------------------
# 3) owner 回归：仅自建、无授权 → 只见到自建库
# ---------------------------------------------------------------------------
def test_list_kbs_owner_only_regression(kb_db):
    kb1 = _mk_kb(kb_db, owner_id=2, name="A库1")
    kb2 = _mk_kb(kb_db, owner_id=2, name="A库2")
    _mk_kb(kb_db, owner_id=1, name="管理员库")      # A 无权

    data = kb_service.list_kbs(kb_db, _user(2, "user"), 1, 100)
    ids = {k["id"] for k in data["items"]}
    assert data["total"] == 2
    assert ids == {kb1.id, kb2.id}


# ---------------------------------------------------------------------------
# 4) 不重复计数：自建库再被冗余授予 write → OR 不产生行放大
# ---------------------------------------------------------------------------
def test_list_kbs_no_duplicate_when_owned_and_granted(kb_db):
    kb_a = _mk_kb(kb_db, owner_id=2, name="A自建库")
    _grant(kb_db, kb_a.id, user_id=2, permission="write")   # 冗余授权（A 本就是 owner）

    data = kb_service.list_kbs(kb_db, _user(2, "user"), 1, 100)
    assert data["total"] == 1
    assert data["items"][0]["id"] == kb_a.id


# ---------------------------------------------------------------------------
# 5) admin 全量回归：绕过过滤，看到全部
# ---------------------------------------------------------------------------
def test_list_kbs_admin_sees_all(kb_db):
    kb_admin1 = _mk_kb(kb_db, owner_id=1, name="管理员库1")
    kb_admin2 = _mk_kb(kb_db, owner_id=1, name="管理员库2")
    kb_a = _mk_kb(kb_db, owner_id=2, name="A自建库")
    _grant(kb_db, kb_admin1.id, user_id=2, permission="read")  # 让 A 也被授权一个

    data = kb_service.list_kbs(kb_db, _user(1, "admin"), 1, 100)
    ids = {k["id"] for k in data["items"]}
    assert data["total"] == 3
    assert ids == {kb_admin1.id, kb_admin2.id, kb_a.id}
