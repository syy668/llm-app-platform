"""MCP 协议与 4 个工具服务的入参 / 出参 Schema（PRD FR-005 ~ FR-008）。

协议范围（硬性约束 3）：仅实现 JSON-RPC 2.0 的 ``initialize`` / ``tools/list`` /
``tools/call`` 三个方法，传输仅 stdio；HTTP 传输属 P2，不在本阶段。

服务端返回统一包一层 MCP ``content`` 结构（text 内为 JSON 字符串），
便于未来直连真实 MCP 客户端时保持兼容。
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_SERVER_VERSION = "0.2.0"

# JSON-RPC 2.0 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

JsonRpcId = int | str | None


class McpErrorCode(str, enum.Enum):
    """工具层业务错误码（PRD 3.2 各表「错误码」列）。"""

    # sql-query
    SQL_SYNTAX = "SQL_SYNTAX"
    SQL_TIMEOUT = "SQL_TIMEOUT"
    SQL_TOO_LARGE = "SQL_TOO_LARGE"
    DB_UNAVAILABLE = "DB_UNAVAILABLE"
    # chart-render
    CHART_DATA_INVALID = "CHART_DATA_INVALID"
    RENDER_TIMEOUT = "RENDER_TIMEOUT"
    RENDER_FAILED = "RENDER_FAILED"
    # kb-search
    KB_TIMEOUT = "KB_TIMEOUT"
    KB_UNAVAILABLE = "KB_UNAVAILABLE"
    KB_AUTH_FAILED = "KB_AUTH_FAILED"
    # notify
    APPROVAL_MISSING = "APPROVAL_MISSING"
    NOTIFY_INVALID = "NOTIFY_INVALID"
    NOTIFY_FAILED = "NOTIFY_FAILED"
    # 客户端 / 执行器
    TOOL_LOOP_LIMIT = "TOOL_LOOP_LIMIT"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    MCP_PROTOCOL_ERROR = "MCP_PROTOCOL_ERROR"
    MCP_TIMEOUT = "MCP_TIMEOUT"
    TOOL_CIRCUIT_OPEN = "TOOL_CIRCUIT_OPEN"  # 加固任务 2：连续失败触发熔断，调用被快速拒绝


# 可重试错误码（PRD 4.2 可靠性矩阵）
RETRYABLE_ERRORS: frozenset[str] = frozenset(
    {
        McpErrorCode.SQL_TIMEOUT.value,
        McpErrorCode.DB_UNAVAILABLE.value,
        McpErrorCode.KB_TIMEOUT.value,
        McpErrorCode.KB_UNAVAILABLE.value,
        McpErrorCode.MCP_TIMEOUT.value,
        McpErrorCode.MCP_PROTOCOL_ERROR.value,
    }
)
# 确定性错误（重试 0 次，直接把错误信息回喂改写）
DETERMINISTIC_ERRORS: frozenset[str] = frozenset(
    {McpErrorCode.SQL_SYNTAX.value, McpErrorCode.SQL_TOO_LARGE.value, McpErrorCode.CHART_DATA_INVALID.value}
)


class McpModel(BaseModel):
    """MCP 数据模型基类：忽略未知字段，保证向前兼容（协议升级不炸老客户端）。"""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=False, populate_by_name=True)


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 信封
# ---------------------------------------------------------------------------
class JsonRpcRequest(McpModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: JsonRpcId = None
    method: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_notification(self) -> bool:
        """无 id 即通知（如 notifications/initialized），服务端不回包。"""
        return self.id is None


class JsonRpcError(McpModel):
    code: int
    message: str
    data: dict[str, Any] | None = None


class JsonRpcResponse(McpModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: JsonRpcId = None
    result: dict[str, Any] | None = None
    error: JsonRpcError | None = None

    @classmethod
    def ok(cls, request_id: JsonRpcId, result: dict[str, Any]) -> "JsonRpcResponse":
        return cls(id=request_id, result=result)

    @classmethod
    def fail(
        cls, request_id: JsonRpcId, code: int, message: str, data: dict[str, Any] | None = None
    ) -> "JsonRpcResponse":
        return cls(id=request_id, error=JsonRpcError(code=code, message=message, data=data))


# ---------------------------------------------------------------------------
# initialize / tools/list / tools/call
# ---------------------------------------------------------------------------
class McpToolSpec(McpModel):
    name: str
    description: str = ""
    inputSchema: dict[str, Any] = Field(default_factory=dict)  # noqa: N815 - 协议字段名固定


class InitializeResult(McpModel):
    protocolVersion: str = MCP_PROTOCOL_VERSION  # noqa: N815
    capabilities: dict[str, Any] = Field(default_factory=lambda: {"tools": {"listChanged": False}})
    serverInfo: dict[str, str] = Field(default_factory=dict)  # noqa: N815


class ToolTextContent(McpModel):
    type: Literal["text"] = "text"
    text: str


class ToolsCallResult(McpModel):
    """tools/call 的 result：content 数组 + isError 标记（MCP 标准语义）。"""

    content: list[ToolTextContent] = Field(default_factory=list)
    isError: bool = False  # noqa: N815
    structuredContent: dict[str, Any] | None = None  # noqa: N815


# ---------------------------------------------------------------------------
# sql-query（FR-005）
# ---------------------------------------------------------------------------
class SqlQueryInput(McpModel):
    sql: str = Field(min_length=1, max_length=4000)
    max_rows: int = Field(default=200, ge=1, le=1000)


class SqlQueryOutput(McpModel):
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    exec_ms: int = 0
    scanned_rows: int | None = None
    applied_limit: int | None = None


# ---------------------------------------------------------------------------
# chart-render（FR-006）
# ---------------------------------------------------------------------------
class ChartSize(str, enum.Enum):
    S = "s"
    M = "m"
    L = "l"


class ChartRow(McpModel):
    """图表数据行：字段名 -> 值，允许任意键（受白名单字符校验约束）。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class ChartRenderInput(McpModel):
    chart_type: Literal["line", "bar", "pie"] = "bar"
    title: str = Field(default="", max_length=100)
    x_field: str = Field(min_length=1, max_length=64)
    y_fields: list[str] = Field(min_length=1, max_length=5)
    data: list[dict[str, Any]] = Field(default_factory=list, max_length=2000)
    size: ChartSize = ChartSize.M

    @model_validator(mode="after")
    def _check_fields(self) -> "ChartRenderInput":
        if len(self.y_fields) > 5:
            raise ValueError("y_fields 最多 5 个")
        return self


