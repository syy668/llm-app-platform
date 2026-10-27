"""W5-Part1 MCP 传输层包。

- ``transport_http``：HTTP（JSON-RPC over HTTP / SSE）第二套传输，与 W2 的 stdio **并存**；
- stdio 实现仍在 ``app.tools.mcp_stdio`` 与 ``app.tools.mcp_client``，本包一行未改。
"""

from __future__ import annotations

__all__: list[str] = []
