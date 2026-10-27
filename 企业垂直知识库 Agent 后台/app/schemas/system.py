"""系统配置相关的请求模型。"""
from pydantic import BaseModel, Field


class SystemConfigUpdate(BaseModel):
    """系统配置修改请求（全部字段可选，仅提交需要修改的项）。

    llm_model 支持任意 OpenAI 兼容模型名（自定义模型）；
    llm_api_key 提交空值 / 掩码视为「未修改」，不覆盖已保存密钥。
    """

    # 每用户每分钟 AI 请求上限
    ai_rate_limit_per_minute: int | None = Field(None, ge=1, le=10000)
    # 工具执行超时（秒）
    tool_timeout_seconds: int | None = Field(None, ge=5, le=300)
    # 工具最大尝试次数（含首次）
    tool_max_attempts: int | None = Field(None, ge=1, le=10)
    # 工具重试间隔（秒）
    tool_retry_delay_seconds: float | None = Field(None, ge=0, le=60)
    # LLM 采样温度
    llm_temperature: float | None = Field(None, ge=0, le=2)
    # LLM 模型名（任意 OpenAI 兼容模型，如 deepseek-chat / qwen-max / 自部署模型）
    llm_model: str | None = Field(None, min_length=1, max_length=128)
    # LLM 接口地址（OpenAI 兼容；留空使用服务端默认）
    llm_api_base: str | None = Field(None, max_length=512)
    # LLM API Key（写-only：读取接口只返回掩码）
    llm_api_key: str | None = Field(None, max_length=256)

    # ---------- 【v1.7 新增】评测判分模型（可选）----------
    # 留空 / 不传 = 复用上面配置的对话模型；一旦填写则判分使用独立模型，规避自评偏差。
    # 注意：此处不设 min_length，空串是合法输入（语义为「清除该覆盖项」）。
    eval_llm_model: str | None = Field(None, max_length=128)
    eval_llm_api_base: str | None = Field(None, max_length=512)
    # 评测模型 API Key（写-only：读取接口只返回掩码；留空表示复用对话模型的 Key）
    eval_llm_api_key: str | None = Field(None, max_length=256)

    # ---------- 【多服务商热更新 新增】----------
    # LLM 提供方：mock=本地回显(测试) / openai=OpenAI 兼容(DeepSeek/硅基流动/魔搭等) / anthropic
    llm_provider: str | None = Field(None, pattern="^(mock|openai|anthropic)$")    # Embedding 提供方：hash=本地特征哈希(测试) / openai=OpenAI 兼容接口
    embedding_provider: str | None = Field(None, pattern="^(hash|openai)$")
    # Embedding 模型名（OpenAI 兼容，如 BAAI/bge-m3）；留空使用服务端默认
    embedding_model: str | None = Field(None, max_length=128)
    # Embedding 接口地址（OpenAI 兼容）；留空使用服务端默认
    embedding_api_base: str | None = Field(None, max_length=512)
    # Embedding API Key（写-only：读取接口只返回掩码）
    embedding_api_key: str | None = Field(None, max_length=256)
