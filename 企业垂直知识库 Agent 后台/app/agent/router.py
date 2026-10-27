"""意图路由：操作类问题规则直判 + LLM 结构化输出判断（五路）。"""
import re
from typing import Literal

from langchain_core.messages import HumanMessage
from pydantic import BaseModel


class RouteDecision(BaseModel):
    """路由决策：rag / db_query / report / parallel_compare / direct。"""

    route: Literal["rag", "db_query", "report", "parallel_compare", "direct"]


_ROUTE_PROMPT = (
    "你是智能路由助手。请判断用户问题应走哪条路径，只返回 route 字段：\n"
    "- rag：需要从企业内部知识库（文档/资料/制度/操作指引）检索来回答；"
    "凡询问办事流程、操作方法、制度规定的问题（如“如何申请X”“怎么办理X”“X的流程/标准是什么”）"
    "必须归为 rag，即使你认为知道答案；\n"
    "- db_query：需要查询业务数据库中的结构化明细数据（设备 / 工单 / 告警 / 台账等）；\n"
    "- report：需要汇总、统计或生成报表（如“生成工单报表”“统计告警数量”“本周汇总”等）；\n"
    "- parallel_compare：需要对比、比较两份文档/文件/资料（如“对比文档A和文档B”"
    "“比较这两份方案的区别”“A 和 B 有什么不同”等）；\n"
    "- direct：寒暄、常识聊天等与公司内部知识库无关的一般性问题。\n\n"
    "用户问题：{question}"
)

# 【新增】操作类问题规则直判：如何/怎么/怎样 开头或包含，且不属于统计报表/对比类。
# 在 LLM 分类前短路返回 rag——既满足"操作类问题必须走知识库"的业务约束，
# 又省一次 LLM 调用（每题约省 5-15 秒），且分类结果稳定不受模型波动影响。
_OPERATIONAL_RE = re.compile(r"如何|怎么|怎样|怎么办|怎么处理")
_NON_OPERATIONAL_RE = re.compile(r"报表|统计|汇总|对比|比较")


def classify_route(llm, question: str) -> str:
    """判断问题意图，返回五路之一。

    操作类问题（如何/怎么/怎样）优先规则直判为 rag；
    其余交给 LLM 结构化输出分类（DeepSeek 不支持 response_format，
    必须显式使用 method="function_calling" 做结构化输出）。
    """
    if _OPERATIONAL_RE.search(question) and not _NON_OPERATIONAL_RE.search(question):
        return "rag"
    prompt = _ROUTE_PROMPT.format(question=question)
    decision = llm.with_structured_output(RouteDecision, method="function_calling").invoke(
        [HumanMessage(content=prompt)]
    )
    if isinstance(decision, RouteDecision):
        return decision.route
    return getattr(decision, "route", "direct")
