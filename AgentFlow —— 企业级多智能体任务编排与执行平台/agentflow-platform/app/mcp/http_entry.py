"""单进程「双传输」入口：主应用 + MCP HTTP 端点共存。

用途：本地联调 / Demo —— 同一个进程既能提供 ``/api/v1``，又能让远端 MCP 客户端
通过 ``/mcp/v1`` 连上**同一批** ``MCPServerApp`` 实例；而 stdio 传输仍由
``python -m app.tools.mcp_servers.sql_query`` 单独拉起，两条路互不影响。

生产建议：MCP HTTP 单独起进程（``python -m app.mcp.transport_http --server sql-query``），
避免工具执行与 API 抢同一事件循环——这属于 Part2 容器化编排的范畴。
"""

from __future__ import annotations

import argparse

from fastapi import FastAPI

from app.core.logging import get_logger
from app.core.w5_config import w5
from app.mcp.transport_http import load_all_server_apps, mount_mcp_http

logger = get_logger(__name__)


def create_app_with_mcp_http(*, force: bool | None = None) -> FastAPI:
    """W1 ``create_app()`` + 条件挂载 MCP HTTP 路由（不改 W1 的 ``main.py``）。"""
    from app.main import create_app

    app = create_app()
    enabled = w5.MCP_HTTP_ENABLED if force is None else bool(force)
    if not enabled:
        logger.info("mcp_http_disabled", extra={"hint": "MCP_HTTP_ENABLED=false，仅 stdio 传输"})
        return app

    apps = load_all_server_apps()
    mount_mcp_http(app, apps, primary=None)
    logger.info(
        "mcp_http_mounted",
        extra={"path": w5.MCP_HTTP_PATH, "servers": sorted(apps), "auth": w5.mcp_http_auth_enabled},
    )
    return app


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - 进程入口
    parser = argparse.ArgumentParser(description="AgentFlow 主应用 + MCP HTTP 双传输单进程启动")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    import uvicorn

    uvicorn.run(create_app_with_mcp_http(force=True), host=args.host, port=args.port, log_level="info")


__all__ = ["create_app_with_mcp_http", "main"]
