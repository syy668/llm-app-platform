#!/usr/bin/env python
"""本地一键联调：同进程同时起后端 API（8000）与前端静态页（5173）。

为什么不是 ``uvicorn app.main:app``
-----------------------------------
``app.main:app`` 直连 MySQL + Redis，本机没有这两个服务就登录不了、任务也跑不动。
本脚本复用 ``scripts/w4_common.py`` 里已经被 W4 三条端到端链路验证过的 ``EmbeddedEnv``：

    SQLite(WAL) 替代 MySQL · fakeredis 替代 Redis · 进程内 MCP 替代 stdio 子进程 · Mock LLM

因此本地不装任何中间件也能跑通「提交任务 → SSE 实时进度 → 人工审批 → 轨迹回放」全流程。
差异只在于存储与模型被替换，**业务代码一行未改**，路由、依赖注入、SSE 协议与生产完全同源。

用法
----
    ./.venv/Scripts/python.exe scripts/dev_serve.py              # 前后端一起起
    ./.venv/Scripts/python.exe scripts/dev_serve.py --backend-only
    ./.venv/Scripts/python.exe scripts/dev_serve.py --reset       # 重建干净的演示库

账号：admin_user / approver_user / viewer_user，密码统一 ``Passw0rd!``
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 必须在 import app.* 之前：w4_common 在模块级调用 bootstrap_env()，
# 一次性铺好 APP_ENV / MOCK_* / LLM_MOCK / CHECKPOINTER_BACKEND 等离线开关。
import w4_common  # noqa: E402

DEFAULT_BACKEND_PORT = 8000
DEFAULT_FRONTEND_PORT = 5173


# ---------------------------------------------------------------------------
# 前端静态服务（无构建链：index.html + ES module）
# ---------------------------------------------------------------------------
class _SpaHandler(SimpleHTTPRequestHandler):
    """静态文件服务；命中不到文件时回落 index.html，避免刷新子路径 404。"""

    server_version = "AgentFlowDevUI/1.0"

    def send_head(self):  # noqa: D102 - 覆盖父类
        raw_path = unquote(urlparse(self.path).path)
        target = Path(self.translate_path(self.path))
        if raw_path not in ("/", "") and not target.exists():
            self.path = "/index.html"
        return super().send_head()

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003 - 父类签名
        # 静默静态资源访问日志，只保留错误，避免刷屏盖住后端日志
        if str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write("[web] %s %s\n" % (self.address_string(), fmt % args))


def _port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def start_frontend(port: int, directory: Path) -> ThreadingHTTPServer:
    handler = lambda *a, **kw: _SpaHandler(*a, directory=str(directory), **kw)  # noqa: E731
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="devui", daemon=True).start()
    return httpd


def _mount_static(app: object) -> None:
    """把 storage/ 挂到 /static，使 chart-render 给出的相对 URL 可直达。"""
    from fastapi.staticfiles import StaticFiles

    static_dir = PROJECT_ROOT / "storage"
    if not static_dir.is_dir():
        return
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="dev-static")  # type: ignore[attr-defined]
    print(f"[dev] 已挂载 /static -> {static_dir.relative_to(PROJECT_ROOT)}")


def _bind_embedded_infra(env: object) -> None:
    """把进程级基础设施单例指向嵌入式实例（仅本演示进程生效）。

    为什么要做这件事：``/readyz`` 的 ``check_db()`` / ``ping_redis()`` **不走依赖注入**，
    而是直接读模块级单例（``app.db.session._engine`` / ``app.core.redis_client._client``）。
    ``EmbeddedEnv`` 只 override 了 ``get_db`` / ``get_redis`` 这两个「每请求依赖」，
    单例本身仍是生产配置的 MySQL / Redis，于是探针去连根本不存在的依赖，恒报未就绪 ——
    探测对象与「本进程实际在用的存储」不一致，属于装配遗漏而非真实故障。

    把单例改指 SQLite / fakeredis 后，探针报告的就是真实状态；顺带修掉
    ``get_shared_redis()`` 直接调用方（resilience_guard / layered_memory / cancel_service
    的兜底路径）在嵌入式下误连生产 Redis 的隐患。

    生产路径完全不经过本函数，``app/`` 下业务代码零改动。
    """
    from app.core import redis_client as redis_module
    from app.db import session as db_session_module

    db_session_module._engine = env.engine  # noqa: SLF001 - 演示进程内的单例重绑
    db_session_module._session_factory = env.session_factory  # noqa: SLF001
    redis_module._client = env.redis  # noqa: SLF001
    print("[dev] 已将 db/redis 单例绑定到嵌入式实例（/readyz 将反映真实可用性）")


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
async def serve(args: argparse.Namespace) -> None:
    import uvicorn

    db_path = Path(args.db)
    if args.reset and db_path.exists():
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(db_path) + suffix)
            if candidate.exists():
                candidate.unlink()
        print(f"[dev] 已清空演示库 {db_path.name}")

    env = w4_common.EmbeddedEnv(db_path=db_path, llm_mock=True)
    await env.start()
    # 必须在 server.serve() 之前：lifespan startup 会 ping redis，此时单例应已指向 fakeredis
    _bind_embedded_infra(env)
    print("[dev] 嵌入式环境就绪：" + ", ".join(f"{k}={v}" for k, v in env.infra_summary.items()))

    # chart-render 产出的 PNG 落在 storage/charts/，业务侧只给出 /static/charts/xxx.png 这样的相对 URL。
    # 生产由网关/nginx 承接该前缀；本地演示在此挂载，避免点图表 404（仅作用于本进程，不改业务路由）。
    _mount_static(env.app)

    httpd: ThreadingHTTPServer | None = None
    if not args.backend_only:
        httpd = start_frontend(args.frontend_port, PROJECT_ROOT / "frontend")

    print("")
    print("  后端 API   http://127.0.0.1:%d" % args.backend_port)
    print("  接口文档   http://127.0.0.1:%d/docs" % args.backend_port)
    if httpd is not None:
        print("  前端控制台 http://127.0.0.1:%d" % args.frontend_port)
    print("  演示账号   admin_user / approver_user / viewer_user   密码 Passw0rd!")
    print("  停止服务   Ctrl+C")
    print("")

    config = uvicorn.Config(
        env.app,
        host="127.0.0.1",
        port=args.backend_port,
        log_level="info",
        access_log=not args.quiet,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        if httpd is not None:
            httpd.shutdown()
        await env.aclose()
        print("[dev] 已停止，资源已释放")


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentFlow 本地一键联调（后端 + 前端）")
    parser.add_argument("--backend-port", type=int, default=DEFAULT_BACKEND_PORT)
    parser.add_argument("--frontend-port", type=int, default=DEFAULT_FRONTEND_PORT)
    parser.add_argument("--backend-only", action="store_true", help="只起 API，不起前端静态服务")
    parser.add_argument("--reset", action="store_true", help="启动前清空演示库（干净数据）")
    parser.add_argument("--quiet", action="store_true", help="关闭访问日志")
    parser.add_argument(
        "--db",
        default=str(PROJECT_ROOT / "storage" / "_dev_serve.db"),
        help="SQLite 演示库路径",
    )
    args = parser.parse_args()

    for port, name in ((args.backend_port, "后端"), (args.frontend_port, "前端")):
        if args.backend_only and name == "前端":
            continue
        if not _port_free(port):
            print(f"[dev] 端口 {port}（{name}）已被占用，请换端口或先停掉占用进程", file=sys.stderr)
            return 2

    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
