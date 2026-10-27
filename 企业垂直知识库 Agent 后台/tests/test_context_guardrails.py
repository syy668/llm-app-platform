"""【v1.16】输出截断护栏 + 历史滑动窗口护栏 单元测试。

两项改造各自解决一个「静默故障」：

A. 输出端硬限制与截断感知
   - `max_tokens` 未下发时上限由上游默认值决定（ChatAnthropic 默认仅 1024）；
   - 命中上限时上游返回 HTTP 200 + finish_reason=length，**不抛异常**，
     半截回答会被当成正常结果落库，事后没有任何线索。
   本组用例锁定：参数真的下发到客户端、finish_reason 被正确识别、
   提示被追加到最终文本、trace_logs.is_truncated 真的落库。

B. 历史消息滑动窗口 + 状态过滤
   改造前 `.all()` 拉全量且不看 status：长会话 prompt 线性膨胀，
   失败消息（正文是错误文案）被当作 AI 上一轮回复喂回模型。
   本组用例锁定：轮数超限被丢弃、failed 被过滤、字符预算生效、
   丢弃后不留「悬空的 assistant 开头」。

全部用例离线运行（sqlite + mock LLM），不依赖 MySQL / Redis / 真实模型。
"""
import asyncio

import pytest

from app.agent import graph
from app.agent.graph import answer_node
from app.config import settings
from app.core import tracing
from app.llm import factory
from app.llm.truncation import (
    TRUNCATION_NOTICE,
    append_notice,
    is_truncated_message,
    is_truncated_result,
)
from app.models import Conversation, Message
from app.models.trace_log import TraceLog
from app.services import agent_service, stream_service
from app.services.agent_service import (
    _trim_to_chars,
    _trim_to_turns,
    load_history_window,
)

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

# ---------------------------------------------------------------------------
# A. 截断识别
# ---------------------------------------------------------------------------


def test_is_truncated_message_openai_finish_reason_length():
    """OpenAI 兼容协议：finish_reason=length 即截断。"""
    msg = AIMessage(content="半截话", response_metadata={"finish_reason": "length"})
    assert is_truncated_message(msg) is True


def test_is_truncated_message_anthropic_stop_reason_max_tokens():
    """Anthropic 用的是 stop_reason=max_tokens，字段名与取值都不同。"""
    msg = AIMessage(content="半截话", response_metadata={"stop_reason": "max_tokens"})
    assert is_truncated_message(msg) is True


@pytest.mark.parametrize("reason", ["stop", "end_turn", None, ""])
def test_is_truncated_message_normal_finish(reason):
    """正常结束 / 无元数据（如 Mock 模型）一律不算截断。"""
    metadata = {} if reason is None else {"finish_reason": reason}
    assert is_truncated_message(AIMessage(content="完整回答", response_metadata=metadata)) is False
    assert is_truncated_message(AIMessage(content="完整回答")) is False


def test_is_truncated_result_scans_all_generations():
    """Trace 回调拿到的是 LLMResult，也要能识别（batch 下任一条截断即算）。"""
    ok = ChatGeneration(message=AIMessage(content="a", response_metadata={"finish_reason": "stop"}))
    cut = ChatGeneration(message=AIMessage(content="b", response_metadata={"finish_reason": "length"}))

    assert is_truncated_result(LLMResult(generations=[[ok], [cut]])) is True
    assert is_truncated_result(LLMResult(generations=[[ok]])) is False


def test_append_notice_is_idempotent():
    """重复追加不应把提示叠两遍（重试/二次加工场景）。"""
    once = append_notice("正文")
    assert once.endswith(TRUNCATION_NOTICE)
    assert append_notice(once) == once


# ---------------------------------------------------------------------------
# A2. answer 节点：截断时追加提示并回传标记
# ---------------------------------------------------------------------------


class _FakeLLM:
    """只实现 invoke 的假模型：返回构造时给定的 AIMessage。"""

    def __init__(self, response):
        self.response = response

    def invoke(self, messages):
        return self.response


