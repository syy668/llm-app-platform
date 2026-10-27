"""MCP HTTP 传输（MCP JSON-RPC 2.0 over HTTP / SSE）—— W5-Part1 任务 1。

## 定位：第二套传输，不替换 stdio

```
                       ┌─────────────────────────┐
   stdio 子进程  ───────▶│  MCPServerApp（同一实例） │◀───────  HTTP /mcp/v1
   （W2 原有，冻结）      └─────────────────────────┘         （本文件新增）
```

- **服务端**：``build_mcp_http_router()`` 把任意 ``MCPServerApp`` 挂成 ``/mcp/v1`` 端点，
  ``initialize`` / ``tools/list`` / ``tools/call`` 三个方法直接复用 ``MCPServerApp.handle``，
  **工具内部实现零改动**（4 个 Server 业务逻辑一行未动）。
- **客户端**：``HttpTransport`` 实现 ``MCPTransport`` 抽象（与 ``StdioTransport`` 同契约），
  因此可直接喂给 W2 的 ``MCPClient`` / ``MCPClientPool``，动态发现工具无需改客户端代码。
- **兼容模式**：同一个 ``MCPServerApp`` 实例既可 ``serve_stdio()`` 也可 ``create_mcp_http_app()``，
  两种启动方式共享同一份工具表（见 ``tests/test_w5_mcp_http.py::test_same_instance_serves_both_transports``）。

## 协议端点

| 方法 | 路径 | 语义 |
|---|---|---|
| POST | ``/mcp/v1`` | 单发 JSON-RPC（也接受批量数组）；通知返回 202 无包体 |
| GET  | ``/mcp/v1/sse`` | 打开 SSE 长会话，首帧下发 ``endpoint``（含 session_id） |
| POST | ``/mcp/v1/messages?session_id=`` | 向 SSE 会话投递请求，202 受理，响应由 SSE 回流 |

## 可靠性

- 鉴权：``X-MCP-API-Key`` 或 ``Authorization: Bearer``（SSE 额外支持 ``?api_key=``，兼容 EventSource），
  ``MCP_HTTP_API_KEY`` 为空时跳过（仅限本地，启动日志会告警）；
- 连接池：``httpx.AsyncClient`` + ``Limits(max_connections, max_keepalive_connections)``；
- 超时：连接超时与读超时分离，逐次可分方法覆盖；
- 熔断：复用 W4 ``app.tools.kb_adapter.CircuitBreaker``（3 次失败 / 30s 半开），
  **只对传输层故障计数**，JSON-RPC ``error`` 属于服务端正常应答，不触发熔断。
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx
from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.core.logging import get_logger
from app.core.w5_config import w5
from app.schemas.mcp import INVALID_REQUEST, PARSE_ERROR, McpErrorCode
from app.tools.kb_adapter import CircuitBreaker
from app.tools.mcp_client import DEFAULT_SERVERS, MCPClient, MCPClientPool, MCPTransport, MCPTransportError
from app.tools.mcp_stdio import MCPServerApp

logger = get_logger(__name__)

API_KEY_HEADER = "X-MCP-API-Key"
SESSION_HEADER = "Mcp-Session-Id"

# HTTP 层专有错误码（不属于 MCP 工具错误码集合，客户端按传输故障处理）
MCP_HTTP_UNAUTHORIZED = -32001  # 鉴权失败
MCP_HTTP_CIRCUIT_OPEN = "MCP_CIRCUIT_OPEN"  # 本地熔断已打开
MCP_HTTP_NOT_READY = "MCP_HTTP_NOT_READY"  # 客户端未 start / 已关闭
MCP_HTTP_SESSION_MISSING = "MCP_HTTP_SESSION_MISSING"


# ---------------------------------------------------------------------------
# 1) 服务端：鉴权
# ---------------------------------------------------------------------------
def extract_api_key(request: Request) -> str:
    """按优先级取 key：头 ``X-MCP-API-Key`` → ``Authorization: Bearer`` → ``?api_key=``。"""
    key = (request.headers.get(API_KEY_HEADER) or "").strip()
    if key:
        return key
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.query_params.get("api_key") or "").strip()


def is_authorized(provided: str, expected: str) -> bool:
    """常量时间比较；``expected`` 为空视为免鉴权（本地调试）。"""
    if not expected:
        return True
    return hmac.compare_digest(provided or "", expected)


def _unauthorized(reason: str = "MCP HTTP 鉴权失败：缺少或错误的 API Key") -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": MCP_HTTP_UNAUTHORIZED, "message": reason},
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


# ---------------------------------------------------------------------------
# 2) 服务端：SSE 会话注册表
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class HttpMcpSession:
    """一个 SSE 长会话：请求从 POST /messages 进，响应从 GET /sse 出。"""

    session_id: str
    server_name: str
    queue: asyncio.Queue[dict[str, Any] | None] = field(default_factory=asyncio.Queue)
    created_at: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    @property
    def age(self) -> float:
        return time.monotonic() - self.created_at


class HttpSessionRegistry:
    """内存会话表（单进程）；多副本部署时应改为 Redis，属 Part2 容器化范畴。"""

    def __init__(self, *, max_sessions: int | None = None, ttl_seconds: int | None = None) -> None:
        self.max_sessions = max_sessions or w5.MCP_HTTP_MAX_SESSIONS
        self.ttl_seconds = ttl_seconds or w5.MCP_HTTP_SESSION_TTL_SECONDS
        self._sessions: dict[str, HttpMcpSession] = {}

    def create(self, server_name: str) -> HttpMcpSession:
        self.reap()
        while len(self._sessions) >= self.max_sessions:
            oldest = min(self._sessions.values(), key=lambda s: s.created_at)
            self.drop(oldest.session_id)
        session = HttpMcpSession(session_id=uuid.uuid4().hex, server_name=server_name)
        self._sessions[session.session_id] = session
        logger.info("mcp_http_session_open", extra={"session_id": session.session_id, "server": server_name})
        return session

    def get(self, session_id: str) -> HttpMcpSession | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.age > self.ttl_seconds:
            self.drop(session_id)
            return None
        session.touch()
        return session

    def drop(self, session_id: str) -> None:
        if self._sessions.pop(session_id, None) is not None:
            logger.info("mcp_http_session_close", extra={"session_id": session_id})

    def reap(self) -> int:
        dead = [sid for sid, s in self._sessions.items() if s.age > self.ttl_seconds]
        for sid in dead:
            self.drop(sid)
        return len(dead)

    def __len__(self) -> int:  # pragma: no cover - 仅诊断
        return len(self._sessions)

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "session_id": s.session_id,
                "server": s.server_name,
                "age_seconds": round(s.age, 3),
                "pending": s.queue.qsize(),
            }
            for s in self._sessions.values()
        ]


def _sse_frame(event: str, data: str) -> str:
    """SSE 帧：协议要求 ``data`` 内换行必须逐行加前缀，这里统一压成单行 JSON。"""
    return f"event: {event}\ndata: {data}\n\n"


async def sse_event_stream(
    session: HttpMcpSession,
    *,
    endpoint: str,
    ttl_seconds: float,
    registry: "HttpSessionRegistry | None" = None,
    heartbeat_seconds: float | None = None,
) -> Any:
    """SSE 会话事件流（从 ``/sse`` 路由里抽出来，便于单测逐帧消费）。

    帧序：``endpoint``（下发 session_id） → N×``message``（业务响应） → ``close``。
    空闲时每 ``heartbeat_seconds`` 发一次 SSE 注释 ``: ping``，防止中间代理断连。
    """
    heartbeat = heartbeat_seconds or min(15.0, max(0.5, ttl_seconds / 2))
    try:
        yield _sse_frame("endpoint", endpoint)
        deadline = time.monotonic() + ttl_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = await asyncio.wait_for(session.queue.get(), timeout=min(heartbeat, remaining))
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            if item is None:  # 显式关闭
                break
            yield _sse_frame("message", json.dumps(item, ensure_ascii=False, default=str))
    finally:
        # 无论正常结束还是客户端断开，都要回收会话（此处不 yield，避免 GeneratorExit 告警）
        if registry is not None:
            registry.drop(session.session_id)

    yield _sse_frame("close", json.dumps({"session_id": session.session_id}, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 3) 服务端：路由
# ---------------------------------------------------------------------------
def _pick_server(
    server_apps: dict[str, MCPServerApp], name: str | None, primary: str | None
) -> MCPServerApp | None:
    if name:
        return server_apps.get(name)
    if primary:
        return server_apps.get(primary)
    if len(server_apps) == 1:
        return next(iter(server_apps.values()))
    return None


async def _read_json(request: Request) -> tuple[Any, JSONResponse | None]:
    try:
        return await request.json(), None
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None, JSONResponse(
            status_code=400,
            content={"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": "JSON 解析失败"}},
        )


async def _dispatch(app: MCPServerApp, payload: Any) -> tuple[int, Any]:
    """复用 ``MCPServerApp.handle``（工具实现零改动），返回 (HTTP 状态码, 响应体)。"""
    if isinstance(payload, list):
        if not payload:
            return 400, {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": INVALID_REQUEST, "message": "批量请求不能为空数组"},
            }
        responses: list[Any] = []
        for item in payload:
            response = await app.handle(item)
            if response is not None:
                responses.append(response)
        return (200, responses) if responses else (202, None)

    if not isinstance(payload, dict):
        return 400, {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": INVALID_REQUEST, "message": "请求必须是 JSON 对象或 JSON 数组"},
        }

    response = await app.handle(payload)
    return (202, None) if response is None else (200, response)


def build_mcp_http_router(
    server_apps: dict[str, MCPServerApp],
    *,
    api_key: str | None = None,
    primary: str | None = None,
    path: str | None = None,
    sessions: HttpSessionRegistry | None = None,
) -> APIRouter:
    """把 1~N 个 ``MCPServerApp`` 挂成 MCP HTTP 端点。

    ``server_apps``：server 名 -> 实例。多 Server 同进程时用 ``?server=`` 选择，
    单 Server 时省略即用唯一实例（``primary`` 可显式指定）。
    """
    if not server_apps:
        raise ValueError("build_mcp_http_router 至少需要一个 MCPServerApp")

    expected_key = (w5.MCP_HTTP_API_KEY if api_key is None else api_key).strip()
    resolved_primary = primary or (next(iter(server_apps)) if len(server_apps) == 1 else None)
    # 注意：不能用 `sessions or HttpSessionRegistry()` —— 空注册表 len()==0 为假值，
    # 会被替换成新实例，导致外部注入的会话表失效（测试里踩过一次）。
    registry = sessions if sessions is not None else HttpSessionRegistry()
    prefix = path or w5.MCP_HTTP_PATH
    router = APIRouter(prefix=prefix, tags=["mcp-http"])

    if not expected_key:
        logger.warning("mcp_http_auth_disabled", extra={"path": prefix, "hint": "生产必须配置 MCP_HTTP_API_KEY"})

    def guard(request: Request) -> JSONResponse | None:
        if is_authorized(extract_api_key(request), expected_key):
            return None
        logger.warning("mcp_http_unauthorized", extra={"client": request.client.host if request.client else None})
        return _unauthorized()

    # ---------------- 3.1 单发 / 批量 JSON-RPC ----------------
    @router.post("", summary="MCP JSON-RPC 端点（initialize / tools/list / tools/call）")
    async def rpc(request: Request, server: str | None = Query(default=None, description="多 Server 同进程时指定")) -> Any:
        denied = guard(request)
        if denied is not None:
            return denied

        app = _pick_server(server_apps, server, resolved_primary)
        if app is None:
            return JSONResponse(
                status_code=400,
                content={
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": INVALID_REQUEST,
                        "message": f"未知 server: {server!r}，可选 {sorted(server_apps)}",
                    },
                },
            )

        payload, bad = await _read_json(request)
        if bad is not None:
            return bad

        status, body = await _dispatch(app, payload)
        if body is None:  # 通知：202 无包体
            return Response(status_code=status)
        return JSONResponse(status_code=status, content=body)

    # ---------------- 3.2 SSE 长会话 ----------------
    @router.get("/sse", summary="打开 SSE 长会话（首帧 endpoint 下发 session_id）")
    async def sse(
        request: Request,
        server: str | None = Query(default=None),
        max_seconds: int | None = Query(default=None, ge=1, le=3600, description="会话最长存活秒数（测试用）"),
    ) -> Any:
        denied = guard(request)
        if denied is not None:
            return denied

        app = _pick_server(server_apps, server, resolved_primary)
        if app is None:
            return JSONResponse(
                status_code=400,
                content={"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "未知 server"}},
            )

        session = registry.create(app.name)
        session_id = session.session_id
        endpoint = f"{prefix}/messages?session_id={session_id}&server={app.name}"
        ttl = float(max_seconds or w5.MCP_HTTP_SSE_MAX_SECONDS)

        return StreamingResponse(
            sse_event_stream(session, endpoint=endpoint, ttl_seconds=ttl, registry=registry),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                SESSION_HEADER: session_id,
            },
        )

    # ---------------- 3.3 向 SSE 会话投递请求 ----------------
    @router.post("/messages", status_code=202, summary="向 SSE 会话投递 JSON-RPC，响应经 SSE 回流")
    async def post_message(
        request: Request,
        session_id: str = Query(min_length=8),
    ) -> Any:
        denied = guard(request)
        if denied is not None:
            return denied

        session = registry.get(session_id)
        if session is None:
            return JSONResponse(
                status_code=404,
                content={
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": MCP_HTTP_SESSION_MISSING, "message": "会话不存在或已过期"},
                },
            )

        payload, bad = await _read_json(request)
        if bad is not None:
            return bad

        app = server_apps.get(session.server_name)
        if app is None:  # pragma: no cover - 会话建立后 server 表不会变
            return JSONResponse(
                status_code=400,
                content={"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "会话绑定的 server 已卸载"}},
            )

        if isinstance(payload, list):
            for item in payload:
                response = await app.handle(item)
                if response is not None:
                    await session.queue.put(response)
        elif isinstance(payload, dict):
            response = await app.handle(payload)
            if response is not None:
                await session.queue.put(response)
        else:
            return JSONResponse(
                status_code=400,
                content={"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "非法请求体"}},
            )

        return Response(status_code=202)

    # 便于自检 / 运维观测
    router.session_registry = registry  # type: ignore[attr-defined]
    return router


def create_mcp_http_app(
    server_app: MCPServerApp | dict[str, MCPServerApp],
    *,
    api_key: str | None = None,
    path: str | None = None,
    title: str = "MCP HTTP Server",
) -> FastAPI:
    """独立进程形态：一个（或一组）``MCPServerApp`` → 完整 FastAPI 应用。"""
    apps = server_app if isinstance(server_app, dict) else {server_app.name: server_app}
    app = FastAPI(title=title, version="0.5.0", docs_url="/docs", openapi_url="/openapi.json")
    app.include_router(build_mcp_http_router(apps, api_key=api_key, path=path))
    return app


def mount_mcp_http(
    app: FastAPI,
    server_apps: dict[str, MCPServerApp],
    *,
    api_key: str | None = None,
    path: str | None = None,
    primary: str | None = None,
) -> APIRouter:
    """把 MCP HTTP 端点挂到已有应用（双传输并存场景：主应用同时提供 stdio 与 HTTP）。"""
    router = build_mcp_http_router(server_apps, api_key=api_key, path=path, primary=primary)
    app.include_router(router)
    return router


# ---------------------------------------------------------------------------
# 4) 客户端：HTTP 传输（实现 W2 的 MCPTransport 抽象）
# ---------------------------------------------------------------------------
class HttpTransport(MCPTransport):
    """JSON-RPC over HTTP 客户端传输。

    与 ``StdioTransport`` 同契约（start / request / close），因此 ``MCPClient`` 无需任何改动
    即可换传输——这正是「双传输并存」的关键：传输可插拔，客户端与工具层共用一套。
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        server_name: str = "",
        api_key: str | None = None,
        path: str | None = None,
        timeout: float | None = None,
        max_connections: int | None = None,
        retry_times: int | None = None,
        retry_backoff: float | None = None,
        breaker: CircuitBreaker | None = None,
        forward_server: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = (base_url or w5.MCP_HTTP_BASE_URL).rstrip("/")
        self.path = path or w5.MCP_HTTP_PATH
        self.url = f"{self.base_url}{self.path}"
        self.server_name = server_name
        self.api_key = w5.MCP_HTTP_API_KEY if api_key is None else api_key
        self.timeout = float(timeout or w5.MCP_HTTP_TIMEOUT_SECONDS)
        self.max_connections = max_connections or w5.MCP_HTTP_MAX_CONNECTIONS
        self.retry_times = w5.MCP_HTTP_RETRY_TIMES if retry_times is None else max(0, retry_times)
        self.retry_backoff = w5.MCP_HTTP_RETRY_BACKOFF_SECONDS if retry_backoff is None else retry_backoff
        self.forward_server = forward_server
        self.breaker = breaker or CircuitBreaker(
            name=f"mcp-http:{server_name or 'default'}",
            failure_threshold=w5.MCP_HTTP_BREAKER_THRESHOLD,
            recovery_seconds=w5.MCP_HTTP_BREAKER_RECOVERY_SECONDS,
        )
        self._client = client
        self._owns_client = client is None
        self._next_id = 0
        self._lock = asyncio.Lock()

    # ---------------- 生命周期 ----------------
    async def start(self) -> None:
        if self._client is not None:
            return
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=w5.MCP_HTTP_CONNECT_TIMEOUT_SECONDS),
            limits=httpx.Limits(
                max_connections=self.max_connections,
                max_keepalive_connections=w5.MCP_HTTP_MAX_KEEPALIVE,
                keepalive_expiry=w5.MCP_HTTP_KEEPALIVE_EXPIRY_SECONDS,
            ),
            headers=self._headers(),
        )
        self._owns_client = True
        logger.info("mcp_http_transport_started", extra={"url": self.url, "server": self.server_name})

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    @property
    def ready(self) -> bool:
        return self._client is not None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers[API_KEY_HEADER] = self.api_key
        return headers

    # ---------------- 请求 ----------------
    async def request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        if self._client is None:
            raise MCPTransportError(f"MCP HTTP 客户端未启动（{self.server_name}）", MCP_HTTP_NOT_READY)

        if not self.breaker.allow():
            raise MCPTransportError(
                f"MCP HTTP 熔断已打开（{self.server_name}），剩余 {self.breaker.snapshot().remaining_seconds:.1f}s",
                MCP_HTTP_CIRCUIT_OPEN,
            )

        async with self._lock:
            self._next_id += 1
            request_id = self._next_id

        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        query = {"server": self.server_name} if (self.forward_server and self.server_name) else None

        last_error: Exception | None = None
        for attempt in range(self.retry_times + 1):
            try:
                response = await self._client.post(
                    self.url, json=payload, params=query, timeout=timeout, headers=self._headers()
                )
            except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
                last_error = MCPTransportError(
                    f"MCP HTTP 调用超时（{method}, {timeout}s）", McpErrorCode.MCP_TIMEOUT.value
                )
                logger.warning("mcp_http_timeout", extra={"method": method, "attempt": attempt + 1})
                await self._sleep_before_retry(attempt)
                continue
            except httpx.HTTPError as exc:
                last_error = MCPTransportError(f"MCP HTTP 连接失败: {exc}")
                logger.warning("mcp_http_transport_error", extra={"method": method, "error": str(exc)[:200]})
                await self._sleep_before_retry(attempt)
                continue

            if response.status_code == 401:
                self.breaker.record_failure()
                raise MCPTransportError("MCP HTTP 鉴权失败（401）：请检查 MCP_HTTP_API_KEY", MCP_HTTP_UNAUTHORIZED)
            if response.status_code == 202:  # 通知：服务端不受理回包
                self.breaker.record_success()
                return {"jsonrpc": "2.0", "id": request_id, "result": {}}
            if response.status_code >= 500:
                last_error = MCPTransportError(f"MCP HTTP 服务端错误 {response.status_code}")
                await self._sleep_before_retry(attempt)
                continue

            try:
                message = response.json()
            except ValueError as exc:
                last_error = MCPTransportError(f"MCP HTTP 响应非 JSON: {response.text[:200]!r}")
                logger.warning("mcp_http_bad_body", extra={"error": str(exc)[:200]})
                await self._sleep_before_retry(attempt)
                continue

            # 服务端正常应答（含 JSON-RPC error）：传输健康，不计熔断失败
            self.breaker.record_success()
            if isinstance(message, list):
                message = next(
                    (item for item in message if isinstance(item, dict) and item.get("id") == request_id),
                    message[0] if message else {},
                )
            if not isinstance(message, dict):
                raise MCPTransportError("MCP HTTP 响应结构非法（非对象）")
            if message.get("id") != request_id:
                raise MCPTransportError(
                    f"MCP HTTP 响应 id 错配: 期望 {request_id}, 实际 {message.get('id')}"
                )
            return message

        self.breaker.record_failure()
        raise last_error or MCPTransportError(f"MCP HTTP 调用失败（{method}）")

    async def _sleep_before_retry(self, attempt: int) -> None:
        # 毫秒级退避（测试默认 0.05s），用 await 而非同步 sleep，避免阻塞事件循环
        if attempt < self.retry_times and self.retry_backoff > 0:
            await asyncio.sleep(self.retry_backoff * (attempt + 1))

    def snapshot(self) -> dict[str, Any]:
        return {"url": self.url, "server": self.server_name, "breaker": self.breaker.snapshot().state,
                "failures": self.breaker.snapshot().failures}


