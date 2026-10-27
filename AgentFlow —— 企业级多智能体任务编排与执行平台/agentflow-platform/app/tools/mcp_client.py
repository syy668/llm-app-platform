"""MCP Client：JSON-RPC 2.0 over stdio 的握手 / 动态发现 / 调用封装（PRD 5.3）。

职责：
1. ``initialize``：协议版本与能力协商；
2. ``tools/list``：动态发现工具清单与 JSON Schema（Executor 据此构造 Function Calling 定义，零硬编码）；
3. ``tools/call``：调用并捕获业务错误码，记录 ``tool.call`` / ``tool.result`` 轨迹日志。

传输层抽象（便于测试）：
- ``StdioTransport``：真实拉起 4 个 MCP Server 子进程（生产路径）
- ``InMemoryTransport``：进程内直连 ``MCPServerApp``（单测路径，无子进程、无 IO 抖动）
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable

from app.core.context import get_task_id, get_trace_id
from app.core.logging import get_logger
from app.core.w2_config import w2
from app.schemas.agent_io import ToolDef
from app.schemas.mcp import MCP_PROTOCOL_VERSION, RETRYABLE_ERRORS, McpErrorCode
from app.tools.mcp_stdio import MCPServerApp
from app.tools.tool_governance import IdempotencyStore, ToolVersionRouter, default_idempotency_store
from app.utils.masking import mask_mapping, mask_text

logger = get_logger(__name__)

EventHook = Callable[[str, dict[str, Any]], Any]


class MCPTransportError(RuntimeError):
    """传输层故障（子进程崩溃 / 超时 / 协议错乱）。"""

    def __init__(self, message: str, code: str = McpErrorCode.MCP_PROTOCOL_ERROR.value) -> None:
        self.code = code
        super().__init__(message)


class ToolInvocationError(RuntimeError):
    """工具返回业务错误（携带 PRD 错误码，供 Executor 走重试/降级矩阵）。"""

    def __init__(self, code: str, message: str, data: dict[str, Any] | None = None) -> None:
        self.code = code
        self.message = message
        self.data = data or {}
        super().__init__(f"{code}: {message}")

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_ERRORS


# ---------------------------------------------------------------------------
# 传输层
# ---------------------------------------------------------------------------
class MCPTransport(ABC):
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]: ...

    @abstractmethod
    async def close(self) -> None: ...


class StdioTransport(MCPTransport):
    """子进程 + 行分隔 JSON。请求串行化（W2 单 client 单请求，避免响应错配）。"""

    def __init__(self, command: list[str], *, server_name: str = "", cwd: str | Path | None = None) -> None:
        self.command = command
        self.server_name = server_name
        self.cwd = str(cwd or w2.project_root)
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._next_id = 0
        self._stderr_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._proc is not None:
            return
        env = os.environ.copy()
        root = str(w2.project_root)
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["PYTHONUNBUFFERED"] = "1"
        # 协议流编码必须与父进程解码口径一致（UTF-8）。父进程按 utf-8 解码子进程 stdout，
        # 若子进程 stdout 落到 locale 编码（Windows 中文环境 = GBK），含中文的工具结果
        # （kb-search 的片段、sql 的列名）会被写成 GBK 字节 → 父进程 UTF-8 解码噪音 →
        # JSON-RPC 解析失败 → 表现为 MCP_PROTOCOL_ERROR，且对每一次重试都复现。
        # 这里显式钉死子进程编码，不依赖启动者的 shell 是否设置了 PYTHONUTF8。
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=env,
                # 任务 17：stdio 单行 JSON 上限提到 4MB——asyncio 管道默认 64KB 行限制，
                # 大结果集（如分周趋势图 56 行×多列）会炸 "Separator is found, but
                # chunk is longer than limit"，表现为 chart-render EXECUTOR_ERROR。
                limit=_STDIO_LINE_LIMIT,
            )
        except (OSError, NotImplementedError) as exc:  # pragma: no cover - 平台不支持子进程
            raise MCPTransportError(f"无法启动 MCP Server {self.server_name}: {exc}") from exc
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                return
            logger.debug(
                "mcp_server_stderr",
                extra={"server": self.server_name, "line": line.decode(errors="ignore").strip()},
            )

    async def request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise MCPTransportError(f"MCP Server {self.server_name} 未启动")

        async with self._lock:
            self._next_id += 1
            request_id = self._next_id
            payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            try:
                self._proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
                await self._proc.stdin.drain()
                raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise MCPTransportError(
                    f"MCP 调用超时（{method}, {timeout}s）", McpErrorCode.MCP_TIMEOUT.value
                ) from exc
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise MCPTransportError(f"MCP Server {self.server_name} 连接中断: {exc}") from exc
            except ValueError as exc:
                # asyncio StreamReader 行超限的标志性报错，统一按协议错误处理（可被哨兵重启）
                raise MCPTransportError(
                    f"MCP Server {self.server_name} 响应行超限: {exc}",
                    McpErrorCode.MCP_PROTOCOL_ERROR.value,
                ) from exc

        if not raw:
            raise MCPTransportError(f"MCP Server {self.server_name} 已退出（stdout EOF）")
        # 固定 UTF-8 解码：与子进程侧 PYTHONIOENCODING 保持同一口径，避免 locale 编码串线
        text = raw.decode("utf-8", errors="replace")
        try:
            message = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MCPTransportError(f"MCP 响应非 JSON: {text[:200]!r}") from exc
        if message.get("id") != request_id:
            raise MCPTransportError(f"MCP 响应 id 错配: 期望 {request_id}, 实际 {message.get('id')}")
        return message

    async def close(self) -> None:
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=3)
        except (asyncio.TimeoutError, ProcessLookupError):  # pragma: no cover
            proc.kill()
            await proc.wait()


class InMemoryTransport(MCPTransport):
    """进程内直连服务端（单测与本地无子进程环境使用）。"""

    def __init__(self, app: MCPServerApp, *, server_name: str | None = None) -> None:
        self.app = app
        self.server_name = server_name or app.name
        self._calls: list[dict[str, Any]] = []

    async def start(self) -> None:
        return None

    async def request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        self._calls.append({"method": method, "params": params})
        payload = {"jsonrpc": "2.0", "id": len(self._calls), "method": method, "params": params}
        response = await asyncio.wait_for(self.app.handle(payload), timeout=timeout)
        if response is None:  # 通知
            return {"jsonrpc": "2.0", "id": payload["id"], "result": {}}
        return response

    async def close(self) -> None:
        return None

    @property
    def calls(self) -> list[dict[str, Any]]:
        return list(self._calls)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class MCPClient:
    """单 server 客户端：一次 initialize，缓存 tools/list 结果。"""

    def __init__(
        self,
        server_name: str,
        transport: MCPTransport,
        *,
        timeout: float | None = None,
        on_event: EventHook | None = None,
    ) -> None:
        self.server_name = server_name
        self.transport = transport
        self.timeout = timeout or float(w2.MCP_CALL_TIMEOUT_SECONDS)
        self.on_event = on_event
        self.initialized = False
        self.server_info: dict[str, Any] = {}
        self._tools: list[ToolDef] = []

    # ---- 协议 ----
    async def start(self) -> None:
        await self.transport.start()

    async def initialize(self) -> dict[str, Any]:
        response = await self.transport.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"roots": {"listChanged": False}, "sampling": {}},
                "clientInfo": {"name": "agentflow-executor", "version": "0.2.0"},
            },
            timeout=self.timeout,
        )
        result = _unwrap(response)
        self.server_info = dict(result.get("serverInfo") or {})
        self.initialized = True
        logger.info(
            "mcp_initialized",
            extra={
                "server": self.server_name,
                "protocol": result.get("protocolVersion"),
                "server_info": self.server_info,
            },
        )
        return result

    async def list_tools(self, *, refresh: bool = False) -> list[ToolDef]:
        if self._tools and not refresh:
            return self._tools
        if not self.initialized:
            await self.initialize()
        response = await self.transport.request("tools/list", {}, timeout=self.timeout)
        result = _unwrap(response)
        tools: list[ToolDef] = []
        for item in result.get("tools") or []:
            tools.append(
                ToolDef(
                    name=str(item.get("name", "")),
                    description=str(item.get("description", "")),
                    inputSchema=item.get("inputSchema") or {},
                    server=self.server_name,
                )
            )
        self._tools = [t for t in tools if t.name]
        logger.info(
            "mcp_tools_discovered",
            extra={"server": self.server_name, "tools": [t.name for t in self._tools]},
        )
        return self._tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """任务 B 健康哨兵：只读工具遭遇 stdio 子进程瞬断（秒退/管道断裂/未启动）时，
        后台自动重启子进程一次并重试当前请求；notify 等有副作用的工具不在此列。
        超时（MCP_TIMEOUT）不是传输层故障，不触发重启——交给 Executor 重试矩阵。
        """
        try:
            return await self._call_tool_once(name, arguments, timeout=timeout)
        except MCPTransportError as exc:
            if name not in READ_ONLY_TOOLS or exc.code != McpErrorCode.MCP_PROTOCOL_ERROR.value:
                raise
            logger.warning(
                "mcp_stdio_sentinel_restart",
                extra={
                    "server": self.server_name,
                    "tool": name,
                    "error": str(exc)[:200],
                    "trace_id": get_trace_id() or None,
                },
            )
            await self.restart()
            return await self._call_tool_once(name, arguments, timeout=timeout)

    async def restart(self) -> None:
        """重启 stdio 子进程并重新握手（initialize），清空缓存的工具清单。"""
        try:
            await self.transport.close()
        except Exception as exc:  # noqa: BLE001 - 旧进程清理失败不阻断重启
            logger.debug("mcp_stdio_restart_close_failed", extra={"server": self.server_name, "error": str(exc)[:120]})
        self.initialized = False
        self._tools = []
        await self.start()
        await self.initialize()
        logger.info("mcp_stdio_restarted", extra={"server": self.server_name})

    async def _call_tool_once(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if not self.initialized:
            await self.initialize()

        effective_timeout = timeout or self.timeout
        started = time.perf_counter()
        await self._emit(
            "tool.call",
            {
                "server": self.server_name,
                "tool": name,
                "params": mask_mapping(arguments),
                "trace_id": get_trace_id() or None,
            },
        )

        try:
            response = await self.transport.request(
                "tools/call", {"name": name, "arguments": arguments}, timeout=effective_timeout
            )
            result = _unwrap(response)
            latency_ms = int((time.perf_counter() - started) * 1000)

            if result.get("isError"):
                code, message, data = _parse_error_content(result)
                raise ToolInvocationError(code, message, data)

            payload = result.get("structuredContent")
            if payload is None:
                payload = _parse_text_content(result)

            await self._emit(
                "tool.result",
                {
                    "server": self.server_name,
                    "tool": name,
                    "ok": True,
                    "latency_ms": latency_ms,
                    "result_digest": _digest(payload),
                    "trace_id": get_trace_id() or None,
                },
            )
            return payload if isinstance(payload, dict) else {"result": payload}

        except ToolInvocationError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            await self._emit(
                "tool.result",
                {
                    "server": self.server_name,
                    "tool": name,
                    "ok": False,
                    "error_code": exc.code,
                    # 可诊断性：错误文案必须落 trace，否则回放只剩一个错误码，无从定位
                    "error_message": mask_text(str(exc))[:300],
                    "latency_ms": latency_ms,
                    "trace_id": get_trace_id() or None,
                },
            )
            raise
        except MCPTransportError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            await self._emit(
                "tool.result",
                {
                    "server": self.server_name,
                    "tool": name,
                    "ok": False,
                    "error_code": exc.code,
                    "error_message": mask_text(str(exc))[:300],
                    "latency_ms": latency_ms,
                    "trace_id": get_trace_id() or None,
                },
            )
            raise

    async def close(self) -> None:
        await self.transport.close()

    # ---- 内部 ----
    async def _emit(self, event: str, payload: dict[str, Any]) -> None:
        logger.info(f"mcp_{event.replace('.', '_')}", extra=payload)
        if self.on_event is None:
            return
        result = self.on_event(event, payload)
        if asyncio.iscoroutine(result):
            await result


def _unwrap(response: dict[str, Any]) -> dict[str, Any]:
    error = response.get("error")
    if error:
        raise MCPTransportError(
            f"JSON-RPC 错误 {error.get('code')}: {error.get('message')}",
            McpErrorCode.MCP_PROTOCOL_ERROR.value,
        )
    result = response.get("result")
    if not isinstance(result, dict):
        raise MCPTransportError("JSON-RPC 响应缺少 result")
    return result


def _parse_error_content(result: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    text = ""
    for item in result.get("content") or []:
        if item.get("type") == "text":
            text = item.get("text", "")
            break
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return McpErrorCode.MCP_PROTOCOL_ERROR.value, text or "工具执行失败", {}
    return (
        str(parsed.get("code") or McpErrorCode.MCP_PROTOCOL_ERROR.value),
        str(parsed.get("message") or "工具执行失败"),
        dict(parsed.get("data") or {}),
    )


def _parse_text_content(result: dict[str, Any]) -> Any:
    for item in result.get("content") or []:
        if item.get("type") == "text":
            try:
                return json.loads(item.get("text", "{}"))
            except json.JSONDecodeError:
                return {"text": item.get("text", "")}
    return {}


def _digest(payload: Any, limit: int = 300) -> str:
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(payload)
    return text if len(text) <= limit else text[:limit] + "…"


# ---------------------------------------------------------------------------
# 连接池：4 个 server 的聚合入口
# ---------------------------------------------------------------------------
DEFAULT_SERVERS: tuple[str, ...] = ("sql-query", "chart-render", "kb-search", "notify")

#: stdio 单行 JSON 上限（4MB）：默认 64KB 会把大结果集响应行炸成 ValueError
_STDIO_LINE_LIMIT = 4 * 1024 * 1024

#: 任务 B 健康哨兵：stdio 子进程瞬断（秒退/管道断裂）时**允许自动重启并重试一次**的工具。
#: 仅限只读工具——notify 有真实外发副作用，绝不自动重试（由 Executor 重试矩阵/幂等层兜）。
READ_ONLY_TOOLS: frozenset[str] = frozenset({"sql-query", "kb-search", "chart-render"})

TOOL_TIMEOUTS: dict[str, float] = {
    "sql-query": float(w2.SQL_EXEC_TIMEOUT_SECONDS),
    "chart-render": float(w2.CHART_RENDER_TIMEOUT_SECONDS),
    "kb-search": float(w2.KB_TIMEOUT_SECONDS),
    "notify": float(w2.NOTIFY_TIMEOUT_SECONDS),
}


class MCPClientPool:
    """工具名 -> server 动态路由（由 tools/list 结果反推，不硬编码）。

    任务 11 治理中间件（均可选，缺省行为与升级前完全一致）：
    - ``version_router``：版本路由适配器——按 w2.TOOL_VERSION_ROUTES 把请求改写为
      新版本工具名与 payload；目标版本未部署时自动回退旧版；
    - ``idempotency``：幂等结果缓存——task_id 绑定时，同一 (task, tool, args) 的
      成功结果在 TTL 内只真实执行一次（防超时重试造成重复副作用）。
    """

    def __init__(
        self,
        clients: dict[str, MCPClient],
        *,
        version_router: ToolVersionRouter | None = None,
        idempotency: IdempotencyStore | None = None,
    ) -> None:
        self.clients = clients
        self.version_router = version_router or ToolVersionRouter()
        self.idempotency = idempotency
        self._index: dict[str, str] = {}
        self._tools: list[ToolDef] = []
        self.unavailable: dict[str, str] = {}

    @classmethod
    async def create_default(cls, *, on_event: EventHook | None = None) -> "MCPClientPool":
        """生产路径：为 4 个 server 各起一个 stdio 子进程，并接线治理中间件。"""
        clients = {
            name: MCPClient(
                name,
                StdioTransport(w2.mcp_server_command(name), server_name=name),
                timeout=w2.MCP_CALL_TIMEOUT_SECONDS,
                on_event=on_event,
            )
            for name in DEFAULT_SERVERS
        }
        return cls(clients, idempotency=default_idempotency_store())

    @classmethod
    def from_apps(cls, apps: dict[str, MCPServerApp], *, on_event: EventHook | None = None) -> "MCPClientPool":
        """测试路径：进程内直连（apps 的 key 为 server 名）。"""
        return cls(
            {
                name: MCPClient(name, InMemoryTransport(app, server_name=name), timeout=10, on_event=on_event)
                for name, app in apps.items()
            }
        )

    async def discover(self) -> list[ToolDef]:
        """逐个 server initialize + tools/list；单个失败降级为不可用，不阻断主流程。"""
        tools: list[ToolDef] = []
        for name, client in self.clients.items():
            try:
                await client.start()
                await client.initialize()
                discovered = await client.list_tools()
                tools.extend(discovered)
                for tool in discovered:
                    self._index[tool.name] = name
            except Exception as exc:  # noqa: BLE001
                self.unavailable[name] = str(exc)
                logger.warning("mcp_server_unavailable", extra={"server": name, "error": str(exc)})
        self._tools = tools
        return tools

    @property
    def tools(self) -> list[ToolDef]:
        return list(self._tools)

    def server_for(self, tool: str) -> str | None:
        return self._index.get(tool)

    async def call(self, tool: str, arguments: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        # 任务 11 改进 1：版本路由——按配置把旧契约请求改写为新版本工具名 + payload
        resolved = self.version_router.resolve(tool, arguments)
        server = self._index.get(resolved.wire_tool)
        if server is None and resolved.changed:
            # 兼容层韧性：目标版本工具尚未部署（tools/list 没发现）→ 回退 v1 原契约
            logger.warning(
                "tool_version_fallback",
                extra={"tool": tool, "target_version": resolved.version, "wire_tool": resolved.wire_tool},
            )
            resolved = self.version_router.fallback_v1(tool, arguments)
            server = self._index.get(resolved.wire_tool)
        if server is None:
            raise ToolInvocationError(McpErrorCode.TOOL_NOT_FOUND.value, f"工具 {tool} 未在 tools/list 中注册")

        # 任务 11 改进 2：幂等预检——task_id 绑定 + Redis 存活时，同一副作用调用只真实执行一次
        idem_key: str | None = None
        if self.idempotency is not None:
            task_id = get_task_id()
            if task_id is not None:
                idem_key = self.idempotency.build_key(task_id, resolved.tool, resolved.arguments)
                cached = await self.idempotency.load(idem_key)
                if cached is not None:
                    logger.info(
                        "tool_idempotency_hit",
                        extra={"tool": resolved.tool, "task_id": task_id, "ttl_seconds": self.idempotency.ttl},
                    )
                    return cached

        result = await self.clients[server].call_tool(
            resolved.wire_tool, resolved.wire_arguments, timeout=timeout or TOOL_TIMEOUTS.get(server)
        )
        if idem_key is not None:
            # call_tool 正常返回 = 成功；失败/超时走异常分支，不落缓存（重试仍会真实发起）
            await self.idempotency.save(idem_key, result)
        return result

    async def aclose(self) -> None:
        for client in self.clients.values():
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - 关闭失败不掩盖主流程异常
                logger.warning("mcp_close_failed", extra={"server": client.server_name})
        # 任务 B：若被关闭的是进程级共享池，同步摘除单例引用，
        # 让下一次 get_shared_pool() 重建全新子进程（reset_runtime 语义保持不变）。
        global _shared_pool
        if _shared_pool is self:
            _shared_pool = None


# ---------------------------------------------------------------------------
# 任务 B：进程级共享池单例
# ---------------------------------------------------------------------------
#: 生产装配只允许通过 get_shared_pool() 拿池——W2（task_service）与 W3（stream_service）
#: 两条 runtime 装配路径曾经各自 create_default，导致 8 个 MCP 子进程并存、
#: 熔断/重启状态分裂；收敛单例后全生命周期内只有一套 4 个子进程。
_shared_pool: "MCPClientPool | None" = None
_shared_pool_lock: asyncio.Lock | None = None


async def get_shared_pool() -> "MCPClientPool":
    """返回进程级 MCP 子进程池单例（首次调用时创建并 discover）。

    事件钩子统一挂 ``stream_context.mcp_event_hook``（W2/W3 依赖同一实现），
    工具轨迹（tool.call/tool.result → task_executions）不因单例化而丢失。
    """
    global _shared_pool, _shared_pool_lock
    if _shared_pool is not None:
        return _shared_pool
    if _shared_pool_lock is None:
        _shared_pool_lock = asyncio.Lock()
    async with _shared_pool_lock:
        if _shared_pool is None:
            from app.services.stream_context import mcp_event_hook

            pool = await MCPClientPool.create_default(on_event=mcp_event_hook)
            await pool.discover()
            _shared_pool = pool
            logger.info("mcp_shared_pool_created", extra={"servers": list(pool.clients)})
    return _shared_pool


__all__ = [
    "MCPTransport",
    "StdioTransport",
    "InMemoryTransport",
    "MCPClient",
    "MCPClientPool",
    "MCPTransportError",
    "ToolInvocationError",
    "DEFAULT_SERVERS",
    "TOOL_TIMEOUTS",
    "READ_ONLY_TOOLS",
    "get_shared_pool",
]
