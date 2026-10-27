"""运行时可调系统配置（热更新，无需重启服务）。

设计要点：
- 两层取值：管理员保存的「覆盖值」持久化在 JSON 文件中，优先级最高；
  未覆盖的键每次读取时动态解析「默认值」（来自 .env / 代码默认），
  因此 .env 调整与默认行为保持与旧逻辑完全一致。
- 恢复默认：delete_override(key) 删除覆盖值后，该键立即回退到 .env 默认值，
  避免「配置页保存过一次，.env 就永久失效」。
- 文件存储：默认 data/system_config.json（可用 .env: SYSTEM_CONFIG_FILE 覆盖路径），
  原子写入（临时文件 + os.replace），进程内线程安全（RLock）。
- 读取侧永不抛错：文件缺失 / 损坏 / 值越界时回退默认值，不影响业务。
- 密钥脱敏：llm_api_key 为敏感字段，get_config / get_value 返回掩码；
  业务侧需真实值时使用 get_secret（生效值）或 get_override（覆盖值）。
- 【v1.7 新增】评测判分模型可独立配置：eval_llm_model / eval_llm_api_base /
  eval_llm_api_key 三项可选项，留空（或删除覆盖值）时评测自动回退复用全局
  llm_model，保持与 v1.6 及之前版本完全一致的默认行为。

本模块只依赖 app.config，禁止反向导入业务模块（避免循环依赖）。
"""
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

from ..config import BASE_DIR, settings

logger = logging.getLogger(__name__)

# 令牌桶限流默认值与 rate_limit.py 旧逻辑保持一致
_DEFAULT_RATE_LIMIT = 30
# 工具执行超时默认值与 agent/graph.py 旧常量保持一致
_DEFAULT_TOOL_TIMEOUT = 30
# 工具重试默认值与 agent/tools/retry.py 旧逻辑保持一致
_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_RETRY_DELAY = 0.5
# LLM 温度默认值与改造前 agent/rag 链路的硬编码保持一致（现统一由 app/llm/factory.py 使用）
_DEFAULT_LLM_TEMPERATURE = 0.2
# DeepSeek 默认接口地址（与改造前 agent 链路硬编码保持一致，现统一由 app/llm/factory.py 使用）
_DEFAULT_LLM_API_BASE = "https://api.deepseek.com/v1"
# llm_api_key 掩码：GET /system/config 永远返回掩码，不回显真实密钥
SECRET_MASK = "******"

# 可调字段元信息：key -> (类型, 下界, 上界, 说明)
_FIELD_SPECS: dict[str, tuple[type, Any, Any, str]] = {
    "ai_rate_limit_per_minute": (int, 1, 10000, "每用户每分钟 AI 请求上限"),
    "tool_timeout_seconds": (int, 5, 300, "工具执行超时（秒）"),
    "tool_max_attempts": (int, 1, 10, "工具最大尝试次数"),
    "tool_retry_delay_seconds": (float, 0.0, 60.0, "工具重试间隔（秒）"),
    "llm_temperature": (float, 0.0, 2.0, "LLM 采样温度"),
    "llm_model": (str, 1, 128, "LLM 模型名（任意 OpenAI 兼容模型）"),
    "llm_api_base": (str, 0, 512, "LLM 接口地址（OpenAI 兼容）"),
    # 【v1.7 新增】评测判分模型（可选）：下界为 0，允许空串表示「复用对话模型」。
    # 与 llm_model 的差异仅在此处——对话模型必填，评测模型可留空。
    "eval_llm_model": (str, 0, 128, "评测判分模型名（留空则复用对话模型）"),
    "eval_llm_api_base": (str, 0, 512, "评测判分接口地址（留空则复用对话模型地址）"),
    # 【B2-4/多服务商 新增】提供方与 Embedding 热更新键（枚举键在 _validate 中校验）
    "llm_provider": (str, 1, 32, "LLM 提供方"),
    "embedding_provider": (str, 1, 32, "Embedding 提供方"),
    "embedding_model": (str, 0, 128, "Embedding 模型名"),
    "embedding_api_base": (str, 0, 512, "Embedding 接口地址（OpenAI 兼容）"),
}
# 敏感字段：读取时脱敏，真实值仅经 get_secret / get_override 提供
_SECRET_FIELDS = {"llm_api_key", "embedding_api_key", "eval_llm_api_key"}

