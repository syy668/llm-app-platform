"""评测并发性能改造测试（离线，sqlite 内存库）。

覆盖：
  1. 串行 / 并发两种模式下评测分数完全一致（同一批确定性 fake，逐行对比）；
  2. 并发上限生效：fake run_agent 记录峰值并发数 == EVAL_MAX_CONCURRENCY；
  3. 异常保护：单样本失败不打断整体任务，失败明细写入 run.error_msg，自动重试生效；
  4. 实时进度上报：每完成一个样本 success_items 被更新（调用序列递增）；
  5. 批量落库：得分按 EVAL_SCORE_BATCH_SIZE 分批 flush，最终行数完整无误；
  6. Trace 批量落库语义：TraceCallbackHandler 内存暂存 + flush 一次性写入
     trace_logs / trace_tool_calls，内容完整不丢失。

运行方式：离线（不依赖 MySQL / Redis / LLM）。
"""
import threading
import time

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles

import app.services.eval_service as eval_service
from app.config import settings
from app.core.database import Base, SessionLocal, engine
from app.models.eval_dataset import EvalDataset, EvalDatasetItem
from app.models.eval_run import EvalRun, EvalRunScore
from app.schemas.eval import RunCreate


@compiles(BigInteger, "sqlite")
def _bigint_as_integer(type_, compiler, **kw):
    """sqlite 方言下把 BIGINT 渲染为 INTEGER（BigInteger 主键自增兼容）。"""
    return "INTEGER"


N_ITEMS = 6

# 确定性指标：两模式对比要求分数完全一致，fake RAGAS 按题目序号恒定输出
_FAKE_SCORES = {
    "faithfulness": 0.8,
    "answer_relevancy": 0.6,
    "context_precision": 0.7,
    "context_recall": 0.9,
}


