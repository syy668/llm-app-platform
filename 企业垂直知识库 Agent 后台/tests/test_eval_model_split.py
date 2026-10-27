"""v1.7 回归测试：评测判分模型与对话模型解耦 + Provider 选择器后端契约。

覆盖四类关注点：
1. 配置项白名单：eval_llm_model / eval_llm_api_base / eval_llm_api_key 可读写，
   密钥只回掩码，空串语义为「清除覆盖（复用对话模型）」；
2. 向后兼容：未配置评测模型时，get_eval_chat_model 与 get_chat_model(purpose="eval")
   返回**同一个缓存实例**，行为与 v1.6 逐字节一致；
3. 解耦生效：配置评测模型后，模型名 / 接口地址 / 密钥可独立，且不影响对话模型；
4. 契约：RAGAS 判分器确实走 get_eval_chat_model；provider 覆盖值驱动工厂分发，
   provider=mock 时模型名不参与调用（避免「页面填了 Key 仍走 Mock」的误解）。

全部离线执行：不连任何大模型，不需要真实 Key。
"""
import pytest

from conftest import assert_error, assert_ok
from app.core import runtime_config
from app.llm import MockChatModel, factory as llm_factory


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """独立配置文件 + 清空工厂缓存，隔离每个用例的覆盖值。"""
    monkeypatch.setenv("SYSTEM_CONFIG_FILE", str(tmp_path / "system_config.json"))
    runtime_config.reset_cache()
    llm_factory.reset_cache()
    yield
    runtime_config.reset_cache()
    llm_factory.reset_cache()


# ---------------------------------------------------------------------------
# 1. 配置项读写契约


def test_eval_model_saved_and_secret_masked(admin_client):
    """评测模型三项可保存；API Key 只回掩码，get_secret 可取真实值。"""
    data = assert_ok(
        admin_client.put(
            "/api/v1/system/config",
            json={
                "eval_llm_model": "Qwen/Qwen2.5-72B-Instruct",
                "eval_llm_api_base": "https://api.siliconflow.cn/v1",
                "eval_llm_api_key": "sk-eval-abc",
            },
        )
    )
    assert data["eval_llm_model"] == "Qwen/Qwen2.5-72B-Instruct"
    assert data["eval_llm_api_base"] == "https://api.siliconflow.cn/v1"
    assert data["eval_llm_api_key"] == "******"
    assert runtime_config.get_secret("eval_llm_api_key") == "sk-eval-abc"
    # 评测密钥与对话密钥互不干扰
    assert runtime_config.get_secret("llm_api_key") != "sk-eval-abc"
    assert {"eval_llm_model", "eval_llm_api_base", "eval_llm_api_key"} <= set(
        data["overridden_keys"]
    )


def test_blank_eval_model_clears_override(admin_client):
    """清空评测模型 = 删除覆盖值（回退复用对话模型），而非存一个空串覆盖。"""
    assert_ok(admin_client.put("/api/v1/system/config", json={"eval_llm_model": "x-model"}))
    assert runtime_config.get_value("eval_llm_model") == "x-model"

    data = assert_ok(admin_client.put("/api/v1/system/config", json={"eval_llm_model": ""}))
    assert data["eval_llm_model"] == ""
    assert "eval_llm_model" not in data["overridden_keys"]


def test_eval_secret_survives_disk_reload(admin_client):
    """回归：_load_overrides 曾把「非 llm_api_key」的密钥静默丢弃，重启即失效。"""
    assert_ok(
        admin_client.put(
            "/api/v1/system/config",
            json={"eval_llm_model": "m1", "eval_llm_api_key": "sk-eval-keep"},
        )
    )
    runtime_config.reset_cache()  # 模拟进程重启后重新读盘
    assert runtime_config.get_value("eval_llm_model") == "m1"
    assert runtime_config.get_secret("eval_llm_api_key") == "sk-eval-keep"


def test_eval_model_too_long_rejected(admin_client):
    """超长评测模型名触发 422（长度护栏）。"""
    resp = admin_client.put("/api/v1/system/config", json={"eval_llm_model": "m" * 200})
    assert_error(resp, 422, 422)


