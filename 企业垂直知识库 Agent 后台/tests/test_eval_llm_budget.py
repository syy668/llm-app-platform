"""【v1.19.x 回归】判分链路对长答案的预算三件套 + missing_rates 显形。

背景 bug（run72 空 9/30 条）：faithfulness 的 NLI 输出是「每条陈述=原文+理由+verdict」
的巨型 JSON，长中文答案可拆出几十条陈述：
  1. max_tokens=2048（与对话共用）被截断 → JSON 解析失败 → 指标 NaN；
  2. 单请求 120s 太紧（长答案 NLI 实测 40~110s）→ 超时空值；
  3. RunConfig(600s) < 240×(3+1)=960 → 会先于 LLM 超时被打断。
且空值被静默剔除后均值虚高（30 条只算 21 条仍显示 0.8587），missing_rates 让它显形。
"""
import pytest

from app.config import settings
from app.llm import factory
from app.services.eval_service import _aggregate_metrics


def test_eval_llm_budget_defaults():
    """三项预算默认值成套：输出上限解耦、单请求放宽、任务级上限覆盖重试叠加。"""
    assert settings.eval_llm_max_tokens == 8192          # 判分专用，不再沿用对话的 2048
    assert settings.eval_llm_timeout == 240              # 长答案 NLI 实测 40~110s
    assert settings.eval_ragas_timeout >= settings.eval_llm_timeout * (
        settings.eval_llm_max_retries + 1
    )  # 任务级超时必须盖住「单请求 × 重试叠加」，否则先于 LLM 超时被打断


def test_eval_chat_model_passes_decoupled_max_tokens(monkeypatch):
    """判分模型必须携带独立的 max_tokens=8192，而不是沿用对话的 2048。"""
    captured: dict = {}

    def fake_create_model(provider, model, api_base, api_key, temperature,
                          timeout, max_retries, **kwargs):
        captured["max_tokens"] = kwargs.get("max_tokens")
        return object()  # 只验证传参，不真构造客户端

    monkeypatch.setattr(factory, "_create_model", fake_create_model)
    factory.get_eval_chat_model()
    assert captured["max_tokens"] == settings.eval_llm_max_tokens
    assert captured["max_tokens"] != settings.llm_max_tokens  # 与对话 2048 解耦


def test_aggregate_metrics_missing_rates_visible():
    """部分指标缺失时，均值旁必须带 missing_rates；无缺失时不加键。"""
    sums = {
        "faithfulness": [0.8, 0.9],        # 2 有效 / 3 成功 → 缺 1
        "answer_relevancy": [0.6],         # 1 有效 / 3 成功 → 缺 2
        "context_precision": [0.7, 0.7],   # 2 有效 / 3 成功 → 缺 1
        "context_recall": [0.9],           # 1 有效 / 3 成功 → 缺 2
    }
    m = _aggregate_metrics(sums, scored=3)
    assert m["faithfulness"] == pytest.approx(0.85)          # 均值口径不变
    assert m["missing_rates"]["faithfulness"] == pytest.approx(0.3333)
    assert m["missing_rates"]["answer_relevancy"] == pytest.approx(0.6667)

    full = {k: [0.5, 0.5] for k in sums}
    assert "missing_rates" not in _aggregate_metrics(full, scored=2)  # 无缺失不加键
    assert "missing_rates" not in _aggregate_metrics(sums, scored=0)  # 零成功不加键
