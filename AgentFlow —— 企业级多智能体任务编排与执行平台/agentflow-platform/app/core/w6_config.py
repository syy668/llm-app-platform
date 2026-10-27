"""W5-Part2 专项配置：SSE 分布式会话 + OpenTelemetry 全链路埋点 + 边界加固。

为什么再开一个配置文件（与 W2 ``w2_config.py`` / W5 ``w5_config.py`` 完全同构）：
- W1 ``app/core/config.py``、W5 ``app/core/w5_config.py`` 均属冻结基线，**不修改**；
- Part2 新增的运行期开关（会话后端、OTel、SSE 守护）全部收口在本文件；
- 读取同一份 ``.env``（``extra="ignore"``），与 W1/W2/W5 字段互不干扰。

**默认值仍然刻意选择「零影响」**：
- ``SESSION_REGISTRY_BACKEND=memory``：单进程内存会话表，本地/CI 完全不需要 Redis；
- ``ENABLE_OTEL=false``：埋点代码在关闭时**连 patch 都不打**，性能开销为 0；
- ``SSE_GUARD_ENABLED=false``：SSE 守护（心跳 / 超时清理 / 断线重连）默认不接管事件流，
  保证 W3 既有 SSE 行为逐字节不变。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class W6Settings(BaseSettings):
    """W5-Part2 全量可调参数。环境变量名即字段名（大写），读取 ``.env``。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ============ 任务 1：SSE 会话注册表（memory / redis） ============
    # memory = 单进程（默认，本地测试零依赖）；redis = 跨副本共享，多副本部署必开
    SESSION_REGISTRY_BACKEND: Literal["memory", "redis"] = "memory"
    # key 前缀：<prefix>:<session_id> 存会话元数据，<prefix>:<session_id>:q 存待投递消息
    SESSION_REGISTRY_KEY_PREFIX: str = "mcp:sess"
    # 会话 index（zset，score=创建时间戳）用于过期回收
    SESSION_REGISTRY_INDEX_KEY: str = "mcp:sess:index"
    # 单个会话待投递队列上限，超出即丢最老消息（防止消费者永不上线导致内存/Redis 膨胀）
    SESSION_REGISTRY_QUEUE_MAX: int = Field(default=256, ge=8, le=10000)
    # 队列轮询间隔：越小延迟越低、Redis QPS 越高（默认 50ms 足够 SSE 场景）
    SESSION_REGISTRY_POLL_INTERVAL_SECONDS: float = Field(default=0.05, ge=0.005, le=1.0)
    # 同步客户端超时（元数据读写走同步客户端，见 session_backend 的设计说明）
    SESSION_REGISTRY_SYNC_TIMEOUT_SECONDS: float = Field(default=0.05, ge=0.01, le=2.0)
    # Redis 不可用时是否降级为进程内会话表（true=不阻断服务，false=直接报错）
    SESSION_REGISTRY_DEGRADE_TO_MEMORY: bool = True
    # 启动时是否强制探活（false 则首次使用时才连）
    SESSION_REGISTRY_PING_ON_START: bool = True

    # ============ 任务 2：OpenTelemetry ============
    # 总开关：false 时不初始化 provider、不打任何 patch、不产生任何 span
    ENABLE_OTEL: bool = False
    OTEL_SERVICE_NAME: str = "agentflow-platform"
    OTEL_SERVICE_VERSION: str = "0.5.2-w5p2"
    OTEL_DEPLOYMENT_ENV: str = "local"
    # 导出方式：otlp = OTLP/HTTP；console = 控制台（无采集器时自动降级到此）
    OTEL_EXPORTER: Literal["otlp", "console", "none"] = "otlp"
    # 采集器不可达时是否自动降级为控制台导出（任务书要求「无采集器时自动降级」）
    OTEL_AUTO_FALLBACK_CONSOLE: bool = True
    OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://127.0.0.1:4318"
    OTEL_EXPORTER_OTLP_TRACES_PATH: str = "/v1/traces"
    OTEL_EXPORTER_OTLP_HEADERS: str = ""  # "k1=v1,k2=v2"
    OTEL_EXPORTER_TIMEOUT_SECONDS: float = Field(default=3.0, gt=0.0, le=30.0)
    # 采样比：1.0 = 全采；生产可降到 0.1
    OTEL_SAMPLE_RATIO: float = Field(default=1.0, ge=0.0, le=1.0)
    # 批量导出参数
    OTEL_BATCH_MAX_QUEUE: int = Field(default=2048, ge=64, le=65536)
    OTEL_BATCH_MAX_EXPORT: int = Field(default=512, ge=32, le=8192)
    OTEL_BATCH_SCHEDULE_DELAY_MS: int = Field(default=2000, ge=100, le=30000)
    # 不埋点的路径前缀（健康检查 / 指标 / 文档）
    OTEL_EXCLUDE_PATHS: str = "/health,/ready,/healthz,/readyz,/metrics,/docs,/redoc,/openapi.json,/favicon.ico"
    # SQL 语句是否记录参数（默认只记占位化语句，避免 PII 落 span）
    OTEL_CAPTURE_SQL_PARAMS: bool = False
    # 单个属性值最大长度（防止大 payload 撑爆 span）
    OTEL_MAX_ATTR_LENGTH: int = Field(default=512, ge=32, le=8192)
    # 子开关（可在总开关打开的前提下单独关掉某类埋点）
    OTEL_TRACE_HTTP: bool = True
    OTEL_TRACE_GRAPH: bool = True
    OTEL_TRACE_MCP: bool = True
    OTEL_TRACE_LLM: bool = True
    OTEL_TRACE_SQL: bool = True
    # 指标前缀（Prometheus 文本暴露时使用）
    OTEL_METRICS_PREFIX: str = "agentflow"

    # ============ 任务 4：SSE 边界加固 ============
    # 是否用守护包装层接管 SSE 事件流（false 时完全走 W3 原逻辑）
    SSE_GUARD_ENABLED: bool = False
    SSE_HEARTBEAT_SECONDS: float = Field(default=15.0, ge=0.5, le=120.0)
    # 单条事件最长等待：超过即补心跳帧，不判定断线
    SSE_IDLE_TIMEOUT_SECONDS: float = Field(default=20.0, ge=1.0, le=600.0)
    # 连接总时长上限：到点主动关闭，提示客户端带 Last-Event-ID 重连
    SSE_CONNECTION_MAX_SECONDS: float = Field(default=900.0, ge=10.0, le=86400.0)
    # 断线重连提示帧的重试间隔（毫秒），写入 SSE ``retry:`` 字段
    SSE_RETRY_INTERVAL_MS: int = Field(default=3000, ge=200, le=60000)
    # 任务取消后是否清理任务级 Redis 临时键（取消信号 / 流游标 / 快照缓存）
    TASK_CANCEL_CLEANUP_ENABLED: bool = True
    # 一致性校验容忍的 checkpoint 落后步数（多副本读快照时的正常抖动）
    TASK_CONSISTENCY_MAX_STEP_LAG: int = Field(default=1, ge=0, le=10)

    # ============ 校验 / 派生 ============
    @field_validator("OTEL_EXPORTER_OTLP_ENDPOINT")
    @classmethod
    def _normalize_endpoint(cls, v: str) -> str:
        return v.strip().rstrip("/")

    @field_validator("SESSION_REGISTRY_KEY_PREFIX", "SESSION_REGISTRY_INDEX_KEY")
    @classmethod
    def _normalize_key(cls, v: str) -> str:
        return v.strip().strip(":")

    @property
    def otel_exclude_prefixes(self) -> tuple[str, ...]:
        return tuple(p.strip() for p in self.OTEL_EXCLUDE_PATHS.split(",") if p.strip())

    @property
    def otel_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        for pair in self.OTEL_EXPORTER_OTLP_HEADERS.split(","):
            if "=" in pair:
                key, _, value = pair.partition("=")
                headers[key.strip()] = value.strip()
        return headers

    @property
    def otel_traces_url(self) -> str:
        return f"{self.OTEL_EXPORTER_OTLP_ENDPOINT}{self.OTEL_EXPORTER_OTLP_TRACES_PATH}"

    @property
    def session_backend_is_redis(self) -> bool:
        return self.SESSION_REGISTRY_BACKEND == "redis"

    def session_meta_key(self, session_id: str) -> str:
        return f"{self.SESSION_REGISTRY_KEY_PREFIX}:{session_id}"

    def session_queue_key(self, session_id: str) -> str:
        return f"{self.SESSION_REGISTRY_KEY_PREFIX}:{session_id}:q"

    def describe(self) -> dict[str, object]:
        """自检 / 健康检查用（不含密钥明文）。"""
        return {
            "session_registry": {
                "backend": self.SESSION_REGISTRY_BACKEND,
                "key_prefix": self.SESSION_REGISTRY_KEY_PREFIX,
                "poll_interval": self.SESSION_REGISTRY_POLL_INTERVAL_SECONDS,
                "degrade_to_memory": self.SESSION_REGISTRY_DEGRADE_TO_MEMORY,
            },
            "otel": {
                "enabled": self.ENABLE_OTEL,
                "exporter": self.OTEL_EXPORTER,
                "endpoint": self.OTEL_EXPORTER_OTLP_ENDPOINT,
                "sample_ratio": self.OTEL_SAMPLE_RATIO,
                "trace": {
                    "http": self.OTEL_TRACE_HTTP,
                    "graph": self.OTEL_TRACE_GRAPH,
                    "mcp": self.OTEL_TRACE_MCP,
                    "llm": self.OTEL_TRACE_LLM,
                    "sql": self.OTEL_TRACE_SQL,
                },
            },
            "sse_guard": {
                "enabled": self.SSE_GUARD_ENABLED,
                "heartbeat": self.SSE_HEARTBEAT_SECONDS,
                "idle_timeout": self.SSE_IDLE_TIMEOUT_SECONDS,
                "connection_max": self.SSE_CONNECTION_MAX_SECONDS,
            },
        }


@lru_cache(maxsize=1)
def get_w6_settings() -> W6Settings:
    return W6Settings()


w6 = get_w6_settings()


def otel_enabled() -> bool:
    """运行期读取开关（允许测试 monkeypatch ``w6.ENABLE_OTEL``）。"""
    return bool(w6.ENABLE_OTEL)


def sse_guard_enabled() -> bool:
    return bool(w6.SSE_GUARD_ENABLED)


__all__ = ["W6Settings", "get_w6_settings", "w6", "otel_enabled", "sse_guard_enabled"]
