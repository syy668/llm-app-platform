"""全局统一 LLM 工厂：唯一的大模型出口。

改造前的问题（批次2）：
- `app/agent/llm.py` 恒构造 `ChatOpenAI`，完全不读 `llm_provider`；
- `app/rag/llm.py` 自己用原生 SDK 分支，读 `llm_provider`；
- `app/eval/metrics.py` 又自己 new 一个 `ChatOpenAI`。
三处各自为政 → 同一个系统里 `LLM_PROVIDER=mock` 只对 `/kbs/{id}/query` 生效，
会话问答照样打真实 API，配置与行为不一致。

现在所有链路只认这一个工厂：
    Agent 图（classify / answer / 对比拆词）  ┐
    /kbs/{id}/query（纯知识库问答）          ├─→ get_chat_model() / generate_text()
    RAGAS 评测判分（judge LLM）              ┘

provider 取值：mock / openai / anthropic（与系统配置页 `llm_provider` 枚举一致）。
取值优先级：系统配置页覆盖值 > .env > 代码默认（沿用 runtime_config 既有规则）。
热更新：签名变化（provider / model / 温度 / 地址 / 密钥 / 超时）即自动重建客户端，
后台改完配置无需重启进程。

【v1.7 新增】评测判分可解耦：`get_eval_chat_model()` 在 `eval_llm_model` 已配置时
使用独立的模型名 / 地址 / 密钥（provider 仍与全局一致），未配置则完全复用
`get_chat_model(purpose="eval")`，默认行为与之前逐字节相同。
"""
import hashlib
import logging
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from ..config import settings
from ..core import runtime_config
from .mock import MockChatModel

logger = logging.getLogger(__name__)

# 受支持的提供方（与 runtime_config._ENUM_FIELDS["llm_provider"] 保持一致）
PROVIDERS: tuple[str, ...] = ("mock", "openai", "anthropic")

# 未配置模型名时的兜底（与改造前代码默认值一致）
_DEFAULT_MODEL = {"openai": "deepseek-chat", "anthropic": "claude-sonnet-4-5"}

# 用途预设：(超时秒, 最大重试次数)
# chat = 会话/问答链路（沿用原 agent/llm.py 的 60s / 2 次）；
# eval = RAGAS 判分（沿用 EVAL_LLM_TIMEOUT / EVAL_LLM_MAX_RETRIES，默认 120s / 3 次）。
_PURPOSE_PRESETS: dict[str, tuple[float, int]] = {
    "chat": (60.0, 2),
}

# 按签名缓存实例，避免每次请求重复构造客户端（签名含配置指纹，天然支持热更新）
_cache: dict[tuple, BaseChatModel] = {}


def get_provider() -> str:
    """当前生效的提供方；非法值一律回退 mock（宁可降级也不炸服务）。"""
    raw = str(
        runtime_config.get_value("llm_provider") or settings.llm_provider or "mock"
    ).strip().lower()
    if raw not in PROVIDERS:
        logger.warning("未知 LLM provider=%r，回退 mock", raw)
        return "mock"
    return raw


def _key_fingerprint(api_key: str) -> str:
    """密钥只以指纹形式进入缓存键，避免明文常驻内存字典。"""
    return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:12]


def _model_name(provider: str) -> str:
    """模型名：配置页覆盖值 > .env > 内置默认。"""
    configured = str(runtime_config.get_value("llm_model") or "").strip()
    if configured:
        return configured
    return str(settings.llm_model or "").strip() or _DEFAULT_MODEL.get(provider, "")


def _api_base() -> str:
    """接口地址：仅取显式配置（覆盖值 > .env），空串交给各 SDK 用官方默认。"""
    return str(runtime_config.get_value("llm_api_base") or "").strip()


def _api_key() -> str:
    """密钥：配置页覆盖值 > .env（get_secret 内部已处理该优先级）。"""
    try:
        return str(runtime_config.get_secret("llm_api_key") or "").strip()
    except KeyError:
        return ""


# 百炼域名标记：只有明确命中时才追加百炼专属参数（其他厂商不认识会 400）
_DASHSCOPE_HOST_MARKERS = ("dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com")


def _dashscope_extra_body(api_base: str) -> dict[str, Any] | None:
    """阿里云百炼专属参数：显式 **关闭思考模式**（enable_thinking=False）。

    为什么必须关（实测，2026-10-24）：
    - qwen3.x / qwen3.5~3.8 系列默认开启思考。思考模式下 `tool_choice` 不可靠：
      classify 节点走 `with_structured_output`（内部 tool_choice=required）时，
      百炼直接返回 400
      `tool_choice parameter does not support being set to required or object in thinking mode`；
      换个问法虽不报 400，却返回正文而**不返回 tool_calls**，等于静默失败——
      反而比报错更难查。
    - 本系统全链路（路由分类 / RAG 问答 / RAGAS 判分）都依赖结构化输出与低延迟，
      思考模式两条都不满足。

    只对百炼生效：参数名是百炼私有的，传给 DeepSeek / 硅基流动 / 魔搭会 400。
    """
    if not any(m in (api_base or "").lower() for m in _DASHSCOPE_HOST_MARKERS):
        return None
    return {"enable_thinking": False}