def _state(question="住宿标准是多少"):
    return {"messages": [], "question": question, "tool_output": "【来源1】住宿标准 500 元"}


def test_answer_node_appends_notice_when_truncated(monkeypatch):
    """命中 max_tokens：最终文本末尾必须带提示，且 state 标记 truncated=True。"""
    resp = AIMessage(content="住宿标准为", response_metadata={"finish_reason": "length"})
    monkeypatch.setattr(graph, "get_chat_model", lambda *a, **kw: _FakeLLM(resp))

    out = answer_node(_state())

    assert out["truncated"] is True
    assert out["final_answer"].startswith("住宿标准为")
    assert out["final_answer"].endswith(TRUNCATION_NOTICE)


def test_answer_node_keeps_answer_when_finished(monkeypatch):
    """正常结束不追加任何东西（提示只应出现在真截断时）。"""
    resp = AIMessage(content="住宿标准为 500 元", response_metadata={"finish_reason": "stop"})
    monkeypatch.setattr(graph, "get_chat_model", lambda *a, **kw: _FakeLLM(resp))

    out = answer_node(_state())

    assert out["truncated"] is False
    assert out["final_answer"] == "住宿标准为 500 元"
    assert TRUNCATION_NOTICE not in out["final_answer"]


# ---------------------------------------------------------------------------
# A3. max_tokens 真的下发到客户端
# ---------------------------------------------------------------------------


def test_max_tokens_forwarded_to_openai(monkeypatch):
    """工厂必须把 max_tokens 显式传给 ChatOpenAI（不传则由上游默认值决定）。"""
    factory.reset_cache()
    llm = factory._create_model(
        "openai", "deepseek-chat", "https://api.example.com/v1", "sk-test",
        0.1, 30.0, 1, max_tokens=1024,
    )
    assert llm.max_tokens == 1024


def test_max_tokens_forwarded_to_anthropic(monkeypatch):
    """ChatAnthropic 的默认上限仅 1024，比本系统默认值更短，必须显式覆盖。"""
    pytest.importorskip("langchain_anthropic")
    factory.reset_cache()
    llm = factory._create_model(
        "anthropic", "claude-sonnet-4-5", "", "sk-ant-test",
        0.1, 30.0, 1, max_tokens=1024,
    )
    assert llm.max_tokens == 1024


def test_get_chat_model_reads_max_tokens_from_settings(monkeypatch):
    """默认值来自 LLM_MAX_TOKENS（settings.llm_max_tokens），不写死在调用点。"""
    factory.reset_cache()
    monkeypatch.setattr(factory, "get_provider", lambda: "openai")
    monkeypatch.setattr(factory, "_model_name", lambda p: "deepseek-chat")
    monkeypatch.setattr(factory, "_api_base", lambda: "https://api.example.com/v1")
    monkeypatch.setattr(factory, "_api_key", lambda: "sk-test")
    monkeypatch.setattr(settings, "llm_max_tokens", 512)

    assert factory.get_chat_model().max_tokens == 512
    factory.reset_cache()


def test_generate_text_appends_notice_when_truncated(monkeypatch):
    """/kbs/{id}/query 链路（generate_text）也要带截断提示，口径与 Agent 链路一致。"""
    resp = AIMessage(content="根据资料", response_metadata={"finish_reason": "length"})
    monkeypatch.setattr(factory, "get_chat_model", lambda *a, **kw: _FakeLLM(resp))

    text = factory.generate_text("【资料】x\n\n【问题】y", system="s")

    assert text.endswith(TRUNCATION_NOTICE)
    assert text.startswith("根据资料")


# ---------------------------------------------------------------------------
# B. 历史窗口：纯函数部分
# ---------------------------------------------------------------------------


