#!/usr/bin/env python
"""AgentFlow 演示环境一键启动 / 体检 / 停止（v1.1 任务 4）。

四个必需组件（缺一个演示就不完整）：

| 组件 | 端口 | 作用 | 启动方式 |
|---|---|---|---|
| Redis Stack | 6379 | 事件流 / 断点 / 黑名单 / 限流 / 短期记忆（需 RedisJSON + RediSearch） | Docker 容器 |
| MySQL | 3307 | 任务 / 子任务 / 审批 / 轨迹 / 记忆（8 张表） | 本机 mysqld（数据目录见 README） |
| AgentFlow 后端 | 8000 | 编排 + API + SSE | 本项目 ``.venv`` |
| 项目一「企业垂直知识库」 | 8010 | kb-search 的真实检索数据源 | 项目一自己的 ``.venv`` |

可选：Vue 前端 5173（``agentflow-web``，需要先 ``npm install``）。

用法::

    python scripts/demo_up.py                 # 体检 + 拉起缺失的服务（默认 mock 知识库）
    python scripts/demo_up.py --real          # 走项目一真实检索（MOCK_EXTERNAL_RAG=false）
    python scripts/demo_up.py --status        # 只体检，不启动任何进程
    python scripts/demo_up.py --no-frontend   # 不启动 vite
    python scripts/demo_up.py --down          # 只停本脚本拉起的进程（不碰 MySQL/Redis）

设计约定（避免"脚本把环境搞坏"）：

- **只启动缺失的服务**：端口已在监听就跳过，绝不重复拉起、也绝不 kill 别人的进程；
- MySQL / Redis Stack 只做连通性检查 + 给出启动命令，不代管数据目录（本机数据比脚本金贵）；
- 后端 / 项目一用独立进程组启动，PID 写入 ``storage/_demo_pids.json``，``--down`` 只停这里记录的进程；
- ``--mock/--real`` 通过**子进程环境变量**注入，不改 ``.env``（想持久化请自行改 .env，脚本会打印当前值）。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STORAGE = ROOT / "storage"
PID_FILE = STORAGE / "_demo_pids.json"
BACKEND_LOG = STORAGE / "_uvicorn.log"
PROJECT_ONE_LOG = STORAGE / "_project1.log"
FRONTEND_LOG = STORAGE / "_vite.log"

IS_WINDOWS = platform.system().lower().startswith("win")
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe" if IS_WINDOWS else ROOT / ".venv" / "bin" / "python"
#: 项目一默认位置（与本仓库同级；可用 --project-one 覆盖）
DEFAULT_PROJECT_ONE = ROOT.parent.parent / "企业垂直知识库 Agent 后台"
FRONTEND_DIR = ROOT.parent / "agentflow-web"


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def http_ok(url: str, timeout: float = 3.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - 本机固定地址
            body = resp.read(400).decode("utf-8", "replace")
            return resp.status == 200, body
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__


def say(message: str) -> None:
    print(message, flush=True)


def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(  # noqa: S603 - 命令由本脚本静态拼装
            cmd,
            cwd=str(cwd or ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        # 命令不存在（如本机没装 docker / mysqladmin）不是错误，交给调用方决定降级口径
        return 127, f"command not found: {cmd[0]}"
    except OSError as exc:  # noqa: BLE001
        return 126, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def spawn(name: str, cmd: list[str], *, cwd: Path, log_path: Path, env: dict[str, str]) -> subprocess.Popen[Any]:
    """后台启动一个长驻进程，输出重定向到日志文件。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 - 交给子进程持有
    handle.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} start {name}: {' '.join(cmd)} =====\n")
    handle.flush()
    kwargs: dict[str, Any] = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(  # noqa: S603
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        **kwargs,
    )
    say(f"  → 已启动 {name}（pid={proc.pid}，日志 {log_path.name}）")
    return proc


