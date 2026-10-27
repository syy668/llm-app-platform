"""批次2 回归测试：全局统一 LLM 工厂 + Mock 模型。

覆盖四类关注点：
1. provider 分发：mock / openai / anthropic 各自返回正确的 LangChain 模型类型；
2. mock 行为：知识库问答输出与改造前逐字节一致、路由规则判定、真流式、结构化输出；
3. 两条链路共用工厂：/kbs/{id}/query（rag.llm.answer）与 Agent answer 节点都能在 mock 下产出结果；
4. 架构守卫：除工厂本身外，业务代码不得再出现 ChatOpenAI/ChatAnthropic 直接构造。

全部离线执行：不连任何大模型，不需要真实 Key。
"""
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from app.agent.router import classify_route
from app.llm import MockChatModel, factory as llm_factory
from app.llm.mock import (
    _extract_question,
    _guess_route,
    _mock_rag_answer,
    _split_pair,
)
from app.rag import llm as rag_llm

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _reset_llm_cache():
    """每个用例前后清空工厂缓存，避免实例串味。"""
    llm_factory.reset_cache()
    yield
    llm_factory.reset_cache()


@pytest.fixture
def force_mock(monkeypatch):
    """强制 provider=mock（不受运行库 data/system_config.json 影响）。"""
    monkeypatch.setattr(llm_factory, "get_provider", lambda: "mock")
    return llm_factory.get_chat_model()


# ---------------------------------------------------------------------------
# 1. provider 分发
# ---------------------------------------------------------------------------
class TestProviderDispatch:

    def test_default_is_mock_in_test_env(self):
        """conftest 设置 LLM_PROVIDER=mock，且配置文件无 provider 覆盖。"""
        assert llm_factory.get_provider() == "mock"
        assert isinstance(llm_factory.get_chat_model(), MockChatModel)

    def test_unknown_provider_falls_back_to_mock(self, monkeypatch):
        """非法 provider（如手改配置文件写错）降级为 mock，不让服务炸掉。"""
        monkeypatch.setattr(
            llm_factory.runtime_config, "get_value",
            lambda key: "gpt-5-ultra" if key == "llm_provider" else None,
        )
        assert llm_factory.get_provider() == "mock"

    def test_openai_provider_builds_chatopenai(self, monkeypatch):
        from langchain_openai import ChatOpenAI

        monkeypatch.setattr(llm_factory, "get_provider", lambda: "openai")
        monkeypatch.setattr(llm_factory, "_api_key", lambda: "sk-test-key")
        model = llm_factory.get_chat_model()
        assert isinstance(model, ChatOpenAI)
        # 构造成功即证明参数名与当前 langchain_openai 版本匹配（不发起任何请求）
        assert model.temperature == pytest.approx(
            float(llm_factory.runtime_config.get_value("llm_temperature"))
        )

    def test_anthropic_provider_builds_chatanthropic(self, monkeypatch):
        from langchain_anthropic import ChatAnthropic

        monkeypatch.setattr(llm_factory, "get_provider", lambda: "anthropic")
        monkeypatch.setattr(llm_factory, "_api_key", lambda: "sk-ant-test")
        model = llm_factory.get_chat_model()
        assert isinstance(model, ChatAnthropic)

    def test_openai_without_key_raises_actionable_error(self, monkeypatch):
        monkeypatch.setattr(llm_factory, "get_provider", lambda: "openai")
        monkeypatch.setattr(llm_factory, "_api_key", lambda: "")
        with pytest.raises(RuntimeError, match="LLM_API_KEY"):
            llm_factory.get_chat_model()

    def test_anthropic_without_key_raises_actionable_error(self, monkeypatch):
        monkeypatch.setattr(llm_factory, "get_provider", lambda: "anthropic")
        monkeypatch.setattr(llm_factory, "_api_key", lambda: "")
        with pytest.raises(RuntimeError, match="LLM_API_KEY"):
            llm_factory.get_chat_model()

    def test_cache_reuse_same_config(self, force_mock):
        """同配置重复获取返回同一实例（避免每请求重建客户端）。"""
        assert llm_factory.get_chat_model() is force_mock

    def test_cache_invalidated_on_config_change(self, force_mock):
        """温度变化 → 签名变化 → 重建实例（支撑配置页热更新）。"""
        hotter = llm_factory.get_chat_model(temperature=1.3)
        assert hotter is not force_mock

    def test_hot_switch_provider_returns_new_type(self, monkeypatch, force_mock):
        """运行中切换 provider，下一次获取即返回新类型的模型（无需重启）。"""
        from langchain_openai import ChatOpenAI

        current = {"provider": "mock"}
        monkeypatch.setattr(llm_factory, "get_provider", lambda: current["provider"])
        monkeypatch.setattr(llm_factory, "_api_key", lambda: "sk-test-key")
        assert isinstance(llm_factory.get_chat_model(), MockChatModel)

        current["provider"] = "openai"
        assert isinstance(llm_factory.get_chat_model(), ChatOpenAI)


