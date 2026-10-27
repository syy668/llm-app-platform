"""Agent 图状态。"""
from typing import TypedDict


class AgentState(TypedDict):
    """LangGraph 状态（进程内运行，不落盘，无需序列化）。"""

    messages: list            # LangChain 消息：会话历史 + 当前提问
    question: str             # 当前用户提问
    kb_id: int | None         # 绑定的知识库 ID（可空）
    route: str                # 路由结果：rag / db_query / report / parallel_compare / direct
    tool_output: str          # 工具执行结果（检索/查询到的上下文）
    final_answer: str         # 最终回答
    # 【v1.9】结构化检索来源（文档名/块号/相似度/原文），供前端引用卡片溯源；
    # 与 tool_output 里的 [来源i] 标号一一对应（i 从 1 开始，顺序一致）。
    sources: list[dict]
    # ---- 以下为对话页「参数配置」随请求传入的可选参数（缺省 None = 用默认行为） ----
    top_k: int | None                    # 本次检索条数；None = 用知识库配置（kb.top_k）或 5
    search_mode: str | None              # 【新增】本次检索模式 vector_only/bm25_vector_hybrid；None = 混合检索（默认）
    route_enabled: dict[str, bool] | None  # 路由开关 {rag/db_query/report/parallel_compare: bool}；None = 全部启用
    # ---- 【v1.12】短追问意图继承 ----
    last_route: str | None               # 上一轮 assistant 消息的 agent_route；None = 首轮
    route_inherited: bool                # 本次 route 是否由上一轮继承而来（跳过 LLM 分类）
    # ---- 【v1.16】输出截断感知 ----
    # answer 节点的 LLM 返回若 finish_reason=length（命中 max_tokens），
    # final_answer 末尾已追加提示，这里同步置 True 供落库 / SSE 透传 / 大盘统计。
    truncated: bool
