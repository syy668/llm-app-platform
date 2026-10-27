"""系统设置（裁判配置）的请求/响应模型。

温度口径：请求体里**故意不提供 temperature 字段** —— 裁判温度锁死 0.0，
前端即便传了也会被 pydantic 忽略（extra 默认 ignore），后端更不会读。
"""

from pydantic import BaseModel, Field


class JudgeConfigUpdate(BaseModel):
    """更新裁判配置。只提交需要修改的字段；api_key 留空或掩码 = 不修改。"""

    model: str | None = Field(default=None, description="裁判模型名")
    api_base: str | None = Field(default=None, description="OpenAI 兼容 base_url")
    api_key: str | None = Field(default=None, description="API Key；留空/掩码表示沿用现有密钥")
    embedding_model: str | None = Field(default=None, description="向量模型（answer_relevancy 用）")
    # 向量模型独立连接（留空 = 与裁判同源）。用于「LLM 走百炼、embedding 走硅基流动 bge-m3」
    embedding_api_base: str | None = Field(default=None, description="向量模型 base_url；留空=沿用裁判的")
    embedding_api_key: str | None = Field(default=None, description="向量模型 Key；留空/掩码表示不修改")
    # 注意：这里**没有** temperature —— 可复现性优先，裁判温度强制为 0


class JudgePreflightResult(BaseModel):
    ok: bool
    message: str
    model: str | None = None
    latency_ms: int | None = None
