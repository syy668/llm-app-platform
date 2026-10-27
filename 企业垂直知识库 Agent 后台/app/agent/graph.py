"""LangGraph 智能体图：五路条件路由（RAG / 业务库查询 / 报表生成 / 并行对比 / 直接回答）。

图结构：
  START -> classify(路由分类)
            ├─ rag              -> rag_node              ─┐
            ├─ db_query         -> db_query_node         ─┤
            ├─ report           -> report_node           ─┼-> answer(生成回答) -> END
            ├─ parallel_compare -> parallel_compare_node ─┘
            └─ direct ────────────────────────────────────┘
  其中 report 节点直接产出 Markdown 报表并结束（无需再走 LLM 合成）；
  parallel_compare 节点用两线程并行执行两路 RAG 检索，合并上下文后仍走 answer 节点生成对比总结。
"""
import concurrent.futures
import contextvars
import json
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from ..core import runtime_config
from ..core.tracing import TraceCallbackHandler
from ..llm import get_chat_model
from ..llm.truncation import append_notice, is_truncated_message
from .intent_inherit import decide_inherit
from .router import classify_route
from .state import AgentState
from .tools.db_query_tool import NO_BUSINESS_DATA_MARK, query_business_db
from .tools.rag_tool import rag_retrieve_with_sources
from .tools.report_tool import generate_report

logger = logging.getLogger(__name__)

# 工具执行超时默认值（秒）；系统配置页可在线调整，未配置时回退该值
TOOL_TIMEOUT_SECONDS = 30

_ANSWER_SYSTEM_PROMPT = (
    "你是企业内部知识库与业务数据助手。你必须直接、清晰地回答用户提出的问题："
    "先正面给出答案，再补充必要细节；不要输出与问题无关的资料、铺垫和废话；"
    # 【v1.18】原文是「仅依据提供的资料/数据回答」+「资料中没有就说找不到」，
    # 这两句在 direct 分支（用户关了检索 / 分类为闲聊）里没有任何资料，模型会
    # 直接把「知识库中未找到相关信息」当答案返回 —— 用户看到的就是「关了开关
    # 反而更差」。资料约束改成条件式：有资料才只用资料，没资料就用自身知识正常答。
    "仅在提供了资料/数据时，才要求只依据资料回答并注明来源；"
    "未提供任何资料的轮次，请直接用你自己的知识正常回答，"
    "不要声称检索过、也不要输出“知识库中未找到相关信息”这类本轮无据可依的话；"
    "不要编造，不要转移话题，不要泛泛罗列无关内容。"
    # 【v1.9 冲突检测】知识库里并存新旧制度时，模型最爱“二选一”或折中编一个数，
    # 这属于最隐蔽的幻觉（答案看着有出处，其实是模型自己裁决的）。这里给出死命令。
    "【冲突处理死命令】如果多份资料之间存在矛盾（例如不同的金额标准、天数、时限、"
    "适用范围），严禁自行判断、取舍或折中，必须客观并列列出各自的说法，"
    "并逐条注明其来源（资料序号或文档名）；如有必要，明确提示用户“资料之间存在冲突，"
    "请以最新/指定制度为准”。"
)


def _run_with_timeout(fn, *args, timeout: int | None = None):
    """在独立线程执行工具并施加超时（工具自建会话，线程安全）。

    超时值优先取系统配置页保存的覆盖值（热更新），未配置时回退默认 30 秒。
    使用 contextvars.copy_context 传播 trace 上下文，供工具重试写入 trace_tool_calls。
    """
    if timeout is None:
        timeout = int(runtime_config.get_value("tool_timeout_seconds"))
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    ctx = contextvars.copy_context()
    future = executor.submit(ctx.run, fn, *args)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise TimeoutError(f"工具执行超时（>{timeout}s）")
    finally:
        executor.shutdown(wait=False)


# 【v1.9 修补】业务库只有「设备 / 工单 / 告警」三张表。分类器只要看到"是多少 / 占比 / 统计"
# 就容易判成 db_query 或 report，但问题里没有任何一张表的关键词时（如"退款率""GMV"），
# 两个节点都只能拿到兜底概览文案 —— report 节点还会把它包装成《业务数据汇总报表》，
# 于是出现"答非所问却像正式结论"的伪报表。这类问题的答案要么在文档里、要么本就不存在，
# 因此统一纠偏到 rag，由生成层的「未找到」约束给出明确拒答。
_BUSINESS_DATA_KEYWORDS = (
    "设备", "服务器", "交换机", "防火墙", "工单", "告警", "台账", "资产",
    "device", "ticket", "alert",
)