def _signature(provider: str, model: str, temperature: float, api_base: str,
               api_key: str, timeout: float, max_retries: int,
               max_tokens: int | None = None) -> tuple:
    """配置指纹：任一项变化即重建客户端（含 max_tokens，改 .env 重启后立即生效）。"""
    return (
        provider,
        model,
        round(temperature, 4),
        api_base,
        _key_fingerprint(api_key),
        round(float(timeout), 2),
        int(max_retries),
        int(max_tokens or 0),
    )


def _create_model(
    provider: str,
    model: str,
    api_base: str,
    api_key: str,
    temperature: float,
    timeout: float,
    max_retries: int,
    *,
    purpose: str = "chat",
    max_tokens: int | None = None,
) -> BaseChatModel:
    """按配置签名缓存并构造实例（provider 分发）。

    【v1.7 抽出】供 get_chat_model 与 get_eval_chat_model 共用，避免评测侧
    复制一份 ChatOpenAI/ChatAnthropic 构造逻辑（否则工厂又退回到「多处各自为政」）。

    【v1.16】强制下发 `max_tokens`（默认取 LLM_MAX_TOKENS=2048）：
    - 不传时上限由上游默认值决定，各家不一（ChatAnthropic 默认仅 1024）；
    - 显式传值可同时封住「单条回答烧掉上万 token」的成本风险。
    截断本身不报错（HTTP 200 + finish_reason=length），
    识别与提示统一由 `app/llm/truncation.py` 负责。
    """
    if max_tokens is None:
        max_tokens = int(settings.llm_max_tokens)

    signature = _signature(
        provider, model, temperature, api_base, api_key, timeout, max_retries, max_tokens
    )
    cached = _cache.get(signature)
    if cached is not None:
        return cached

    instance: BaseChatModel
    if provider == "mock":
        instance = MockChatModel()
    elif provider == "openai":
        if not api_key:
            raise RuntimeError(
                "LLM_PROVIDER=openai 但未配置 LLM_API_KEY："
                "请在「系统配置」页填写 API Key，或在 .env 中设置 LLM_API_KEY"
            )
        from langchain_openai import ChatOpenAI

        instance = ChatOpenAI(
            model=model or _DEFAULT_MODEL["openai"],
            api_key=api_key,
            base_url=api_base or None,
            temperature=temperature,
            timeout=timeout,
            max_retries=max_retries,
            # 【v1.16】输出硬上限：与 finish_reason 检查配套，缺一不可
            max_tokens=max_tokens,
            # 【v1.15】百炼思考模式与 tool_choice 不兼容，显式关闭；其他厂商为 None 不传
            extra_body=_dashscope_extra_body(api_base),
        )
    elif provider == "anthropic":
        if not api_key:
            raise RuntimeError(
                "LLM_PROVIDER=anthropic 但未配置 LLM_API_KEY："
                "请在「系统配置」页填写 API Key，或在 .env 中设置 LLM_API_KEY"
            )
        from langchain_anthropic import ChatAnthropic

        # 注意：ChatAnthropic 的 max_tokens 是**必填语义**的字段，SDK 默认 1024，
        # 不显式传就会比本系统的默认 2048 更早截断（这是最容易漏的一处）。
        instance = ChatAnthropic(
            model=model or _DEFAULT_MODEL["anthropic"],
            api_key=api_key,
            base_url=api_base or None,
            temperature=temperature,
            timeout=timeout,
            max_retries=max_retries,
            max_tokens=max_tokens,
        )
    else:  # pragma: no cover - get_provider 已收敛到白名单，此处仅防御
        raise RuntimeError(f"不支持的 LLM_PROVIDER: {provider}")

    logger.info(
        "LLM 工厂创建实例 provider=%s model=%s purpose=%s temp=%s max_tokens=%s",
        provider, model, purpose, temperature, max_tokens,
    )
    _cache[signature] = instance
    return instance


def _optional_override(key: str) -> str:
    """读取「可留空」的覆盖项（eval_llm_model / eval_llm_api_base 等）。

    未配置或读取异常一律返回空串，由调用方决定回退到全局配置。
    """
    try:
        return str(runtime_config.get_value(key) or "").strip()
    except KeyError:  # pragma: no cover - 键名在 _FIELD_SPECS 内，仅防御
        return ""


def _optional_secret(key: str) -> str:
    """读取可选的敏感覆盖项（eval_llm_api_key）：未配置返回空串。"""
    try:
        return str(runtime_config.get_secret(key) or "").strip()
    except KeyError:  # pragma: no cover
        return ""


