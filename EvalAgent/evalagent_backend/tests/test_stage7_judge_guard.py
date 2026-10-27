"""第七步：裁判「防假成功」两道防线。

背景：kimi-k3 曾完美穿过旧的探活（只发「回复两个字：就绪」），
真实评测却每条样本都解析不出 RAGAS 要的 JSON，四项指标全空 ——
一整轮跑完才判失败，钱白烧、人也白等。

这里锁两件事：
1. **结构探活**：能聊天 ≠ 能吐结构化 JSON。有内容但解析不出 JSON，保存前就必须拦；
2. **空指标熔断**：连续 N 条全空 = 模型不兼容，立即终止整轮，剩余样本不再烧钱。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import EvalDataset, EvalItem, EvalRun, EvalScore
from app.services import runner as runner_mod
from app.services.judge import (
    STRUCTURED_OUTPUT_UNSUPPORTED,
    JudgeOutcome,
    JudgeUnavailable,
    RagasJudge,
    _extract_json_object,
    _has_probability,
)
from app.services.rag_client import RagAnswer
from app.services.runtime_config import JudgeConfig
from app.services.serialize import run_to_dict

CANDIDATE = JudgeConfig(
    model="probe-model", api_base="http://127.0.0.1:1/v1", api_key="sk-test", embedding_model="emb"
)


def _fake_llm(content: str):
    """伪造 LangchainLLMWrapper：只有 .langchain_llm.invoke 会被 preflight 用到。"""

    class _Inner:
        def invoke(self, _prompt):  # noqa: ANN001, ANN202
            return SimpleNamespace(content=content)

    return SimpleNamespace(langchain_llm=_Inner())


def _fake_embeddings(dim: int = 8, zero: bool = False, boom: str = ""):
    """伪造 LangchainEmbeddingsWrapper（预检要 embed 两句文本）。"""

    class _Emb:
        def embed_documents(self, texts):  # noqa: ANN001, ANN202
            if boom:
                raise RuntimeError(boom)
            vec = [0.0] * dim if zero else [0.01 * (i + 1) for i in range(dim)]
            return [list(vec) for _ in texts]

    return _Emb()


@pytest.fixture()
def probe(monkeypatch):
    """把 _make_clients 换掉，避免真的建 ChatOpenAI 客户端。"""

    def _use(content: str, embeddings=None):
        monkeypatch.setattr(
            RagasJudge,
            "_make_clients",
            staticmethod(
                lambda cfg, counter: (_fake_llm(content), embeddings or _fake_embeddings())
            ),
        )

    return _use


# ============================ 结构探活 ============================
def test_preflight_passes_on_clean_json(probe):
    probe('{"faithfulness": 0.92, "reason": "上下文与回答完全一致"}')
    out = RagasJudge().preflight(CANDIDATE)
    assert "结构化输出正常" in out


def test_preflight_tolerates_markdown_fence_and_chatter(probe):
    """真机上模型爱包围栏、爱加"结果如下"——这些不能算不支持结构化输出。"""
    probe('好的，结果如下：\n```json\n{"faithfulness": 0.8, "reason": "ok"}\n```\n以上。')
    assert "结构化输出正常" in RagasJudge().preflight(CANDIDATE)


def test_preflight_fails_when_content_has_no_json(probe):
    """核心场景：模型能聊天（有内容），但压根不吐 JSON → 必须判定不支持。"""
    probe("好的，我的判断是这段回答完全忠实于上下文，忠实度大约是 0.9。")
    with pytest.raises(JudgeUnavailable) as exc:
        RagasJudge().preflight(CANDIDATE)
    assert STRUCTURED_OUTPUT_UNSUPPORTED in str(exc.value)
    assert "解析不出 JSON" in str(exc.value)


def test_preflight_fails_when_json_has_no_score_field(probe):
    """形似但不是：JSON 解析得出来，却没有 0~1 的分数位，照样拦。"""
    probe('{"answer": "忠实", "detail": {"level": "high"}}')
    with pytest.raises(JudgeUnavailable) as exc:
        RagasJudge().preflight(CANDIDATE)
    assert STRUCTURED_OUTPUT_UNSUPPORTED in str(exc.value)


def test_preflight_fails_on_empty_content(probe):
    """思考型模型把 max_tokens 吃光 → content 为空，文案要指向思考模式而不是"不支持结构化"。"""
    probe("")
    with pytest.raises(JudgeUnavailable) as exc:
        RagasJudge().preflight(CANDIDATE)
    assert "空 content" in str(exc.value)
    assert STRUCTURED_OUTPUT_UNSUPPORTED not in str(exc.value)


def test_preflight_fails_on_transport_error(monkeypatch):
    class _Boom:
        def invoke(self, _prompt):  # noqa: ANN001, ANN202
            raise RuntimeError("401 invalid_api_key")

    monkeypatch.setattr(
        RagasJudge,
        "_make_clients",
        staticmethod(lambda cfg, counter: (SimpleNamespace(langchain_llm=_Boom()), _fake_embeddings())),
    )
    with pytest.raises(JudgeUnavailable) as exc:
        RagasJudge().preflight(CANDIDATE)
    assert "探活失败" in str(exc.value)


# ============================ 向量模型探活（answer_relevancy 的唯一依赖）============================
def test_preflight_probes_embedding_and_reports_dim(probe):
    probe('{"faithfulness": 0.9, "reason": "ok"}', _fake_embeddings(dim=16))
    out = RagasJudge().preflight(CANDIDATE)
    assert "16 维" in out


def test_preflight_fails_on_zero_vectors(probe):
    """网关会用全零向量"成功"返回，answer_relevancy 会恒为 0 —— 必须拦住。"""
    probe('{"faithfulness": 0.9, "reason": "ok"}', _fake_embeddings(zero=True))
    with pytest.raises(JudgeUnavailable) as exc:
        RagasJudge().preflight(CANDIDATE)
    assert "全零向量" in str(exc.value)


def test_preflight_fails_when_embedding_endpoint_unreachable(probe):
    probe('{"faithfulness": 0.9, "reason": "ok"}', _fake_embeddings(boom="Connection refused"))
    with pytest.raises(JudgeUnavailable) as exc:
        RagasJudge().preflight(CANDIDATE)
    assert "向量模型探活失败" in str(exc.value)
    assert "Connection refused" in str(exc.value)


# ============================ 向量配置的同源回落 ============================
def test_embedding_defaults_to_judge_endpoint():
    cfg = JudgeConfig(
        model="m", api_base="https://dashscope.example/v1", api_key="sk-judge", embedding_model="text-embedding-v3"
    )
    assert cfg.emb_api_base == "https://dashscope.example/v1"
    assert cfg.emb_api_key == "sk-judge"


def test_embedding_can_point_to_another_provider():
    """典型配置：裁判 LLM 在百炼，embedding 走硅基流动的 bge-m3（与项目一检索同模型）。"""
    cfg = JudgeConfig(
        model="qwen3.8-flash",
        api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="sk-dashscope",
        embedding_model="BAAI/bge-m3",
        embedding_api_base="https://api.siliconflow.cn/v1",
        embedding_api_key="sk-siliconflow",
    )
    assert cfg.emb_api_base == "https://api.siliconflow.cn/v1"
    assert cfg.emb_api_key == "sk-siliconflow"


def test_signature_changes_when_only_embedding_endpoint_changes():
    """只改向量服务商也必须触发重建，否则表现为"界面改了没生效"。"""
    a = JudgeConfig(model="m", api_base="b", api_key="k", embedding_model="e")
    b = JudgeConfig(
        model="m", api_base="b", api_key="k", embedding_model="e",
        embedding_api_base="https://api.siliconflow.cn/v1",
    )
    assert a.signature() != b.signature()


def test_run_dict_exposes_judge_embedding_model():
    """历史 run 必须能回答"当时用的哪个向量模型"，否则换 embedding 后老数据不可归因。"""
    run = EvalRun(
        run_no="EV-T7-SNAP", dataset_id=1, target="kb", status="done",
        evaluator_config={"judge_model": "qwen3.8-flash", "judge_embedding_model": "BAAI/bge-m3"},
    )
    data = run_to_dict(run)
    assert data["model"] == "qwen3.8-flash"
    assert data["judge_embedding_model"] == "BAAI/bge-m3"


# ============================ JSON 抠取工具 ============================
@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        '说明一下：{"a": 1} 就这样。',
        '{"a": 1}\n{"b": 2}',
    ],
)
def test_extract_json_object_accepts_common_shapes(text):
    assert _extract_json_object(text) == {"a": 1}


@pytest.mark.parametrize("text", ["", "没有 JSON", "[1, 2, 3]", "{坏掉的 json}"])
def test_extract_json_object_rejects_non_object(text):
    assert _extract_json_object(text) is None


def test_has_probability_ignores_bool_and_out_of_range():
    assert _has_probability({"faithfulness": 0.7}) is True
    assert _has_probability({"faithfulness": "0.7"}) is True  # 字符串数字也认
    assert _has_probability({"ok": True}) is False            # True 不能算 1.0
    assert _has_probability({"score": 95}) is False           # 百分制不是 RAGAS 口径
    assert _has_probability({"reason": "good"}) is False


# ============================ 空指标熔断 ============================
def _seed(count: int = 3):
    db = SessionLocal()
    tag = f"T7-{int(time.time() * 1000)}"
    ds = EvalDataset(name=f"熔断测试 {tag}", code=tag, target_type="kb", kb_id=6, item_count=count)
    db.add(ds)
    db.flush()
    db.add_all([
        EvalItem(dataset_id=ds.id, seq=i, question=f"Q{i}", reference_answer=f"A{i}")
        for i in range(1, count + 1)
    ])
    run = EvalRun(run_no=f"EV-{tag}", dataset_id=ds.id, target="kb", status="pending", total_items=count)
    db.add(run)
    db.commit()
    ids = {"dataset_id": ds.id, "run_id": run.id}
    db.close()
    return ids


def _cleanup(ids: dict) -> None:
    db = SessionLocal()
    db.execute(EvalScore.__table__.delete().where(EvalScore.run_id == ids["run_id"]))
    db.execute(EvalRun.__table__.delete().where(EvalRun.dataset_id == ids["dataset_id"]))
    db.execute(EvalItem.__table__.delete().where(EvalItem.dataset_id == ids["dataset_id"]))
    db.execute(EvalDataset.__table__.delete().where(EvalDataset.id == ids["dataset_id"]))
    db.commit()
    db.close()


def _patch_judge(monkeypatch, score_fn, delay: float = 0.0) -> dict:
    calls = {"n": 0}

    async def fake_query(kb_id, question, top_k=None):  # noqa: ANN001
        return RagAnswer(question=question, answer=f"答-{question}", contexts=["ctx"], latency_ms=3)

    def wrapped(*args, **kwargs):
        calls["n"] += 1
        if delay:
            time.sleep(delay)  # 模拟真实裁判耗时（否则三个任务会挤在同一批里完成）
        return score_fn(*args, **kwargs)

    monkeypatch.setattr(runner_mod.rag_client, "query", fake_query)
    monkeypatch.setattr(runner_mod.judge, "preflight", lambda *a, **k: "ok")
    monkeypatch.setattr(runner_mod.judge, "score", wrapped)
    return calls


ALL_NULL = JudgeOutcome(
    scores={k: None for k in ("faithfulness", "answer_relevancy", "context_precision", "context_recall")},
    raw={"stub": "empty"},
    tokens=7,
)
ALL_OK = JudgeOutcome(
    scores={"faithfulness": 0.9, "answer_relevancy": 0.8, "context_precision": 0.7, "context_recall": 0.6},
    raw={"stub": "ok"},
    tokens=9,
)


def test_empty_metric_streak_aborts_early_and_stops_burning_tokens(monkeypatch):
    """连续 3 条全空 → 立刻终止：后续样本不再打分，状态 failed，文案统一。

    关于 `calls` 的上界：并发闸是信号量，一条样本结束的瞬间在途的第 N+1 条就会拿到
    许可并启动（线程已在跑，取消不掉）。所以「一条都不多跑」做不到，实测是
    **多跑 1 条在途样本**；这里的价值是「不再把剩下全部跑完」，6 条里只烧 3~4 条。
    """
    ids = _seed(count=6)
    monkeypatch.setattr(settings, "RUN_ITEM_CONCURRENCY", 1)
    monkeypatch.setattr(settings, "RUN_EMPTY_METRIC_STREAK_LIMIT", 3)
    calls = _patch_judge(monkeypatch, lambda *a, **k: ALL_NULL, delay=0.2)

    try:
        asyncio.run(runner_mod.execute_run(ids["run_id"]))

        db = SessionLocal()
        run = db.get(EvalRun, ids["run_id"])
        rows = db.execute(select(EvalScore).where(EvalScore.run_id == run.id)).scalars().all()

        assert run.status == "failed"
        assert run.error_msg == runner_mod.EMPTY_METRIC_ABORT_MSG
        assert run.metrics is None
        assert 3 <= calls["n"] <= 4, f"熔断后不该继续跑满 6 条，实际调了 {calls['n']} 次"
        assert len(rows) == 6 and run.processed_items == 6
        # 真跑过的 3 条：指标全空；没跑的 3 条：带「任务提前终止」标记（都得留痕，
        # 否则审计只看到 3 条会以为样本丢了）
        assert sum(1 for r in rows if r.status == "failed") == 6
        aborted = [r for r in rows if "任务提前终止" in (r.error_msg or "")]
        assert len(aborted) == 3
        assert all(r.faithfulness is None for r in rows)
        db.close()
    finally:
        _cleanup(ids)


def test_single_empty_sample_does_not_kill_a_healthy_run(monkeypatch):
    """偶发一条解析失败不该判死整轮：阈值是 3，后面恢复了就照常 done。"""
    ids = _seed(count=3)
    monkeypatch.setattr(settings, "RUN_ITEM_CONCURRENCY", 1)
    monkeypatch.setattr(settings, "RUN_EMPTY_METRIC_STREAK_LIMIT", 3)
    seq = {"i": 0}

    def sometimes_empty(*a, **k):  # noqa: ANN002, ANN003
        seq["i"] += 1
        return ALL_NULL if seq["i"] == 1 else ALL_OK

    _patch_judge(monkeypatch, sometimes_empty)
    try:
        asyncio.run(runner_mod.execute_run(ids["run_id"]))
        db = SessionLocal()
        run = db.get(EvalRun, ids["run_id"])
        assert run.status == "done"
        assert run.metrics is not None and run.metrics["faithfulness"] == pytest.approx(0.9)
        assert run.success_items == 2 and run.failed_items == 1
        db.close()
    finally:
        _cleanup(ids)


def test_zero_success_with_judge_empties_uses_compat_message(monkeypatch):
    """整轮跑完仍零成功（样本数 < 熔断阈值）：error_msg 也要指向模型兼容性。"""
    ids = _seed(count=2)
    monkeypatch.setattr(settings, "RUN_ITEM_CONCURRENCY", 1)
    monkeypatch.setattr(settings, "RUN_EMPTY_METRIC_STREAK_LIMIT", 5)
    _patch_judge(monkeypatch, lambda *a, **k: ALL_NULL)
    try:
        asyncio.run(runner_mod.execute_run(ids["run_id"]))
        db = SessionLocal()
        run = db.get(EvalRun, ids["run_id"])
        assert run.status == "failed"
        assert run.error_msg == runner_mod.EMPTY_METRIC_ABORT_MSG
        assert run.metrics is None
        db.close()
    finally:
        _cleanup(ids)
