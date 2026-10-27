"""评测「静默空分」护栏测试（v1.17）。

## 背景：真实故障，不是假设

真实库 run 63 / 64 / 65 三连：进度条正常走（28/30、19/30），得分记录也落库了，
但**每一条的四项指标都是 NULL**。首页汇总列全是「—」，明细表全是「-」。

根因链条：
  1. 判分模型配成了 `qwen3.7-flash-2026-07-15`（带日期后缀），上游只允许免费额度访问；
  2. 额度耗尽后每次判分调用返回 403 AllocationQuota.FreeTierOnly；
  3. ragas 把每个指标的异常吞成 NaN，`evaluate_one` 再把 NaN 翻成 None；
  4. `handle()` 只看 kind=="ok" 就 `success += 1`，于是「一条也没算出来」被记成成功；
  5. 前端 `if (scoresMap[id]) return` 里空数组是 truthy，一次空拉取就永久锁死「暂无得分」。

最终观感：任务"成功"、指标"暂无"、钱和算力全白花，界面上没有任何一处指向故障。

## 本文件锁死六件事

1. 403（额度/未开通）必须翻译成能指导动作的文案，且不能被 402/429 抢走；
2. `RagasScoringUnavailable` 是**不可重试**异常（重试只会把同一条 403 再打一遍）；
3. 探活失败 → run 立刻 failed + 写明原因，**一条样本都不跑**；
4. 四项指标全空 → 抛异常，不允许伪装成「成功但无分」；
5. 连续 3 条失败且零成功 → 熔断中止，不让 30 个样本白白烧完；
6. 熔断不误杀：一旦有过成功样本，后续失败不再中止（判分链路是通的，跑完仍有价值）。
"""
from datetime import datetime, timedelta

import pytest

import app.services.eval_service as eval_service
from app.config import settings
from app.core.database import Base, SessionLocal, engine
from app.core.llm_errors import llm_error_hint
from app.eval.metrics import RagasScoringUnavailable
from app.models.eval_dataset import EvalDataset, EvalDatasetItem
from app.models.eval_run import RUN_DONE, RUN_FAILED, EvalRun, EvalRunScore
from app.schemas.eval import RunCreate

N_ITEMS = 5
_FAKE = {
    "faithfulness": 0.8,
    "answer_relevancy": 0.6,
    "context_precision": 0.7,
    "context_recall": 0.9,
}
_REAL_BG = eval_service._run_eval_background


# ---------------------------------------------------------------------------
# 403 文案：额度类错误必须能和余额不足区分开
# ---------------------------------------------------------------------------

class _FakeHTTPError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def test_403_quota_denied_has_actionable_hint():
    """403（免费额度用尽/未开通）不能落到「余额不足」或「稍后重试」。"""
    exc = _FakeHTTPError(
        403,
        "Free quota exhausted. To continue accessing the model on a paid basis, "
        "please add funds or disable the use free tier only mode.",
    )
    hint = llm_error_hint(exc)
    assert "403" in hint
    assert "额度" in hint
    # 必须给出动作：换模型名 或 开通付费额度
    assert "模型名" in hint or "开通" in hint


def test_403_text_without_status_code_still_not_mistaken_for_402():
    """没有 status_code 的变体：文本同时含 quota/insufficient，仍要走 403 分支。"""

    class _Bare(Exception):
        pass

    hint = llm_error_hint(_Bare("Error code: 403 - AllocationQuota.FreeTierOnly"))
    assert "403" in hint


def test_402_still_maps_to_insufficient():
    """回归：402 不能被新加的 403 分支抢走。"""
    hint = llm_error_hint(_FakeHTTPError(402, "account balance is insufficient"))
    assert "余额不足" in hint


# ---------------------------------------------------------------------------
# 异常语义：不可重试
# ---------------------------------------------------------------------------

def test_scoring_unavailable_is_not_retryable():
    assert RagasScoringUnavailable.retryable is False


def test_sample_with_scoring_unavailable_is_not_retried(monkeypatch):
    """配置性故障只允许调用一次：重试 3 次毫无意义且拖慢失败的暴露。"""
    calls = {"n": 0}

    def boom(item, ctx):
        calls["n"] += 1
        raise RagasScoringUnavailable(
            f"判分模型不可用（qwen3.7-flash-2026-07-15）：{llm_error_hint(_FakeHTTPError(403, 'quota'))}"
        )

    monkeypatch.setattr(eval_service, "_eval_one_sample_sync", boom)
    ctx = {"run_id": 1, "kb": None, "retry_attempts": 3, "evaluator": None}
    kind, payload = eval_service._eval_sample_with_retry(
        eval_service._SampleSpec(id=7, question="问题", reference_answer="答案"), ctx
    )

    assert calls["n"] == 1, "配置性故障不得重试"
    assert kind == "failed"
    assert "status_code" not in payload and "sk-" not in payload, "不得外泄原始异常/密钥"
    assert "item#7" in payload