# 【v1.7 新增】评测模型的可选字段：提交空串 = 清除覆盖值（回退复用对话模型），
# 而不是存一个空串覆盖 .env，避免「清空后仍显示已覆盖」的错觉。
_OPTIONAL_OVERRIDE_KEYS = {"eval_llm_model", "eval_llm_api_base"}

# 提供方枚举白名单（与 .env 取值一致）：非法值写入直接拒绝
_ENUM_FIELDS: dict[str, tuple[str, ...]] = {
    "llm_provider": ("mock", "openai", "anthropic"),
    "embedding_provider": ("hash", "openai"),
}

# 【多服务商 新增】OpenAI 兼容服务商预设（后台页面一键切换，无需改代码/重启）。
# 说明：预设的 api_base 与推荐模型为各服务商公开文档的通用值，接口规范随厂商
# 更新可能变动（需核实），管理员可在页面上自行改写模型名/地址后保存。
# DeepSeek / ModelScope 官方目前未提供 Embedding 接口（需核实），
# 需要 Embedding 时可看 SiliconFlow（BAAI/bge 系列）或百炼（text-embedding-v3/v4）。
_PROVIDER_PRESETS: list[dict[str, Any]] = [
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "llm_api_base": "https://api.deepseek.com/v1",
        "llm_models": ["deepseek-chat", "deepseek-reasoner"],
        "embedding_api_base": "",
        "embedding_models": [],
    },
    {
        "id": "siliconflow",
        "name": "SiliconFlow 硅基流动",
        "llm_api_base": "https://api.siliconflow.cn/v1",
        "llm_models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"],
        "embedding_api_base": "https://api.siliconflow.cn/v1",
        "embedding_models": ["BAAI/bge-m3", "BAAI/bge-large-zh-v1.5"],
    },
    {
        "id": "dashscope",
        "name": "DashScope 阿里云百炼",
        # 华北 2（北京）；新加坡地域改为 https://dashscope-intl.aliyuncs.com/compatible-mode/v1
        "llm_api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        # 只放「默认关闭思考模式」的商业版型号。本系统的问答与判分都是**非流式**调用，
        # 思考模式会先产 reasoning_content 再出正文（首字延迟翻倍、判分温度也不适用），
        # 而 qwen3.8 / 3.7 / 3.6 / 3.5 系列默认开启思考 —— 想用它们须显式传
        # enable_thinking=false（本预设不代劳，避免悄悄改变回答风格）。
        # 更早的 qwen3-max 在实测账号上已返回 404 Model not exist（已下线），故不列入。
        "llm_models": ["qwen-plus", "qwen-max", "qwen-flash", "qwen-long"],
        # 百炼 Embedding 与对话同一 base；但换 embedding 必须重跑文档处理（向量空间不同），
        # 此处仅作元数据，后台页面未开放切换。
        "embedding_api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "embedding_models": ["text-embedding-v4", "text-embedding-v3"],
    },
    {
        "id": "modelscope",
        "name": "ModelScope 魔搭",
        "llm_api_base": "https://api-inference.modelscope.cn/v1",
        "llm_models": ["Qwen/Qwen2.5-72B-Instruct", "deepseek-ai/DeepSeek-V3"],
        "embedding_api_base": "",
        "embedding_models": [],
    },
]

_lock = threading.RLock()
# 覆盖值缓存：仅包含管理员显式保存过的键；None 表示尚未从磁盘加载
_overrides: dict[str, Any] | None = None