def classify_node(state: AgentState) -> dict:
    """路由分类节点：先做短追问意图继承，未命中再走 LLM 结构化分类。

    【v1.12】继承短路：上一轮是 rag/db_query 且本轮是极短指代型追问时，
    直接沿用上一轮路由并**跳过 LLM 调用**（确定性 + 省一次调用）。
    判定规则见 `intent_inherit.decide_inherit`。
    """
    question = state["question"]

    inherited, reason = decide_inherit(state.get("last_route"), question)

    # 被对话参数禁用的路由不继承：否则会继承完又被 route_condition 回退成 direct，
    # 状态里的 route_inherited 与实际执行路径不符，日志容易误读。
    if inherited and (state.get("route_enabled") or {}).get(inherited) is False:
        logger.info("路由继承跳过: %s 已被对话参数禁用，改由 LLM 分类", inherited)
        inherited, reason = None, "target_route_disabled"

    if inherited:
        logger.info(
            "路由继承: %s -> %s (inherited_from_last_turn=true, reason=%s)",
            state.get("last_route"), inherited, reason,
        )
        return {"route": inherited, "route_inherited": True}

    llm = get_chat_model()
    route = classify_route(llm, question)

    # 纠偏：判成业务库/报表，但问题里没有任何业务表关键词 —— 业务库没有这份数据
    if route in ("db_query", "report"):
        lowered = (question or "").lower()
        if not any(k in lowered for k in _BUSINESS_DATA_KEYWORDS):
            logger.info("路由纠偏: %s -> rag（问题不含业务库表关键词，业务库无对应数据）", route)
            route = "rag"

    logger.info("路由决策: %s (inherited_from_last_turn=false, reason=%s)", route, reason)
    return {"route": route, "route_inherited": False}


def rag_node(state: AgentState) -> dict:
    """RAG 检索工具节点。

    检索条数优先取随请求传入的 top_k；未传时用知识库自身配置（kb.top_k），再回退 5。
    检索模式【新增】取随请求传入的 search_mode（vector_only/bm25_vector_hybrid）；未传时默认混合检索。
    【v1.9】同时带回结构化 sources，供前端渲染可点击的引用卡片。
    """
    kb_id = state.get("kb_id")
    if not kb_id:
        return {"tool_output": "未绑定知识库，无法执行 RAG 检索。", "sources": []}
    top_k = state.get("top_k") or None
    search_mode = state.get("search_mode") or None  # 【新增】
    output, sources = _run_with_timeout(
        rag_retrieve_with_sources, state["question"], kb_id, top_k, search_mode
    )
    return {"tool_output": output, "sources": sources}


def db_query_node(state: AgentState) -> dict:
    """业务库查询工具节点。"""
    output = _run_with_timeout(query_business_db, state["question"])
    return {"tool_output": output}


def _derive_report_title(question: str) -> str:
    """根据问题关键词推导报表标题。"""
    if any(k in question for k in ("工单", "ticket")):
        return "工单汇总报表"
    if any(k in question for k in ("设备", "device")):
        return "设备汇总报表"
    if any(k in question for k in ("告警", "alert")):
        return "告警汇总报表"
    return "业务数据汇总报表"


def report_node(state: AgentState) -> dict:
    """报表生成节点：先查业务库取数据，再调用 generate_report 产出 Markdown。

    【v1.9 修补】业务库取不到匹配数据时，直接返回「未找到」说明，不生成报表。
    报表节点不走 LLM，本身没有判断力 —— 一旦把兜底文案包进《业务数据汇总报表》，
    用户会当成结论使用，这比明确说"没有这份数据"危害大得多。
    """
    question = state["question"]
    data = _run_with_timeout(query_business_db, question)
    if data.startswith(NO_BUSINESS_DATA_MARK):
        logger.info("报表请求无匹配业务数据，返回未找到说明 question=%r", question)
        return {"final_answer": data, "tool_output": data}
    title = _derive_report_title(question)
    report = generate_report.invoke({"data": data, "title": title})
    return {"final_answer": report, "tool_output": report}


# ---------------- parallel_compare：轻量化多子 Agent 并行对比检索 ----------------

