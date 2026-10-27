"""SSE 流式问答服务。

复用现有 LangGraph 图（get_graph）的 astream 能力，不改动 graph.py 业务逻辑：
- rag / db_query / direct：通过 stream_mode="messages" 流式返回 answer 节点的大模型片段；
- report：通过 stream_mode="updates" 拿到 report 节点产出的完整 Markdown，直接流式输出（不调 LLM）。
复用 TraceCallbackHandler 与 agent_service 的会话/知识库/消息工具函数。

注意：本服务自建 DB 会话（async 端点不能跨线程复用依赖注入的同步 session）。
"""
import json
import logging
import uuid

from langchain_core.messages import HumanMessage

from ..agent.graph import disabled_route, get_graph
from ..core.database import SessionLocal
from ..core.exceptions import ForbiddenError, NotFoundError
from ..core.llm_errors import llm_error_hint
from ..core.tracing import TraceCallbackHandler
from ..llm.truncation import TRUNCATION_NOTICE, append_notice
from ..models import Conversation
from ..rag.citation_guard import validate_citations
from ..services.agent_service import (
    _check_kb_access,
    _maybe_autotitle,
    _safe_write_failed,
    _to_langchain_message,
    _write_message,
    latest_assistant_route,
    load_history_window,
)

logger = logging.getLogger(__name__)


