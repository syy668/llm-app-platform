"""MCP 协议底座（JSON-RPC 2.0 over stdio）：服务端与传输层。

实现范围严格受限于硬性约束 3：
- 方法：仅 ``initialize`` / ``tools/list`` / ``tools/call``（外加 ``notifications/initialized`` 通知的静默吞掉）
- 传输：仅 stdio（标准输入输出，按行分隔 JSON）；HTTP 传输为 P2，不实现

协议细节：
- 请求无 ``id`` 视为通知，服务端不回包
- 协议级错误（方法不存在 / 参数不是 JSON）走 JSON-RPC ``error``
- 工具级错误（SQL 语法拒绝、审批缺失等）走 ``result.isError=true`` + content JSON，
  与 MCP 官方语义一致，便于客户端拿到**业务错误码**而不是把错误码塞进 message 字符串
"""

from __future__ import annotations

import asyncio
import json
import sys
from abc import ABC, abstractmethod
from typing import Any, TextIO

from pydantic import BaseModel, ValidationError

from app.core.logging import get_logger
from app.schemas.mcp import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    MCP_PROTOCOL_VERSION,
    MCP_SERVER_VERSION,
    JsonRpcRequest,
    McpErrorCode,
    McpToolSpec,
    ToolTextContent,
    ToolsCallResult,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 工具抽象
# ---------------------------------------------------------------------------
class ToolError(Exception):
    """工具业务错误：携带 PRD 定义的错误码，客户端据此决定重试/降级。"""

    def __init__(self, code: str | McpErrorCode, message: str, *, data: dict[str, Any] | None = None) -> None:
        self.code = code.value if isinstance(code, McpErrorCode) else str(code)
        self.message = message
        self.data = data or {}
        super().__init__(f"{self.code}: {message}")


class McpTool(ABC):
    """单个工具：入参出参均由 Pydantic 强制校验（PRD 要求所有输出结构化）。"""

    name: str
    description: str
    input_model: type[BaseModel]

    def spec(self) -> McpToolSpec:
        return McpToolSpec(
            name=self.name,
            description=self.description,
            inputSchema=self.input_model.model_json_schema(),
        )

    async def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            validated = self.input_model.model_validate(arguments)
        except ValidationError as exc:
            raise ToolError(INVALID_PARAMS_CODE, f"入参校验失败: {exc.errors()[:3]}") from exc
        return await self.execute(validated)

    @abstractmethod
    async def execute(self, args: BaseModel) -> dict[str, Any]:
        """真正执行；返回 JSON 可序列化 dict。"""


INVALID_PARAMS_CODE = "INVALID_PARAMS"  # 入参非法（非业务错误码，客户端不重试）


class FunctionTool(McpTool):
    """用函数快速注册工具（服务端内部使用，避免为每个工具写样板类）。"""

    def __init__(
        self,
        name: str,
        description: str,
        input_model: type[BaseModel],
        handler: Any,
    ) -> None:
        self.name = name
        self.description = description
        self.input_model = input_model
        self._handler = handler

    async def execute(self, args: BaseModel) -> dict[str, Any]:
        result = self._handler(args)
        if asyncio.iscoroutine(result):
            result = await result
        return result