def test_delete_eval_model_override(admin_client):
    """DELETE 单个键可恢复「复用对话模型」语义（幂等）。"""
    assert_ok(admin_client.put("/api/v1/system/config", json={"eval_llm_model": "m2"}))
    data = assert_ok(admin_client.delete("/api/v1/system/config/eval_llm_model"))
    assert data["eval_llm_model"] == ""
    assert "eval_llm_model" not in data["overridden_keys"]
    # 幂等：再删一次同样 200
    assert data["eval_llm_model"] == assert_ok(
        admin_client.delete("/api/v1/system/config/eval_llm_model")
    )["eval_llm_model"]


# ---------------------------------------------------------------------------
# 2. 未配置时：完全复用对话模型（向后兼容）


class TestEvalModelFallback:
    def test_unset_shares_chat_model_instance(self, monkeypatch):
        """未配置 eval_llm_model → 复用对话模型名，但 max_tokens 用判分预算 8192。

        【v1.19.x 变更】判分预算与对话解耦（EVAL_LLM_MAX_TOKENS=8192 vs 对话 2048）后，
        两者配置指纹必然不同 → 不再共享同一实例。断言从「同一实例」改为
        「同为 MockChatModel 且实例分离」——指纹分离正是预算解耦的机制保证。
        """
        monkeypatch.setattr(llm_factory, "get_provider", lambda: "mock")
        eval_model = llm_factory.get_eval_chat_model(temperature=0.0)
        chat_eval_model = llm_factory.get_chat_model(purpose="eval", temperature=0.0)
        assert isinstance(eval_model, MockChatModel)
        assert isinstance(chat_eval_model, MockChatModel)
        assert eval_model is not chat_eval_model

    def test_unset_ignores_eval_base_and_key(self, monkeypatch):
        """只填了 base/key 而没填模型名 → 整组忽略（避免模型与地址错配 404）。"""
        from langchain_openai import ChatOpenAI

        runtime_config.update_config(
            {
                "eval_llm_api_base": "https://api.unused.example/v1",
                "eval_llm_api_key": "sk-unused",
            }
        )
        monkeypatch.setattr(llm_factory, "get_provider", lambda: "openai")
        monkeypatch.setattr(llm_factory, "_api_key", lambda: "sk-chat")
        monkeypatch.setattr(llm_factory, "_api_base", lambda: "https://api.deepseek.com/v1")

        model = llm_factory.get_eval_chat_model(temperature=0.0)
        assert isinstance(model, ChatOpenAI)
        assert model.model_name == llm_factory._model_name("openai")
        assert str(model.openai_api_base) == "https://api.deepseek.com/v1"
        assert model.openai_api_key.get_secret_value() == "sk-chat"


# ---------------------------------------------------------------------------
# 3. 配置后：判分模型与对话模型解耦


