"""系统配置接口测试（离线运行，不依赖 MySQL / Redis / DeepSeek）。

覆盖：
- GET /system/config：登录用户可读，返回统一响应体，API Key 只回掩码；
- PUT /system/config：仅管理员（普通用户 403），参数校验（422 / 400）；
- 覆盖值保存后热更新生效（限流、工具超时 / 重试、LLM 模型 / 地址 / 密钥）；
- API Key：保存后 get_secret 可取真实值，重复提交掩码 / 空值不覆盖；
- 自定义模型名（非内置白名单）可保存；
- 对话请求级参数：route_condition 路由开关回退逻辑。
"""
import pytest
from pydantic import ValidationError

from conftest import assert_error, assert_ok
from app.agent.graph import route_condition
from app.api import rate_limit
from app.api.v1.chat import ChatRequest
from app.core import runtime_config


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """每个用例使用独立的配置文件，且用例结束后清空缓存，避免相互污染。"""
    monkeypatch.setenv("SYSTEM_CONFIG_FILE", str(tmp_path / "system_config.json"))
    runtime_config.reset_cache()
    yield
    runtime_config.reset_cache()


def test_get_config_requires_login(client):
    """未登录读取配置返回 401。"""
    resp = client.get("/api/v1/system/config")
    assert resp.status_code == 401


def test_get_config_defaults(admin_client):
    """登录用户可读取配置，返回全部字段及默认值（API Key 未配置时为空串）。"""
    resp = admin_client.get("/api/v1/system/config")
    data = assert_ok(resp)
    assert set(data.keys()) == {
        "ai_rate_limit_per_minute",
        "tool_timeout_seconds",
        "tool_max_attempts",
        "tool_retry_delay_seconds",
        "llm_temperature",
        "llm_model",
        "llm_api_base",
        "llm_api_key",
        # 【v1.7 新增】评测判分模型可独立配置（留空 = 复用对话模型）
        "eval_llm_model",
        "eval_llm_api_base",
        "eval_llm_api_key",
        # 【多服务商热更新 新增】
        "llm_provider",
        "embedding_provider",
        "embedding_model",
        "embedding_api_base",
        "embedding_api_key",
        "provider_presets",
        # 【v1.6 新增】当前存在覆盖值的键名列表（配置页据此区分 env 默认值）
        "overridden_keys",
    }
    assert data["overridden_keys"] == []  # 独立配置文件 + 未保存过任何覆盖值

    assert data["ai_rate_limit_per_minute"] == 30
    assert data["tool_timeout_seconds"] == 30
    assert data["tool_max_attempts"] == 3
    assert data["tool_retry_delay_seconds"] == 0.5
    assert data["llm_temperature"] == 0.2
    assert data["llm_model"] == "deepseek-chat"
    # 接口地址未配置时为空串；若加载了项目 .env 已有配置则回显该默认值
    assert isinstance(data["llm_api_base"], str)
    # API Key 未配置时为空串；若加载了项目 .env 已有密钥则返回掩码（不回显明文）
    assert data["llm_api_key"] in ("", "******")
    # 【多服务商热更新 新增】服务商标随配置下发，且密钥类默认值为空串
    assert {p["id"] for p in data["provider_presets"]} == {
        "deepseek",
        "siliconflow",
        "dashscope",
        "modelscope",
    }
    assert data["embedding_api_key"] in ("", "******")
    # 【v1.7】评测判分模型默认留空 → 表示复用对话模型；密钥同 llm_api_key 只回掩码
    assert data["eval_llm_model"] == ""
    assert data["eval_llm_api_base"] == ""
    assert data["eval_llm_api_key"] in ("", "******")


def test_get_config_normal_user_allowed(user_client):
    """普通用户可读取配置（页面只读展示）。"""
    resp = user_client.get("/api/v1/system/config")
    assert_ok(resp)


def test_put_config_forbidden_for_normal_user(user_client):
    """普通用户修改配置返回 403。"""
    resp = user_client.put("/api/v1/system/config", json={"llm_temperature": 0.5})
    assert_error(resp, 403, 403)