def get_chat_model(
    *,
    purpose: str = "chat",
    temperature: float | None = None,
    timeout: float | None = None,
    max_retries: int | None = None,
    max_tokens: int | None = None,
) -> BaseChatModel:
    """返回当前配置对应的 LangChain ChatModel（带缓存）。

    - provider=mock       → MockChatModel（离线、确定性、支持流式与结构化输出）
    - provider=openai     → ChatOpenAI（OpenAI 兼容协议，DeepSeek/硅基流动/自建网关通用）
    - provider=anthropic  → ChatAnthropic

    purpose 只影响超时/重试预设（chat / eval），业务代码无需感知 provider 差异。
    """
    provider = get_provider()
    preset_timeout, preset_retries = _PURPOSE_PRESETS.get(
        purpose, (settings.eval_llm_timeout, settings.eval_llm_max_retries)
    )
    timeout = float(preset_timeout if timeout is None else timeout)
    max_retries = int(preset_retries if max_retries is None else max_retries)

    if temperature is None:
        temperature = float(runtime_config.get_value("llm_temperature"))
    temperature = float(temperature)

    model = _model_name(provider)
    api_base = _api_base()
    api_key = "" if provider == "mock" else _api_key()

    # 【v1.19.x】max_tokens 支持调用方注入（eval 回退路径传判分专用预算 8192）；
    # 不传时沿用对话上限 LLM_MAX_TOKENS，既有调用点零影响。
    return _create_model(
        provider, model, api_base, api_key, temperature, timeout, max_retries,
        purpose=purpose,
        max_tokens=int(max_tokens if max_tokens is not None else settings.llm_max_tokens),
    )


def get_eval_chat_model(
    *,
    temperature: float = 0.0,
    timeout: float | None = None,
    max_retries: int | None = None,
) -> BaseChatModel:
    """RAGAS 判分专用模型：支持与对话模型解耦，避免「自评自判」的偏差。

    【v1.7 新增】取值规则（provider 始终与全局一致，只有模型名/地址/密钥可分离）：
    1. `eval_llm_model` 未配置 → **完全复用** get_chat_model(purpose="eval")，
       签名一致因此共享同一个缓存实例，默认行为与 v1.6 逐字节相同；
    2. `eval_llm_model` 已配置 → 使用独立的 模型名 / 接口地址 / 密钥，
       其中地址与密钥未单独配置时按字段回退到全局值；
    3. provider=mock 时模型名不参与调用（Mock 为内置规则模型），配置仅作占位。
    """
    eval_model = _optional_override("eval_llm_model")
    if not eval_model:
        # 向后兼容主路径：与改造前完全一致的取值链路
        return get_chat_model(
            purpose="eval", temperature=temperature, timeout=timeout, max_retries=max_retries,
            max_tokens=int(settings.eval_llm_max_tokens),
        )

    provider = get_provider()
    api_base = _optional_override("eval_llm_api_base") or _api_base()
    api_key = ""
    if provider != "mock":
        api_key = _optional_secret("eval_llm_api_key") or _api_key()

    timeout = float(settings.eval_llm_timeout if timeout is None else timeout)
    max_retries = int(settings.eval_llm_max_retries if max_retries is None else max_retries)

    logger.info(
        "评测判分使用独立模型 eval_model=%s（对话模型为 %s）", eval_model, _model_name(provider)
    )
    return _create_model(
        provider,
        eval_model,
        api_base,
        api_key,
        float(temperature),
        timeout,
        max_retries,
        purpose="eval",
        # 【v1.19.x】faithfulness 的 NLI 输出 = 每条陈述（原文+理由+verdict）的巨型 JSON，
        # 长中文答案可拆出几十条陈述，2048 必被截断 → 解析失败 → 指标 NaN
        # （run72 空 9/30 条的根因）。判分专用上限与对话解耦，默认 8192。
        max_tokens=int(settings.eval_llm_max_tokens),
    )


def generate_text(
    prompt: str,
    *,
    system: str | None = None,
    temperature: float | None = None,
    purpose: str = "chat",
) -> str:
    """单轮文本生成便捷入口（/kbs/{id}/query 使用）。

    与 get_chat_model 走同一条 provider 分发，因此 mock 模式下同样返回确定性结果。

    【v1.16】与 Agent 链路保持同一口径：命中 max_tokens 时在文本末尾追加提示。
    这里的返回类型是 str（调用方只取文本），故把「截断」表达为文本后缀，
    而不是改签名——避免为一个提示语去动所有调用点。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from .truncation import append_notice, is_truncated_message

    messages: list[Any] = []
    if system:
        messages.append(SystemMessage(content=system))
    messages.append(HumanMessage(content=prompt))
    resp = get_chat_model(purpose=purpose, temperature=temperature).invoke(messages)
    text = str(getattr(resp, "content", "") or "")
    if is_truncated_message(resp):
        logger.warning("文本生成被 max_tokens 截断（/kbs query 链路），已追加提示")
        text = append_notice(text)
    return text


def reset_cache() -> None:
    """清空客户端缓存（测试或配置剧烈变更时使用）。"""
    _cache.clear()
