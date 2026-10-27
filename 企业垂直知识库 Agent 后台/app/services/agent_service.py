"""Agent 智能体服务编排：加载会话历史、执行智能体、写入消息。"""
import logging
import uuid

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..agent.graph import run_agent
from ..config import settings
from ..core.exceptions import BizError, ForbiddenError, NotFoundError
from ..core.llm_errors import llm_error_hint
from ..models import Conversation, KnowledgeBase, Message
from ..models.message import MSG_DONE
from ..rag.citation_guard import validate_citations

logger = logging.getLogger(__name__)

# 进入上下文的角色白名单：system 由各节点自己注入，tool 消息缺 tool_call_id
# （LangChain 校验会拒绝），因此历史里只放行 user / assistant。
_HISTORY_ROLES = ("user", "assistant")


def _check_kb_access(db: Session, user, kb_id: int | None) -> None:
    """校验用户对知识库的访问权限（admin 全量，普通用户仅本人创建）。"""
    if kb_id is None:
        return
    kb = db.get(KnowledgeBase, kb_id)
    if not kb:
        raise NotFoundError("知识库不存在")
    if user.role != "admin" and kb.owner_id != user.id:
        raise ForbiddenError("无权访问该知识库")


def _to_langchain_message(msg: Message):
    """ORM 消息 -> LangChain 消息；tool 消息无 tool_call_id，暂不纳入历史。"""
    if msg.role == "user":
        return HumanMessage(content=msg.content)
    if msg.role == "assistant":
        return AIMessage(content=msg.content)
    if msg.role == "system":
        return SystemMessage(content=msg.content)
    return None


# ---------------------------------------------------------------- 历史窗口（v1.16）


def _trim_to_turns(rows: list[Message], max_turns: int) -> list[Message]:
    """只保留最近 `max_turns` 轮（1 轮 = 1 条 user + 其后的 assistant）。

    以 **user 消息**为轮次边界从后往前数，第 N 个 user 的位置即窗口起点：
    这样「末轮只有提问、回答还没落库」的中间态也不会算错轮数。
    """
    if max_turns <= 0:
        return []
    seen = 0
    for idx in range(len(rows) - 1, -1, -1):
        if rows[idx].role == "user":
            seen += 1
            if seen == max_turns:
                return rows[idx:]
    return rows  # 轮数不足，全量保留


def _trim_to_chars(rows: list[Message], max_chars: int) -> list[Message]:
    """按字符预算从**最早**的消息开始丢弃，直到总量 <= max_chars。

    为什么在轮数之外还要字符预算：5 轮不代表可控——用户粘一份 8000 字文档进来，
    5 轮就是 4 万字符的 prompt。两个维度必须同时封顶。

    丢弃后若开头剩一条 assistant（它的提问已被丢掉），一并移除：
    一条没有对应提问的 AI 发言会让模型误以为「这是我自己说过的话」，反而更容易被带偏。
    """
    if max_chars <= 0:
        return []
    total = sum(len(r.content or "") for r in rows)
    start = 0
    while start < len(rows) and total > max_chars:
        total -= len(rows[start].content or "")
        start += 1
    kept = rows[start:]
    while kept and kept[0].role != "user":
        kept = kept[1:]
    return kept


def load_history_window(
    db: Session,
    conversation_id: int,
    *,
    max_turns: int | None = None,
    max_chars: int | None = None,
) -> list[Message]:
    """加载进入 LLM 上下文的历史消息：状态过滤 + 轮数窗口 + 字符预算。

    改造前是 `db.query(Message).filter(cid).order_by(id).all()`——两个问题：
    1. **无上限**：会话越长 prompt 越大，成本与首字延迟随轮数线性增长，
       最终撞上模型上下文窗口直接报错（而不是优雅降级）；
    2. **不过滤状态**：`failed` 消息的正文是「LLM 未返回内容/余额不足」这类
       错误文案，被当成 assistant 回复喂回去，模型会顺着错误话术继续编。

    现在：SQL 层先按状态过滤并只取尾部 `max_turns*2+1` 条（不再 .all()），
    再在内存里按轮数与字符双预算裁剪，返回**时间正序**的消息列表。

    参数缺省取 .env：CHAT_HISTORY_MAX_TURNS（默认 5）/ CHAT_HISTORY_MAX_CHARS（默认 2000）。
    """
    max_turns = int(settings.chat_history_max_turns if max_turns is None else max_turns)
    max_chars = int(settings.chat_history_max_chars if max_chars is None else max_chars)

    rows = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .filter(Message.role.in_(_HISTORY_ROLES))
        # 状态过滤：user 提问一律保留（提问本身总是有效的），
        # assistant 只认 done —— failed / running 的正文都不是「已完成的回答」。
        .filter(or_(Message.role == "user", Message.status == MSG_DONE))
        .order_by(Message.id.desc())
        # SQL 层就裁掉绝大部分历史，避免把整表拉进内存再筛
        .limit(max_turns * 2 + 1)
        .all()
    )
    rows.reverse()
    rows = _trim_to_turns(rows, max_turns)
    rows = _trim_to_chars(rows, max_chars)
    logger.debug(
        "历史窗口 conversation_id=%s 保留=%s条 字符=%s（上限 %s轮/%s字）",
        conversation_id, len(rows), sum(len(r.content or "") for r in rows),
        max_turns, max_chars,
    )
    return rows


