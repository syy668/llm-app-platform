"""全局配置：Pydantic-Settings 统一收口，零硬编码魔数（PRD NFR-M3）。"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import quote_plus

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_SECRET = "dev-only-secret-change-me-0123456789abcdef"


class Settings(BaseSettings):
    """全部运行期配置。环境变量优先于 .env 文件，字段名即环境变量名。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ---------------- 应用 ----------------
    APP_NAME: str = "AgentFlow Platform"
    APP_ENV: Literal["local", "test", "staging", "prod"] = "local"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ---------------- MySQL ----------------
    MYSQL_HOST: str = "127.0.0.1"
    MYSQL_PORT: int = 3306
    MYSQL_USER: str = "agentflow"
    MYSQL_PASSWORD: str = "agentflow_pwd"
    MYSQL_DB: str = "agentflow"
    MYSQL_POOL_SIZE: int = 10
    MYSQL_MAX_OVERFLOW: int = 20
    MYSQL_POOL_RECYCLE: int = 1800
    MYSQL_ECHO: bool = False

    # ---------------- Redis ----------------
    REDIS_HOST: str = "127.0.0.1"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str | None = None
    REDIS_MAX_CONNECTIONS: int = 50
    REDIS_KEY_PREFIX: str = "agentflow"

    # ---------------- 鉴权（双 Token + 黑名单） ----------------
    SECRET_KEY: str = _DEFAULT_SECRET
    JWT_ALGORITHM: str = "HS256"
    JWT_ISSUER: str = "agentflow-platform"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 120
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    BCRYPT_ROUNDS: int = 12
    AUTH_MAX_SESSIONS_PER_USER: int = 5

    # ---------------- LLM（W1 仅配置预留） ----------------
    LLM_PROVIDER: str = "deepseek"
    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "deepseek-chat"
    LLM_FALLBACK_MODEL: str = "Qwen/Qwen2.5-72B-Instruct"
    LLM_TEMPERATURE: float = Field(default=0.2, ge=0.0, le=2.0)
    LLM_MAX_TOKENS: int = 8192
    LLM_TIMEOUT_SECONDS: int = 60
    LLM_MAX_RETRY: int = 2

    # ---------------- 外部依赖 Mock 开关 ----------------
    MOCK_EXTERNAL_RAG: bool = True
    MOCK_EXTERNAL_EVAL: bool = True
    #: 项目一《企业垂直知识库 Agent 后台》后端根地址。
    #: 注意不能填 8000 —— 那是 AgentFlow 自己的端口，会造成自我调用。
    EXTERNAL_RAG_BASE_URL: str = "http://127.0.0.1:8010"
    EXTERNAL_RAG_TOKEN: str = ""
    EXTERNAL_RAG_TIMEOUT_SECONDS: int = 8
    #: 检索契约：project1_kb_query（项目一真实接口） / legacy_kb_search（PRD A-01 假设接口） / mall_only
    EXTERNAL_RAG_CONTRACT: str = "project1_kb_query"
    #: 项目一知识库 ID（kb-search 检索的目标库）
    EXTERNAL_RAG_KB_ID: int = 1
    #: 服务账号：优先用 EXTERNAL_RAG_TOKEN（静态令牌）；留空则用账号密码自动登录换取令牌
    EXTERNAL_RAG_USERNAME: str = ""
    EXTERNAL_RAG_PASSWORD: str = ""
    #: 令牌缓存秒数（项目一 access_token 有效期 30 分钟，提前刷新）
    EXTERNAL_RAG_TOKEN_TTL_SECONDS: int = 1500
    EXTERNAL_EVAL_BASE_URL: str = "http://localhost:8100"
    EXTERNAL_EVAL_TIMEOUT_SECONDS: int = 10

    # ---------------- W6 评测触发链路（W5-Part2 欠账收口，PRD L704-706） ----------------
    # 项目二 EvalAgent 根地址：空值 + 非 mock 时触发评测直接 502（EVAL_UPSTREAM_UNAVAILABLE）
    #
    # 【v1.4 重要更正】这个地址在本机**没有对应服务**：项目二 EvalAgent 实际是
    # 纯 Vue 前端（evalagent_vue/，见其 README「不新增任何后端服务」），它自己也是
    # 通过 HTTP 调项目一的 /api/v1/eval/*。真正持有 RAGAS 判分底座的只有项目一。
    # 因此非 mock 模式下优先走下面的 PROJECT1_EVAL_*（真实 RAGAS），
    # EVALAGENT_BASE_URL 保留仅为兼容「假设存在项目二后端」的旧部署形态。
    EVALAGENT_BASE_URL: str = ""
    # 后台轮询评测结果的间隔与最大重试次数（30 次 × 10s ≈ 5 分钟）
    EVAL_POLL_INTERVAL_S: int = 10
    EVAL_POLL_MAX_RETRY: int = 30

    # ---------------- v1.4 真实 RAGAS 评测（直连项目一） ----------------
    #: 项目一评测数据集 ID（int）。0 = 未配置 → 非 mock 时直接 502 并给出可读原因。
    #: 项目一的 POST /eval/runs 只接受整数 dataset_id，AgentFlow 侧原有的字符串
    #: dataset_id（如 "agentflow-30"）是自由标签，不能透传，必须映射成这个 ID。
    PROJECT1_EVAL_DATASET_ID: int = 0
    #: 项目一评测接口根的覆盖值；留空复用 EXTERNAL_RAG_BASE_URL（同一个后端）
    PROJECT1_EVAL_BASE_URL: str = ""
    #: 单次触发/轮询请求超时（评测触发是同步返回的，不需要长超时）
    PROJECT1_EVAL_TIMEOUT_SECONDS: int = 30
    #: 真实评测轮询间隔与上限：单样本 40~280s（4 项指标多次判分），
    #: 5 条约 5 分钟，30 条约 30 分钟。上限按「30 条 × 6 分钟」留足余量。
    PROJECT1_EVAL_POLL_INTERVAL_S: int = 5
    PROJECT1_EVAL_POLL_MAX_RETRY: int = 400

    # ---------------- LangGraph（W1 仅预留） ----------------
    LANGGRAPH_CHECKPOINT_TTL_SECONDS: int = 72 * 3600

    # ---------------- 校验 ----------------
    @field_validator("SECRET_KEY")
    @classmethod
    def _secret_key_min_length(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("SECRET_KEY 长度必须 >= 32 位（openssl rand -hex 32）")
        return v

    @field_validator("LOG_LEVEL")
    @classmethod
    def _log_level_upper(cls, v: str) -> str:
        level = v.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"非法 LOG_LEVEL: {v}")
        return level

    def model_post_init(self, __context: object) -> None:  # noqa: D401
        if self.APP_ENV == "prod" and self.SECRET_KEY == _DEFAULT_SECRET:
            raise ValueError("生产环境必须通过环境变量注入独立 SECRET_KEY")

    # ---------------- 派生配置 ----------------
    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """SQLAlchemy 2.0 异步 DSN（aiomysql）。"""
        pwd = quote_plus(self.MYSQL_PASSWORD)
        return (
            f"mysql+aiomysql://{self.MYSQL_USER}:{pwd}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DB}?charset=utf8mb4"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def alembic_url(self) -> str:
        """Alembic 迁移使用同一异步 DSN（alembic/env.py 走 async engine）。"""
        return self.database_url

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redis_url(self) -> str:
        auth = f":{quote_plus(self.REDIS_PASSWORD)}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.CORS_ORIGINS.split(",") if item.strip()]

    @property
    def is_test(self) -> bool:
        return self.APP_ENV == "test"

    def key(self, *parts: str) -> str:
        """统一 Redis key 前缀拼装：settings.key("auth", "blacklist", jti)。"""
        return ":".join((self.REDIS_KEY_PREFIX, *parts))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例；测试通过 cache_clear() 或环境变量覆盖。"""
    return Settings()


settings = get_settings()