@pytest.fixture()
def env6(monkeypatch):
    """内存库 + 评测四表 + 6 条样本数据集；替换问答/检索/RAGAS 三处外部依赖。"""
    eval_service._tables_ready = False
    Base.metadata.create_all(bind=engine, checkfirst=True)

    from app.models.knowledge_base import KnowledgeBase

    db = SessionLocal()
    try:
        kb = KnowledgeBase(
            id=999, name="并发测试知识库", owner_id=1, embedding_model="mock",
            chunk_size=500, chunk_overlap=50, top_k=3, rerank_threshold=0.0,
            status="ready", doc_count=0,
        )
        db.add(kb)
        ds = EvalDataset(kb_id=999, name="并发测试集", item_count=N_ITEMS, created_by=1)
        db.add(ds)
        db.flush()
        for i in range(N_ITEMS):
            db.add(EvalDatasetItem(
                dataset_id=ds.id, question=f"问题{i}", reference_answer=f"答案{i}",
            ))
        db.commit()

        state: dict = {"options": [], "peak": 0, "active": 0, "fail_question": None, "attempts": []}
        lock = threading.Lock()

        def fake_run_agent(messages, question, kb_id=None, trace_context=None, options=None):
            with lock:
                state["options"].append(options)
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            try:
                time.sleep(0.15)  # 模拟 LLM 耗时，保证并发调度可被观测
                if state["fail_question"] is not None and question == state["fail_question"]:
                    raise RuntimeError("模拟上游 API 异常")
                return {"answer": f"回答[{question}]", "route": "rag"}
            finally:
                with lock:
                    state["active"] -= 1

        def fake_hybrid_retrieve(db, kb, question, top_k, search_mode=None):
            state["last_top_k"] = top_k
            state["last_search_mode"] = search_mode
            return [{"content": f"召回[{question}]"}]

        class _FakeEvaluator:
            def evaluate_one(self, question, answer, contexts, reference_answer):
                return dict(_FAKE_SCORES)

        monkeypatch.setattr(eval_service, "run_agent", fake_run_agent)
        monkeypatch.setattr(eval_service.retriever, "hybrid_retrieve", fake_hybrid_retrieve)
        monkeypatch.setattr(eval_service, "RagasEvaluator", _FakeEvaluator)

        yield state
    finally:
        db.rollback()
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session():
    """与 env6 共用同一内存库的会话（表已由 env6 建好）。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _make_admin():
    from app.api.deps import CurrentUser

    return CurrentUser(id=1, username="admin", display_name=None, role="admin", status="active")


# 模块导入期固化真实后台函数引用（测试内 create_run 前会把它 patch 成 no-op，
# 二次调用时若再从模块属性捕获会拿到桩函数）
_REAL_BG = eval_service._run_eval_background


def _create_run_and_eval(env6, monkeypatch, db, **run_kwargs):
    """建 run（拦截自动线程）→ 同线程执行真实后台主流程 → 返回 run_id。"""
    monkeypatch.setattr(eval_service, "_run_eval_background", lambda run_id: None)
    run_dict = eval_service.create_run(db, _make_admin(), RunCreate(dataset_id=1, **run_kwargs))
    _REAL_BG(run_dict["id"])
    return run_dict["id"]


def _scores(db, run_id):
    rows = db.query(EvalRunScore).filter(EvalRunScore.run_id == run_id).order_by(EvalRunScore.item_id).all()
    return [
        (r.item_id, r.answer, r.retrieved_contexts,
         r.faithfulness, r.answer_relevancy, r.context_precision, r.context_recall)
        for r in rows
    ]


# ---------- 1) 串行 / 并发分数一致性 ----------

def test_serial_and_concurrent_scores_identical(env6, db_session, monkeypatch):
    """并发开/关两种模式：得分逐行完全一致（不破坏 RAGAS 指标计算结果）。"""
    monkeypatch.setattr(settings, "eval_concurrent_enable", False)
    serial_run = _create_run_and_eval(env6, monkeypatch, db_session, rag_config={"top_k": 8})
    serial_rows = _scores(db_session, serial_run)

    monkeypatch.setattr(settings, "eval_concurrent_enable", True)
    monkeypatch.setattr(settings, "eval_max_concurrency", 2)
    conc_run = _create_run_and_eval(env6, monkeypatch, db_session, rag_config={"top_k": 8})
    conc_rows = _scores(db_session, conc_run)

    assert len(serial_rows) == N_ITEMS
    assert serial_rows == conc_rows  # item/answer/contexts/四项指标 全部一致

    # options 透传口径在两种模式下也一致
    assert env6["last_top_k"] == 8


# ---------- 2) 并发上限 ----------

def test_concurrency_respects_max_cap(env6, db_session, monkeypatch):
    """峰值并发数 == EVAL_MAX_CONCURRENCY（既不超限，也确实并发了）。"""
    monkeypatch.setattr(settings, "eval_concurrent_enable", True)
    monkeypatch.setattr(settings, "eval_max_concurrency", 2)
    _create_run_and_eval(env6, monkeypatch, db_session)

    assert env6["peak"] == 2  # 6 个 0.15s 任务 / 2 worker，峰值必然达到且仅 2


def test_concurrency_speedup(env6, db_session, monkeypatch):
    """并发 2 时整体耗时应明显低于串行（约减半）。"""

    def timed(concurrent: bool) -> float:
        monkeypatch.setattr(settings, "eval_concurrent_enable", concurrent)
        monkeypatch.setattr(settings, "eval_max_concurrency", 2)
        t0 = time.time()
        _create_run_and_eval(env6, monkeypatch, db_session)
        return time.time() - t0

    serial_t = timed(False)
    conc_t = timed(True)
    assert conc_t < serial_t * 0.8  # 6 任务×0.15s：串行~0.9s，并发~0.45s


# ---------- 3) 异常保护：失败不打断 + 重试 + 错误明细 ----------

def test_failed_sample_recorded_without_breaking_run(env6, db_session, monkeypatch):
    """1 条样本持续失败：run 正常 done，其余 5 条得分完整，error_msg 记录失败明细。"""
    env6["fail_question"] = "问题3"
    monkeypatch.setattr(settings, "eval_concurrent_enable", True)
    monkeypatch.setattr(settings, "eval_retry_attempts", 2)
    run_id = _create_run_and_eval(env6, monkeypatch, db_session)

    run = db_session.get(EvalRun, run_id)
    assert run.status == "done"
    assert len(_scores(db_session, run_id)) == N_ITEMS - 1
    assert run.error_msg is not None
    assert "1/6" in run.error_msg and "item#" in run.error_msg


def test_retry_then_success(env6, db_session, monkeypatch):
    """首挑失败、重试成功：样本计入成功，error_msg 为空。"""
    real_bg = eval_service._run_eval_background
    monkeypatch.setattr(eval_service, "_run_eval_background", lambda run_id: None)

    # 问题0 第一次调用抛瞬时故障，重试后成功
    inner = eval_service.run_agent

    def run_agent_fail_once(messages, question, kb_id=None, trace_context=None, options=None):
        if question == "问题0" and not env6.get("q0_failed"):
            env6["q0_failed"] = True
            raise RuntimeError("瞬时故障")
        return inner(messages, question, kb_id=kb_id, trace_context=trace_context, options=options)

    monkeypatch.setattr(eval_service, "run_agent", run_agent_fail_once)
    monkeypatch.setattr(settings, "eval_concurrent_enable", True)
    monkeypatch.setattr(settings, "eval_retry_attempts", 2)

    run_dict = eval_service.create_run(db_session, _make_admin(), RunCreate(dataset_id=1))
    _REAL_BG(run_dict["id"])

    run = db_session.get(EvalRun, run_dict["id"])
    assert run.status == "done"
    assert run.success_items == N_ITEMS
    assert run.error_msg is None
    assert len(_scores(db_session, run_dict["id"])) == N_ITEMS


# ---------- 4) 实时进度上报 ----------

def test_progress_reported_incrementally(env6, db_session, monkeypatch):
    """每完成一个样本 success_items 被更新：进度回调序列为 1..N 递增。"""
    seen: list[int] = []
    orig = eval_service._update_progress

    # 【v1.13.1】签名扩为 (db, run_id, success, processed, failed)：进度口径拆细后
    # success 单独看会漏掉跳过/失败的样本，spy 用 *rest 兼容，并顺手锁住 processed 递增。
    def spy_progress(db, run_id, success, *rest):
        seen.append(success)
        orig(db, run_id, success, *rest)

    monkeypatch.setattr(eval_service, "_update_progress", spy_progress)
    monkeypatch.setattr(settings, "eval_concurrent_enable", True)
    monkeypatch.setattr(settings, "eval_max_concurrency", 2)
    run_id = _create_run_and_eval(env6, monkeypatch, db_session)

    assert seen == sorted(seen) and len(seen) == N_ITEMS  # 单调递增、每样本一次
    run = db_session.get(EvalRun, run_id)
    assert run.success_items == N_ITEMS
    assert run.processed_items == N_ITEMS and run.failed_items == 0


# ---------- 5) 批量落库 ----------

def test_scores_batched_and_complete(env6, db_session, monkeypatch):
    """batch_size=2：分 3 批落库，最终 6 行完整无丢失；每批 flush 时行数翻倍可见。"""
    flush_sizes: list[int] = []
    orig_flush = eval_service._flush_scores

    def spy_flush(db, rows):
        flush_sizes.append(len(rows))
        orig_flush(db, rows)

    monkeypatch.setattr(eval_service, "_flush_scores", spy_flush)
    monkeypatch.setattr(settings, "eval_concurrent_enable", True)
    monkeypatch.setattr(settings, "eval_score_batch_size", 2)
    run_id = _create_run_and_eval(env6, monkeypatch, db_session)

    assert [s for s in flush_sizes if s] == [2, 2, 2]  # 6 条 / 阈值 2 = 3 批（收尾空 flush 忽略）
    assert len(_scores(db_session, run_id)) == N_ITEMS


# ---------- 6) Trace 批量落库语义 ----------

def test_trace_handler_buffers_then_batch_writes(db_session):
    """TraceCallbackHandler：采集阶段零写库，flush 一次性写入完整链路（1 log + N calls）。"""
    from app.core.tracing import TraceCallbackHandler, ensure_trace_tables
    from app.models.trace_log import TraceLog
    from app.models.trace_tool_call import TraceToolCall

    ensure_trace_tables()

    handler = TraceCallbackHandler(
        trace_id="trace-test-001", conversation_id=None, user_id=1, kb_id=999, question="测试问题",
    )
    # 模拟一次问答：路由 + rag 工具 + 2 次 LLM 调用
    handler.on_chain_start({"name": "classify"}, {}, run_id="r1", metadata={"langgraph_node": "classify"})
    handler.on_chain_end({"route": "rag"}, run_id="r1")
    handler.on_chain_start(None, {}, run_id="r2", metadata={"langgraph_node": "rag"})
    handler.on_llm_start(None, ["p"], run_id="l1")
    handler.on_llm_end(type("R", (), {"llm_output": {"token_usage": {
        "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}})(), run_id="l1")
    handler.on_chain_end({"tool_output": "检索结果"}, run_id="r2")

    # flush 前库里没有任何记录（内存暂存）
    assert db_session.query(TraceLog).filter(TraceLog.trace_id == "trace-test-001").count() == 0

    handler.flush(status="success")

    logs = db_session.query(TraceLog).filter(TraceLog.trace_id == "trace-test-001").all()
    calls = db_session.query(TraceToolCall).filter(TraceToolCall.trace_id == "trace-test-001").all()
    assert len(logs) == 1
    assert len(calls) == 1
    log = logs[0]
    assert log.agent_route == "rag"
    assert log.llm_call_count == 1
    assert log.total_tokens == 150
    assert log.tool_call_count == 1
    assert log.status == "success"
    assert calls[0].step_name == "rag_tool"
    assert calls[0].tool_output == "检索结果"
    # 内容完整：tool_input 含 question/kb_id
    assert calls[0].tool_input == {"question": "测试问题", "kb_id": 999}

    # 清理 trace 测试数据
    db_session.query(TraceToolCall).filter(TraceToolCall.trace_id == "trace-test-001").delete()
    db_session.query(TraceLog).filter(TraceLog.trace_id == "trace-test-001").delete()
    db_session.commit()
