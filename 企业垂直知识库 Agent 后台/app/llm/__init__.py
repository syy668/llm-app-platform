"""统一 LLM 出口包。

对外只暴露四个入口，业务代码不要直接 import langchain_openai / anthropic：
    get_chat_model()      —— 需要 LangChain ChatModel（Agent 图、结构化输出）
    get_eval_chat_model() —— RAGAS 判分专用（支持 eval_llm_* 独立模型，未配置则复用对话模型）
    generate_text()       —— 只要一段文本（知识库问答）
    get_provider()        —— 当前生效的 provider（日志/展示用）
"""
from .factory import (
    PROVIDERS,
    generate_text,
    get_chat_model,
    get_eval_chat_model,
    get_provider,
    reset_cache,
)
from .mock import MockChatModel

__all__ = [
    "PROVIDERS",
    "MockChatModel",
    "generate_text",
    "get_chat_model",
    "get_eval_chat_model",
    "get_provider",
    "reset_cache",
]