class TestEvalModelOverride:
    def _force_openai(self, monkeypatch, chat_key="sk-chat", chat_base="https://api.deepseek.com/v1"):
        from langchain_openai import ChatOpenAI  # noqa: F401  确保依赖可用

        monkeypatch.setattr(llm_factory, "get_provider", lambda: "openai")
        monkeypatch.setattr(llm_factory, "_api_key", lambda: chat_key)
        monkeypatch.setattr(llm_factory, "_api_base", lambda: chat_base)

    def test_independent_model_base_and_key(self, monkeypatch):
        """评测模型三项全配 → 与对话模型完全不同的实例。"""
        runtime_config.update_config(
            {
                "llm_model": "deepseek-chat",
                "eval_llm_model": "Qwen/Qwen2.5-72B-Instruct",
                "eval_llm_api_base": "https://api.siliconflow.cn/v1",
                "eval_llm_api_key": "sk-eval-xyz",
            }
        )
        self._force_openai(monkeypatch)

        eval_model = llm_factory.get_eval_chat_model(temperature=0.0)
        chat_model = llm_factory.get_chat_model()

        assert eval_model is not chat_model
        assert eval_model.model_name == "Qwen/Qwen2.5-72B-Instruct"
        assert str(eval_model.openai_api_base) == "https://api.siliconflow.cn/v1"
        assert eval_model.openai_api_key.get_secret_value() == "sk-eval-xyz"
        # 对话链路不受评测配置影响
        assert chat_model.model_name == "deepseek-chat"
        assert str(chat_model.openai_api_base) == "https://api.deepseek.com/v1"

    def test_partial_override_falls_back_per_field(self, monkeypatch):
        """只配模型名 → 地址与密钥按字段回退全局值。"""
        runtime_config.update_config({"eval_llm_model": "deepseek-reasoner"})
        self._force_openai(monkeypatch)

        model = llm_factory.get_eval_chat_model(temperature=0.0)
        assert model.model_name == "deepseek-reasoner"
        assert str(model.openai_api_base) == "https://api.deepseek.com/v1"
        assert model.openai_api_key.get_secret_value() == "sk-chat"

    def test_eval_uses_eval_purpose_timeout(self, monkeypatch):
        """判分模型仍套用 EVAL_LLM_TIMEOUT / EVAL_LLM_MAX_RETRIES 预设。"""
        runtime_config.update_config({"eval_llm_model": "deepseek-reasoner"})
        self._force_openai(monkeypatch)

        model = llm_factory.get_eval_chat_model(temperature=0.0)
        timeout = getattr(model, "request_timeout", None) or getattr(model, "timeout", None)
        assert float(timeout) == float(llm_factory.settings.eval_llm_timeout)
        assert int(model.max_retries) == int(llm_factory.settings.eval_llm_max_retries)

    def test_eval_temperature_is_fixed_zero(self, monkeypatch):
        """判分温度固定 0，不受配置页 llm_temperature 影响。"""
        runtime_config.update_config({"llm_temperature": 0.9, "eval_llm_model": "m-eval"})
        self._force_openai(monkeypatch)

        model = llm_factory.get_eval_chat_model(temperature=0.0)
        assert model.temperature == pytest.approx(0.0)
        assert llm_factory.get_chat_model().temperature == pytest.approx(0.9)

    def test_ragas_evaluator_uses_eval_entry(self, monkeypatch):
        """RAGAS 判分器必须走 get_eval_chat_model（架构守卫，防止回退成对话模型）。"""
        from app.eval import metrics

        captured = {}

        def fake_get_eval_chat_model(**kwargs):
            captured.update(kwargs)
            return llm_factory.get_chat_model()

        monkeypatch.setattr(metrics, "get_eval_chat_model", fake_get_eval_chat_model)
        metrics.RagasEvaluator()
        assert captured == {"temperature": 0.0}


# ---------------------------------------------------------------------------
# 4. Provider 覆盖值驱动工厂（解决「页面填了 Key 仍走 Mock」）


class TestProviderSwitch:
    def test_provider_override_drives_factory(self, admin_client):
        """页面保存 llm_provider=openai 后，工厂立即按 openai 分发（无需重启）。"""
        assert_ok(
            admin_client.put(
                "/api/v1/system/config", json={"llm_provider": "openai", "llm_api_key": "sk-x"}
            )
        )
        assert llm_factory.get_provider() == "openai"
        from langchain_openai import ChatOpenAI

        assert isinstance(llm_factory.get_chat_model(), ChatOpenAI)

    def test_mock_provider_keeps_mock_even_with_real_model_name(self, admin_client):
        """provider=mock 时模型名不参与调用：这正是「填了 DeepSeek 仍走 Mock」的机制。"""
        assert_ok(
            admin_client.put(
                "/api/v1/system/config",
                json={"llm_provider": "mock", "llm_model": "deepseek-ai/DeepSeek-V3"},
            )
        )
        assert isinstance(llm_factory.get_chat_model(), MockChatModel)
        # 评测侧同样回落 mock
        assert isinstance(llm_factory.get_eval_chat_model(temperature=0.0), MockChatModel)

    def test_invalid_provider_rejected(self, admin_client):
        """非法 provider 触发 422（枚举护栏在 schema 与 runtime_config 双层）。"""
        assert_error(
            admin_client.put("/api/v1/system/config", json={"llm_provider": "ollama"}),
            422,
            422,
        )
