"""全局限流配置（v1.1 任务 2）。

单独一个文件的原因与 W2/W3/W5/W6 一致：**w1 的 config.py 冻结不动**，新阶段开关各自收口，
读取同一份 ``.env``（字段名即环境变量名，大小写敏感）。

三类配额刻意分开，因为成本差两个数量级：

- ``api``：普通 CRUD / 查询，最便宜，额度最大；
- ``llm``：会驱动 LLM 的写接口（建任务 / 断点续跑 / 批量审批），单次几秒到几十秒，额度最小；
- ``kb``：知识库检索（``/kb/status`` 探测等），受外部项目一限制，中等额度；
- ``task``：单任务维度的总闸门（同一任务被反复轮询 / 重复 resume 时兜底）。

设计口径：固定窗口（``INCR + EXPIRE``）而不是令牌桶——实现简单、口径可解释、
跨进程一致（Redis 计数），代价是窗口边界可能有 2 倍突发，对"防滥用"足够。
``RATE_LIMIT_ENABLED=false`` 时中间件完全不接管（零 Redis 访问、零开销）。
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RateLimitSettings(BaseSettings):
    """限流全量可调参数。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    #: 总开关。测试环境（APP_ENV=test）默认不生效，避免用例互相污染（见中间件实现）
    RATE_LIMIT_ENABLED: bool = True
    #: 固定窗口长度（秒）
    RATE_LIMIT_WINDOW_SECONDS: int = Field(default=60, ge=1, le=3600)

    #: 普通接口：每用户每窗口请求数
    RATE_LIMIT_API_PER_MINUTE: int = Field(default=300, ge=1, le=100000)
    #: LLM 类接口：每用户每窗口请求数
    RATE_LIMIT_LLM_PER_MINUTE: int = Field(default=30, ge=1, le=10000)
    #: 知识库检索类接口：每用户每窗口请求数
    RATE_LIMIT_KB_PER_MINUTE: int = Field(default=60, ge=1, le=10000)
    #: 单任务维度：每任务每窗口请求数（轮询风暴兜底）
    RATE_LIMIT_TASK_PER_MINUTE: int = Field(default=240, ge=1, le=100000)

    #: 免限流路径前缀（逗号分隔）：健康检查 / 文档 / 静态资源不该被限流
    RATE_LIMIT_EXEMPT_PATHS: str = (
        "/health,/healthz,/readyz,/metrics,/docs,/redoc,/openapi.json,"
        "/static,/favicon.ico"
    )
    #: 拿不到用户身份（未登录 / 令牌无效）时是否按 IP 限流
    RATE_LIMIT_PER_IP_ENABLED: bool = True
    #: 是否在响应头回写下发额度信息（前端可据此做"剩余次数"提示）
    RATE_LIMIT_EXPOSE_HEADERS: bool = True
    #: 限流 key 前缀（与其它业务 key 区分，便于独立清理）
    RATE_LIMIT_KEY_PREFIX: str = "rl"

    # ---- 加固任务 3：LLM 类令牌桶 + 用户级日配额 ----
    #: LLM 类是否改用令牌桶（允许轻微突发）；False 时回退固定窗口
    RATE_LIMIT_LLM_BUCKET_ENABLED: bool = True
    #: 令牌桶容量（最大突发量）。0 = 自动：max(1, RATE_LIMIT_LLM_PER_MINUTE // 2)
    RATE_LIMIT_LLM_BUCKET_CAPACITY: int = Field(default=0, ge=0, le=10000)
    #: 单用户每日 LLM 类请求数上限（0 = 不启用日配额）
    RATE_LIMIT_DAILY_TASKS_PER_USER: int = Field(default=100, ge=0, le=1000000)
    #: 单用户每日 Token 消耗预算（0 = 不启用成本配额）
    RATE_LIMIT_DAILY_TOKEN_BUDGET_PER_USER: int = Field(default=200000, ge=0, le=100000000)

    def exempt_prefixes(self) -> tuple[str, ...]:
        return tuple(item.strip() for item in self.RATE_LIMIT_EXEMPT_PATHS.split(",") if item.strip())

    def limit_for(self, kind: str) -> int:
        return {
            "api": self.RATE_LIMIT_API_PER_MINUTE,
            "llm": self.RATE_LIMIT_LLM_PER_MINUTE,
            "kb": self.RATE_LIMIT_KB_PER_MINUTE,
            "task": self.RATE_LIMIT_TASK_PER_MINUTE,
        }.get(kind, self.RATE_LIMIT_API_PER_MINUTE)

    def bucket_params(self, kind: str) -> tuple[bool, int, float]:
        """返回 ``(是否启用令牌桶, 容量, 每秒补充速率)``。

        当前仅 LLM 类启用（成本最高、最需要"削突发 + 平滑补给"）；
        速率 = 配额 / 窗口秒数，容量默认取配额一半（允许轻微突发）。
        """
        if kind != "llm" or not self.RATE_LIMIT_LLM_BUCKET_ENABLED:
            return False, 0, 0.0
        limit = self.RATE_LIMIT_LLM_PER_MINUTE
        capacity = self.RATE_LIMIT_LLM_BUCKET_CAPACITY or max(1, limit // 2)
        refill = limit / max(1, self.RATE_LIMIT_WINDOW_SECONDS)
        return True, capacity, refill


rate_limit = RateLimitSettings()

__all__ = ["RateLimitSettings", "rate_limit"]
