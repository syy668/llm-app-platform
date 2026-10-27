"""v1.15 两个「静默失败」的护栏测试。

两条都来自同一次真实故障（对话页出现空白色气泡 + 「AI 服务连接超时」）：

1. **接口地址里的占位符**：管理员从云厂商控制台复制了模板地址
   `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`，
   未替换 `{WorkspaceId}` 就保存。请求时 DNS 解析失败，SDK 只抛
   `APIConnectionError("Connection error.")`，前端显示「连接超时，请检查网络」——
   真因（地址里还有花括号）完全看不出来。现在保存时直接拒绝。

2. **百炼思考模式下 tool_choice 不可用**：qwen3.x 系列默认开思考，思考模式下
   `tool_choice=required` 会 400（`does not support being set to required ...
   in thinking mode`），或者更糟——不报错但不返回 tool_calls，让
   `with_structured_output` 静默失败。工厂对百炼地址显式传 enable_thinking=False。

这两条都属于「不该留给用户猜」的类别，所以测试锁死。
"""
import pytest

from app.core import runtime_config
from app.llm import factory

PLACEHOLDER_BASE = "https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """独立配置文件 + 清缓存，避免与其他用例互相污染。"""
    monkeypatch.setenv("SYSTEM_CONFIG_FILE", str(tmp_path / "system_config.json"))
    runtime_config.reset_cache()
    yield
    runtime_config.reset_cache()


# ---------------------------------------------------------------- 地址格式护栏


def test_placeholder_base_is_rejected_with_actionable_message():
    """带 {WorkspaceId} 的模板地址必须被拒，且文案要点出「占位符」这个真因。"""
    with pytest.raises(ValueError) as exc:
        runtime_config._validate("llm_api_base", PLACEHOLDER_BASE)
    msg = str(exc.value)
    assert "占位符" in msg
    assert "{WorkspaceId}" in msg  # 要指出具体是哪个占位符，否则用户还得自己找
    assert "https://" in msg  # 顺带给出一个可用的正确示例


def test_base_without_scheme_is_rejected():
    """漏写协议头的地址（控制台里很常见的复制错误）也要拦。"""
    with pytest.raises(ValueError) as exc:
        runtime_config._validate("llm_api_base", "dashscope.aliyuncs.com/compatible-mode/v1")
    assert "https://" in str(exc.value)


def test_valid_base_passes_and_keeps_trailing_slash():
    """合法地址原样通过：不擅自改写用户填的地址（避免引入新的不可预期）。"""
    assert runtime_config._validate("llm_api_base", DASHSCOPE_BASE) == DASHSCOPE_BASE


def test_empty_eval_base_still_means_reuse_chat_model():
    """评测地址留空 = 复用对话模型，这条既有语义不能被新护栏打断。"""
    assert runtime_config._validate("eval_llm_api_base", "") == ""


def test_update_config_rejects_placeholder_and_leaves_store_untouched():
    """写入侧同样拒绝：不能出现「接口返回 200 但地址其实存坏了」的情况。"""
    runtime_config.update_config({"llm_api_base": DASHSCOPE_BASE})
    with pytest.raises(ValueError):
        runtime_config.update_config({"llm_api_base": PLACEHOLDER_BASE})
    # 失败的那次不能覆盖上一次的合法值
    assert runtime_config.get_value("llm_api_base") == DASHSCOPE_BASE


# ------------------------------------------------------- 百炼思考模式自动关闭


def test_dashscope_base_disables_thinking():
    """百炼地址 → 显式关闭思考模式（否则 tool_choice 不可用）。"""
    assert factory._dashscope_extra_body(DASHSCOPE_BASE) == {"enable_thinking": False}
    # 新加坡节点的域名（dashscope-intl）同样要命中
    assert factory._dashscope_extra_body(
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    ) == {"enable_thinking": False}


@pytest.mark.parametrize(
    "base",
    [
        "https://api.deepseek.com/v1",
        "https://api.siliconflow.cn/v1",
        "https://api-inference.modelscope.cn/v1",
        "",
        None,
    ],
)
def test_other_vendors_get_no_extra_body(base):
    """非百炼一律不传：enable_thinking 是百炼私有参数，传给别人会 400。"""
    assert factory._dashscope_extra_body(base) is None


def test_chat_model_carries_extra_body_only_for_dashscope(monkeypatch):
    """工厂真正构造出的实例要带上该参数（防止只测了纯函数、漏了接线）。"""
    monkeypatch.setattr(factory.runtime_config, "get_value", lambda key: {
        "llm_provider": "openai",
        "llm_model": "qwen3.7-plus",
        "llm_api_base": DASHSCOPE_BASE,
        "llm_temperature": 0.2,
    }.get(key))
    monkeypatch.setattr(factory.runtime_config, "get_secret", lambda key: "sk-test-key")
    factory._cache.clear()
    model = factory.get_chat_model()
    assert getattr(model, "extra_body", None) == {"enable_thinking": False}
    factory._cache.clear()