def test_ordinary_exception_still_retries(monkeypatch):
    """回归：普通异常仍按 EVAL_RETRY_ATTEMPTS 重试。"""
    calls = {"n": 0}

    def flaky(item, ctx):
        calls["n"] += 1
        raise RuntimeError("上游偶发超时")

    monkeypatch.setattr(eval_service, "_eval_one_sample_sync", flaky)
    monkeypatch.setattr(eval_service.time, "sleep", lambda s: None)
    ctx = {"run_id": 1, "kb": None, "retry_attempts": 3, "evaluator": None}
    kind, _ = eval_service._eval_sample_with_retry(
        eval_service._SampleSpec(id=8, question="问题", reference_answer="答案"), ctx
    )

    assert calls["n"] == 3
    assert kind == "failed"


# ---------------------------------------------------------------------------
# 数据环境：数据集 + 内存四表，外部依赖全部 fake
# ---------------------------------------------------------------------------

@pytest.fixture()
def env(monkeypatch):
    eval_service._tables_ready = False
    Base.metadata.create_all(bind=engine, checkfirst=True)

    from app.models.knowledge_base import KnowledgeBase

    db = SessionLocal()
    try:
        db.merge(KnowledgeBase(
            id=997, name="判分护栏测试库", owner_id=1, embedding_model="mock",
            chunk_size=500, chunk_overlap=50, top_k=3, rerank_threshold=0.0,
            status="ready", doc_count=0,
        ))
        ds = EvalDataset(kb_id=997, name="判分护栏测试集", item_count=N_ITEMS, created_by=1)
        db.add(ds)
        db.flush()
        dataset_id = ds.id
        for i in range(N_ITEMS):
            db.add(EvalDatasetItem(
                dataset_id=dataset_id, question=f"问题{i}", reference_answer=f"答案{i}",
            ))
        db.commit()
        yield {"dataset_id": dataset_id}
    finally:
        db.rollback()
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session(env):
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


class _KeepOpenSession:
    """屏蔽服务内部的 db.close()，避免用例手里的会话变 detached。"""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):
        pass


@pytest.fixture()
def patched_session(db_session, monkeypatch):
    monkeypatch.setattr(eval_service, "SessionLocal", lambda: _KeepOpenSession(db_session))
    return db_session


def _admin():
    from app.api.deps import CurrentUser

    return CurrentUser(id=1, username="admin", display_name=None, role="admin", status="active")


def _start(env, monkeypatch, db, **kwargs):
    monkeypatch.setattr(eval_service, "_run_eval_background", lambda run_id: None)
    data = eval_service.create_run(db, _admin(), RunCreate(dataset_id=env["dataset_id"], **kwargs))
    return data["id"]


def _score_row(item, run_id):
    return {
        "run_id": run_id,
        "item_id": item.id,
        "question": item.question,
        "answer": f"回答[{item.question}]",
        "retrieved_contexts": [f"召回[{item.question}]"],
        "latency_ms": 3,
        **_FAKE,
    }


# ---------------------------------------------------------------------------
# 探活：失败必须在第一个样本之前拦住
# ---------------------------------------------------------------------------

class _FakeEvaluator:
    """可配置的判分器替身：控制 preflight 与单样本产出。"""

    preflight_calls = 0
    evaluate_calls = 0
    raise_on_preflight: Exception | None = None
    raise_on_evaluate: Exception | None = None

    def preflight(self, *, force: bool = False):
        type(self).preflight_calls += 1
        if self.raise_on_preflight is not None:
            raise self.raise_on_preflight

    def evaluate_one(self, question, answer, contexts, reference):
        type(self).evaluate_calls += 1
        if self.raise_on_evaluate is not None:
            raise self.raise_on_evaluate
        return dict(_FAKE)


@pytest.fixture()
def fake_evaluator(monkeypatch):
    _FakeEvaluator.preflight_calls = 0
    _FakeEvaluator.evaluate_calls = 0
    _FakeEvaluator.raise_on_preflight = None
    _FakeEvaluator.raise_on_evaluate = None
    monkeypatch.setattr(eval_service, "RagasEvaluator", _FakeEvaluator)
    monkeypatch.setattr(settings, "eval_concurrent_enable", False)
    return _FakeEvaluator