def latest_assistant_route(db: Session, conversation_id: int) -> str | None:
    """取最近一条带路由的 assistant 消息的 agent_route（v1.12 意图继承用）。

    【v1.16】独立单行查询，不复用上文的历史窗口：窗口会过滤 failed 消息、
    也会被轮数/字符预算截掉，而「上一轮的真实路由」必须与窗口无关——
    否则同一个追问在长会话里会突然不继承了，行为不可解释。
    """
    row = (
        db.query(Message.agent_route)
        .filter(Message.conversation_id == conversation_id)
        .filter(Message.role == "assistant")
        .filter(Message.agent_route.isnot(None))
        .order_by(Message.id.desc())
        .limit(1)
        .first()
    )
    return row[0] if row and row[0] else None


def last_assistant_route(history: list[Message]) -> str | None:
    """在给定的消息列表里取最近一条带路由的 assistant 的 agent_route。

    跳过 failed 之外的判断：失败消息也可能带着已判定好的 route，沿用无害；
    只要求 route 非空，避免把首轮（无任何 assistant 消息）误当成「上一轮是 direct」。

    【v1.16】主链路已改用 `latest_assistant_route(db, conversation_id)`：
    历史不再是全量列表，本函数保留供「已有消息列表在手」的场景（如单测）使用。
    """
    for msg in reversed(history or []):
        if msg.role == "assistant" and msg.agent_route:
            return msg.agent_route
    return None


def _write_message(
    db: Session,
    conversation: Conversation,
    user,
    role: str,
    content: str,
    status: str,
    trace_id: str,
    agent_route: str | None = None,
    error_msg: str | None = None,
    sources: list[dict] | None = None,
) -> Message:
    """写消息；数据库异常时回滚并上抛，由调用方转换为统一错误。"""
    try:
        msg = Message(
            conversation_id=conversation.id,
            user_id=user.id,
            role=role,
            content=content,
            status=status,
            trace_id=trace_id,
            agent_route=agent_route,
            error_msg=error_msg,
            sources=sources or None,
        )
        db.add(msg)
        db.commit()
        db.refresh(msg)
        return msg
    except Exception:
        db.rollback()
        raise


def _safe_write_failed(db, conversation, user, trace_id: str, exc: Exception) -> None:
    """尽力写入一条失败状态的 assistant 消息，自身异常仅记录日志。

    「内容」写**用户可读的失败原因**，不再写空串：前端对 failed 消息渲染的是
    content（Chat.vue 的 `m.status === 'failed'` 分支），空串会留一个空气泡；
    更糟的是刷新页面从历史加载后，用户完全看不出为什么失败，只能再点一次。
    原始异常（可能含 base_url / 密钥片段）仍然只落 error_msg，不外显。
    """
    try:
        _write_message(
            db, conversation, user, "assistant",
            llm_error_hint(exc), "failed", trace_id, error_msg=str(exc)[:500],
        )
    except Exception:
        db.rollback()
        logger.exception("写入失败消息也失败 conversation_id=%s", conversation.id)


AUTO_TITLE_MAX_LEN = 30


def build_auto_title(question: str) -> str | None:
    """由首问生成会话标题：压缩空白后截断到 30 字（超出补省略号）。"""
    compact = " ".join(question.split())
    if not compact:
        return None
    if len(compact) > AUTO_TITLE_MAX_LEN:
        return compact[:AUTO_TITLE_MAX_LEN] + "…"
    return compact


def _maybe_autotitle(db, conversation: Conversation, question: str) -> None:
    """首问落库后自动填充会话标题（title 为空才填；用户重命名后永不覆盖）。

    尽力而为：标题写入失败只记日志，不影响问答主链路。
    """
    if conversation.title:
        return
    title = build_auto_title(question)
    if not title:
        return
    try:
        conversation.title = title
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("自动填充会话标题失败 conversation_id=%s", conversation.id)