def _msg(role: str, content: str, status: str = "done") -> Message:
    """构造未落库的消息对象（纯函数测试无需 DB）。"""
    return Message(conversation_id=1, user_id=1, role=role, content=content, status=status)


def test_trim_to_turns_keeps_latest_n_turns():
    """7 轮 -> 只保留最近 3 轮（以 user 为轮次边界）。"""
    rows = []
    for i in range(7):
        rows.append(_msg("user", f"问题{i}"))
        rows.append(_msg("assistant", f"回答{i}"))

    kept = _trim_to_turns(rows, 3)

    assert [m.content for m in kept] == ["问题4", "回答4", "问题5", "回答5", "问题6", "回答6"]


def test_trim_to_turns_keeps_all_when_fewer_turns():
    rows = [_msg("user", "问题0"), _msg("assistant", "回答0")]
    assert _trim_to_turns(rows, 5) == rows


def test_trim_to_chars_drops_oldest_first():
    """字符预算不足时，从最早的消息开始丢，保留最新的上下文。"""
    rows = [
        _msg("user", "旧" * 100),
        _msg("assistant", "旧答" * 100),
        _msg("user", "新问题"),
        _msg("assistant", "新回答"),
    ]

    kept = _trim_to_chars(rows, 20)

    assert [m.content for m in kept] == ["新问题", "新回答"]


def test_trim_to_chars_removes_orphan_assistant_head():
    """提问被丢掉后，孤立的 assistant 回复也要移除，避免模型以为这就是完整上下文。"""
    rows = [
        _msg("user", "很长的问题" * 50),
        _msg("assistant", "被保留的回答"),
        _msg("user", "新问题"),
    ]

    kept = _trim_to_chars(rows, 20)

    assert kept and kept[0].role == "user"
    assert [m.content for m in kept] == ["新问题"]


# ---------------------------------------------------------------------------
# B2. 历史窗口：SQL 层（状态过滤 + limit + 双预算）
# ---------------------------------------------------------------------------


@pytest.fixture
def hist_db(sqlite_engine):
    from sqlalchemy.orm import sessionmaker

    Conversation.__table__.create(sqlite_engine)
    Message.__table__.create(sqlite_engine)
    db = sessionmaker(bind=sqlite_engine)()
    yield db
    db.close()


def _make_conv(db) -> Conversation:
    conv = Conversation(user_id=1, kb_id=None, title="t")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def test_load_history_window_filters_failed_assistant(hist_db):
    """核心断言：failed 的 assistant 正文是错误提示，绝不能进上下文。"""
    conv = _make_conv(hist_db)
    hist_db.add_all([
        Message(conversation_id=conv.id, user_id=1, role="user", content="第一个问题", status="done"),
        Message(conversation_id=conv.id, user_id=1, role="assistant",
                content="正常回答", status="done"),
        Message(conversation_id=conv.id, user_id=1, role="user", content="第二个问题", status="done"),
        Message(conversation_id=conv.id, user_id=1, role="assistant",
                content="LLM 账户余额不足，请充值后重试。", status="failed"),
    ])
    hist_db.commit()

    rows = load_history_window(hist_db, conv.id)

    contents = [m.content for m in rows]
    assert "LLM 账户余额不足，请充值后重试。" not in contents
    assert "第二个问题" in contents       # 提问保留（提问本身有效）
    assert contents == ["第一个问题", "正常回答", "第二个问题"]


def test_load_history_window_drops_old_turns(hist_db):
    """超出 CHAT_HISTORY_MAX_TURNS 的轮次被丢弃，顺序仍为时间正序。"""
    conv = _make_conv(hist_db)
    for i in range(6):
        hist_db.add_all([
            Message(conversation_id=conv.id, user_id=1, role="user", content=f"问题{i}", status="done"),
            Message(conversation_id=conv.id, user_id=1, role="assistant", content=f"回答{i}", status="done"),
        ])
    hist_db.commit()

    rows = load_history_window(hist_db, conv.id, max_turns=2)

    assert [m.content for m in rows] == ["问题4", "回答4", "问题5", "回答5"]