def test_preflight_failure_fails_run_without_touching_any_sample(
    env, patched_session, monkeypatch, fake_evaluator
):
    """判分链路不可用：run 立即 failed + 写明原因，样本一条都不跑。"""
    monkeypatch.setattr(settings, "eval_preflight_enable", True)
    fake_evaluator.raise_on_preflight = RagasScoringUnavailable(
        "判分模型不可用（qwen3.7-flash-2026-07-15）：该模型的可用额度已用尽或未开通（HTTP 403）"
    )
    ran = {"n": 0}

    def should_not_run(item, ctx):
        ran["n"] += 1
        return ("ok", _score_row(item, ctx["run_id"]))

    monkeypatch.setattr(eval_service, "_eval_sample_with_retry", should_not_run)

    run_id = _start(env, monkeypatch, patched_session)
    _REAL_BG(run_id)

    patched_session.expire_all()
    run = patched_session.get(EvalRun, run_id)

    assert run.status == RUN_FAILED
    assert ran["n"] == 0, "探活失败时不得跑任何样本（否则就是白烧检索+生成）"
    assert run.processed_items == 0
    assert run.metrics is None
    assert fake_evaluator.evaluate_calls == 0
    assert "评测未启动" in run.error_msg and "403" in run.error_msg


def test_preflight_disabled_by_config_skips_probe(
    env, patched_session, monkeypatch, fake_evaluator
):
    """EVAL_PREFLIGHT_ENABLE=false 时不探活（离线/无额度联调场景），样本照常跑。"""
    monkeypatch.setattr(settings, "eval_preflight_enable", False)
    monkeypatch.setattr(
        eval_service, "_eval_sample_with_retry",
        lambda item, ctx: ("ok", _score_row(item, ctx["run_id"])),
    )
    run_id = _start(env, monkeypatch, patched_session)
    _REAL_BG(run_id)

    patched_session.expire_all()
    run = patched_session.get(EvalRun, run_id)
    assert fake_evaluator.preflight_calls == 0
    assert run.status == RUN_DONE
    assert run.success_items == N_ITEMS


# ---------------------------------------------------------------------------
# 四项全空：不允许伪装成功
# ---------------------------------------------------------------------------

def test_all_null_scores_raise_instead_of_passing_as_success():
    """`RagasEvaluator.evaluate_one` 四项全空 → 抛异常（不返回全 None 的 dict）。

    这里直接测真实类的判定逻辑：把 ragas 的 `evaluate` 换成一个返回全 NaN 的假结果，
    模拟「判分调用全部 403 被吞」的真实形态。
    """
    import pandas as pd

    from app.eval import metrics as metrics_mod

    ev = metrics_mod.RagasEvaluator.__new__(metrics_mod.RagasEvaluator)  # 跳过构造（不建 LLM）
    ev.metrics = []
    ev._preflight_passed = True
    ev._judge_model = _NoopJudge()

    class _FakeResult:
        def to_pandas(self):
            return pd.DataFrame([{
                "faithfulness": float("nan"),
                "answer_relevancy": float("nan"),
                "context_precision": float("nan"),
                "context_recall": float("nan"),
            }])

    monkeypatch_target = metrics_mod.evaluate
    try:
        metrics_mod.evaluate = lambda **kwargs: _FakeResult()
        with pytest.raises(RagasScoringUnavailable) as ei:
            ev.evaluate_one("问", "答", ["ctx"], "参考")
    finally:
        metrics_mod.evaluate = monkeypatch_target

    assert ei.value.retryable is False
    assert "四项指标均未产出" in str(ei.value)


class _NoopJudge:
    """探针替身：run 中途重探时返回正常，用来证明「异常文案来自探活」的路径可跑。"""

    def invoke(self, *a, **kw):
        return type("M", (), {"content": "ok"})()


def test_partial_null_scores_are_kept():
    """只有一两项为空（如 contexts 为空只打穿 faithfulness）属正常，不得抛异常。"""
    import pandas as pd

    from app.eval import metrics as metrics_mod

    ev = metrics_mod.RagasEvaluator.__new__(metrics_mod.RagasEvaluator)
    ev.metrics = []
    ev._preflight_passed = True
    ev._judge_model = _NoopJudge()

    class _FakeResult:
        def to_pandas(self):
            return pd.DataFrame([{
                "faithfulness": float("nan"),
                "answer_relevancy": 0.5,
                "context_precision": float("nan"),
                "context_recall": 0.9,
            }])

    original = metrics_mod.evaluate
    try:
        metrics_mod.evaluate = lambda **kwargs: _FakeResult()
        out = ev.evaluate_one("问", "答", [], "参考")
    finally:
        metrics_mod.evaluate = original

    assert out["faithfulness"] is None
    assert out["answer_relevancy"] == 0.5
    assert out["context_recall"] == 0.9


