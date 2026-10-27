"""v1.12 短追问意图继承（Contextual Intent Inheritance）。

覆盖三层：
  1. 规则层 `decide_inherit`：命中/未命中的各条边界；
  2. 节点层 `classify_node`：命中继承时**必须跳过 LLM**（否则等于没省），
     未命中时照常调 LLM，被禁用路由不继承；
  3. 链路层：`last_assistant_route` 取值 + `agent_service.chat` 把上一轮路由
     透传给 `run_agent` 的 options。
"""
import pytest

import app.agent.graph as graph
import app.services.agent_service as agent_service
from app.agent.intent_inherit import decide_inherit, visible_len
from app.models import Conversation, Message


# ---------------------------------------------------------------- 规则层

@pytest.mark.parametrize(
    "question",
    ["去成都呢？", "那北京呢", "这个标准多少", "还有别的吗", "换成销售部呢", "刚才那份制度"],
)
def test_short_contextual_followup_inherits(question):
    """上一轮 rag + 极短指代型追问 -> 继承 rag。"""
    route, reason = decide_inherit("rag", question)
    assert route == "rag"
    assert "contextual_marker" in reason


def test_greeting_does_not_inherit():
    """用户给的用例：「你好呀，今天天气不错」不是追问，不能被拽回检索。"""
    route, reason = decide_inherit("rag", "你好呀，今天天气不错")
    assert route is None
    assert reason == "greeting_without_query"


def test_first_turn_never_inherits():
    """用户给的用例：首轮问「去成都呢？」没有可继承的意图。"""
    route, reason = decide_inherit(None, "去成都呢？")
    assert route is None
    assert reason == "no_last_route"


def test_db_query_route_is_inherited_as_itself():
    """上一轮查业务库，追问也继承 db_query（而不是一律变成 rag）。"""
    route, _ = decide_inherit("db_query", "那上个月呢？")
    assert route == "db_query"


@pytest.mark.parametrize("last_route", ["direct", "report", "parallel_compare"])
def test_non_inheritable_route(last_route):
    """direct 没有可继承的检索意图；report/parallel_compare 产出重，不继承。"""
    route, reason = decide_inherit(last_route, "去成都呢？")
    assert route is None
    assert reason.startswith("last_route_not_inheritable")


def test_long_question_does_not_inherit():
    """超过 12 字说明是完整新问题，交给 LLM 正常分类。"""
    question = "帮我详细说明一下研发部在各个城市的住宿标准分别是什么"
    route, reason = decide_inherit("rag", question)
    assert route is None
    assert reason.startswith("too_long")


def test_tiny_pointing_query_inherits():
    """「谁审批？」这类极短指向型疑问，即使没有「呢/那」也继承。"""
    route, reason = decide_inherit("rag", "谁审批？")
    assert route == "rag"
    assert "tiny_pointing_query" in reason

    route, _ = decide_inherit("rag", "多少钱")
    assert route == "rag"


def test_short_open_question_does_not_inherit():
    """「怎么报销」是开放式新问题，不算指向型追问（避免把闲聊拽回检索）。"""
    route, reason = decide_inherit("rag", "怎么报销")
    assert route is None
    assert reason.startswith("no_signal")


def test_visible_len_ignores_punctuation_and_space():
    assert visible_len("去 成 都 呢？") == 4
    assert visible_len("   ") == 0
    assert visible_len("那，北京呢！！") == 4


# ---------------------------------------------------------------- 节点层

def test_classify_node_inherits_without_calling_llm(monkeypatch):
    """命中继承时必须一次 LLM 都不调 —— 这是「短路」的实质。"""
    def _boom(*a, **kw):
        raise AssertionError("命中继承时不应创建/调用 LLM")

    monkeypatch.setattr(graph, "get_chat_model", _boom)
    monkeypatch.setattr(graph, "classify_route", _boom)

    out = graph.classify_node({"question": "去成都呢？", "last_route": "rag"})

    assert out["route"] == "rag"
    assert out["route_inherited"] is True


