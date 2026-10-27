"""W5-Part1 专项配置：MCP HTTP 传输 + FR-014 分层记忆。

为什么单独一个文件（与 W2 的 ``w2_config.py`` 完全同构）：
- W1 的 ``app/core/config.py`` 属基线冻结文件，不修改；
- W5 新增的运行期开关（MCP HTTP、分层记忆）全部收口在本文件；
- 读取同一份 ``.env``（``extra="ignore"``，与 W1/W2 字段互不干扰）。

**默认值刻意选择「全部关闭」**：
- ``MCP_HTTP_ENABLED=false``：stdio 仍是唯一默认传输，HTTP 只作为第二套并存能力；
- ``ENABLE_LAYERED_MEMORY=false``：分层记忆是外层增强，关闭时对 W1~W4 行为**零影响**。
这样 W1~W4 的 150 条基线用例在未显式开启时跑出来的结果与之前逐字节一致。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class W5Settings(BaseSettings):
    """W5 全量可调参数。环境变量名即字段名（大写），读取 ``.env``。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ===================== MCP HTTP 传输（任务 1） =====================
    # 双传输并存开关：false 时进程内只走 stdio，HTTP 端点不挂载
    MCP_HTTP_ENABLED: bool = False
    # 服务端监听（独立进程 / 容器内）
    MCP_HTTP_LISTEN_HOST: str = "127.0.0.1"
    MCP_HTTP_LISTEN_PORT: int = Field(default=9100, ge=1, le=65535)
    # 空字符串 = 不校验（仅本地调试）；生产必须配置（启动日志会告警）
    MCP_HTTP_API_KEY: str = ""
    # 客户端连接的目标地址（可为远端服务）
    MCP_HTTP_BASE_URL: str = "http://127.0.0.1:9100"
    MCP_HTTP_PATH: str = "/mcp/v1"

    # ---------------- 连接池 / 超时 ----------------
    MCP_HTTP_TIMEOUT_SECONDS: float = Field(default=15.0, gt=0.0, le=120.0)
    MCP_HTTP_CONNECT_TIMEOUT_SECONDS: float = Field(default=3.0, gt=0.0, le=60.0)
    MCP_HTTP_MAX_CONNECTIONS: int = Field(default=20, ge=1, le=200)
    MCP_HTTP_MAX_KEEPALIVE: int = Field(default=10, ge=1, le=200)
    MCP_HTTP_KEEPALIVE_EXPIRY_SECONDS: float = Field(default=30.0, gt=0.0)
    # 仅对传输层故障重试（业务错误码绝不重试，避免重复副作用）
    MCP_HTTP_RETRY_TIMES: int = Field(default=1, ge=0, le=3)
    MCP_HTTP_RETRY_BACKOFF_SECONDS: float = Field(default=0.05, ge=0.0, le=5.0)

    # ---------------- 熔断 ----------------
    MCP_HTTP_BREAKER_THRESHOLD: int = Field(default=3, ge=1, le=20)
    MCP_HTTP_BREAKER_RECOVERY_SECONDS: float = Field(default=30.0, ge=1.0, le=600.0)

    # ---------------- SSE 长会话 ----------------
    MCP_HTTP_SSE_MAX_SECONDS: int = Field(default=300, ge=1, le=3600)
    MCP_HTTP_SESSION_TTL_SECONDS: int = Field(default=600, ge=10, le=86400)
    MCP_HTTP_MAX_SESSIONS: int = Field(default=128, ge=1, le=4096)

    # ===================== FR-014 分层记忆（任务 2） =====================
    # 全局总开关：false 时**完全不检索、不注入、不落库**（连 Redis 都不碰）
    ENABLE_LAYERED_MEMORY: bool = False
    # 短期：任务会话消息，Redis，TTL 24h，任务内完整可用、不摘要
    MEMORY_SHORT_TTL_SECONDS: int = Field(default=24 * 3600, ge=60)
    MEMORY_SHORT_MAX_MESSAGES: int = Field(default=400, ge=10, le=5000)
    # 中期：任务结束后压缩摘要落 memories(scope='mid')，TTL 30 天
    MEMORY_MID_TTL_DAYS: int = Field(default=30, ge=1, le=365)
    MEMORY_MID_SUMMARY_BUDGET: int = Field(default=800, ge=100, le=4000)
    # 长期：Reviewer >= 3 分才归档，importance 取整后落 3~5
    MEMORY_LONG_MIN_REVIEW_SCORE: float = Field(default=3.0, ge=0.0, le=5.0)
    MEMORY_LONG_TOP_K: int = Field(default=3, ge=1, le=3)  # PlannerInput.memory_hits 上限 3
    MEMORY_RECALL_MIN_SCORE: float = Field(default=0.15, ge=0.0, le=1.0)
    MEMORY_RECALL_CANDIDATES: int = Field(default=200, ge=10, le=2000)
    # 注入片段单条字符上限（对应 PlannerInput「各 ≤200 Token」≈ 400 字符）
    MEMORY_INJECT_MAX_CHARS: int = Field(default=400, ge=50, le=2000)
    # 本地关键词兜底（MOCK_EXTERNAL_RAG=true 时生效；也可在非 mock 下作为通道故障兜底）
    MEMORY_LOCAL_RECALL_ENABLED: bool = True
    # 项目一 RAG 向量化的接口路径
    MEMORY_RAG_UPSERT_PATH: str = "/api/v1/kb/upsert"
    MEMORY_RAG_SEARCH_PATH: str = "/api/v1/kb/search"
    MEMORY_RAG_TENANT: str = "agentflow-memory"
    # 记忆向量后端（非 mock 模式生效）：local-embedding=本地 embedding+余弦（默认，真实语义）；
    # http-rag=项目一 /kb/upsert+/kb/search（当前项目一未提供，404 必降级）；keyword=本地关键词
    MEMORY_VECTOR_BACKEND: str = "local-embedding"
    # 本地 embedding（OpenAI 兼容 /embeddings；默认百炼 text-embedding-v3，1024 维）
    MEMORY_EMBEDDING_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    MEMORY_EMBEDDING_API_KEY: str = ""  # 留空则回退 http-rag 路径（保持旧行为）
    MEMORY_EMBEDDING_MODEL: str = "text-embedding-v3"
    MEMORY_EMBEDDING_DIMS: int = Field(default=1024, ge=64, le=4096)
    MEMORY_EMBEDDING_BATCH_SIZE: int = Field(default=6, ge=1, le=25)  # text-embedding-v3 单批上限 6
    MEMORY_EMBEDDING_TIMEOUT_SECONDS: float = Field(default=15.0, gt=0, le=60)
    # 向量分数阈值：text-embedding-v3 无关文本余弦基线 ≈0.3~0.4，不能用关键词的 0.15；
    # 0.55 为实测校准值：相关/同义改写档 0.61+，噪声档 0.45~0.53，卡在缝上
    MEMORY_RECALL_MIN_SCORE_VECTOR: float = Field(default=0.55, ge=0.0, le=1.0)

    # ===================== 校验 / 派生 =====================
    @field_validator("MCP_HTTP_PATH")
    @classmethod
    def _normalize_path(cls, v: str) -> str:
        path = "/" + v.strip().strip("/")
        return path or "/mcp/v1"

    @field_validator("MCP_HTTP_BASE_URL")
    @classmethod
    def _normalize_base(cls, v: str) -> str:
        return v.strip().rstrip("/")

    @field_validator("MEMORY_VECTOR_BACKEND")
    @classmethod
    def _check_vector_backend(cls, v: str) -> str:
        allowed = {"local-embedding", "http-rag", "keyword"}
        if v not in allowed:
            raise ValueError(f"MEMORY_VECTOR_BACKEND 必须是 {sorted(allowed)} 之一")
        return v

    @property
    def mcp_http_url(self) -> str:
        return f"{self.MCP_HTTP_BASE_URL}{self.MCP_HTTP_PATH}"

    @property
    def mcp_http_auth_enabled(self) -> bool:
        return bool(self.MCP_HTTP_API_KEY.strip())

    @property
    def memory_mid_ttl_seconds(self) -> int:
        return self.MEMORY_MID_TTL_DAYS * 24 * 3600

    def listen_url(self, *, host: str | None = None) -> str:
        h = host or self.MCP_HTTP_LISTEN_HOST
        shown = "0.0.0.0" if h in {"0.0.0.0", "::"} else h
        return f"http://{shown}:{self.MCP_HTTP_LISTEN_PORT}{self.MCP_HTTP_PATH}"

    def describe(self) -> dict[str, object]:
        """自检 / 健康检查用（不含密钥明文）。"""
        return {
            "mcp_http_enabled": self.MCP_HTTP_ENABLED,
            "mcp_http_path": self.MCP_HTTP_PATH,
            "mcp_http_auth": self.mcp_http_auth_enabled,
            "mcp_http_timeout": self.MCP_HTTP_TIMEOUT_SECONDS,
            "mcp_http_breaker": {
                "threshold": self.MCP_HTTP_BREAKER_THRESHOLD,
                "recovery_seconds": self.MCP_HTTP_BREAKER_RECOVERY_SECONDS,
            },
            "enable_layered_memory": self.ENABLE_LAYERED_MEMORY,
            "memory_short_ttl": self.MEMORY_SHORT_TTL_SECONDS,
            "memory_mid_ttl_days": self.MEMORY_MID_TTL_DAYS,
            "memory_long_min_score": self.MEMORY_LONG_MIN_REVIEW_SCORE,
        }


@lru_cache(maxsize=1)
def get_w5_settings() -> W5Settings:
    return W5Settings()


w5 = get_w5_settings()


def layered_memory_enabled() -> bool:
    """运行期读取开关（允许测试用 monkeypatch 直接改 ``w5.ENABLE_LAYERED_MEMORY``）。"""
    return bool(w5.ENABLE_LAYERED_MEMORY)


__all__ = ["W5Settings", "get_w5_settings", "w5", "layered_memory_enabled"]