# ---------------------------------------------------------------------------
# 2. Mock 模型行为
# ---------------------------------------------------------------------------
class TestMockModel:

    def test_llm_type_and_identifying_params(self, force_mock):
        assert force_mock._llm_type == "mock"
        assert force_mock._identifying_params["provider"] == "mock"

    def test_kb_query_output_matches_legacy_mock(self, force_mock):
        """知识库问答输出与改造前 rag/llm.py 的 mock 分支逐字节一致。"""
        contexts = ["第一条资料", "第二条资料", "第三条资料", "第四条资料"]
        got = rag_llm.answer("打印机卡纸怎么办", contexts, "运维知识库")
        assert got == "根据知识库资料，检索到以下相关内容：\n\n第一条资料\n\n第二条资料\n\n第三条资料"

    def test_kb_query_without_context(self, force_mock):
        assert rag_llm.answer("报销流程", [], "运维知识库") == "资料中未找到相关信息。"

    def test_prompt_builder_shape(self):
        prompt = rag_llm.build_prompt("Q", ["C1"], "KB")
        assert "【资料】" in prompt and "【问题】Q" in prompt and "【回答】" in prompt

    def test_rag_answer_extraction_ignores_marker_prefix(self):
        prompt = "说明文字\n【资料】\n[资料 1] 甲\n\n[资料 2] 乙\n\n【问题】问\n\n【回答】"
        assert _mock_rag_answer(prompt) == "根据知识库资料，检索到以下相关内容：\n\n甲\n\n乙"

    def test_agent_answer_uses_tool_output(self, force_mock):
        prompt = (
            "你是企业内部助手。\n【工具检索/查询结果】\n公司共 3 台设备\n"
            "请基于以上结果直接回答上面的用户问题。用户问题：公司有多少台设备"
        )
        text = llm_factory.generate_text(prompt)
        assert "公司有多少台设备" in text
        assert "公司共 3 台设备" in text

    def test_agent_answer_without_tool_output_says_not_found(self, force_mock):
        text = llm_factory.generate_text("【工具检索/查询结果】\n\n请基于以上结果回答。用户问题：X")
        assert "未找到相关信息" in text

    def test_comparison_split_returns_two_queries(self, force_mock):
        prompt = '只输出 JSON：{"query_a": "...", "query_b": "..."}\n\n用户问题：对比A方案和B方案'
        text = llm_factory.generate_text(prompt)
        assert '"query_a"' in text and '"query_b"' in text
        assert "A方案" in text and "B方案" in text

    def test_comparison_split_via_graph_prompt_shape(self, force_mock):
        """复刻 graph._EXTRACT_QUERIES_PROMPT 的真实形态，验证拆词可用。"""
        from app.agent.graph import _EXTRACT_QUERIES_PROMPT

        prompt = _EXTRACT_QUERIES_PROMPT.format(question="对比文档A和文档B")
        text = llm_factory.generate_text(prompt)
        assert '"query_a": "文档A"' in text
        assert '"query_b": "文档B"' in text

    def test_direct_answer_does_not_echo_system_prompt(self, force_mock):
        """direct 路由：回答里出现的应是用户的提问，而不是 system prompt 正文。"""
        from langchain_core.messages import SystemMessage

        messages = [
            SystemMessage(content="你是企业内部知识库与业务数据助手。必须直接、清晰地回答用户提出的问题："),
            HumanMessage(content="你好，你是谁"),
        ]
        content = force_mock.invoke(messages).content
        assert "你好，你是谁" in content
        assert "必须直接、清晰地回答用户提出的问题" not in content

    def test_stream_yields_multiple_chunks_matching_invoke(self, force_mock):
        """真流式：分片输出，拼起来与一次性 invoke 完全一致。"""
        messages = [HumanMessage(content="你好，介绍一下自己")]
        chunks = [c.content for c in force_mock.stream(messages)]
        assert len(chunks) > 1
        assert "".join(chunks) == force_mock.invoke(messages).content

    def test_structured_output_returns_schema_instance(self, force_mock):
        from app.agent.router import RouteDecision

        runnable = force_mock.with_structured_output(RouteDecision, method="function_calling")
        result = runnable.invoke([HumanMessage(content="用户问题：帮我生成工单汇总报表")])
        assert isinstance(result, RouteDecision)
        assert result.route == "report"

    def test_structured_output_include_raw(self, force_mock):
        from app.agent.router import RouteDecision

        runnable = force_mock.with_structured_output(RouteDecision, include_raw=True)
        result = runnable.invoke([HumanMessage(content="用户问题：你好")])
        assert result["parsed"].route == "direct"
        assert result["parsing_error"] is None

    def test_structured_output_unknown_schema_raises(self, force_mock):
        from pydantic import BaseModel

        class Other(BaseModel):
            foo: str

        runnable = force_mock.with_structured_output(Other)
        with pytest.raises(ValueError, match="不支持"):
            runnable.invoke([HumanMessage(content="x")])


