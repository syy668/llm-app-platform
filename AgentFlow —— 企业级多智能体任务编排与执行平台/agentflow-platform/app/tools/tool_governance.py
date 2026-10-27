"""工具治理中间件（任务 11）：版本路由适配器 + 幂等结果缓存。

两件事都在 ``MCPClientPool.call`` 这个唯一咽喉处生效，**不改任何工具实现与 MCP 协议层**：

1. **版本路由**（``ToolVersionRouter``）：底层接口从 v1 升级到 v2 时，运维只需在
   ``w2.TOOL_VERSION_ROUTES`` 里配 ``{"kb-search": "v2"}``——适配器把 Agent 发出的
   旧契约参数（``query``）映射成新契约（``search_query``）并改写实际下发的工具名
   （``kb-search-v2``），上层 Planner/Executor **完全无感知**。目标版本工具尚未部署时
   自动回退旧版（兼容层韧性：配置先行、服务渐进上线）。payload 适配器按
   ``(tool, version)`` 注册，新工具升级时在这里加一个映射函数即可。

2. **幂等保护**（``IdempotencyStore``）：Executor 的重试矩阵（3 次工具重试）遇到
   "推送群消息 / 扣减库存"这类副作用工具时，超时重试会造成重复副作用。每次调用前用
   ``task_id + tool + 参数 hash`` 生成幂等键查 Redis——命中说明是重试，直接返回上次
   的成功结果，**禁止真实调用**；未命中则真实调用，成功后写缓存（TTL 600s）。
   失败/超时不落缓存（重试仍会真实发起）；task_id 未绑定（离线/评测路径）与
   Redis 故障一律 fail-open，绝不阻断工具调用。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

from app.core.config import settings
from app.core.logging import get_logger
from app.core.w2_config import w2

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 1. 版本路由适配器
# ---------------------------------------------------------------------------
PayloadAdapter = Callable[[dict[str, Any]], dict[str, Any]]


def _map_kb_search_v2(args: dict[str, Any]) -> dict[str, Any]:
    """kb-search v1 -> v2 契约映射：``query`` 改名 ``search_query``，其余透传。"""
    return {("search_query" if key == "query" else key): value for key, value in args.items()}


#: (tool, target_version) -> payload 适配器。新版本参数结构变化时在这里注册映射函数。
PAYLOAD_ADAPTERS: dict[tuple[str, str], PayloadAdapter] = {
    ("kb-search", "v2"): _map_kb_search_v2,
}


@dataclass(frozen=True, slots=True)
class ToolResolution:
    """一次工具调用的版本解析结果。"""

    tool: str                 # Agent 请求的工具名（幂等键用它，版本切换不产生双份缓存）
    wire_tool: str            # 实际下发的工具名
    arguments: dict[str, Any]       # Agent 请求的原始参数
    wire_arguments: dict[str, Any]  # 实际下发的参数（映射后）
    version: str              # 生效版本（"v1" = 直通）
    changed: bool             # 是否发生了路由改写


class ToolVersionRouter:
    """轻量版本路由：routes 为空时零开销直通（与升级前行为逐字节一致）。"""

    def __init__(self, routes: dict[str, str] | None = None) -> None:
        self.routes: dict[str, str] = dict(routes) if routes is not None else dict(w2.TOOL_VERSION_ROUTES)
        for tool, target in self.routes.items():
            if (tool, target) not in PAYLOAD_ADAPTERS:
                logger.warning(
                    "tool_version_adapter_missing",
                    extra={"tool": tool, "version": target, "hint": "参数将原样透传"},
                )

    def resolve(self, tool: str, arguments: dict[str, Any]) -> ToolResolution:
        target = self.routes.get(tool)
        if not target or target == "v1":
            return ToolResolution(tool, tool, arguments, arguments, "v1", False)
        adapter = PAYLOAD_ADAPTERS.get((tool, target))
        wire_args = adapter(arguments) if adapter is not None else dict(arguments)
        return ToolResolution(
            tool=tool,
            wire_tool=f"{tool}-{target}",
            arguments=arguments,
            wire_arguments=wire_args,
            version=target,
            changed=True,
        )

    def fallback_v1(self, tool: str, arguments: dict[str, Any]) -> ToolResolution:
        """目标版本未部署时的兼容回退：按 v1 原契约直发。"""
        return ToolResolution(tool, tool, arguments, arguments, "v1", False)


# ---------------------------------------------------------------------------
# 2. 幂等结果缓存
# ---------------------------------------------------------------------------
class IdempotencyStore:
    """工具调用幂等结果缓存（Redis，fail-open）。

    key = ``{prefix}:{task_id}:{tool}:{sha256(canonical_args)[:32]}``，TTL 默认 600s。
    只缓存**成功**结果——调用方（MCPClientPool）仅在 ``call_tool`` 正常返回后 save，
    失败/超时不写缓存，后续重试仍会真实发起。
    """

    def __init__(self, redis: Any, *, ttl_seconds: int | None = None, prefix: str = "tool:idem") -> None:
        self.redis = redis
        self.ttl = int(ttl_seconds or w2.TOOL_IDEMPOTENCY_TTL_SECONDS)
        self.prefix = prefix

    def build_key(self, task_id: int | str, tool: str, arguments: dict[str, Any]) -> str:
        canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
        return settings.key(self.prefix, str(task_id), tool, digest)

    async def load(self, key: str) -> dict[str, Any] | None:
        if self.redis is None:
            return None
        try:
            raw = await self.redis.get(key)
        except Exception as exc:  # noqa: BLE001 - 存储故障 fail-open
            logger.warning("idempotency_read_failed", extra={"error": str(exc)[:200]})
            return None
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else {"result": payload}

    async def save(self, key: str, payload: dict[str, Any]) -> None:
        if self.redis is None:
            return
        try:
            raw = json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return
        try:
            await self.redis.set(key, raw, ex=self.ttl)
        except Exception as exc:  # noqa: BLE001
            logger.warning("idempotency_write_failed", extra={"error": str(exc)[:200]})


def default_idempotency_store() -> IdempotencyStore:
    """生产路径默认实例：共享 Redis 单例 + w2 TTL。Redis 不可用时实例仍可用（fail-open）。"""
    from app.core.redis_client import get_shared_redis

    return IdempotencyStore(get_shared_redis(), ttl_seconds=w2.TOOL_IDEMPOTENCY_TTL_SECONDS)


__all__ = [
    "PAYLOAD_ADAPTERS",
    "IdempotencyStore",
    "ToolResolution",
    "ToolVersionRouter",
    "default_idempotency_store",
]