# ---------------------------------------------------------------------------
# 熔断：不让 30 个样本白烧
# ---------------------------------------------------------------------------

def test_consecutive_failures_abort_run(env, patched_session, monkeypatch, fake_evaluator):
    """连续 3 条失败且零成功 → 熔断中止，剩下一半样本不再执行。"""
    ran = {"n": 0}

    def always_fail(item, ctx):
        ran["n"] += 1
        return (
            "failed",
            f"item#{item.id}({item.question[:30]}): 判分模型不可用（403 额度用尽）",
        )

    monkeypatch.setattr(eval_service, "_eval_sample_with_retry", always_fail)
    run_id = _start(env, monkeypatch, patched_session)
    _REAL_BG(run_id)

    patched_session.expire_all()
    run = patched_session.get(EvalRun, run_id)

    assert run.status == RUN_FAILED
    assert ran["n"] == eval_service._EVAL_ABORT_CONSECUTIVE_FAILURES, (
        f"只应跑到熔断阈值就停，实际跑了 {ran['n']} 条（共 {N_ITEMS} 条）"
    )
    assert run.success_items == 0
    assert run.failed_items == eval_service._EVAL_ABORT_CONSECUTIVE_FAILURES
    assert run.metrics is None, "零成功时 metrics 必须是 None，不能写全 null 的 dict"
    assert "熔断中止" in run.error_msg


def test_abort_does_not_fire_after_a_success(env, patched_session, monkeypatch, fake_evaluator):
    """有过成功样本后，后续连续失败不再熔断（判分链路是通的，跑完对用户仍有价值）。"""
    ran = {"n": 0}

    def first_ok_then_fail(item, ctx):
        ran["n"] += 1
        if ran["n"] == 1:
            return ("ok", _score_row(item, ctx["run_id"]))
        return ("failed", f"item#{item.id}: 上游偶发超时")

    monkeypatch.setattr(eval_service, "_eval_sample_with_retry", first_ok_then_fail)
    run_id = _start(env, monkeypatch, patched_session)
    _REAL_BG(run_id)

    patched_session.expire_all()
    run = patched_session.get(EvalRun, run_id)

    assert ran["n"] == N_ITEMS, "不应熔断，必须跑完全部样本"
    assert run.status == RUN_DONE, "部分失败且有过成功 → done + error_msg（原语义不变）"
    assert run.success_items == 1
    assert run.failed_items == N_ITEMS - 1
    assert run.metrics is not None and run.metrics["faithfulness"] == pytest.approx(0.8)
    assert "条样本评测失败" in run.error_msg


def test_all_null_rows_never_count_as_success(env, patched_session, monkeypatch, fake_evaluator):
    """端到端：判分恒抛 → 得分表 0 条、success 0、status=failed、原因可读。

    这一条直接对应真实故障的观感：以前是 done + 30 条全 NULL 得分，
    现在是 failed + 0 条得分 + 指得出原因的文案。
    """
    fake_evaluator.raise_on_evaluate = RagasScoringUnavailable(
        "判分模型不可用（qwen3.7-flash-2026-07-15）：该模型的可用额度已用尽或未开通（HTTP 403）"
    )

    def via_real_eval(item, ctx):
        # 用真实 _eval_one_sample_sync 的分支逻辑：让 evaluate_one 抛，走不可重试路径
        try:
            ctx["evaluator"].evaluate_one(item.question, "答", ["ctx"], item.reference_answer)
        except RagasScoringUnavailable as exc:
            return ("failed", f"item#{item.id}({item.question[:30]}): {exc}")
        return ("ok", _score_row(item, ctx["run_id"]))

    monkeypatch.setattr(eval_service, "_eval_sample_with_retry", via_real_eval)
    run_id = _start(env, monkeypatch, patched_session)
    _REAL_BG(run_id)

    patched_session.expire_all()
    run = patched_session.get(EvalRun, run_id)
    rows = patched_session.query(EvalRunScore).filter(EvalRunScore.run_id == run_id).all()

    assert run.status == RUN_FAILED
    assert rows == [], "没有任何有效得分时不得写入全 NULL 的得分行"
    assert run.success_items == 0
    assert run.metrics is None
    assert "403" in run.error_msg
