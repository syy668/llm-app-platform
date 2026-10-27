"""W2 专项配置：LLM 降级链、Agent 预算、MCP 工具约束、压缩阈值。

设计说明（为什么单独一个文件）：
- W1 的 ``app/core/config.py`` 冻结不动（硬性约束），W2 新增的运行期开关全部收口在本文件；
- 与 W1 的 Settings 读取同一份 ``.env``（同名前缀字段保持兼容），互不覆盖；
- 所有魔数（超时 / 白名单 / 预算 / 阈值）均来自此处，满足 PRD NFR-M3「零硬编码魔数」。
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


class W2Settings(BaseSettings):
    """W2 全量可调参数。环境变量名即字段名（大写），读取 ``.env``。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ---------------- LLM：主模型 DeepSeek-V3 + 降级硅基流动 Qwen2.5-72B ----------------
    # 无 API Key 时置 true，可直接跑通全链路（返回确定性 Mock 文本，便于本地自测与 CI）
    LLM_MOCK: bool = True
    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "deepseek-chat"
    LLM_TEMPERATURE: float = Field(default=0.2, ge=0.0, le=2.0)  # PRD 8.2：Planner 固定 0.2
    # 任务 9：单次生成上限收紧到 2048（防输出爆炸/长文本失控），实际值可在 .env 覆盖
    LLM_MAX_TOKENS: int = 2048
    LLM_TIMEOUT_SECONDS: int = 60
    # 任务 9：LLM 调用外层 asyncio.wait_for 硬性总超时（防 API 挂起卡死节点），
    # 必须小于 httpx 的 LLM_TIMEOUT_SECONDS 才有意义
    LLM_CALL_TOTAL_TIMEOUT_SECONDS: int = 30
    # 降级链（PRD 4.2「LLM 输出不合规 → 切换备用模型重试 1 次」）
    LLM_FALLBACK_BASE_URL: str = "https://api.siliconflow.cn/v1"
    LLM_FALLBACK_API_KEY: str = ""
    LLM_FALLBACK_MODEL: str = "Qwen/Qwen2.5-72B-Instruct"
    LLM_FALLBACK_ENABLED: bool = True

    # 结构化输出修复重试（FR-002 验收 2：最多 2 次）
    STRUCTURED_REPAIR_MAX_RETRY: int = 2

    # v2.1 上下文超载硬熔断：发给 LLM 的 messages 总字符数上限（约 12000 字符 ≈ 6K Token），
    # 超限由 structured.truncate_context 保留 System 后从最早的历史消息开始丢弃
    CONTEXT_MAX_CHARS: int = 12000

    # 任务 9：单任务 Token 消耗硬熔断（防重试风暴烧穿日配额）。
    # Redis 键 task:token_usage:{task_id} 累计，每次 LLM 返回 usage 后 INCRBY，
    # 下一次请求前预检达到上限即抛 TOKEN_BUDGET_EXCEEDED 强制中断执行图
    MAX_TOKENS_PER_TASK: int = 50000
    TASK_TOKEN_USAGE_TTL_SECONDS: int = 86400  # 24h 后自动过期

    # ---------------- 节点 Token 预算（PRD 9.1，32K 窗口） ----------------
    TOKEN_BUDGET_PLANNER: int = 6000
    TOKEN_BUDGET_EXECUTOR: int = 8000
    TOKEN_BUDGET_REVIEWER: int = 4000
    TOKEN_BUDGET_SUMMARIZER: int = 5000
    # 压缩触发：单节点预估占用 > 预算 × 该比例（PRD 9.2）
    COMPRESS_TRIGGER_RATIO: float = Field(default=0.8, gt=0.0, le=1.0)
    # messages 轮数超限也触发（PRD 9.2）
    COMPRESS_MESSAGE_ROUNDS_THRESHOLD: int = 12
    COMPRESS_KEEP_RECENT_ROUNDS: int = 3
    COMPRESS_MAX_LAYERS: int = 2
    COMPRESS_ENABLED: bool = True

    # ---------------- Agent 行为上限 ----------------
    PLAN_MIN_SUBTASKS: int = 3  # FR-002：首次规划 3~15
    PLAN_MAX_SUBTASKS: int = 15
    REPLAN_MAX_COUNT: int = 2  # FR-003
    TOOL_LOOP_LIMIT: int = 8  # FR-004 验收 3
    # 重试退避基准（秒）；测试置 0 可关闭等待
    TOOL_RETRY_BACKOFF_ENABLED: bool = True
    # 单次 MCP 调用总超时（覆盖 server 内部超时）
    MCP_CALL_TIMEOUT_SECONDS: int = 30
    # 加固任务 2：Executor 外层强制硬超时（不管传输层配多少，调用最多等这么久）
    # 任务 16：per-tool 超时预算——废除 executor 一刀切硬超时。
    # 未命中工具映射时用 DEFAULT_TOOL_TIMEOUT_SECONDS 兜底；
    # KB 检索链路（登录 + embedding + 混合检索）实测可达 8~12s，5s 一刀切必然全灭。
    DEFAULT_TOOL_TIMEOUT_SECONDS: int = 5
    # 加固任务 2：工具熔断器——连续失败 N 次 → 冷却 M 秒（Redis 计数）
    TOOL_CIRCUIT_FAILURE_THRESHOLD: int = 5
    TOOL_CIRCUIT_COOLDOWN_SECONDS: int = 60
    # 任务 11 工具治理：版本路由表（工具名 -> 目标版本，如 {"kb-search": "v2"}）。
    # 空 = 全部走当前版本（零行为变化）；切换后由 ToolVersionRouter 改写工具名与 payload，
    # 目标版本工具未部署时自动回退旧版（兼容层韧性）。
    TOOL_VERSION_ROUTES: dict[str, str] = Field(default_factory=dict)
    # 任务 11 工具治理：幂等结果缓存 TTL（秒）。key = task_id + tool + args hash，
    # 只缓存成功结果——超时重试期间同一副作用调用（发通知/扣库存）只会真实执行一次。
    TOOL_IDEMPOTENCY_TTL_SECONDS: int = 600

    # ---------------- sql-query MCP Server ----------------
    SQL_ALLOWED_TABLES: str = "orders,traffic_records,ad_campaigns,skus,categories"
    SQL_MAX_ROWS: int = 1000
    SQL_DEFAULT_LIMIT: int = 1000
    SQL_EXEC_TIMEOUT_SECONDS: int = 10
    SQL_MAX_SCAN_ROWS: int = 1_000_000
    # 只读账号（PRD FR-005 安全约束 ④）：仅 SELECT on mall
    SQL_RO_HOST: str = "127.0.0.1"
    SQL_RO_PORT: int = 3306
    SQL_RO_USER: str = "mall_ro"
    SQL_RO_PASSWORD: str = "mall_ro_pwd"
    SQL_RO_DB: str = "mall"

    # ---------------- chart-render MCP Server ----------------
    CHART_MAX_ROWS: int = 500
    CHART_RENDER_TIMEOUT_SECONDS: int = 15
    CHART_USE_PROCESS_POOL: bool = True
    CHART_POOL_SIZE: int = 2
    CHART_OUTPUT_DIR: str = "storage/charts"
    CHART_STATIC_URL_PREFIX: str = "/static/charts"

    # ---------------- kb-search MCP Server ----------------
    KB_MAX_QUERY_LEN: int = 200
    KB_DEFAULT_TOP_K: int = 3
    KB_MAX_TOP_K: int = 5
    KB_TIMEOUT_SECONDS: int = 20
    KB_RETRY_TIMES: int = 2
    KB_MAX_CALLS_PER_TASK: int = 10  # FR-007 频率限制
    KB_TENANT: str = "agentflow"

    # ---------------- notify MCP Server（恒 Mock，不出网） ----------------
    NOTIFY_TIMEOUT_SECONDS: int = 5
    NOTIFY_MAX_RECEIVERS: int = 10
    NOTIFY_MAX_CONTENT_LEN: int = 2000
    NOTIFY_DRY_RUN: bool = True  # 恒 true：绝不真正出网

    # ---------------- Reviewer ----------------
    REVIEW_PASS_THRESHOLD: float = 3.5  # FR-013
    REVIEW_SKIP_AFTER_FAILURES: int = 2  # 连续 2 次不合规 → REVIEW_SKIPPED
    REVIEW_LOCAL_WEIGHT: float = 0.6
    #: 拒绝线：总分低于该值判 REJECT（硬性不可交付），介于拒绝线与通过线之间判 REVISE
    REVIEW_REJECT_THRESHOLD: float = 2.5
    #: 可信度下限：置信度低于该值时，即使总分达标也降级为 REVISE（防止"分高但没证据"）
    REVIEW_CONFIDENCE_FLOOR: float = 0.55
    #: v1.1：REJECT 后是否插入 review_gate 人工批注闸门（true=挂起等批注；
    #: false=退回"直接 replan"的旧行为，便于一键回退）
    REVIEW_HUMAN_INTERVENTION_ENABLED: bool = True

    # ---------------- 通用 ----------------
    STORAGE_DIR: str = "storage"
    API_SYNC_TIMEOUT_SECONDS: int = 300  # PRD 10.3 项目二回调 sync 模式
    # checkpointer 后端：auto=优先 RedisSaver，不可用降级内存；redis=强制；memory=纯内存（测试）
    CHECKPOINTER_BACKEND: Literal["auto", "redis", "memory"] = "auto"

    @field_validator("SQL_ALLOWED_TABLES")
    @classmethod
    def _normalize_tables(cls, v: str) -> str:
        return ",".join(item.strip().lower() for item in v.split(",") if item.strip())

    # ---------------- 派生 ----------------
    @property
    def sql_allowed_tables(self) -> set[str]:
        return {item for item in self.SQL_ALLOWED_TABLES.split(",") if item}

    @property
    def project_root(self) -> Path:
        return _project_root()

    @property
    def chart_output_path(self) -> Path:
        path = Path(self.CHART_OUTPUT_DIR)
        return path if path.is_absolute() else _project_root() / path

    @property
    def token_budgets(self) -> dict[str, int]:
        """PRD 9.1 预算表：节点 -> 预算 Token。"""
        return {
            "planner": self.TOKEN_BUDGET_PLANNER,
            "executor": self.TOKEN_BUDGET_EXECUTOR,
            "reviewer": self.TOKEN_BUDGET_REVIEWER,
            "summarizer": self.TOKEN_BUDGET_SUMMARIZER,
            "replan": self.TOKEN_BUDGET_PLANNER,  # 复用规划预算
            "notifier": self.TOKEN_BUDGET_EXECUTOR,
        }

    def token_budget(self, node: str) -> int:
        return self.token_budgets.get(node, self.TOKEN_BUDGET_EXECUTOR)

    def mcp_server_command(self, server: str) -> list[str]:
        """stdio 启动命令：当前解释器 + 模块方式拉起，保证 venv 内依赖可见。"""
        return [sys.executable, "-m", f"app.tools.mcp_servers.{server.replace('-', '_')}"]


@lru_cache(maxsize=1)
def get_w2_settings() -> W2Settings:
    return W2Settings()


w2 = get_w2_settings()


__all__ = ["W2Settings", "get_w2_settings", "w2"]