_EXTRACT_QUERIES_PROMPT = (
    "用户问题要求对比两份文档/资料。请从中提取两个独立的检索关键词或主题，"
    "只输出 JSON（不要输出其他内容）：\n"
    '{{"query_a": "资料A的检索词", "query_b": "资料B的检索词"}}\n\n'
    "用户问题：{question}"
)

# 规则切分兜底：去掉引导动词后按常见连接词拆分
_LEADING_VERB_RE = re.compile(r"^(请|帮我|麻烦)?(对比|比较)(一下)?")
_SPLIT_RE = re.compile(r"[、，,。]|和|与|跟|及|[Vv][Ss]")


def _fallback_split(question: str) -> tuple[str, str]:
    """LLM 拆词失败时的规则兜底：按连接词切成两段；切不开则两边都用原问题。"""
    text = _LEADING_VERB_RE.sub("", question).strip(" ？?：:")
    parts = [p.strip(" 的？?。.、") for p in _SPLIT_RE.split(text) if p.strip()]
    if len(parts) >= 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    return question, question


def _extract_compare_queries(question: str) -> tuple[str, str]:
    """从对比类问题中拆出两个独立检索词。

    用原生 llm.invoke + JSON 解析（不用 with_structured_output），
    避免嵌套 Chain 在 trace 中产生多余的步骤记录；失败回退规则切分。
    """
    try:
        llm = get_chat_model()
        resp = llm.invoke([HumanMessage(content=_EXTRACT_QUERIES_PROMPT.format(question=question))])
        text = (resp.content or "").strip()
        if text.startswith("```"):  # 兼容 ```json 包裹
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            data = json.loads(text[start:end + 1])
            qa, qb = str(data.get("query_a", "")).strip(), str(data.get("query_b", "")).strip()
            if qa and qb:
                return qa, qb
    except Exception:
        logger.debug("对比检索词 LLM 提取失败，回退规则切分", exc_info=True)
    return _fallback_split(question)


def _collect_retrieval(
    future: concurrent.futures.Future, label: str
) -> tuple[str, list[dict]]:
    """收取单路并行检索结果；超时/失败降级为提示文本，不阻断另一路。"""
    try:
        return future.result()
    except concurrent.futures.TimeoutError:
        return f"{label} 检索超时，未获取到相关资料。", []
    except Exception as exc:
        logger.warning("并行对比检索失败 %s: %s", label, exc)
        return f"{label} 检索失败：{exc}", []


def _shift_source_labels(text: str, offset: int) -> str:
    """把文本里的 [来源k] 整体顺延 offset，避免两路合并后标号撞车。"""
    return re.sub(r"\[来源(\d+)\]", lambda m: f"[来源{int(m.group(1)) + offset}]", text)


def parallel_compare_node(state: AgentState) -> dict:
    """并行对比检索节点：拆词 -> 两路并行 RAG 检索 -> 合并 Context。

    两路检索在独立线程并发执行（各自带超时与 trace 上下文传播），
    墙钟耗时约等于较慢一路，而非两路之和。
    【v1.9】两路来源合并后统一重新编号（A 在前、B 顺延），保证答案里的
    [来源i] 与前端引用卡片的 sources[i-1] 仍然一一对应。
    """
    kb_id = state.get("kb_id")
    if not kb_id:
        return {"tool_output": "未绑定知识库，无法执行对比检索。", "sources": []}
    top_k = state.get("top_k") or None
    search_mode = state.get("search_mode") or None  # 【新增】两路对比检索保持同一请求级模式

    query_a, query_b = _extract_compare_queries(state["question"])
    logger.info("并行对比检索: A=%r B=%r kb_id=%s", query_a, query_b, kb_id)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(_run_with_timeout, rag_retrieve_with_sources, query_a, kb_id, top_k, search_mode)
        fb = pool.submit(_run_with_timeout, rag_retrieve_with_sources, query_b, kb_id, top_k, search_mode)
        out_a, hits_a = _collect_retrieval(fa, "资料A")
        out_b, hits_b = _collect_retrieval(fb, "资料B")

    sources: list[dict] = []
    for group, hits in (("A", hits_a), ("B", hits_b)):
        for h in hits:
            item = dict(h)
            item["index"] = len(sources) + 1
            item["group"] = group
            sources.append(item)
    out_b = _shift_source_labels(out_b, len(hits_a))

    merged = (
        "以下是针对用户对比问题的两路并行检索结果。"
        "请对比分析资料A与资料B，按「相同点 / 差异点 / 结论」结构输出对比总结：\n\n"
        f"===== 资料 A（检索词：{query_a}）=====\n{out_a}\n\n"
        f"===== 资料 B（检索词：{query_b}）=====\n{out_b}"
    )
    return {"tool_output": merged, "sources": sources}