# ---------------------------------------------------------------------------
# 服务端
# ---------------------------------------------------------------------------
class MCPServerApp:
    """一个 MCP Server 实例（4 个 server 各自持有一个）。"""

    def __init__(self, name: str, tools: list[McpTool], version: str = MCP_SERVER_VERSION) -> None:
        self.name = name
        self.version = version
        self._tools: dict[str, McpTool] = {tool.name: tool for tool in tools}

    @property
    def tools(self) -> list[McpTool]:
        return list(self._tools.values())

    async def handle(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        """处理一条 JSON-RPC 消息；返回 None 表示通知、无需回包。"""
        try:
            request = JsonRpcRequest.model_validate(raw)
        except ValidationError as exc:
            return {
                "jsonrpc": "2.0",
                "id": raw.get("id") if isinstance(raw, dict) else None,
                "error": {"code": INVALID_REQUEST, "message": f"非法请求: {exc.errors()[:3]}"},
            }

        try:
            result = await self._dispatch(request)
        except _MethodNotFound as exc:
            # 未实现的方法（如 resources/list）明确回 -32601，客户端据此判断能力边界
            return {
                "jsonrpc": "2.0",
                "id": request.id,
                "error": {"code": METHOD_NOT_FOUND, "message": f"W2 仅支持 initialize/tools/list/tools/call，未实现 {exc.method}"},
            }
        except _InvalidParams as exc:
            return {
                "jsonrpc": "2.0",
                "id": request.id,
                "error": {"code": INVALID_PARAMS, "message": str(exc)},
            }
        except ToolError as exc:  # 兜底（正常情况下工具错误已在 tools/call 内转为 isError）
            return {"jsonrpc": "2.0", "id": request.id, "error": {"code": INTERNAL_ERROR, "message": str(exc)}}
        except Exception as exc:  # noqa: BLE001
            logger.exception("mcp_internal_error", extra={"server": self.name, "method": request.method})
            return {
                "jsonrpc": "2.0",
                "id": request.id,
                "error": {"code": INTERNAL_ERROR, "message": f"服务端异常: {exc}"},
            }

        if request.is_notification:
            return None
        return {"jsonrpc": "2.0", "id": request.id, "result": result}

    async def _dispatch(self, request: JsonRpcRequest) -> dict[str, Any]:
        method = request.method

        if method == "initialize":
            return {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "version": self.version},
            }

        if method in {"notifications/initialized", "notifications/cancelled"}:
            return {}

        if method == "ping":
            return {}

        if method == "tools/list":
            return {"tools": [tool.spec().model_dump(by_alias=True) for tool in self._tools.values()]}

        if method == "tools/call":
            return await self._call_tool(request.params)

        raise _MethodNotFound(method)

    async def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name", ""))
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise _InvalidParams("arguments 必须是对象")

        tool = self._tools.get(name)
        if tool is None:
            payload = ToolsCallResult(
                content=[ToolTextContent(text=json.dumps({"code": McpErrorCode.TOOL_NOT_FOUND.value, "message": f"未知工具 {name}"}, ensure_ascii=False))],
                isError=True,
            )
            return payload.model_dump(by_alias=True)

        try:
            output = await tool.run(arguments)
        except ToolError as exc:
            payload = ToolsCallResult(
                content=[
                    ToolTextContent(
                        text=json.dumps(
                            {"code": exc.code, "message": exc.message, "data": exc.data}, ensure_ascii=False
                        )
                    )
                ],
                isError=True,
            )
            logger.warning(
                "mcp_tool_error",
                extra={
                    "server": self.name,
                    "tool": name,
                    "error_code": exc.code,
                    # 注意：不能叫 message，LogRecord 已保留该属性，会抛 KeyError
                    "error_message": exc.message,
                },
            )
            return payload.model_dump(by_alias=True)

        payload = ToolsCallResult(
            content=[ToolTextContent(text=json.dumps(output, ensure_ascii=False, default=str))],
            isError=False,
            structuredContent=output,
        )
        return payload.model_dump(by_alias=True)


class _MethodNotFound(Exception):
    def __init__(self, method: str) -> None:
        self.method = method


class _InvalidParams(Exception):
    pass


async def serve_stdio(app: MCPServerApp, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    """stdio 主循环：逐行读 JSON-RPC，逐行写响应。

    注意：stdout 只允许写协议报文，日志一律走 stderr（logging 默认即 stderr），
    否则会污染协议流导致客户端解析失败。
    """
    reader = stdin or sys.stdin
    writer = stdout or sys.stdout
    write_lock = asyncio.Lock()

    async def write(payload: dict[str, Any]) -> None:
        async with write_lock:
            writer.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            writer.flush()

    logger.info("mcp_server_started", extra={"server": app.name, "transport": "stdio", "tools": [t.name for t in app.tools]})

    while True:
        line = await asyncio.to_thread(reader.readline)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            await write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON 解析失败"}})
            continue

        if isinstance(message, list):  # 批量请求：逐条处理（协议允许，W2 仅串行）
            for item in message:
                response = await app.handle(item)
                if response is not None:
                    await write(response)
            continue

        response = await app.handle(message)
        if response is not None:
            await write(response)

    logger.info("mcp_server_stopped", extra={"server": app.name})


def run_server(app: MCPServerApp) -> None:
    """子进程入口（每个 mcp_servers/*.py 的 main() 调用）。"""
    try:
        asyncio.run(serve_stdio(app))
    except KeyboardInterrupt:  # pragma: no cover
        pass
    except _MethodNotFound as exc:  # pragma: no cover - 由 handle 内部转换
        logger.error("mcp_method_not_found", extra={"method": exc.method})


__all__ = [
    "ToolError",
    "McpTool",
    "FunctionTool",
    "MCPServerApp",
    "serve_stdio",
    "run_server",
    "METHOD_NOT_FOUND",
]
