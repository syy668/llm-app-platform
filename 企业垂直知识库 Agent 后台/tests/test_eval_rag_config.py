"""rag_config 增量功能测试（服务层真实逻辑，非路由 mock）。

覆盖：
  1. 不传 rag_config -> create_run 持久化 None，构建的 agent options 为 None（沿用知识库默认参数）；
  2. 传入 rag_config -> 持久化到 eval_runs.rag_config，agent options 透传 top_k；
  3. RunCreate 校验：非法 search_mode / 越界 top_k -> 422；
  4. 删除数据集（保留运行记录）与删除运行记录（级联删得分）。

运行方式：离线（sqlite 内存库，不依赖 MySQL / Redis / LLM）。
"""
import threading

import pytest
from pydantic import ValidationError
from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles

import app.services.eval_service as eval_service
from app.core.database import Base, SessionLocal, engine
from app.models.eval_dataset import EvalDataset, EvalDatasetItem
from app.models.eval_run import EvalRun, EvalRunScore
from app.schemas.eval import RunCreate


@compiles(BigInteger, "sqlite")
def _bigint_as_integer(type_, compiler, **kw):
    """sqlite 方言下把 BIGINT 渲染为 INTEGER：使 BigInteger 主键复用 rowid 自增
    （SQLite 仅 INTEGER PRIMARY KEY 支持 rowid 别名自增，仅影响测试库建表）。"""
    return "INTEGER"


@pytest.fixture()
def db_session():
    """内存库 + 评测四表 + 最小数据集（1 个知识库、1 条样本）。"""
    eval_service._tables_ready = False  # 触发 ensure_eval_tables 全流程（含补列迁移）
    Base.metadata.create_all(bind=engine, checkfirst=True)

    from app.models.knowledge_base import KnowledgeBase

    db = SessionLocal()
    try:
        # 知识库配置：top_k=3（用于验证 rag_config.top_k 覆盖它）
        kb = KnowledgeBase(
            id=999, name="测试知识库", owner_id=1, embedding_model="mock",
            chunk_size=500, chunk_overlap=50, top_k=3, rerank_threshold=0.0,
            status="ready", doc_count=0,
        )
        db.add(kb)
        ds = EvalDataset(kb_id=999, name="rag_config 测试集", item_count=1, created_by=1)
        db.add(ds)
        db.flush()
        db.add(EvalDatasetItem(dataset_id=ds.id, question="测试问题", reference_answer="测试答案"))
        db.commit()
        yield db
    finally:
        db.rollback()
        db.close()
        Base.metadata.drop_all(bind=engine)


def _make_admin():
    from app.api.deps import CurrentUser

    return CurrentUser(id=1, username="admin", display_name=None, role="admin", status="active")


# ---------- 1) 不传 rag_config ----------

def test_create_run_without_rag_config(db_session, monkeypatch):
    """不传 rag_config：任务正常创建，rag_config=None，options=None（知识库默认参数）。"""
    # 拦截后台线程，避免真实评测执行
    monkeypatch.setattr(eval_service, "_run_eval_background", lambda run_id: None)

    run_dict = eval_service.create_run(db_session, _make_admin(), RunCreate(dataset_id=1))
    assert run_dict["rag_config"] is None

    row = db_session.get(EvalRun, run_dict["id"])
    assert row.rag_config is None
    assert eval_service._build_agent_options(row.rag_config) is None


# ---------- 2) 传入 rag_config ----------

class _FakeEvaluator:
    """离线打桩：RagasEvaluator 构建需真实 LLM/Embedding，测试中替换。"""

    def evaluate_one(self, question, answer, contexts, reference_answer):
        return {
            "faithfulness": 0.9,
            "answer_relevancy": 0.8,
            "context_precision": 0.7,
            "context_recall": 0.6,
        }


@pytest.fixture()
def fake_rag_stack(monkeypatch):
    """替换问答/检索/RAGAS 三处外部依赖，专注验证 rag_config 透传逻辑。"""
    captured: dict = {}

    def fake_run_agent(messages, question, kb_id=None, trace_context=None, options=None):
        captured["options"] = options
        return {"answer": "测试回答", "route": "rag"}

    def fake_hybrid_retrieve(db, kb, question, top_k, search_mode=None):
        captured["top_k"] = top_k  # 验证召回条数是否使用 rag_config.top_k
        captured["search_mode"] = search_mode  # 【新增】验证检索模式是否透传到召回
        return [{"content": "召回片段"}]

    monkeypatch.setattr(eval_service, "run_agent", fake_run_agent)
    monkeypatch.setattr(eval_service.retriever, "hybrid_retrieve", fake_hybrid_retrieve)
    monkeypatch.setattr(eval_service, "RagasEvaluator", _FakeEvaluator)
    return captured