def test_put_config_requires_login(client):
    """未登录修改配置返回 401。"""
    resp = client.put("/api/v1/system/config", json={"llm_temperature": 0.5})
    assert resp.status_code == 401


def test_put_config_empty_payload(admin_client):
    """空修改体返回 400。"""
    resp = admin_client.put("/api/v1/system/config", json={})
    assert_error(resp, 400, 400)


def test_put_config_out_of_range(admin_client):
    """越界参数触发参数校验 422。"""
    resp = admin_client.put(
        "/api/v1/system/config", json={"ai_rate_limit_per_minute": 0}
    )
    assert_error(resp, 422, 422)
    resp = admin_client.put(
        "/api/v1/system/config", json={"tool_max_attempts": 99}
    )
    assert_error(resp, 422, 422)
    resp = admin_client.put(
        "/api/v1/system/config", json={"tool_retry_delay_seconds": -1}
    )
    assert_error(resp, 422, 422)


def test_put_config_custom_model(admin_client):
    """自定义模型名（非内置选项）可保存并生效。"""
    resp = admin_client.put("/api/v1/system/config", json={"llm_model": "qwen-max"})
    data = assert_ok(resp)
    assert data["llm_model"] == "qwen-max"
    assert runtime_config.get_value("llm_model") == "qwen-max"


def test_put_config_blank_model_rejected(admin_client):
    """空模型名返回 400。"""
    resp = admin_client.put("/api/v1/system/config", json={"llm_model": "  "})
    assert_error(resp, 400, 400)


def test_put_config_hot_reload(admin_client, monkeypatch):
    """保存覆盖值后：接口返回新值，限流 / 工具超时 / 重试读取侧同步生效。"""
    resp = admin_client.put(
        "/api/v1/system/config",
        json={
            "ai_rate_limit_per_minute": 66,
            "tool_timeout_seconds": 120,
            "tool_max_attempts": 5,
            "tool_retry_delay_seconds": 1.5,
            "llm_temperature": 0.7,
            "llm_model": "deepseek-reasoner",
            "llm_api_base": "https://api.example.com/v1",
        },
    )
    data = assert_ok(resp)
    assert data["ai_rate_limit_per_minute"] == 66
    assert data["tool_timeout_seconds"] == 120
    assert data["tool_max_attempts"] == 5
    assert data["tool_retry_delay_seconds"] == 1.5
    assert data["llm_temperature"] == 0.7
    assert data["llm_model"] == "deepseek-reasoner"
    assert data["llm_api_base"] == "https://api.example.com/v1"

    # 读取侧热更新：无需重启
    assert rate_limit._rate_limit_per_minute() == 66
    assert int(runtime_config.get_value("tool_timeout_seconds")) == 120
    assert int(runtime_config.get_value("tool_max_attempts")) == 5
    assert float(runtime_config.get_value("tool_retry_delay_seconds")) == 1.5
    # 覆盖值可读回（供业务侧区分「显式配置」与「默认值」）
    assert runtime_config.get_override("llm_api_base") == "https://api.example.com/v1"


def test_api_key_masked_in_get(admin_client):
    """API Key 保存后：GET 只返回掩码，get_secret 可取真实值。"""
    resp = admin_client.put(
        "/api/v1/system/config", json={"llm_api_key": "sk-test-1234567890"}
    )
    data = assert_ok(resp)
    assert data["llm_api_key"] == "******"

    data = assert_ok(admin_client.get("/api/v1/system/config"))
    assert data["llm_api_key"] == "******"
    assert runtime_config.get_secret("llm_api_key") == "sk-test-1234567890"


def test_api_key_mask_resubmit_keeps_value(admin_client):
    """重复提交掩码 / 空值不覆盖已保存密钥。"""
    assert_ok(admin_client.put("/api/v1/system/config", json={"llm_api_key": "sk-abc"}))
    assert_ok(admin_client.put("/api/v1/system/config", json={"llm_api_key": "******"}))
    assert_ok(admin_client.put("/api/v1/system/config", json={"llm_api_key": ""}))
    assert runtime_config.get_secret("llm_api_key") == "sk-abc"

    # 重新输入新密钥可覆盖
    assert_ok(admin_client.put("/api/v1/system/config", json={"llm_api_key": "sk-new"}))
    assert runtime_config.get_secret("llm_api_key") == "sk-new"


