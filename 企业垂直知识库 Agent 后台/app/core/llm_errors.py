"""把模型服务的调用异常翻译成用户能行动的一句话（v1.14）。

## 为什么需要

线上真实故障：硅基流动账户欠费，`/v1/embeddings` 与 `/v1/chat/completions` 全返
`402 {'code': 30001, 'message': 'account balance is insufficient'}`。
后端把异常统一吞成「AI 服务调用失败，请稍后重试」——但「稍后重试」对欠费、
密钥失效这类问题**永远无效**，用户只会反复点击，最后只能来问开发者。

## 安全边界

**只做分类，绝不透出原始异常文本**：其中可能包含 base_url、模型名，
openai SDK 的部分报错还会带上 Authorization 头片段。所以这里只吐固定文案。

## 分类优先级

`status_code` 最可靠，先看它；没有该属性的异常（超时、连接错误、
自定义 RuntimeError）再按关键词兜底。
"""
from __future__ import annotations

# 各分类的固定文案：既要说明原因，也要给出**下一步动作**
_HINT_INSUFFICIENT = "AI 服务余额不足：请在「系统配置」中更换可用的密钥，或为当前服务商充值"
_HINT_INVALID_KEY = "AI 服务密钥无效或已过期：请在「系统配置」中重新填写密钥"
# 403 与 402 是两件事：402 是账户没钱，403 常见于「免费额度用尽 / 该模型未开通」。
# 线上真实故障（run 63~65 全部指标为空）：判分模型选了带日期后缀的 qwen3.7-flash-2026-07-15，
# 上游只允许免费额度访问，额度耗尽后返回 403 AllocationQuota.FreeTierOnly ——
# 只看状态码会误判成 402，只按关键词会误判成 429，必须单独给一句能指导动作的话。
_HINT_QUOTA_DENIED = (
    "该模型的可用额度已用尽或未开通（HTTP 403）：请在「系统配置」中改用可调用的模型名"
    "（去掉日期后缀或用同系列正式版），或为该服务商开通付费额度"
)
_HINT_RATE_LIMIT = "AI 服务触发限流，请稍后重试"
_HINT_MODEL_NOT_FOUND = (
    "模型不可用：当前配置的模型名或接口地址与该服务商不匹配，"
    "请在「系统配置」中核对模型名格式（百炼 / DeepSeek 用短名，硅基流动 / 魔搭带组织前缀）"
    "与接口地址是否同属一家"
)
_HINT_TIMEOUT = "AI 服务连接超时，请检查网络或稍后重试"
_HINT_EMBEDDING = "向量化服务不可用，检索已降级为字面匹配，请稍后重试"
_HINT_FALLBACK = "AI 服务调用失败，请稍后重试"


def llm_error_hint(exc: BaseException | None) -> str:
    """把模型服务异常映射成面向用户的提示文案。永不抛错。"""
    if exc is None:
        return _HINT_FALLBACK

    status = getattr(exc, "status_code", None)
    try:
        text = str(exc).lower()
    except Exception:  # pragma: no cover - 异常对象不应在格式化时炸掉
        text = ""

    # 1) HTTP 状态码（openai.APIStatusError 等）
    if status == 402:
        return _HINT_INSUFFICIENT
    if status == 401:
        return _HINT_INVALID_KEY
    if status == 403:
        return _HINT_QUOTA_DENIED
    if status == 429:
        return _HINT_RATE_LIMIT
    # 404 在模型服务里几乎只有两种含义：模型名不存在，或 base_url 路径写错。
    # 两者对用户的动作是一样的——回去核对「模型名 + 接口地址」，所以合成一句。
    if status == 404:
        return _HINT_MODEL_NOT_FOUND

    # 2) 关键词兜底：不同 SDK 不一定挂 status_code
    # 403 必须先于 "quota"/"insufficient" 判定：额度类错误文本里这两个词都会出现，
    # 但 403（未开通/免费额度用尽）与 402（余额不足）对用户是不同动作。
    if "403" in text or "freetier" in text or "free tier" in text:
        return _HINT_QUOTA_DENIED
    if "402" in text or "insufficient" in text or "balance" in text or "quota" in text:
        return _HINT_INSUFFICIENT
    if "401" in text or "invalid api key" in text or "invalid_api_key" in text or "unauthorized" in text:
        return _HINT_INVALID_KEY
    if "model_not_found" in text or "does not exist" in text or "model not found" in text:
        return _HINT_MODEL_NOT_FOUND
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return _HINT_RATE_LIMIT
    if "timeout" in text or "timed out" in text or "connection" in text or "connect" in text:
        return _HINT_TIMEOUT
    if "embedding" in text:
        return _HINT_EMBEDDING
    return _HINT_FALLBACK