def _store_path() -> Path:
    """覆盖值文件路径：.env SYSTEM_CONFIG_FILE 优先，否则项目根目录 data/ 下。"""
    custom = os.getenv("SYSTEM_CONFIG_FILE", "").strip()
    if custom:
        return Path(custom)
    return BASE_DIR / "data" / "system_config.json"


def _default_value(key: str) -> Any:
    """动态解析某键的默认值（每次调用时读取环境，保证 .env 改动即时可见）。"""
    if key == "ai_rate_limit_per_minute":
        try:
            return max(1, int(os.getenv("AI_RATE_LIMIT_PER_MINUTE", str(_DEFAULT_RATE_LIMIT))))
        except ValueError:
            return _DEFAULT_RATE_LIMIT
    if key == "tool_timeout_seconds":
        return _DEFAULT_TOOL_TIMEOUT
    if key == "tool_max_attempts":
        try:
            return max(1, int(os.getenv("TOOL_MAX_ATTEMPTS", str(_DEFAULT_MAX_ATTEMPTS))))
        except ValueError:
            return _DEFAULT_MAX_ATTEMPTS
    if key == "tool_retry_delay_seconds":
        try:
            return max(0.0, float(os.getenv("TOOL_RETRY_DELAY_SECONDS", str(_DEFAULT_RETRY_DELAY))))
        except ValueError:
            return _DEFAULT_RETRY_DELAY
    if key == "llm_temperature":
        return _DEFAULT_LLM_TEMPERATURE
    if key == "llm_model":
        model = (settings.llm_model or "").strip()
        return model if model else "deepseek-chat"
    if key == "llm_api_base":
        return (settings.llm_api_base or "").strip()
    if key == "llm_api_key":
        return settings.llm_api_key or ""
    # 【v1.7 新增】评测判分模型：默认空串（= 复用对话模型），
    # 支持 EVAL_LLM_MODEL / EVAL_LLM_API_BASE / EVAL_LLM_API_KEY 环境变量兜底。
    if key == "eval_llm_model":
        return os.getenv("EVAL_LLM_MODEL", "").strip()
    if key == "eval_llm_api_base":
        return os.getenv("EVAL_LLM_API_BASE", "").strip()
    if key == "eval_llm_api_key":
        return os.getenv("EVAL_LLM_API_KEY", "").strip()
    if key == "llm_provider":
        return settings.llm_provider
    if key == "embedding_provider":
        return settings.embedding_provider
    if key == "embedding_model":
        return (settings.embedding_model or "").strip()
    if key == "embedding_api_base":
        return (settings.embedding_api_base or "").strip()
    if key == "embedding_api_key":
        return settings.embedding_api_key or ""
    raise KeyError(f"未知的系统配置项: {key}")


# 【v1.15】接口地址格式护栏。
# 真实故障：管理员从云厂商控制台复制了模板地址
#   https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
# 未替换 {WorkspaceId} 就保存了。请求时 DNS 解析失败 → openai SDK 抛
# APIConnectionError("Connection error.") → 前端只看到「AI 服务连接超时，请检查网络」，
# 完全指向不了真因，还会让人去排查网络。占位符这类问题在**保存时**就能判死，
# 所以在这里直接拒绝，把错误暴露在配置页，而不是留到下一次问答。
_API_BASE_KEYS = {"llm_api_base", "eval_llm_api_base", "embedding_api_base"}
_PLACEHOLDER_RE = re.compile(r"\{[^}]{1,64}\}")
_URL_SCHEME_RE = re.compile(r"^https?://[^\s/]+", re.IGNORECASE)


def _validate_api_base(text: str, label: str) -> str:
    """校验 OpenAI 兼容接口地址；空串放行（语义是「沿用默认值」）。"""
    if not text:
        return text
    if any(ch.isspace() for ch in text):
        raise ValueError(f"{label} 不能包含空格")
    placeholder = _PLACEHOLDER_RE.search(text)
    if placeholder:
        raise ValueError(
            f"{label} 里的 {placeholder.group(0)} 是占位符，需替换成真实值后再保存"
            "（如阿里云百炼公共地址为 https://dashscope.aliyuncs.com/compatible-mode/v1）"
        )
    if not _URL_SCHEME_RE.match(text):
        raise ValueError(f"{label} 必须是 https:// 或 http:// 开头的完整地址")
    return text


