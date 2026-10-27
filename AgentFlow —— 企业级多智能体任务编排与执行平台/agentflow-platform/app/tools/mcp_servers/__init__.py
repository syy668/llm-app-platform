"""4 个 MCP Server 的占位包（sql-query / chart-render / kb-search / notify）。

W1 边界：**不实现任何 server 业务逻辑**，docker-compose 也不启动这些服务。
W2 交付；每个 server 独立进程入口，stdio 传输优先（PRD FR-005 ~ FR-008）。
"""