def answer_node(state: AgentState) -> dict:
    """生成回答节点：拼接历史 + 用户问题 + 工具结果，调用 LLM 输出最终答案。

    【修复】此前仅拼接 state["messages"]；评测链路 run_agent([], question) 的
    messages 为空，导致 LLM 收不到用户问题——direct 路由答"您没有提出具体问题"、
    rag 路由对着检索结果自由发挥答非所问。现确保当前问题一定在消息中：
    chat 链路末条 human 消息即问题（不重复注入），评测链路注入 question。
    """
    llm = get_chat_model()
    messages = [SystemMessage(content=_ANSWER_SYSTEM_PROMPT)] + list(state["messages"])
    question = (state.get("question") or "").strip()
    last_human = next(
        (m for m in reversed(messages) if isinstance(m, HumanMessage)), None
    )
    if question and not (
        last_human is not None and (last_human.content or "").strip() == question
    ):
        messages.append(HumanMessage(content=question))
    if state.get("tool_output"):
        messages.append(
            HumanMessage(
                content=f"【工具检索/查询结果】\n{state['tool_output']}\n\n"
                        "请基于以上结果直接回答上面的用户问题：先正面回答，再补充细节并注明来源；"
                        "若结果中没有足够信息，请明确说明“知识库中未找到相关信息”。"
            )
        )
    else:
        # 【v1.11】direct 路由：本轮没有任何检索结果。
        # 模型能看到完整历史消息，很容易「接着上一轮答」并顺手补一句
        # 「来源：《X》第N块」—— 这个出处是凭记忆贴的，本轮并没有证据。
        # 与其让前端拿不到 sources 只能默默渲染成灰标签，不如从生成层就禁止这种伪溯源。
        messages.append(
            HumanMessage(
                content=(
                    "【本轮未检索知识库】本次为直接回答模式，你手上没有任何资料或数据。\n"
                    "严禁输出「[来源N]」「来源：《文档名》第N块」这类溯源标注——本轮没有可引用的资料，"
                    "标注出处即属编造，会误导用户。\n"
                    "若答案来自上文对话中出现过的内容，请在相应位置注明"
                    "「（依据上文回答，本轮未重新检索）」，不要标注文档与块号。"
                )
            )
        )
    resp = llm.invoke(messages)
    answer = resp.content or ""
    # 【v1.16】截断感知：命中 max_tokens 时上游**不会**报错（HTTP 200 +
    # finish_reason=length），不主动检查就会把半截回答当完整结果返回并落库。
    # 提示直接追加在最终文本里（而不是只在这次响应里），保证刷新后依然可见。
    truncated = is_truncated_message(resp)
    if truncated:
        logger.warning(
            "回答被 max_tokens 截断（finish_reason=length），已追加提示。"
            "如需更长输出请调大 LLM_MAX_TOKENS，或让用户缩小问题范围。"
        )
        answer = append_notice(answer)
    return {"final_answer": answer, "truncated": truncated}


_TOOL_ROUTES = ("rag", "db_query", "report", "parallel_compare")


def disabled_route(state: AgentState) -> str | None:
    """分类想走的那路工具是否被对话页参数关掉；返回被关闭的路由名，否则 None。

    【v1.18】抽成独立函数是为了让「实际执行了什么」可被观测：条件边返回 direct
    只改变走哪条边，**不会改写 state["route"]**，于是对外仍显示「知识库检索」，
    用户明明关了开关却看到「知识库检索 + 未找到」，自然判断成开关没生效。
    """
    route = state.get("route", "direct")
    if route in _TOOL_ROUTES and (state.get("route_enabled") or {}).get(route) is False:
        return route
    return None


def route_condition(state: AgentState) -> str:
    """条件边：根据分类结果路由到对应节点。

    对话页「参数配置」可禁用某路工具（route_enabled），
    被禁用的路由回退为直接回答（direct），不中断问答。
    """
    blocked = disabled_route(state)
    if blocked:
        logger.info("路由 %s 已被对话参数禁用，回退直接回答", blocked)
        return "direct"
    route = state.get("route", "direct")
    return route if route in _TOOL_ROUTES else "direct"


