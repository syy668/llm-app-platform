"""模型服务异常 → 用户可行动文案的映射（v1.14）。

对应线上故障：硅基流动欠费 402，后端统一吞成「请稍后重试」，用户无从判断。
本文件同时锁住一条安全边界：**原始异常文本不得透出**（可能含 base_url、密钥片段）。
"""
import pytest

from app.core.llm_errors import llm_error_hint


class _ApiError(Exception):
    """模拟 openai.APIStatusError：带 status_code。"""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def test_402_maps_to_insufficient_balance():
    exc = _ApiError(402, "Error code: 402 - {'code': 30001, 'message': 'account balance is insufficient'}")
    hint = llm_error_hint(exc)
    assert "余额不足" in hint
    assert "系统配置" in hint, "必须给出下一步动作，而不只是描述"


def test_401_maps_to_invalid_key():
    assert "密钥" in llm_error_hint(_ApiError(401, "Unauthorized"))


def test_429_maps_to_rate_limit():
    assert "限流" in llm_error_hint(_ApiError(429, "Too Many Requests"))


def test_keyword_fallback_without_status_code():
    """部分 SDK 的异常不带 status_code，只能靠关键词。"""
    assert "余额不足" in llm_error_hint(RuntimeError("402 Payment Required: insufficient balance"))
    assert "密钥" in llm_error_hint(RuntimeError("invalid api key provided"))
    assert "限流" in llm_error_hint(RuntimeError("rate limit exceeded"))


def test_404_model_not_found_maps_to_actionable_hint():
    """线上真实故障：把硅基流动的模型名填到百炼，调用报 404 model_not_found。"""
    exc = _ApiError(
        404,
        "Error code: 404 - {'error': {'message': 'The model `Qwen/Qwen2.5-14B-Instruct` "
        "does not exist or you do not have access to it.', 'code': 'model_not_found'}}",
    )
    hint = llm_error_hint(exc)
    assert "模型" in hint
    assert "系统配置" in hint, "必须给出下一步动作"
    # 安全边界：用户填的模型名同样不外泄（文案只讲格式，不回声）
    assert "Qwen/Qwen2.5-14B-Instruct" not in hint


def test_model_not_found_without_status_code():
    """部分异常不带 status_code，只能靠 model_not_found / does not exist 兜底。"""
    assert "模型" in llm_error_hint(RuntimeError("model_not_found: bad model name"))
    assert "模型" in llm_error_hint(RuntimeError("The model `foo` does not exist"))


def test_timeout_and_connection():
    assert "超时" in llm_error_hint(TimeoutError("request timed out"))
    assert "超时" in llm_error_hint(RuntimeError("connection reset by peer"))


def test_unknown_error_gets_generic_hint():
    assert "请稍后重试" in llm_error_hint(RuntimeError("some internal bug"))
    assert "请稍后重试" in llm_error_hint(None)


def test_original_payload_never_leaks():
    """异常原文里的 base_url / 密钥片段绝不能出现在给用户的文案里。"""
    leak = _ApiError(
        402,
        "Error at https://api.siliconflow.cn/v1/embeddings with key sk-secret123456 balance insufficient",
    )
    hint = llm_error_hint(leak)
    for secret in ("sk-secret123456", "siliconflow", "http"):
        assert secret not in hint


def test_never_raises_on_broken_exception():
    """旁路逻辑不允许因为异常对象本身有问题而二次抛出。"""

    class Weird(Exception):
        def __str__(self):
            raise RuntimeError("__str__ broken")

    assert isinstance(llm_error_hint(Weird()), str)