class TestMockRules:
    """规则表本身的行为（与 mock.py 文档中的关键词表对应）。"""

    @pytest.mark.parametrize(
        "question,expected",
        [
            ("生成设备汇总报表", "report"),
            ("统计一下本周告警数量", "report"),
            ("公司现在有多少台设备", "db_query"),
            ("列出所有工单", "db_query"),
            ("对比文档A和文档B", "parallel_compare"),
            ("比较这两份方案的区别", "parallel_compare"),
            ("打印机卡纸怎么办", "rag"),
            ("报销制度是怎么规定的", "rag"),
            ("你好呀", "direct"),
        ],
    )
    def test_guess_route(self, question, expected):
        assert _guess_route(question) == expected

    def test_extract_question_uses_last_marker(self):
        """路由 prompt 自带说明文字，必须只取「用户问题：」之后的部分。"""
        text = "report：需要汇总、统计或生成报表\n用户问题：你好呀"
        assert _extract_question(text) == "你好呀"

    @pytest.mark.parametrize(
        "question,expected",
        [
            ("对比A方案和B方案", ("A方案", "B方案")),
            ("比较文档1与文档2", ("文档1", "文档2")),
        ],
    )
    def test_split_pair(self, question, expected):
        assert _split_pair(question) == expected

    def test_split_pair_cannot_split_returns_same(self):
        q = "这两份资料有何区别"
        assert _split_pair(q) == (q, q)


# ---------------------------------------------------------------------------
# 3. 两条链路共用同一个工厂
# ---------------------------------------------------------------------------
class TestBothPathsShareFactory:

    def test_classify_route_works_with_mock(self, force_mock):
        """Agent 路由分类（with_structured_output + function_calling）在 mock 下可用。"""
        assert classify_route(force_mock, "帮我生成设备汇总报表") == "report"
        assert classify_route(force_mock, "公司有多少台工单") == "db_query"
        # 操作类问题走规则直判（不经过 LLM），结果仍应为 rag
        assert classify_route(force_mock, "入职手续怎么办") == "rag"

    def test_agent_answer_node_runs_under_mock(self, force_mock):
        """Agent answer 节点端到端：mock 下能产出非空回答，不再依赖真实 API。"""
        from app.agent.graph import answer_node

        result = answer_node({
            "messages": [HumanMessage(content="你好")],
            "question": "你好",
            "tool_output": "",
        })
        assert result["final_answer"].strip()
        assert "模拟回答" in result["final_answer"]

    def test_both_paths_use_same_model_instance(self, monkeypatch, force_mock):
        """两条链路取到的是同一个缓存实例，证明出口唯一。"""
        from app.agent.graph import get_chat_model as graph_get_model

        assert llm_factory.get_chat_model() is force_mock
        assert graph_get_model() is force_mock


# ---------------------------------------------------------------------------
# 4. 架构守卫
# ---------------------------------------------------------------------------
class TestArchitectureGuard:

    def test_no_direct_chat_model_construction_outside_factory(self):
        """业务代码不得再直接 new ChatOpenAI / ChatAnthropic（必须走工厂）。"""
        offenders = []
        for path in (ROOT / "app").rglob("*.py"):
            if path.as_posix().endswith("app/llm/factory.py"):
                continue
            text = path.read_text(encoding="utf-8")
            for token in ("ChatOpenAI(", "ChatAnthropic("):
                if token in text:
                    offenders.append(f"{path.relative_to(ROOT)}: {token}")
        assert not offenders, "以下文件绕过统一工厂直接构造模型：" + "; ".join(offenders)

    def test_legacy_agent_llm_module_removed(self):
        """旧的 app/agent/llm.py 已废弃，避免两套客户端并存。"""
        assert not (ROOT / "app" / "agent" / "llm.py").exists()

    def test_only_one_entry_for_llm_in_agent_and_rag(self):
        """agent/graph.py 与 rag/llm.py 都从统一工厂导入，不各自持有客户端。"""
        graph_src = (ROOT / "app" / "agent" / "graph.py").read_text(encoding="utf-8")
        rag_src = (ROOT / "app" / "rag" / "llm.py").read_text(encoding="utf-8")
        assert "from ..llm import get_chat_model" in graph_src
        assert "from ..llm import generate_text" in rag_src
        assert "from .llm import" not in graph_src