_graph = None


def get_graph():
    """懒加载并缓存编译后的图。"""
    global _graph
    if _graph is None:
        builder = StateGraph(AgentState)
        builder.add_node("classify", classify_node)
        builder.add_node("rag", rag_node)
        builder.add_node("db_query", db_query_node)
        builder.add_node("report", report_node)
        builder.add_node("parallel_compare", parallel_compare_node)
        builder.add_node("answer", answer_node)

        builder.add_edge(START, "classify")
        builder.add_conditional_edges(
            "classify",
            route_condition,
            {
                "rag": "rag",
                "db_query": "db_query",
                "report": "report",
                "parallel_compare": "parallel_compare",
                "direct": "answer",
            },
        )
        builder.add_edge("rag", "answer")
        builder.add_edge("db_query", "answer")
        builder.add_edge("parallel_compare", "answer")
        builder.add_edge("report", END)   # 报表节点直接输出，无需再走 LLM
        builder.add_edge("answer", END)
        _graph = builder.compile()
    return _graph


def run_agent(
    messages: list,
    question: str,
    kb_id: int | None = None,
    trace_context: dict | None = None,
    options: dict | None = None,
) -> dict:
    """执行智能体，返回 {answer, route}。

    trace_context: 可选，含 trace_id / conversation_id / user_id / kb_id / question；
    传入后自动挂载 Trace 回调处理器采集全链路日志（日志失败不中断主流程）。
    options: 可选，随请求传入的运行参数，支持 top_k / search_mode / route_enabled 三键【新增 search_mode】。
    """
    handler = TraceCallbackHandler(**trace_context) if trace_context else None
    config = {"callbacks": [handler]} if handler else None
    graph = get_graph()
    try:
        result = graph.invoke({
            "messages": messages,
            "question": question,
            "kb_id": kb_id,
            "route": "",
            "tool_output": "",
            "final_answer": "",
            "sources": [],
            "top_k": (options or {}).get("top_k"),
            "search_mode": (options or {}).get("search_mode"),  # 【新增】请求级检索模式透传
            "route_enabled": (options or {}).get("route_enabled"),
            # 【v1.12】上一轮路由：供 classify_node 做短追问意图继承（None = 首轮）
            "last_route": (options or {}).get("last_route"),
            "route_inherited": False,
            # 【v1.16】答案是否被 max_tokens 截断（默认 False，由 answer_node 覆写）
            "truncated": False,
        }, config=config)
    except Exception:
        if handler:
            handler.flush(status="failed")
        raise
    else:
        if handler:
            # 兜底：若回调未捕获到 classify 节点，则从图最终结果补上 route
            if handler.route is None:
                handler.route = result.get("route")
            # 【v1.16】图上已判定截断时同步给 Trace（回调侧也独立检测过一次，
            # 这里做二次兜底：任何一侧判到截断都落库 is_truncated=true）
            if result.get("truncated"):
                handler.is_truncated = True
            handler.flush(status="success")
    # 【v1.18】对外只报「实际执行的」路由：被参数关闭的那一路已回退 direct，
    # 仍报原路由会让前端徽标、落库 agent_route、大盘统计全部与实际不符。
    requested_route = result.get("route", "direct")
    blocked = disabled_route(result)
    return {
        "answer": result.get("final_answer", ""),
        "route": "direct" if blocked else requested_route,
        # 分类本来想走哪一路、以及是哪一路被参数关掉（便于前端说明"为什么没检索"）
        "route_requested": requested_route,
        "route_disabled": blocked,
        # 【v1.9】把工具侧上下文一并带回：供后置引用校验（citation_guard）比对硬事实，
        # 不改动 answer 本身，也不改变既有调用方对 answer/route 的读取方式。
        "context": result.get("tool_output", "") or "",
        # 【v1.9】结构化来源（文档名/块号/相似度/原文），供前端引用卡片溯源
        "sources": result.get("sources", []) or [],
        # 【v1.12】本次路由是否继承自上一轮（跳过 LLM 分类）
        "route_inherited": bool(result.get("route_inherited")),
        # 【v1.16】本次回答是否被 max_tokens 截断（answer 末尾已带提示文案）
        "truncated": bool(result.get("truncated")),
    }