class ChartRenderOutput(McpModel):
    chart_id: str
    image_base64: str = ""
    image_url: str | None = None
    echarts_option: dict[str, Any] = Field(default_factory=dict)
    render_ms: int = 0
    truncated: bool = False
    rows_used: int = 0


# ---------------------------------------------------------------------------
# kb-search（FR-007）
# ---------------------------------------------------------------------------
class KbChunk(McpModel):
    doc_id: str
    title: str = ""
    content: str = Field(default="", max_length=1200)
    score: float = 0.0
    source: str = "kb"


class KbSearchInput(McpModel):
    query: str = Field(min_length=1, max_length=200)
    top_k: int = Field(default=3, ge=1, le=5)
    tenant: str = Field(default="agentflow", max_length=64)


class KbSearchOutput(McpModel):
    chunks: list[KbChunk] = Field(default_factory=list)
    latency_ms: int = 0
    degraded: bool = False
    error_code: str | None = None


# ---------------------------------------------------------------------------
# notify（FR-008，恒 Mock）
# ---------------------------------------------------------------------------
class NotifyChannel(str, enum.Enum):
    FEISHU = "feishu"
    EMAIL = "email"


class NotifyInput(McpModel):
    channel: NotifyChannel = NotifyChannel.FEISHU
    title: str = Field(min_length=1, max_length=50)
    content: str = Field(min_length=1, max_length=2000)
    receivers: list[str] = Field(default_factory=list, max_length=10)
    # 以下两项由系统注入（非模型生成），用于审批前置校验
    task_id: int | None = None
    approval_id: int | None = None


class NotifyStatus(str, enum.Enum):
    SENT = "SENT"
    QUEUED = "QUEUED"
    FAILED = "FAILED"


class NotifyOutput(McpModel):
    message_id: str
    status: NotifyStatus = NotifyStatus.SENT
    mock: bool = True
    sent_at: datetime | None = None


__all__ = [
    "MCP_PROTOCOL_VERSION",
    "MCP_SERVER_VERSION",
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
    "McpErrorCode",
    "RETRYABLE_ERRORS",
    "DETERMINISTIC_ERRORS",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "JsonRpcError",
    "McpToolSpec",
    "InitializeResult",
    "ToolsCallResult",
    "ToolTextContent",
    "SqlQueryInput",
    "SqlQueryOutput",
    "ChartRenderInput",
    "ChartRenderOutput",
    "ChartSize",
    "KbSearchInput",
    "KbSearchOutput",
    "KbChunk",
    "NotifyInput",
    "NotifyOutput",
    "NotifyChannel",
    "NotifyStatus",
]
