"""配置「恢复默认」接口测试：DELETE /system/config/{key}（v1.6 新增）。

背景：`runtime_config` 的覆盖值（data/system_config.json）优先级高于 .env，
一旦管理员在配置页保存过某个键，**.env 就永久失效且没有恢复入口**。
本接口提供逐键删除覆盖值的能力，删完即回退 .env 默认值。

覆盖：
- 删除覆盖值后回退 .env（限流阈值 / 工具参数 / LLM 模型与地址）；
- 敏感键（API Key）删除后回退 .env 密钥；
- provider 删除后回退 .env 的 LLM_PROVIDER（工厂下一次调用即生效）；
- overridden_keys 字段随增删同步变化；
- 幂等（重复删除仍 200）、未知键 404、非管理员 403、未登录 401；
- 持久化文件里确实不再残留该键。
"""
import json

import pytest

from conftest import assert_error, assert_ok
from app.core import runtime_config


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """每个用例独立配置文件 + 清空缓存，避免与真实 data/system_config.json 相互污染。"""
    monkeypatch.setenv("SYSTEM_CONFIG_FILE", str(tmp_path / "system_config.json"))
    runtime_config.reset_cache()
    yield
    runtime_config.reset_cache()


def _config_file(tmp_path):
    return tmp_path / "system_config.json"


def test_delete_override_falls_back_to_env(admin_client, monkeypatch):
    """核心场景：保存覆盖值 → 删除 → 立即回退 .env 的值（无需重启）。"""
    monkeypatch.setenv("AI_RATE_LIMIT_PER_MINUTE", "7")
    runtime_config.reset_cache()

    data = assert_ok(
        admin_client.put("/api/v1/system/config", json={"ai_rate_limit_per_minute": 99})
    )
    assert data["ai_rate_limit_per_minute"] == 99
    assert data["overridden_keys"] == ["ai_rate_limit_per_minute"]

    data = assert_ok(admin_client.delete("/api/v1/system/config/ai_rate_limit_per_minute"))
    assert data["ai_rate_limit_per_minute"] == 7  # 回退 .env
    assert data["overridden_keys"] == []
    # 读取侧同样回退（配置页之外，业务代码拿到的是 .env 值）
    assert runtime_config.get_value("ai_rate_limit_per_minute") == 7
    assert runtime_config.get_override("ai_rate_limit_per_minute") is None


def test_delete_only_removes_target_key(admin_client, monkeypatch):
    """多键场景：只删指定键，其余覆盖值保持不变。"""
    monkeypatch.setenv("AI_RATE_LIMIT_PER_MINUTE", "7")
    runtime_config.reset_cache()
    assert_ok(
        admin_client.put(
            "/api/v1/system/config",
            json={"ai_rate_limit_per_minute": 88, "tool_max_attempts": 6},
        )
    )

    data = assert_ok(admin_client.delete("/api/v1/system/config/ai_rate_limit_per_minute"))
    assert data["ai_rate_limit_per_minute"] == 7
    assert data["tool_max_attempts"] == 6
    assert data["overridden_keys"] == ["tool_max_attempts"]


def test_delete_secret_falls_back_to_env_key(admin_client):
    """敏感键：删掉覆盖的 API Key 后回退 .env 密钥（不回显明文）。"""
    assert_ok(admin_client.put("/api/v1/system/config", json={"llm_api_key": "sk-override"}))
    assert runtime_config.get_secret("llm_api_key") == "sk-override"

    data = assert_ok(admin_client.delete("/api/v1/system/config/llm_api_key"))
    assert "llm_api_key" not in data["overridden_keys"]
    # 回退到 .env 的取值（无配置时为空串），且绝不能回显明文
    from app.config import settings

    assert runtime_config.get_secret("llm_api_key") == (settings.llm_api_key or "")
    assert data["llm_api_key"] in ("", "******")


def test_delete_provider_reverts_and_factory_follows(admin_client, monkeypatch):
    """provider 覆盖值删除后回退 .env，LLM 工厂下一次取用即跟随（热切换）。"""
    from app.config import settings
    from app.llm import factory

    monkeypatch.setenv("LLM_PROVIDER", "mock")
    runtime_config.reset_cache()

    assert_ok(admin_client.put("/api/v1/system/config", json={"llm_provider": "openai"}))
    assert runtime_config.get_value("llm_provider") == "openai"
    factory.reset_cache()
    assert factory.get_provider() == "openai"

    assert_ok(admin_client.delete("/api/v1/system/config/llm_provider"))
    assert runtime_config.get_value("llm_provider") == settings.llm_provider
    factory.reset_cache()
    assert factory.get_provider() == settings.llm_provider


def test_delete_is_idempotent(admin_client, monkeypatch):
    """幂等：键本来没有覆盖值时，删除也返回 200，且配置仍为默认值。"""
    monkeypatch.setenv("AI_RATE_LIMIT_PER_MINUTE", "7")
    runtime_config.reset_cache()

    first = assert_ok(admin_client.delete("/api/v1/system/config/ai_rate_limit_per_minute"))
    second = assert_ok(admin_client.delete("/api/v1/system/config/ai_rate_limit_per_minute"))
    assert first["ai_rate_limit_per_minute"] == 7
    assert second["ai_rate_limit_per_minute"] == 7
    assert second["overridden_keys"] == []


def test_delete_unknown_key_returns_404(admin_client):
    """未知键名返回 404：避免拼写错误被当成"恢复成功"静默吞掉。"""
    resp = admin_client.delete("/api/v1/system/config/not_a_real_key")
    assert_error(resp, 404, 404)


def test_delete_forbidden_for_normal_user(user_client):
    """普通用户无配置变更权限（403）。"""
    resp = user_client.delete("/api/v1/system/config/ai_rate_limit_per_minute")
    assert_error(resp, 403, 403)


def test_delete_requires_login(client):
    """未登录删除配置返回 401。"""
    resp = client.delete("/api/v1/system/config/ai_rate_limit_per_minute")
    assert resp.status_code == 401


def test_delete_persisted_file_no_residue(admin_client, tmp_path):
    """持久化文件里确实不再残留被删的键（否则重启后会"复活"）。"""
    assert_ok(
        admin_client.put(
            "/api/v1/system/config",
            json={"llm_model": "qwen-max", "tool_max_attempts": 4},
        )
    )
    assert json.loads(_config_file(tmp_path).read_text(encoding="utf-8")) == {
        "llm_model": "qwen-max",
        "tool_max_attempts": 4,
    }

    assert_ok(admin_client.delete("/api/v1/system/config/llm_model"))
    on_disk = json.loads(_config_file(tmp_path).read_text(encoding="utf-8"))
    assert on_disk == {"tool_max_attempts": 4}

    # 模拟进程重启：清缓存后从磁盘重新加载，被删键不会复活
    runtime_config.reset_cache()
    assert runtime_config.get_override("llm_model") is None
    assert runtime_config.get_value("tool_max_attempts") == 4
