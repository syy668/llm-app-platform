"""tools 层：MCP Client 与 4 个 MCP Server（均为目录占位）。

W1 边界：不实现 JSON-RPC 握手、不实现 tools/list / tools/call、不启动任何 server 进程。
W2 交付：initialize / tools/list / tools/call 三个方法（stdio 优先，PRD 5.3 / R-2）。
"""