def test_classify_node_falls_back_to_llm(monkeypatch):
    """未命中时照常走 LLM 分类，并标记 route_inherited=False。"""
    calls = {"n": 0}

    def _route(llm, question):
        calls["n"] += 1
        return "direct"

    monkeypatch.setattr(graph, "get_chat_model", lambda *a, **kw: object())
    monkeypatch.setattr(graph, "classify_route", _route)

    out = graph.classify_node({"question": "你好呀，今天天气不错", "last_route": "rag"})

    assert out == {"route": "direct", "route_inherited": False}
    assert calls["n"] == 1


def test_classify_node_skips_inherit_when_route_disabled(monkeypatch):
    """对话参数禁用了 rag -> 不继承（否则继承完又被打回 direct，日志误导）。"""
    monkeypatch.setattr(graph, "get_chat_model", lambda *a, **kw: object())
    monkeypatch.setattr(graph, "classify_route", lambda llm, q: "direct")

    out = graph.classify_node({
        "question": "去成都呢？", "last_route": "rag", "route_enabled": {"rag": False},
    })

    assert out["route"] == "direct"
    assert out["route_inherited"] is False


# ---------------------------------------------------------------- 链路层

@pytest.fixture
def hist_db(sqlite_engine):
    from sqlalchemy.orm import sessionmaker

    Conversation.__table__.create(sqlite_engine)
    Message.__table__.create(sqlite_engine)
    db = sessionmaker(bind=sqlite_engine)()
    yield db
    db.close()


def test_last_assistant_route_picks_latest_assistant(hist_db):
    conv = Conversation(user_id=2, kb_id=7, title="t")
    hist_db.add(conv)
    hist_db.commit()
    hist_db.refresh(conv)

    hist_db.add_all([
        Message(conversation_id=conv.id, user_id=2, role="user", content="研发部住宿标准", status="done"),
        Message(conversation_id=conv.id, user_id=2, role="assistant",
                content="500 元", status="done", agent_route="rag"),
        Message(conversation_id=conv.id, user_id=2, role="user", content="你好", status="done"),
        Message(conversation_id=conv.id, user_id=2, role="assistant",
                content="你好", status="done", agent_route="direct"),
    ])
    hist_db.commit()

    history = hist_db.query(Message).order_by(Message.id.asc()).all()
    assert agent_service.last_assistant_route(history) == "direct"


def test_last_assistant_route_none_on_first_turn(hist_db):
    conv = Conversation(user_id=2, kb_id=7, title="t")
    hist_db.add(conv)
    hist_db.commit()
    hist_db.refresh(conv)

    hist_db.add(Message(conversation_id=conv.id, user_id=2, role="user", content="去成都呢？", status="done"))
    hist_db.commit()

    history = hist_db.query(Message).order_by(Message.id.asc()).all()
    assert agent_service.last_assistant_route(history) is None
    assert agent_service.last_assistant_route([]) is None


def test_chat_forwards_last_route_and_returns_inherited_flag(monkeypatch, hist_db):
    """chat 服务必须把上一轮 route 透传给 run_agent，并把 route_inherited 带进响应。"""
    conv = Conversation(user_id=2, kb_id=None, title="t")  # kb_id=None 跳过知识库权限校验
    hist_db.add(conv)
    hist_db.commit()
    hist_db.refresh(conv)

    hist_db.add_all([
        Message(conversation_id=conv.id, user_id=2, role="user", content="研发部住宿标准", status="done"),
        Message(conversation_id=conv.id, user_id=2, role="assistant",
                content="500 元", status="done", agent_route="rag"),
    ])
    hist_db.commit()

    captured: dict = {}

    def _fake_run(messages, question, kb_id=None, trace_context=None, options=None):
        captured["options"] = options
        return {"answer": "400 元/天", "route": "rag", "context": "",
                "sources": [], "route_inherited": True}

    monkeypatch.setattr(agent_service, "run_agent", _fake_run)

    class _U:
        id = 2
        role = "user"

    out = agent_service.chat(hist_db, _U(), conv, "去成都呢？")

    assert captured["options"]["last_route"] == "rag"
    assert out["route_inherited"] is True
    assert out["agent_route"] == "rag"