def _validate(key: str, value: Any) -> Any:
    """校验并规整单个配置值；越界 / 类型不符时抛 ValueError（写入侧使用）。"""
    if key in _ENUM_FIELDS:
        # 枚举键：仅接受白名单取值（多服务商切换的安全护栏）
        text = str(value).strip().lower()
        if text not in _ENUM_FIELDS[key]:
            allowed = " / ".join(_ENUM_FIELDS[key])
            raise ValueError(f"{_FIELD_SPECS[key][3]} 仅支持: {allowed}")
        return text
    if key in _SECRET_FIELDS:
        # 密钥仅做长度限制，不限制字符内容（各厂商 key 格式不同）
        text = str(value).strip()
        if len(text) > 256:
            raise ValueError("API Key 长度不能超过 256")
        return text
    if key in _FIELD_SPECS:
        expected, low, high, label = _FIELD_SPECS[key]
        if expected is str:
            text = str(value).strip()
            if len(text) < low:
                raise ValueError(f"{label} 不能为空")
            if len(text) > high:
                raise ValueError(f"{label} 长度不能超过 {high}")
            if key in _API_BASE_KEYS:
                text = _validate_api_base(text, label)
            return text
        try:
            value = expected(value)
        except (TypeError, ValueError):
            raise ValueError(f"{label} 必须是数字")
        if not low <= value <= high:
            raise ValueError(f"{label} 取值范围为 {low} ~ {high}")
        return value
    raise KeyError(f"未知的系统配置项: {key}")


def _is_masked(value: Any) -> bool:
    """判断提交的密钥值是否为掩码 / 空值（视为"未修改"）。"""
    return not str(value).strip() or str(value).strip() == SECRET_MASK


def _load_overrides() -> dict[str, Any]:
    """从磁盘加载覆盖值（带缓存）；文件缺失 / 损坏时静默回退为空。"""
    global _overrides
    if _overrides is not None:
        return _overrides
    path = _store_path()
    data: dict[str, Any] = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            data = raw
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 - 损坏文件不应阻断业务
        logger.warning("系统配置文件读取失败，忽略使用默认值: %s", exc)
    # 只保留合法字段且值合法的覆盖项
    clean: dict[str, Any] = {}
    for key, value in data.items():
        # 【v1.7 修复】原判断硬编码 llm_api_key，导致 embedding_api_key /
        # eval_llm_api_key 这类「只在 _SECRET_FIELDS 里」的键重启后被静默丢弃。
        if key not in _FIELD_SPECS and key not in _SECRET_FIELDS:
            continue
        try:
            clean[key] = _validate(key, value)
        except ValueError:
            logger.warning("系统配置项 %s 值非法，已回退默认值", key)
    _overrides = clean
    return _overrides


def _save_overrides(overrides: dict[str, Any]) -> None:
    """原子写入覆盖值文件。调用方需持有锁。"""
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _all_keys() -> list[str]:
    return list(_FIELD_SPECS.keys()) + list(_SECRET_FIELDS)


def get_provider_presets() -> list[dict[str, Any]]:
    """【多服务商 新增】返回 OpenAI 兼容服务商预设列表（只读元数据，随 get_config 下发）。"""
    return _PROVIDER_PRESETS


def get_config() -> dict[str, Any]:
    """返回当前生效的完整配置（默认值 + 覆盖值）；密钥字段返回掩码。

    【多服务商 新增】额外携带 provider_presets（服务商预设，非可写配置项），
    供后台配置页渲染服务商切换下拉框。
    【v1.6 新增】额外携带 overridden_keys：当前存在「覆盖值」的键名列表。
    覆盖值优先于 .env，配置页据此区分「管理员保存过的值」与「.env 默认值」，
    并决定「恢复默认」按钮是否可点。
    """
    with _lock:
        overrides = _load_overrides()
        result: dict[str, Any] = {}
        for key in _all_keys():
            if key in _SECRET_FIELDS:
                result[key] = SECRET_MASK if get_secret(key) else ""
            else:
                result[key] = overrides.get(key, _default_value(key))
        result["provider_presets"] = _PROVIDER_PRESETS
        result["overridden_keys"] = sorted(overrides.keys())
        return result