def test_load_history_window_applies_char_budget(hist_db):
    """轮数没超但总字符超预算时，仍然从最早处截断。"""
    conv = _make_conv(hist_db)
    hist_db.add_all([
        Message(conversation_id=conv.id, user_id=1, role="user", content="长" * 500, status="done"),
        Message(conversation_id=conv.id, user_id=1, role="assistant", content="答" * 500, status="done"),
        Message(conversation_id=conv.id, user_id=1, role="user", content="最新问题", status="done"),
    ])
    hist_db.commit()

    rows = load_history_window(hist_db, conv.id, max_turns=5, max_chars=100)

    assert [m.content for m in rows] == ["最新问题"]


def test_load_history_window_keeps_normal_messages(hist_db):
    """正常短会话一条不少，且不含 system / tool 消息。"""
    conv = _make_conv(hist_db)
    hist_db.add_all([
        Message(conversation_id=conv.id, user_id=1, role="system", content="系统提示", status="done"),
        Message(conversation_id=conv.id, user_id=1, role="user", content="问题", status="done"),
        Message(conversation_id=conv.id, user_id=1, role="assistant", content="回答", status="done"),
    ])
    hist_db.commit()

    rows = load_history_window(hist_db, conv.id)

    assert [m.content for m in rows] == ["问题", "回答"]


def test_load_history_window_scoped_to_conversation(hist_db):
    """不得串会话：别的会话的消息不能进本次上下文。"""
    conv_a = _make_conv(hist_db)
    conv_b = _make_conv(hist_db)
    hist_db.add_all([
        Message(conversation_id=conv_a.id, user_id=1, role="user", content="A 的问题", status="done"),
        Message(conversation_id=conv_b.id, user_id=1, role="user", content="B 的问题", status="done"),
    ])
    hist_db.commit()

    rows = load_history_window(hist_db, conv_a.id)

    assert [m.content for m in rows] == ["A 的问题"]


# ---------------------------------------------------------------------------
# A4 / 链路：chat 返回 truncated
# ---------------------------------------------------------------------------


def test_chat_returns_truncated_flag(hist_db, monkeypatch):
    """run_agent 判定截断时，chat 的响应体必须带 truncated=True。"""
    conv = Conversation(user_id=2, kb_id=None, title="t")  # kb_id=None 跳过知识库权限校验
    hist_db.add(conv)
    hist_db.commit()
    hist_db.refresh(conv)

    def _fake_run(messages, question, kb_id=None, trace_context=None, options=None):
        return {"answer": "半截" + TRUNCATION_NOTICE, "route": "rag", "context": "",
                "sources": [], "route_inherited": False, "truncated": True}

    monkeypatch.setattr(agent_service, "run_agent", _fake_run)

    class _U:
        id = 2
        role = "user"

    out = agent_service.chat(hist_db, _U(), conv, "住宿标准")

    assert out["truncated"] is True
    assert out["answer"].endswith(TRUNCATION_NOTICE)


def test_trace_log_persists_is_truncated(sqlite_engine, monkeypatch):
    """trace_logs.is_truncated 必须真的落库（大盘统计截断率的数据源）。"""
    from sqlalchemy.orm import sessionmaker

    TraceLog.__table__.create(sqlite_engine)
    session_factory = sessionmaker(bind=sqlite_engine)

    monkeypatch.setattr(tracing, "ensure_trace_tables", lambda: None)
    monkeypatch.setattr(tracing, "SessionLocal", session_factory)

    handler = tracing.TraceCallbackHandler(trace_id="trace-cut-1", question="q")
    handler.route = "rag"
    handler.on_llm_end(LLMResult(generations=[[ChatGeneration(
        message=AIMessage(content="半截", response_metadata={"finish_reason": "length"})
    )]]))
    handler.flush(status="success")

    db = session_factory()
    try:
        row = db.query(TraceLog).filter(TraceLog.trace_id == "trace-cut-1").one()
        assert row.is_truncated is True
        assert row.status == "success"
    finally:
        db.close()