def load_pids() -> dict[str, int]:
    if not PID_FILE.exists():
        return {}
    try:
        return json.loads(PID_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_pids(pids: dict[str, int]) -> None:
    STORAGE.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(json.dumps(pids, ensure_ascii=False, indent=2), encoding="utf-8")


def pid_alive(pid: int) -> bool:
    if IS_WINDOWS:
        code, out = run(["tasklist", "/FI", f"PID eq {pid}", "/NH"])
        return code == 0 and str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_pid(pid: int) -> None:
    if IS_WINDOWS:
        run(["taskkill", "/PID", str(pid), "/F"])
    else:
        os.kill(pid, 15)


# ---------------------------------------------------------------------------
# 依赖检查
# ---------------------------------------------------------------------------
@dataclass
class Check:
    name: str
    port: int
    ok: bool
    detail: str


def redis_command(*args: str, timeout: float = 2.0) -> bytes:
    """用最小 RESP 协议直连 Redis（不依赖 redis-cli / docker CLI）。"""
    payload = f"*{len(args)}\r\n".encode()
    for arg in args:
        raw = arg.encode()
        payload += f"${len(raw)}\r\n".encode() + raw + b"\r\n"
    with socket.create_connection(("127.0.0.1", 6379), timeout=timeout) as sock:
        sock.sendall(payload)
        return sock.recv(8192)


def check_redis() -> Check:
    if not port_open("127.0.0.1", 6379):
        return Check("Redis Stack", 6379, False, "端口未监听（需要 RedisJSON + RediSearch）")
    try:
        if b"PONG" not in redis_command("PING").upper():
            return Check("Redis Stack", 6379, False, "PING 无响应")
        modules = redis_command("MODULE", "LIST").lower()
        has_search = b"search" in modules
        has_json = b"rejson" in modules or b"json" in modules
        if has_search and has_json:
            return Check("Redis Stack", 6379, True, "PONG + RediSearch + RedisJSON")
        missing = []
        if not has_search:
            missing.append("RediSearch")
        if not has_json:
            missing.append("RedisJSON")
        return Check("Redis Stack", 6379, True, f"PONG，但缺少 {'/'.join(missing)}（断点持久化会降级）")
    except Exception as exc:  # noqa: BLE001
        return Check("Redis Stack", 6379, False, f"协议探测失败：{type(exc).__name__}")


def check_mysql() -> Check:
    if not port_open("127.0.0.1", 3307):
        return Check("MySQL", 3307, False, "端口未监听")
    code, out = run(["mysqladmin", "-h", "127.0.0.1", "-P", "3307", "ping"])
    # mysqladmin 即使认证失败也会返回 0（它只确认 mysqld 活着），这里统一按"端口可达"处理，
    # 真正的库连通性由后端启动日志 + /readyz 判定（脚本不该替用户猜密码）。
    if "access denied" in out.lower():
        return Check("MySQL", 3307, True, "端口可达（未带账号密码，跳过认证探测）")
    if code == 0:
        tail = out.strip().splitlines()[-1] if out.strip() else "alive"
        return Check("MySQL", 3307, True, tail)
    if code == 127:
        return Check("MySQL", 3307, True, "端口可达（未安装 mysqladmin，跳过认证探测）")
    return Check("MySQL", 3307, True, "端口可达")


def check_http(name: str, port: int, url: str) -> Check:
    if not port_open("127.0.0.1", port):
        return Check(name, port, False, "端口未监听")
    ok, detail = http_ok(url)
    return Check(name, port, ok, detail[:80] if detail else ("OK" if ok else "无响应"))


def collect_checks(project_one_dir: Path) -> list[Check]:
    checks = [
        check_redis(),
        check_mysql(),
        check_http("AgentFlow 后端", 8000, "http://127.0.0.1:8000/healthz"),
        check_http("项目一知识库", 8010, "http://127.0.0.1:8010/openapi.json"),
        check_http("Vue 前端", 5173, "http://127.0.0.1:5173/"),
    ]
    if not project_one_dir.exists():
        checks[3] = Check("项目一知识库", 8010, False, f"目录不存在：{project_one_dir}")
    return checks


def print_checks(checks: list[Check]) -> None:
    say("\n组件状态")
    say("-" * 72)
    for item in checks:
        flag = "OK  " if item.ok else "DOWN"
        say(f"  [{flag}] {item.name:<14} :{item.port:<5} {item.detail}")
    say("-" * 72)


# ---------------------------------------------------------------------------
# 启动缺失组件
# ---------------------------------------------------------------------------
def ensure_redis(auto: bool) -> None:
    if port_open("127.0.0.1", 6379):
        say("Redis Stack 已在运行，跳过")
        return
    if not auto:
        say("Redis Stack 未运行（--no-start 模式不代启）")
        return
    code, _ = run(["docker", "version"])
    if code != 0:
        say("Redis Stack 未运行，且本机没有可用的 docker：请先启动 Docker Desktop，或改用 Memurai（功能受限）")
        return
    say("启动 Redis Stack 容器（redis/redis-stack-server，含 RedisJSON + RediSearch）…")
    code, out = run(
        [
            "docker", "run", "-d", "--name", "agentflow-redis-stack",
            "-p", "6379:6379", "redis/redis-stack-server:latest",
        ]
    )
    if code != 0:
        say(f"  docker run 失败：{out.strip()[:300]}")
        say("  提示：容器已存在时用 `docker start agentflow-redis-stack`")
        return
    for _ in range(20):
        if port_open("127.0.0.1", 6379):
            say("  Redis Stack 已就绪")
            return
        time.sleep(0.5)
    say("  Redis Stack 启动超时，请查看 `docker logs agentflow-redis-stack`")


def ensure_mysql() -> None:
    if port_open("127.0.0.1", 3307):
        say("MySQL 已在运行，跳过")
        return
    say("MySQL 未监听 3307。本项目演示用的 mysqld 需要单独启动（脚本不代管数据目录）：")
    say("  mysqld --datadir=C:/Users/Public/agentflow-mysql-data --port=3307 --console")
    say("  若从未初始化过数据目录：mysqld --initialize-insecure --datadir=<目录> 后再启动")


def ensure_backend(*, mock_rag: bool, auto: bool) -> int | None:
    if port_open("127.0.0.1", 8000):
        say("AgentFlow 后端已在运行，跳过")
        return load_pids().get("backend")
    if not auto:
        return None
    if not PYTHON.exists():
        say(f"找不到虚拟环境解释器：{PYTHON}（请先 python -m venv .venv && pip install -r requirements.txt）")
        return None
    env = os.environ.copy()
    # 通过子进程环境变量覆盖 .env：不修改用户配置文件，且仅影响本次进程
    env["MOCK_EXTERNAL_RAG"] = "true" if mock_rag else "false"
    env.setdefault("PYTHONUNBUFFERED", "1")
    say(f"启动 AgentFlow 后端（8000，MOCK_EXTERNAL_RAG={env['MOCK_EXTERNAL_RAG']}）…")
    proc = spawn(
        "backend",
        [str(PYTHON), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=ROOT,
        log_path=BACKEND_LOG,
        env=env,
    )
    pids = load_pids()
    pids["backend"] = proc.pid
    save_pids(pids)
    return proc.pid


def ensure_project_one(project_one_dir: Path, *, auto: bool) -> int | None:
    if port_open("127.0.0.1", 8010):
        say("项目一知识库已在运行（8010），跳过")
        return load_pids().get("project_one")
    if not auto:
        return None
    if not project_one_dir.exists():
        say(f"项目一目录不存在，跳过：{project_one_dir}")
        return None
    python = project_one_dir / (".venv/Scripts/python.exe" if IS_WINDOWS else ".venv/bin/python")
    if not python.exists():
        say(f"项目一缺少虚拟环境：{python}，跳过（cd 项目一 && python -m venv .venv && pip install -r requirements.txt）")
        return None
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    say("启动项目一知识库（8010，不改它的任何代码，仅换端口）…")
    proc = spawn(
        "project_one",
        [str(python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8010"],
        cwd=project_one_dir,
        log_path=PROJECT_ONE_LOG,
        env=env,
    )
    pids = load_pids()
    pids["project_one"] = proc.pid
    save_pids(pids)
    return proc.pid


def ensure_frontend(*, auto: bool) -> int | None:
    if port_open("127.0.0.1", 5173):
        say("Vue 前端已在运行，跳过")
        return load_pids().get("frontend")
    if not auto or not FRONTEND_DIR.exists():
        return None
    vite = FRONTEND_DIR / "node_modules" / "vite" / "bin" / "vite.js"
    if not vite.exists():
        say(f"前端依赖未安装（{vite} 不存在）：cd agentflow-web && npm install")
        return None
    node = "node"
    say("启动 Vue 前端（5173）…")
    proc = spawn(
        "frontend",
        [node, str(vite), "--host", "127.0.0.1", "--port", "5173"],
        cwd=FRONTEND_DIR,
        log_path=FRONTEND_LOG,
        env=os.environ.copy(),
    )
    pids = load_pids()
    pids["frontend"] = proc.pid
    save_pids(pids)
    return proc.pid


def wait_ready(url: str, *, seconds: int = 60, label: str = "") -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        ok, _ = http_ok(url, timeout=2)
        if ok:
            return True
        time.sleep(1)
    say(f"  {label or url} 在 {seconds}s 内未就绪，请查看 storage/_*.log")
    return False


# ---------------------------------------------------------------------------
# 停止
# ---------------------------------------------------------------------------
def stop_started() -> None:
    pids = load_pids()
    if not pids:
        say("没有本脚本记录的进程（storage/_demo_pids.json 不存在或为空）")
        return
    for name, pid in pids.items():
        if pid and pid_alive(int(pid)):
            say(f"停止 {name}（pid={pid}）")
            kill_pid(int(pid))
        else:
            say(f"{name}（pid={pid}）已不在运行")
    save_pids({})


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="AgentFlow 演示环境一键启动 / 体检 / 停止")
    parser.add_argument("--status", action="store_true", help="只体检，不启动任何进程")
    parser.add_argument("--down", action="store_true", help="停止本脚本拉起的进程（不碰 MySQL/Redis）")
    parser.add_argument("--real", action="store_true", help="走项目一真实检索（MOCK_EXTERNAL_RAG=false）")
    parser.add_argument("--mock", action="store_true", help="走内置周报语料（MOCK_EXTERNAL_RAG=true，默认）")
    parser.add_argument("--no-frontend", action="store_true", help="不启动 Vue 前端")
    parser.add_argument("--no-start", action="store_true", help="不代启任何进程，只体检并给出启动指引")
    parser.add_argument(
        "--project-one",
        default=str(DEFAULT_PROJECT_ONE),
        help=f"项目一目录（默认 {DEFAULT_PROJECT_ONE}）",
    )
    args = parser.parse_args()

    if args.down:
        stop_started()
        return 0

    project_one_dir = Path(args.project_one).resolve()
    mock_rag = not args.real
    if args.mock and args.real:
        say("--mock 与 --real 不能同时使用")
        return 2

    say("=" * 72)
    say("AgentFlow 演示环境")
    say(f"  项目根目录 : {ROOT}")
    say(f"  项目一目录 : {project_one_dir}")
    say(f"  知识库模式 : {'MOCK（内置周报语料，离线可跑）' if mock_rag else 'REAL（项目一 8010 真实检索）'}")
    say(f"  Python     : {PYTHON}")
    say("=" * 72)

    if args.status:
        print_checks(collect_checks(project_one_dir))
        return 0

    auto = not args.no_start
    ensure_redis(auto)
    ensure_mysql()
    ensure_project_one(project_one_dir, auto=auto)
    ensure_backend(mock_rag=mock_rag, auto=auto)
    if not args.no_frontend:
        ensure_frontend(auto=auto)

    # 等两个 HTTP 服务就绪（容器/进程启动有延迟）
    if auto:
        wait_ready("http://127.0.0.1:8010/openapi.json", seconds=60, label="项目一")
        wait_ready("http://127.0.0.1:8000/healthz", seconds=60, label="AgentFlow 后端")

    checks = collect_checks(project_one_dir)
    print_checks(checks)

    say("\n演示入口")
    say("  API 文档      http://127.0.0.1:8000/docs")
    say("  前端          http://127.0.0.1:5173/")
    say("  知识库自检    GET http://127.0.0.1:8000/api/v1/kb/status?probe=true （需 admin/approver 令牌）")
    say("  工具注册表    GET http://127.0.0.1:8000/api/v1/tools")
    say("\nMOCK 开关说明")
    say("  本次后端进程 MOCK_EXTERNAL_RAG=" + ("true（内置语料）" if mock_rag else "false（项目一真实检索）"))
    say("  该值通过子进程环境变量注入，未修改 .env；如需持久生效请改 .env 后重启后端。")
    say("  真实检索模式下必须保证项目一 8010 在线，否则 kb-search 会走「熔断 → mall 兜底」。")
    say("\n常用命令")
    say("  python scripts/demo_up.py --status    # 只体检")
    say("  python scripts/demo_up.py --real      # 切真实检索重启")
    say("  python scripts/demo_up.py --down      # 停掉本脚本拉起的进程")
    return 0 if all(item.ok for item in checks[:4]) else 1


if __name__ == "__main__":
    sys.exit(main())
