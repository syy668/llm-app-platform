"""第十四步：裁判适配的「厂商差异」与可疑标记（深水区补测）。

两块之前没覆盖、但有真实排障故事的逻辑：
1. **关思考参数按厂商区分**：DeepSeek 官方要 `thinking={"type":"disabled"}`，
   阿里百炼要 `enable_thinking=false`，写反 = 参数被静默忽略 = 推理模型照常烧
   reasoning token（content 空 → 指标 NaN，还多烧 3~4 倍钱）。
2. **可疑标记 `_is_suspect` / `_is_refusal_item`**：只标不拦，帮人定位「引用断裂」。

通过 mock ChatOpenAI/OpenAIEmbeddings 捕获 extra_body，直接断言参数对不对，
不真连任何模型。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services import runner as runner_mod
from app.services.judge import JudgeOutcome, RagasJudge, TokenCounter
from app.services.runtime_config import JudgeConfig
from app.services.rag_client import RagAnswer


def _cfg(api_base: str) -> JudgeConfig:
    return JudgeConfig(
        model="deepseek-flash", api_base=api_base, api_key="sk-test",
        embedding_model="BAAI/bge-m3", embedding_api_base="https://api.siliconflow.cn/v1",
        embedding_api_key="sk-emb",
    )


def _capture_extra_body(api_base: str, monkeypatch) -> dict:
    """调用 _make_clients 并捕获传给 ChatOpenAI 的 extra_body。"""
    captured: dict = {}

    class _FakeChat:
        def __init__(self, **kw):
            captured.update(kw)

    class _FakeEmb:
        def __init__(self, **kw):
            captured.update(kw)

    class _FakeWrapper:
        def __init__(self, inner, *a, **k):
            self.langchain_llm = inner

    monkeypatch.setattr("app.services.judge.ChatOpenAI", _FakeChat)
    monkeypatch.setattr("app.services.judge.OpenAIEmbeddings", _FakeEmb)
    monkeypatch.setattr("app.services.judge.LangchainLLMWrapper", _FakeWrapper)
    monkeypatch.setattr("app.services.judge.LangchainEmbeddingsWrapper", _FakeWrapper)

    RagasJudge._make_clients(_cfg(api_base), TokenCounter())
    return captured


def test_deepseek_official_uses_thinking_disabled(monkeypatch):
    """DeepSeek 官方域名必须传 thinking={"type":"disabled"}。"""
    cap = _capture_extra_body("https://api.deepseek.com", monkeypatch)
    assert cap.get("extra_body") == {"thinking": {"type": "disabled"}}


def test_dashscope_uses_enable_thinking_false(monkeypatch):
    """阿里百炼域名必须传 enable_thinking=false，不能传 thinking 对象。"""
    cap = _capture_extra_body("https://dashscope.aliyuncs.com/compatible-mode/v1", monkeypatch)
    assert cap.get("extra_body") == {"enable_thinking": False}


def test_temperature_always_locked_zero(monkeypatch):
    """无论哪个厂商，裁判温度都锁 0，保证可复现。"""
    cap = _capture_extra_body("https://api.deepseek.com", monkeypatch)
    assert cap.get("temperature") == 0


def test_judge_disable_thinking_false_skips_extra_body(monkeypatch):
    """JUDGE_DISABLE_THINKING=false 时不注入任何关思考参数。

    注意 _make_clients 里 `extra_body or None` 会把空 dict 转成 None，
    所以断言的是「没有 thinking / enable_thinking 键」，而不是 extra_body 本身非空。
    """
    monkeypatch.setattr(settings, "JUDGE_DISABLE_THINKING", False)
    cap = _capture_extra_body("https://api.deepseek.com", monkeypatch)
    extra = cap.get("extra_body") or {}
    assert "thinking" not in extra
    assert "enable_thinking" not in extra


# ============================ _is_refusal_item ============================
def test_refusal_item_by_stratum():
    item = SimpleNamespace(stratum="拒答陷阱", reference_answer="")
    assert runner_mod._is_refusal_item(item) is True


def test_refusal_item_by_answer_marker():
    item = SimpleNamespace(stratum=None, reference_answer="未找到相关信息")
    assert runner_mod._is_refusal_item(item) is True


def test_normal_item_not_refusal():
    item = SimpleNamespace(stratum="常规FAQ", reference_answer="重置 VPN 密码")
    assert runner_mod._is_refusal_item(item) is False


# ============================ _is_suspect ============================
def _ans(text: str, contexts: list[str] | None = None) -> RagAnswer:
    return RagAnswer(question="q", answer=text, contexts=contexts or [], latency_ms=5)


def test_suspect_when_contexts_but_refusal_text():
    a = _ans("资料中未找到相关信息", contexts=["ctx-1"])
    o = JudgeOutcome(scores={"faithfulness": 1.0, "answer_relevancy": 0.9})
    assert runner_mod._is_suspect(a, o) is True


def test_suspect_when_ar_zero_but_high_faithfulness():
    """弱裁判把明确答案判 noncommittal → AR 被硬乘 0，标可疑但不改分。"""
    a = _ans("第一步先停机检查滚轮。", contexts=["ctx-1"])
    o = JudgeOutcome(scores={"faithfulness": 0.8, "answer_relevancy": 0.0})
    assert runner_mod._is_suspect(a, o) is True


def test_suspect_when_both_very_low():
    a = _ans("随便答的。", contexts=["ctx-1"])
    o = JudgeOutcome(scores={"faithfulness": 0.1, "answer_relevancy": 0.1})
    assert runner_mod._is_suspect(a, o) is True


def test_not_suspect_for_healthy_score():
    a = _ans("正常答案。", contexts=["ctx-1"])
    o = JudgeOutcome(scores={"faithfulness": 0.8, "answer_relevancy": 0.7})
    assert runner_mod._is_suspect(a, o) is False


def test_not_suspect_when_answer_none():
    o = JudgeOutcome(scores={"faithfulness": None, "answer_relevancy": None})
    assert runner_mod._is_suspect(None, o) is False