def get_value(key: str) -> Any:
    """读取单个非敏感配置项的当前生效值（读取侧永不抛错，异常回退默认值）。

    敏感字段（llm_api_key）一律返回 None，需要真实值请用 get_secret。
    """
    if key in _SECRET_FIELDS:
        return None
    with _lock:
        overrides = _load_overrides()
        if key in overrides:
            try:
                return _validate(key, overrides[key])
            except ValueError:
                pass
        try:
            return _default_value(key)
        except KeyError:
            return None


def get_secret(key: str) -> str:
    """读取敏感配置项（llm_api_key）的当前生效值：覆盖值优先，否则 .env 默认。"""
    if key not in _SECRET_FIELDS:
        raise KeyError(f"非敏感配置项请使用 get_value: {key}")
    with _lock:
        overrides = _load_overrides()
        if key in overrides:
            return str(overrides[key])
        try:
            return str(_default_value(key))
        except KeyError:
            return ""


def get_override(key: str) -> Any:
    """读取某键的「覆盖值」（未保存过返回 None）；敏感字段返回真实值。

    供业务侧区分「用户显式配置」与「默认值」，保证默认行为与旧逻辑逐字节一致。
    """
    with _lock:
        overrides = _load_overrides()
        return overrides.get(key)


def update_config(changes: dict[str, Any]) -> dict[str, Any]:
    """合并保存覆盖值并持久化，返回更新后的完整配置。值非法抛 ValueError。

    llm_api_key 提交空值 / 掩码视为「未修改」，不覆盖已保存密钥。
    【v1.7 新增】eval_llm_model / eval_llm_api_base 提交空串视为「清除覆盖」，
    直接回退复用对话模型，而不是写入一个空串覆盖（恢复默认同样走 delete_override）。
    """
    global _overrides
    validated: dict[str, Any] = {}
    cleared: list[str] = []
    for key, value in changes.items():
        if key in _SECRET_FIELDS and _is_masked(value):
            continue  # 掩码 / 空值 = 保持现状
        if key in _OPTIONAL_OVERRIDE_KEYS and not str(value).strip():
            cleared.append(key)  # 清空 = 恢复「复用对话模型」
            continue
        validated[key] = _validate(key, value)
    with _lock:
        overrides = dict(_load_overrides())
        overrides.update(validated)
        for key in cleared:
            overrides.pop(key, None)
        _save_overrides(overrides)
        _overrides = overrides
        return get_config()


def delete_override(key: str) -> bool:
    """删除某键的「覆盖值」，使其立即回退为 .env / 代码默认值。

    返回值：True 表示确实删掉了一条覆盖值；False 表示该键本来就没有覆盖值
    （幂等语义：重复删除不报错，删完仍是默认值）。
    未知键抛 KeyError，由接口层转为 404。

    【v1.6 新增】解决「覆盖值永久遮蔽 .env」问题：此前配置页保存过一次
    ai_rate_limit_per_minute 后，改 .env 就再也不生效，且没有恢复入口。
    """
    global _overrides
    if key not in _all_keys():
        raise KeyError(f"未知的系统配置项: {key}")
    with _lock:
        overrides = dict(_load_overrides())
        if key not in overrides:
            return False
        overrides.pop(key)
        _save_overrides(overrides)
        _overrides = overrides
        logger.info("系统配置项 %s 的覆盖值已删除，回退默认值", key)
        return True


def reset_cache() -> None:
    """清空内存缓存（测试用），下次读取时重新加载磁盘。"""
    global _overrides
    with _lock:
        _overrides = None