def test_get_value_never_returns_secret(admin_client):
    """get_value 对敏感字段一律返回 None（防误用泄露密钥）。"""
    assert_ok(admin_client.put("/api/v1/system/config", json={"llm_api_key": "sk-xyz"}))
    assert runtime_config.get_value("llm_api_key") is None


def test_get_config_rejects_invalid_saved_file(tmp_path, admin_client, monkeypatch):
    """磁盘覆盖文件损坏 / 值非法时静默回退默认值，读取不报错。"""
    bad_file = tmp_path / "broken.json"
    bad_file.write_text('{"ai_rate_limit_per_minute": "oops"}', encoding="utf-8")
    monkeypatch.setenv("SYSTEM_CONFIG_FILE", str(bad_file))
    runtime_config.reset_cache()

    data = assert_ok(admin_client.get("/api/v1/system/config"))
    assert data["ai_rate_limit_per_minute"] == 30


def test_env_fallback_when_not_overridden(admin_client, monkeypatch):
    """未保存覆盖值时，动态回退 .env（与旧 _rate_limit_per_minute 逻辑兼容）。"""
    monkeypatch.setenv("AI_RATE_LIMIT_PER_MINUTE", "7")
    monkeypatch.setenv("TOOL_MAX_ATTEMPTS", "4")
    runtime_config.reset_cache()
    assert rate_limit._rate_limit_per_minute() == 7
    assert int(runtime_config.get_value("tool_max_attempts")) == 4

    data = assert_ok(admin_client.get("/api/v1/system/config"))
    assert data["ai_rate_limit_per_minute"] == 7


def test_override_wins_over_env(admin_client, monkeypatch):
    """已保存覆盖值时，覆盖值优先于 .env。"""
    monkeypatch.setenv("AI_RATE_LIMIT_PER_MINUTE", "7")
    assert_ok(
        admin_client.put(
            "/api/v1/system/config", json={"ai_rate_limit_per_minute": 99}
        )
    )
    assert rate_limit._rate_limit_per_minute() == 99


# ---------- 对话请求级参数 ----------

def test_route_condition_disabled_route_falls_back_to_direct():
    """被对话参数禁用的路由回退为直接回答。"""
    state = {"route": "rag", "route_enabled": {"rag": False, "db_query": True}}
    assert route_condition(state) == "direct"
    state = {"route": "report", "route_enabled": {"report": False}}
    assert route_condition(state) == "direct"


def test_route_condition_enabled_route_kept():
    """启用 / 未传开关的路由保持原语义。"""
    assert route_condition({"route": "rag"}) == "rag"
    assert route_condition({"route": "db_query", "route_enabled": None}) == "db_query"
    assert route_condition({"route": "report", "route_enabled": {"report": True}}) == "report"
    assert route_condition({"route": "direct"}) == "direct"
    assert route_condition({}) == "direct"


def test_chat_request_validates_top_k():
    """top_k 越界（0 / 51）触发校验错误。"""
    with pytest.raises(ValidationError):
        ChatRequest(question="你好", top_k=0)
    with pytest.raises(ValidationError):
        ChatRequest(question="你好", top_k=51)
    assert ChatRequest(question="你好", top_k=None).top_k is None
    assert ChatRequest(question="你好", top_k=10).top_k == 10


def test_chat_request_validates_route_keys():
    """route_enabled 仅接受 rag / db_query / report 三个键。"""
    with pytest.raises(ValidationError):
        ChatRequest(question="你好", route_enabled={"evil": False})
    payload = ChatRequest(
        question="你好", route_enabled={"rag": True, "db_query": False, "report": True}
    )
    assert payload.route_enabled == {"rag": True, "db_query": False, "report": True}