# ---------------------------------------------------------------------------
# 5) 客户端：HTTP 连接池 / 动态发现
# ---------------------------------------------------------------------------
def create_http_pool(
    servers: Iterable[str] = DEFAULT_SERVERS,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    on_event: Any = None,
    forward_server: bool = True,
    timeout: float | None = None,
) -> MCPClientPool:
    """为多个 server 建 HTTP 客户端池（与 ``MCPClientPool.create_default`` 对位）。

    ``forward_server=True``：多 Server 同进程部署时用 ``?server=`` 路由；
    若每个 server 单独起一个 HTTP 进程，应传 ``False``。
    """
    clients = {
        name: MCPClient(
            name,
            HttpTransport(
                base_url,
                server_name=name,
                api_key=api_key,
                timeout=timeout,
                forward_server=forward_server,
            ),
            timeout=timeout or w5.MCP_HTTP_TIMEOUT_SECONDS,
            on_event=on_event,
        )
        for name in servers
    }
    return MCPClientPool(clients)


async def discover_tools_http(
    base_url: str | None = None,
    *,
    server_name: str = "sql-query",
    api_key: str | None = None,
    forward_server: bool = False,
    timeout: float | None = None,
) -> list[Any]:
    """一次性动态发现：连远端 MCP-HTTP，返回 ``ToolDef`` 列表后立即关闭连接。"""
    transport = HttpTransport(
        base_url,
        server_name=server_name,
        api_key=api_key,
        timeout=timeout,
        forward_server=forward_server,
    )
    client = MCPClient(server_name, transport, timeout=timeout or w5.MCP_HTTP_TIMEOUT_SECONDS)
    try:
        await client.start()
        await client.initialize()
        return await client.list_tools(refresh=True)
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# 6) 独立进程入口（与 stdio 的 ``run_server`` 对位）
# ---------------------------------------------------------------------------
_SERVER_MODULES = {
    "sql-query": "sql_query",
    "chart-render": "chart_render",
    "kb-search": "kb_search",
    "notify": "notify",
}