def test_create_run_with_rag_config_persisted(db_session, monkeypatch):
    """传入 rag_config：持久化到 eval_runs，RunOut 返回该配置。"""
    monkeypatch.setattr(eval_service, "_run_eval_background", lambda run_id: None)

    run_dict = eval_service.create_run(
        db_session, _make_admin(),
        RunCreate(dataset_id=1, rag_config={"chunk_size": 800, "search_mode": "bm25_vector_hybrid", "top_k": 8}),
    )
    assert run_dict["rag_config"] == {"chunk_size": 800, "search_mode": "bm25_vector_hybrid", "top_k": 8}

    row = db_session.get(EvalRun, run_dict["id"])
    assert row.rag_config["top_k"] == 8


def test_agent_options_passthrough_top_k(db_session, fake_rag_stack):
    """后台执行时 rag_config.top_k 作为请求级参数透传 run_agent 与召回。"""
    run_dict = eval_service.create_run(
        db_session, _make_admin(), RunCreate(dataset_id=1, rag_config={"top_k": 8})
    )
    run_id = run_dict["id"]

    # 同线程内直接执行后台评测主流程（不经 threading.Thread）
    eval_service._run_eval_background(run_id)

    assert fake_rag_stack["options"] == {"top_k": 8}
    assert fake_rag_stack["top_k"] == 8  # 打分召回与问答检索口径一致
    row = db_session.get(EvalRun, run_id)
    assert row.status == "done"
    scores = db_session.query(EvalRunScore).filter(EvalRunScore.run_id == run_id).all()
    assert len(scores) == 1


def test_agent_options_passthrough_search_mode(db_session, fake_rag_stack):
    """【新增】rag_config.search_mode 透传：run_agent options 与打分召回均收到 vector_only。"""
    run_dict = eval_service.create_run(
        db_session, _make_admin(),
        RunCreate(dataset_id=1, rag_config={"top_k": 8, "search_mode": "vector_only"}),
    )
    eval_service._run_eval_background(run_dict["id"])

    assert fake_rag_stack["options"] == {"top_k": 8, "search_mode": "vector_only"}
    assert fake_rag_stack["search_mode"] == "vector_only"
    row = db_session.get(EvalRun, run_dict["id"])
    assert row.status == "done"


def test_agent_options_none_keeps_kb_default(db_session, fake_rag_stack):
    """不传 rag_config：run_agent 收到 options=None，问答/召回回退知识库 top_k=3。"""
    run_dict = eval_service.create_run(db_session, _make_admin(), RunCreate(dataset_id=1))
    eval_service._run_eval_background(run_dict["id"])

    assert fake_rag_stack["options"] is None
    assert fake_rag_stack["top_k"] == 3  # 知识库自身配置


def test_background_uses_new_session_thread(db_session, monkeypatch):
    """create_run 启动的后台线程拿到的 target 即当前（可被 monkeypatch 的）函数。"""
    started = threading.Event()
    monkeypatch.setattr(eval_service, "_run_eval_background", lambda run_id: started.set())
    eval_service.create_run(db_session, _make_admin(), RunCreate(dataset_id=1))
    assert started.wait(timeout=2)


# ---------- 3) Pydantic 校验 ----------

def test_rag_config_invalid_search_mode_rejected():
    with pytest.raises(ValidationError):
        RunCreate(dataset_id=1, rag_config={"search_mode": "keyword_only"})


def test_rag_config_top_k_out_of_range_rejected():
    with pytest.raises(ValidationError):
        RunCreate(dataset_id=1, rag_config={"top_k": 0})


def test_rag_config_partial_allowed():
    """只传部分键也合法（全部 Optional）。"""
    payload = RunCreate(dataset_id=1, rag_config={"top_k": 8})
    assert payload.rag_config.model_dump(exclude_none=True) == {"top_k": 8}


# ---------- 4) 删除接口（可选1） ----------

def test_delete_dataset_keeps_runs(db_session, monkeypatch):
    """删除数据集：级联删样本，运行记录保留。"""
    monkeypatch.setattr(eval_service, "_run_eval_background", lambda run_id: None)
    run_dict = eval_service.create_run(db_session, _make_admin(), RunCreate(dataset_id=1))

    result = eval_service.delete_dataset(db_session, 1)
    assert result == {"deleted_dataset_id": 1, "deleted_items": 1}
    assert db_session.get(EvalDataset, 1) is None
    assert db_session.get(EvalRun, run_dict["id"]) is not None  # 运行记录保留


def test_delete_run_cascades_scores(db_session, fake_rag_stack):
    """删除运行记录：级联删除该 run 全部得分明细。"""
    run_dict = eval_service.create_run(db_session, _make_admin(), RunCreate(dataset_id=1))
    eval_service._run_eval_background(run_dict["id"])
    assert db_session.query(EvalRunScore).filter(EvalRunScore.run_id == run_dict["id"]).count() == 1

    result = eval_service.delete_run(db_session, run_dict["id"])
    assert result["deleted_scores"] == 1
    assert db_session.get(EvalRun, run_dict["id"]) is None


def test_delete_run_not_found(db_session):
    from app.core.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        eval_service.delete_run(db_session, 424242)
