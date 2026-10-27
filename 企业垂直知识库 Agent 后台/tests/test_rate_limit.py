"""接口限流配置解析单元测试。

【批次3 加固】三条阈值用例原先直接读 `runtime_config.get_value()`，会受本机
`data/system_config.json` 里是否有显式覆盖值影响（管理员在配置页保存过阈值后，
`.env` 的 AI_RATE_LIMIT_PER_MINUTE 就被覆盖值遮蔽，用例随环境漂移）。
故用 autouse 夹具把 SYSTEM_CONFIG_FILE 指向临时空文件并重置覆盖值缓存，
让"默认值 = 30 / 自定义值 = 环境变量"的语义在任何机器上都成立。
"""
import pytest

from app.api.rate_limit import _is_fake_redis, _rate_limit_per_minute


@pytest.fixture(autouse=True)
def isolated_runtime_config(monkeypatch, tmp_path):
    """隔离系统配置文件：空文件 → 无任何覆盖值，只走 .env 默认值。"""
    from app.core import runtime_config

    monkeypatch.setenv("SYSTEM_CONFIG_FILE", str(tmp_path / "system_config.json"))
    monkeypatch.setattr(runtime_config, "_overrides", None)  # 丢弃已加载的缓存，强制重读空文件


def test_rate_limit_per_minute_default(monkeypatch):
    monkeypatch.delenv("AI_RATE_LIMIT_PER_MINUTE", raising=False)
    assert _rate_limit_per_minute() == 30


def test_rate_limit_per_minute_custom(monkeypatch):
    monkeypatch.setenv("AI_RATE_LIMIT_PER_MINUTE", "7")
    assert _rate_limit_per_minute() == 7


def test_rate_limit_per_minute_invalid(monkeypatch):
    monkeypatch.setenv("AI_RATE_LIMIT_PER_MINUTE", "not_a_number")
    assert _rate_limit_per_minute() == 30


def test_is_fake_redis_true(monkeypatch):
    monkeypatch.setenv("FAKE_REDIS", "true")
    assert _is_fake_redis() is True


def test_is_fake_redis_false(monkeypatch):
    monkeypatch.setenv("FAKE_REDIS", "false")
    assert _is_fake_redis() is False