def load_server_app(name: str) -> MCPServerApp:
    """按名字构造 Server 实例（复用 4 个 Server 的 ``build_app``，不做任何包装改写）。"""
    import importlib

    module_name = _SERVER_MODULES.get(name)
    if module_name is None:
        raise ValueError(f"未知 MCP Server: {name}，可选 {sorted(_SERVER_MODULES)}")
    module = importlib.import_module(f"app.tools.mcp_servers.{module_name}")
    return module.build_app()


def load_all_server_apps() -> dict[str, MCPServerApp]:
    return {name: load_server_app(name) for name in _SERVER_MODULES}


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - 进程入口
    parser = argparse.ArgumentParser(description="以 HTTP 传输启动 MCP Server（stdio 传输不受影响）")
    parser.add_argument("--server", action="append", default=None,
                        help="server 名，可重复；缺省启动全部 4 个（同进程 ?server= 路由）")
    parser.add_argument("--host", default=w5.MCP_HTTP_LISTEN_HOST)
    parser.add_argument("--port", type=int, default=w5.MCP_HTTP_LISTEN_PORT)
    parser.add_argument("--api-key", default=None, help="缺省读 MCP_HTTP_API_KEY")
    parser.add_argument("--path", default=w5.MCP_HTTP_PATH)
    args = parser.parse_args(argv)

    import uvicorn

    names = args.server or list(_SERVER_MODULES)
    apps = {name: load_server_app(name) for name in names}
    api = create_mcp_http_app(
        apps,
        api_key=args.api_key,
        path=args.path,
        title=f"MCP HTTP Server ({', '.join(names)})",
    )
    logger.info(
        "mcp_http_server_starting",
        extra={"host": args.host, "port": args.port, "servers": names, "path": args.path},
    )
    uvicorn.run(api, host=args.host, port=args.port, log_level="info")


__all__ = [
    "API_KEY_HEADER",
    "SESSION_HEADER",
    "MCP_HTTP_UNAUTHORIZED",
    "MCP_HTTP_CIRCUIT_OPEN",
    "MCP_HTTP_NOT_READY",
    "HttpMcpSession",
    "HttpSessionRegistry",
    "HttpTransport",
    "sse_event_stream",
    "extract_api_key",
    "is_authorized",
    "build_mcp_http_router",
    "create_mcp_http_app",
    "mount_mcp_http",
    "create_http_pool",
    "discover_tools_http",
    "load_server_app",
    "load_all_server_apps",
    "main",
]