def _sse(event: str, data: dict) -> str:
    """构造一条 SSE 事件。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def stream_chat(
    user,
    conversation_id: int,
    question: str,
    kb_id: int | None = None,
    options: dict | None = None,
):
    """流式问答异步生成器，逐条 yield SSE 事件字符串。

    options: 可选，对话页随请求传入的运行参数（top_k / route_enabled），透传给智能体图。
    事件：route / delta / report / done / error
    """
    db = SessionLocal()
    try:
        conversation = db.get(Conversation, conversation_id)
        if not conversation:
            yield _sse("error", {"message": "会话不存在"})
            return
        if conversation.user_id != user.id:  # 归属校验（端点已用 get_own_conversation 兜底）
            yield _sse("error", {"message": "无权访问该会话"})
            return

        # 1) 解析知识库并校验权限（复用 agent_service 的校验逻辑）
        if kb_id is None:
            kb_id = conversation.kb_id
        try:
            _check_kb_access(db, user, kb_id)
        except (NotFoundError, ForbiddenError) as exc:
            yield _sse("error", {"message": str(exc.message)})
            return

        # 2) 加载会话历史 + 组装 LangChain 消息（【v1.16】状态过滤 + 最近 N 轮窗口）
        history = load_history_window(db, conversation.id)
        lc_messages = [m for m in (_to_langchain_message(x) for x in history) if m is not None]
        lc_messages.append(HumanMessage(content=question))

        trace_id = str(uuid.uuid4())

        # 3) 写用户提问（先落库）
        try:
            _write_message(db, conversation, user, "user", question, "done", trace_id)
        except Exception:
            logger.exception("写用户提问失败 conversation_id=%s", conversation.id)
            yield _sse("error", {"message": "保存消息失败"})
            return

        # 3.2 首问自动填充会话标题（title 为空才填，重命名过的会话不覆盖）
        _maybe_autotitle(db, conversation, question)

        # 4) 挂载 Trace 回调（与消息共用同一 trace_id）
        handler = TraceCallbackHandler(
            trace_id=trace_id,
            conversation_id=conversation.id,
            user_id=user.id,
            kb_id=kb_id,
            question=question,
        )

        graph = get_graph()
        input_state = {
            "messages": lc_messages,
            "question": question,
            "kb_id": kb_id,
            "route": "",
            "tool_output": "",
            "final_answer": "",
            "top_k": (options or {}).get("top_k"),
            "route_enabled": (options or {}).get("route_enabled"),
            # 【v1.12】上一轮路由，供短追问意图继承（首轮为 None）
            "last_route": latest_assistant_route(db, conversation.id),
            "route_inherited": False,
            # 【v1.16】答案是否被 max_tokens 截断（answer 节点写入）
            "truncated": False,
        }

        route = "direct"
        # 【v1.18】被对话参数关掉的那一路（None = 未关闭）。条件边回退 direct 时
        # 不会改写 state["route"]，必须在这里独立记录，否则前端徽标 / 落库
        # agent_route / 大盘统计都会显示成"其实没执行的那条路由"。
        blocked_route: str | None = None
        route_inherited = False  # 【v1.12】本次路由是否继承上一轮
        answer_parts: list[str] = []
        report_text = ""
        # 【v1.9】工具侧上下文：供后置引用校验比对硬事实（数字 / 日期）
        tool_context = ""
        # 【v1.9】结构化检索来源：随 done 事件下发，供前端引用卡片溯源
        sources: list[dict] = []
        # 【v1.16】本次回答是否被 max_tokens 截断（answer 节点的 update 里带出）
        truncated = False

        try:
            async for mode, chunk in graph.astream(
                input_state,
                config={"callbacks": [handler]},
                stream_mode=["messages", "updates"],
            ):
                if mode == "messages":
                    message, metadata = chunk if isinstance(chunk, tuple) else (chunk, {})
                    content = getattr(message, "content", "") or ""
                    if content:
                        answer_parts.append(content)
                        yield _sse("delta", {"content": content})

                elif mode == "updates":
                    for node_name, output in (chunk or {}).items():
                        if node_name == "classify" and isinstance(output, dict):
                            route = output.get("route", route)
                            # 【v1.12】inherited=true 表示路由继承自上一轮（跳过 LLM 分类）
                            route_inherited = bool(output.get("route_inherited"))
                            # 【v1.18】分类结果若已被对话参数关闭，对外改报 direct，
                            # 并把被关的那一路单独下发，前端才能解释"为什么没检索"
                            blocked_route = disabled_route({
                                "route": route,
                                "route_enabled": input_state.get("route_enabled"),
                            })
                            yield _sse("route", {
                                "route": "direct" if blocked_route else route,
                                "requested": route,
                                "disabled": blocked_route,
                                "inherited": route_inherited,
                            })
                        elif node_name == "report" and isinstance(output, dict):
                            report_text = output.get("final_answer") or output.get("tool_output") or ""
                            yield _sse("report", {"content": report_text})
                        if node_name == "answer" and isinstance(output, dict):
                            # 【v1.16】answer 节点已按 finish_reason 判定截断
                            truncated = bool(output.get("truncated"))
                        if isinstance(output, dict) and output.get("tool_output"):
                            # rag / db_query / parallel_compare / report 都会带回工具结果
                            tool_context = str(output["tool_output"])
                        if isinstance(output, dict) and output.get("sources"):
                            sources = output["sources"] or []

            final_answer = report_text or "".join(answer_parts)
            # 【v1.16】截断提示必须补在**最终文本**上：delta 已逐字推给前端，
            # 提示不能混进 delta（那样前端会重复追加），改由 done 事件下发文案，
            # 同时写进落库正文 —— 保证「当时看到什么」与「刷新后看到什么」一致。
            if truncated:
                final_answer = append_notice(final_answer)
                logger.warning(
                    "流式回答被 max_tokens 截断 conversation_id=%s trace_id=%s",
                    conversation.id, trace_id,
                )
            citation = validate_citations(final_answer, tool_context)
            # 【v1.18】落库/落表一律记「实际执行的」路由
            effective_route = "direct" if blocked_route else route
            if handler.route is None:
                handler.route = effective_route
            if truncated:
                handler.is_truncated = True  # 与回调侧检测取并集，保证 trace 落库
            handler.flush(status="success")

            assistant_msg = _write_message(
                db, conversation, user, "assistant", final_answer, "done",
                trace_id, agent_route=effective_route,
                sources=sources or None,  # 【v1.10】来源快照落库，历史消息也能溯源
            )
            yield _sse("done", {
                "trace_id": trace_id,
                "conversation_id": conversation.id,
                "message_id": assistant_msg.id,
                "agent_route": effective_route,
                # 【v1.18】分类本来想走的路由 + 被对话参数关闭的路由（None=未关闭）。
                # 前端据此在徽标上标「已关闭：知识库检索」，否则用户只会看到一个
                # 与开关无关的「直接回答」，无法确认开关到底有没有生效。
                "route_requested": route,
                "route_disabled": blocked_route,
                # 【v1.9】引用校验结果（suspect=true 时前端展示「待核验」标记）
                "suspect": citation["suspect"],
                "citation_misses": citation["misses"],
                # 【v1.9】结构化来源，前端据此把 [来源i] 渲染成可点击标签
                "sources": sources,
                # 【v1.12】本次路由是否继承自上一轮（短追问意图继承）
                "route_inherited": route_inherited,
                # 【v1.16】输出是否被 max_tokens 截断；notice 由前端追加到气泡末尾
                # （文案只此一份，前端不硬编码，改口径只改后端）
                "truncated": truncated,
                "truncation_notice": TRUNCATION_NOTICE if truncated else "",
            })

        except Exception as exc:
            # SSE 内部异常：写失败 trace、写失败消息，向前端推送 error 事件，服务不崩溃
            logger.exception("流式问答失败 conversation_id=%s", conversation.id)
            try:
                handler.flush(status="failed")
            except Exception:
                logger.exception("流式 Trace 落库失败 trace_id=%s", trace_id)
            try:
                _safe_write_failed(db, conversation, user, trace_id, exc)
            except Exception:
                logger.exception("写失败消息也失败 conversation_id=%s", conversation.id)
            # 【v1.14】透出可行动的原因（余额不足/密钥失效/限流/超时），
            # 原始异常文本不外泄（可能含 base_url、密钥片段）。
            yield _sse("error", {"message": llm_error_hint(exc)})
    finally:
        db.close()
