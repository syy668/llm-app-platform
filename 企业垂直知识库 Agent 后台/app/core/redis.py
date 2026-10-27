"""Redis 客户端封装。

两种后端由 `.env` 的 `FAKE_REDIS` 决定：
- `false`（默认/生产/联调）：连真实 Redis，会话可跨进程共享、重启不丢，限流生效；
- `true`（仅离线演示 / CI 无 Redis）：用 fakeredis 内存实现，进程退出即全部丢失，
  且 fakeredis 不支持 Lua EVAL —— 令牌桶限流会跳过（`rate_limit.py` 已显式放行）。

【批次3 改造】
1. `is_fake_redis()` 提升为公共函数（原先在 `core/redis.py` 与 `api/rate_limit.py`
   各写一份，两处口径容易漂移）；
2. `get_redis()` 增加后端一致性校验：运行期环境变量被改动（测试 monkeypatch、
   配置热切换）时自动重建客户端，避免"配置说 real、句柄还是 fake"导致限流静默失效；
3. 真实连接补 `socket_connect_timeout` / `socket_timeout` / `health_check_interval`：
   Redis 宕机时快速失败（2s 内）而不是把请求挂死，配合各处 fail-open/fail-fast 策略；
4. `probe()` 输出后端类型 + 连通性，供 `/health` 与启动自检使用。
"""
import logging
import os

import redis as redis_lib

from ..config import settings

logger = logging.getLogger(__name__)

# 环境变量名（集中一处，避免字符串散落）
FAKE_REDIS_ENV = "FAKE_REDIS"

# 真实连接超时（秒）：建立连接 2s、读写 5s。Redis 是内部依赖，不应无限等待。
_CONNECT_TIMEOUT = 2.0
_SOCKET_TIMEOUT = 5.0
_HEALTH_CHECK_INTERVAL = 30

_client: redis_lib.Redis | None = None
# 记录当前句柄对应的后端类型，用于运行期一致性校验
_client_backend: str | None = None


def is_fake_redis() -> bool:
    """是否启用内存版 Redis。

    每次调用实时读取环境变量（不缓存），保证测试 monkeypatch 与配置热更新即时生效。
    """
    return os.getenv(FAKE_REDIS_ENV, "false").strip().lower() in ("1", "true", "yes", "on")


def redis_backend() -> str:
    """当前生效的 Redis 后端类型：`fake` / `real`。"""
    return "fake" if is_fake_redis() else "real"


def _build_client() -> redis_lib.Redis:
    """按当前配置创建客户端（不缓存）。"""
    if is_fake_redis():
        import fakeredis

        logger.debug("Redis 后端 = fakeredis 内存实现（FAKE_REDIS=true）")
        return fakeredis.FakeRedis(decode_responses=True)
    return redis_lib.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=_CONNECT_TIMEOUT,
        socket_timeout=_SOCKET_TIMEOUT,
        health_check_interval=_HEALTH_CHECK_INTERVAL,
    )


def get_redis() -> redis_lib.Redis:
    """返回全局 Redis 客户端（懒加载，decode_responses 直接返回字符串）。

    若运行期 FAKE_REDIS 被改动，旧句柄会被丢弃并重建——否则会出现
    "环境变量已是 real，实际还在用 fakeredis" 的静默不一致（限流会因此失效）。
    """
    global _client, _client_backend
    expected = redis_backend()
    if _client is not None and _client_backend != expected:
        logger.warning("Redis 后端变更为 %s，重建客户端连接", expected)
        try:
            _client.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响重建
            pass
        _client = None
    if _client is None:
        _client = _build_client()
        _client_backend = expected
    return _client


def reset_client() -> None:
    """丢弃缓存的客户端（测试 / 配置热切换后强制重连用）。"""
    global _client, _client_backend
    _client = None
    _client_backend = None


def ping_redis() -> bool:
    """探测 Redis 是否可用，异常时返回 False 而不是抛出。"""
    try:
        return bool(get_redis().ping())
    except Exception:
        return False


def probe() -> dict:
    """健康探测：返回后端类型与连通性，供 /health 与启动自检使用。"""
    backend = redis_backend()
    if backend == "fake":
        # fakeredis 恒可用，但连通性不代表真实的跨进程共享能力，需由调用方区分展示
        return {"backend": "fake", "connected": True}
    return {"backend": "real", "connected": ping_redis()}