def chat(
    db: Session,
    user,
    conversation: Conversation,
    question: str,
    kb_id: int | None = None,
    options: dict | None = None,
) -> dict:
    """执行一次 Agent 问答：加载历史 -> 写提问 -> 运行智能体 -> 写回复。

    options: 可选，对话页随请求传入的运行参数（top_k / route_enabled），
    仅透传给智能体，缺省时与旧实现行为一致。
    """
    # 1. 解析知识库（未显式传参则用会话绑定的），并校验权限
    if kb_id is None:
        kb_id = conversation.kb_id
    _check_kb_access(db, user, kb_id)

    # 2. 加载会话历史（【v1.16】状态过滤 + 最近 N 轮 + 字符预算，不再全量 .all()）
    history = load_history_window(db, conversation.id)
    lc_messages = [m for m in (_to_langchain_message(x) for x in history) if m is not None]
    lc_messages.append(HumanMessage(content=question))

    trace_id = str(uuid.uuid4())

    # 3. 写用户提问（先落库，即使 AI 执行失败也保留提问记录）
    try:
        _write_message(db, conversation, user, "user", question, "done", trace_id)
    except Exception:
        logger.exception("写入用户提问失败 conversation_id=%s", conversation.id)
        raise BizError(500, "保存消息失败")

    # 3.2 首问自动填充会话标题（title 为空才填，重命名过的会话不覆盖）
    _maybe_autotitle(db, conversation, question)

    # 3.5 组装 Trace 上下文（与消息共用同一 trace_id，便于关联）
    trace_context = {
        "trace_id": trace_id,
        "conversation_id": conversation.id,
        "user_id": user.id,
        "kb_id": kb_id,
        "question": question,
    }

    # 4. 运行智能体（LLM 调用失败 / 工具超时分别处理）
    # 【v1.12】把上一轮路由透传给 classify_node，供短追问意图继承判定
    run_options = dict(options or {})
    run_options["last_route"] = latest_assistant_route(db, conversation.id)
    try:
        result = run_agent(lc_messages, question, kb_id, trace_context=trace_context, options=run_options)
    except TimeoutError as exc:
        logger.exception("工具调用超时 conversation_id=%s", conversation.id)
        _safe_write_failed(db, conversation, user, trace_id, exc)
        raise BizError(504, "工具调用超时，请稍后重试")
    except Exception as exc:
        logger.exception("Agent 执行失败 conversation_id=%s", conversation.id)
        _safe_write_failed(db, conversation, user, trace_id, exc)
        # 【v1.14】按异常分类给出可行动的原因（余额不足 / 密钥失效 / 限流 / 超时），
        # 不再统一吞成「请稍后重试」——欠费重试一万次也不会成功。
        raise BizError(502, llm_error_hint(exc))

    # 4.5 【v1.9】后置引用校验：答案里的长数字 / 日期能否在检索上下文里找到。
    # 只标记不拦截（防误杀），命中时 citation_guard 内部已打 WARNING 日志。
    citation = validate_citations(result["answer"], result.get("context", ""))

    # 5. 写 AI 回复（【v1.10】连同 sources 快照落库，历史消息也能溯源）
    try:
        assistant_msg = _write_message(
            db, conversation, user, "assistant", result["answer"], "done",
            trace_id, agent_route=result["route"],
            sources=result.get("sources") or None,
        )
    except Exception:
        logger.exception("写入 AI 回复失败 conversation_id=%s", conversation.id)
        raise BizError(500, "保存回复失败")

    return {
        "answer": result["answer"],
        "agent_route": result["route"],
        # 【v1.18】分类本来想走的路由 / 被对话参数关闭的路由（None=未关闭）
        "route_requested": result.get("route_requested", result["route"]),
        "route_disabled": result.get("route_disabled"),
        "trace_id": trace_id,
        "conversation_id": conversation.id,
        "message_id": assistant_msg.id,
        # 【v1.9】引用校验结果：suspect=true 表示答案含上下文中找不到的硬事实
        "suspect": citation["suspect"],
        "citation_misses": citation["misses"],
        # 【v1.9】结构化来源：与答案里的 [来源i] 一一对应，供前端引用卡片溯源
        "sources": result.get("sources", []) or [],
        # 【v1.12】本次路由是否继承自上一轮（短追问意图继承，跳过 LLM 分类）
        "route_inherited": bool(result.get("route_inherited")),
        # 【v1.16】本次回答是否被 max_tokens 截断（answer 末尾已带提示文案）
        "truncated": bool(result.get("truncated")),
    }