def test_trace_log_is_truncated_false_for_normal_answer(sqlite_engine, monkeypatch):
    """正常回答不得误标截断（否则截断率指标直接失去意义）。"""
    from sqlalchemy.orm import sessionmaker

    TraceLog.__table__.create(sqlite_engine)
    session_factory = sessionmaker(bind=sqlite_engine)

    monkeypatch.setattr(tracing, "ensure_trace_tables", lambda: None)
    monkeypatch.setattr(tracing, "SessionLocal", session_factory)

    handler = tracing.TraceCallbackHandler(trace_id="trace-ok-1", question="q")
    handler.on_llm_end(LLMResult(generations=[[ChatGeneration(
        message=AIMessage(content="完整", response_metadata={"finish_reason": "stop"})
    )]]))
    handler.flush(status="success")

    db = session_factory()
    try:
        row = db.query(TraceLog).filter(TraceLog.trace_id == "trace-ok-1").one()
        assert row.is_truncated is False
    finally:
        db.close()


def test_trace_schema_exposes_is_truncated():
    """Trace 列表接口要能带出 is_truncated，否则大盘只能靠手写 SQL 统计。"""
    from app.schemas.trace import TraceLogOut

    assert "is_truncated" in TraceLogOut.model_fields
    assert TraceLogOut.model_fields["is_truncated"].default is False


# ---------------------------------------------------------------------------
# 链路：SSE done 事件带截断标记，落库正文含提示
# ---------------------------------------------------------------------------


class _FakeGraph:
    """假图：模拟 astream 的两类事件（messages 逐字 + updates 节点产出）。"""

    async def astream(self, input_state, config=None, stream_mode=None):
        yield ("messages", (AIMessage(content="住宿标准为"), {}))
        yield ("updates", {"classify": {"route": "rag", "route_inherited": False}})
        yield ("updates", {"rag": {"tool_output": "【来源1】住宿标准 500 元", "sources": []}})
        yield ("updates", {"answer": {"final_answer": "住宿标准为", "truncated": True}})


def test_stream_done_event_carries_truncation(hist_db, monkeypatch):
    """流式链路：done 事件必须带 truncated/truncation_notice，落库正文带提示。"""
    conv = Conversation(user_id=1, kb_id=None, title="t")
    hist_db.add(conv)
    hist_db.commit()
    hist_db.refresh(conv)
    conv_id = conv.id

    # stream_chat 自建 DB 会话并在 finally 里 close；这里用代理包住测试会话，
    # close 置空，便于用例在流结束后继续断言落库内容
    class _SessionProxy:
        def __init__(self, session):
            self._session = session

        def close(self):
            pass

        def __getattr__(self, name):
            return getattr(self._session, name)

    monkeypatch.setattr(stream_service, "SessionLocal", lambda: _SessionProxy(hist_db))
    monkeypatch.setattr(stream_service, "get_graph", lambda: _FakeGraph())
    monkeypatch.setattr(tracing.TraceCallbackHandler, "flush", lambda self, status="success": None)

    class _U:
        id = 1
        role = "admin"

    async def _collect():
        return [e async for e in stream_service.stream_chat(_U(), conv_id, "住宿标准")]

    events = asyncio.run(_collect())
    done_raw = next(e for e in events if e.startswith("event: done"))
    payload = done_raw.split("data: ", 1)[1].strip()

    import json
    data = json.loads(payload)
    assert data["truncated"] is True
    assert TRUNCATION_NOTICE in data["truncation_notice"]

    saved = (
        hist_db.query(Message)
        .filter(Message.conversation_id == conv_id, Message.role == "assistant")
        .one()
    )
    assert saved.content.endswith(TRUNCATION_NOTICE)
