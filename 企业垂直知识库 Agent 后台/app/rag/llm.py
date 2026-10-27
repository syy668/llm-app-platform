"""知识库问答的回答生成（/kbs/{id}/query 链路）。

【批次2 统一】本模块不再自己维护 provider 分支与原生 SDK 客户端，
一律交给 `app/llm` 统一工厂：
- provider=mock       → MockChatModel 回显检索到的资料（离线可跑）
- provider=openai     → OpenAI 兼容接口（DeepSeek / 硅基流动 / 自建网关）
- provider=anthropic  → Claude

只保留「提示词怎么拼」这件事在本模块，模型选择与客户端生命周期归工厂。
"""
from ..llm import generate_text


# 【v1.9 冲突检测】与 app/agent/graph.py::_ANSWER_SYSTEM_PROMPT 保持同一口径：
# 知识库并存新旧制度时，模型自行裁决等于凭空生成一条“权威结论”，属于最隐蔽的幻觉。
_CONFLICT_RULE = (
    "【冲突处理死命令】如果多份资料之间存在矛盾（例如不同的金额标准、天数、时限、"
    "适用范围），严禁自行判断、取舍或折中，必须客观并列列出各自的说法，"
    "并逐条注明其来源（资料序号或文档名）；如有必要，明确提示用户“资料之间存在冲突，"
    "请以最新/指定制度为准”。\n\n"
)


def build_prompt(question: str, contexts: list[str], kb_name: str) -> str:
    ctx = "\n\n".join(f"[资料 {i}] {c}" for i, c in enumerate(contexts, start=1))
    return (
        "你是企业内部知识库助手。请仅依据下面给出的知识库资料回答问题，"
        "资料中没有的信息请明确说明“资料中未找到相关信息”，不要编造。\n\n"
        + _CONFLICT_RULE
        + f"知识库：{kb_name}\n\n"
        f"【资料】\n{ctx}\n\n"
        f"【问题】{question}\n\n"
        "【回答】"
    )


def answer(question: str, contexts: list[str], kb_name: str) -> str:
    """根据检索上下文生成回答（provider 由统一工厂按运行时配置决定，热更新生效）。"""
    return generate_text(build_prompt(question, contexts, kb_name))
