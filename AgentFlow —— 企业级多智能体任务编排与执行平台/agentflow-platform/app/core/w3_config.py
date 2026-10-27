"""W3 专项配置：SSE 事件流、Redis Stream 总线、审批收件箱、轨迹回放。

为什么再开一个配置文件（与 W2 的 ``w2_config.py`` 同样的理由）：
- W1 ``app/core/config.py`` 与 W2 ``w2_config.py`` 均冻结不动，W3 新增开关全部收口在本文件；
- 三者读取同一份 ``.env``，字段名互不重叠（SSE_ / STREAM_ / TRACE_ / APPROVAL_ / CANCEL_ 前缀）；
- 所有魔数（心跳间隔 / 重试毫秒 / 流长度 / 回放上限）均来自此处，满足 NFR-M3「零硬编码魔数」。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class W3Settings(BaseSettings):
    """W3 全量可调参数。环境变量名即字段名（大写），读取 ``.env``。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ---------------- SSE 进度流（FR-009） ----------------
    SSE_ENABLED: bool = True
    # 期望的事件协议版本：LangGraph 0.3.x 仅支持 v1/v2，实现层做「先试 prefer 再降级」协商
    SSE_EVENTS_VERSION: str = "v3"
    # XREAD 阻塞时长（毫秒）：0 表示非阻塞轮询（单测用）
    SSE_BLOCK_MS: int = 5000
    # 非阻塞模式下的轮询间隔
    SSE_POLL_INTERVAL_MS: int = 200
    # 单次读取条数
    SSE_READ_COUNT: int = 100
    # 客户端断线重连建议间隔（SSE retry 字段）
    SSE_RETRY_MS: int = 3000
    # 空闲多久无事件即主动收尾（秒），避免连接泄漏
    SSE_IDLE_TIMEOUT_SECONDS: int = 900
    # 单条连接最长存活（秒）
    SSE_MAX_DURATION_SECONDS: int = 3600
    # 心跳注释帧间隔（秒）——由阻塞读取的返回节奏自然形成，这里用于文档与前端提示
    SSE_HEARTBEAT_SECONDS: int = 15

    # ---------------- Redis Stream 事件总线 ----------------
    STREAM_KEY_PREFIX: str = "events"
    # 单任务 Stream 近似长度上限（XADD MAXLEN ~）
    STREAM_MAXLEN: int = 10000
    # Stream 保留时长（秒），与 PRD 7.2「事件可回放 24h」一致
    STREAM_TTL_SECONDS: int = 24 * 3600
    # auto=优先 Redis，失败降级进程内内存；redis=强制；memory=纯内存（测试）
    STREAM_BACKEND: Literal["auto", "redis", "memory"] = "auto"

    # ---------------- 轨迹回放（FR-015） ----------------
    TRACE_ENABLED: bool = True
    # 单条 trace payload 的最大字符数（超出转 preview 形态，避免 JSON 列爆掉）
    TRACE_PAYLOAD_MAX_CHARS: int = 4000
    # 回放接口单次最多返回的 span 数
    TRACE_REPLAY_MAX_STEPS: int = 500
    # 回放接口最多返回的 checkpoint 快照数
    TRACE_REPLAY_MAX_SNAPSHOTS: int = 100

    # ---------------- 轨迹异步削峰（任务 13） ----------------
    # True：record() 入 asyncio.Queue，后台守护协程攒批落库（业务侧零等待）；
    # False：逐条同步写（e2e 测试确定性用，conftest autouse 关闭）
    TRACE_ASYNC_BUFFER_ENABLED: bool = True
    # 攒够多少条触发一次批量入库
    TRACE_BUFFER_BATCH_SIZE: int = 50
    # 首条入队后最多等多久（秒）没攒满也 flush（防止低流量下数据滞留）
    TRACE_BUFFER_FLUSH_INTERVAL: float = 2.0
    # 队列上限（观测数据不能反向压垮业务，满了丢弃 + 告警）
    TRACE_BUFFER_MAXSIZE: int = 10000

    # ---------------- 审批（FR-011） ----------------
    APPROVAL_INBOX_MAX_PAGE_SIZE: int = 100
    # 是否允许提交人与审批人同一人（MVP 演示放开；生产应置 false）
    APPROVAL_ALLOW_SELF_DECIDE: bool = True
    # 取消标记的 TTL（秒）
    CANCEL_FLAG_TTL_SECONDS: int = 24 * 3600
    # 审批超时收口定时器（PRD L98/L245/L382：超 24h 未处理 → 审批 EXPIRED + 任务 FAILED）。
    # 为什么必须是定时器而不是"打开页面时惰性收口"：审批 TTL(24h) 比断点 TTL(72h) 短，
    # 中间那 48h 是唯一能"从最近 checkpoint 重发"的窗口；惰性收口一旦拖过 72h，
    # 任务就永久救不回来了（历史 task#10 即如此）。
    APPROVAL_SWEEP_ENABLED: bool = True
    APPROVAL_SWEEP_INTERVAL_SECONDS: int = 300
    # 收口时是否顺手清掉该任务的 Redis 断点键（防僵尸复活）。
    # 关掉只影响性能/排查，不影响正确性——终态任务本来就被 ensure_resumable 拦在 409。
    APPROVAL_SWEEP_PURGE_CHECKPOINT: bool = True

    # ---------------- 断点续跑 ----------------
    # resume 默认是否同步阻塞到终态/下一次挂起（演示控制台用 true 更直观）
    RESUME_SYNC_DEFAULT: bool = False
    # 快照接口返回的最近消息条数
    SNAPSHOT_RECENT_MESSAGES: int = Field(default=5, ge=0, le=50)


@lru_cache(maxsize=1)
def get_w3_settings() -> W3Settings:
    return W3Settings()


w3 = get_w3_settings()


__all__ = ["W3Settings", "get_w3_settings", "w3"]
