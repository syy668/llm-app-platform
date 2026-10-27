"""部署清单静态校验（W5-Part2）：没有 Docker 也能挡住明显的编排错误。

为什么需要这个脚本：
- CI runner 上构建镜像成本高、且本机开发环境常常没有 Docker；
- 但「忘了给 mcp-http 加健康探针」「prod 忘了把会话后端切到 redis」「镜像里跑了 root」
  这类问题**完全可以静态发现**，不该等到部署炸了才知道。

检查项（全部为硬断言，任一不满足即 exit 1）：

Dockerfile
  D1 多阶段构建（至少两个 FROM ... AS）
  D2 运行阶段非 root（存在 USER 非 root）
  D3 有 HEALTHCHECK
  D4 入口指向生产装配层 ``app.bootstrap:app``
  D5 运行阶段不安装 build-essential（开发/编译依赖不进运行镜像）

docker-compose.prod.yml
  P1 服务齐全：mysql / redis / migrate / backend / mcp-http
  P2 backend 与 mcp-http 都有 healthcheck
  P3 会话后端为 redis（多副本 SSE 的前提）
  P4 backend / mcp-http 无 container_name（否则无法 --scale）
  P5 迁移与启动分离（migrate 服务存在且 backend 依赖其成功完成）
  P6 端口只用容器端口（不带宿主端口），保证可扩容

docker-compose.dev.yml
  V1 源码挂载 + --reload（热重载）
  V2 默认 memory 会话后端 / OTel 关闭（本地零依赖）

用法：
  python scripts/verify_deploy_manifest.py            # 全绿则输出 DEPLOY MANIFEST OK
  python scripts/verify_deploy_manifest.py --json     # 机器可读
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

try:  # PyYAML 是 requirements-dev 的传递依赖，缺失时降级为正则检查
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


@dataclass
class Result:
    code: str
    title: str
    passed: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "title": self.title, "passed": self.passed, "detail": self.detail}


@dataclass
class Report:
    checks: list[Result] = field(default_factory=list)

    def add(self, code: str, title: str, passed: bool, detail: str = "") -> None:
        self.checks.append(Result(code, title, passed, detail))

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks)


def _read(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _load_yaml(path: Path) -> dict[str, Any]:
    text = _read(path)
    if not text:
        return {}
    if yaml is None:  # pragma: no cover
        return {}
    return yaml.safe_load(text) or {}


# ---------------------------------------------------------------------------
# Dockerfile
# ---------------------------------------------------------------------------
def check_dockerfile(report: Report) -> None:
    path = ROOT / "Dockerfile"
    text = _read(path)
    report.add("D0", "Dockerfile 存在", bool(text), str(path))

    stages = re.findall(r"(?im)^\s*FROM\s+\S+\s+AS\s+(\w+)", text)
    report.add(
        "D1",
        "多阶段构建（builder + runtime）",
        len(stages) >= 2 and "runtime" in stages,
        f"stages={stages}",
    )

    users = re.findall(r"(?im)^\s*USER\s+(\S+)", text)
    non_root = [u for u in users if u.lower() not in {"root", "0"}]
    report.add("D2", "运行阶段以非 root 用户启动", bool(non_root), f"USER={users}")

    report.add("D3", "镜像内置 HEALTHCHECK", bool(re.search(r"(?im)^\s*HEALTHCHECK\b", text)))

    entry = "app.bootstrap:app" in text
    report.add("D4", "入口指向生产装配层 app.bootstrap:app", entry)

    runtime_part = text.split("AS runtime", 1)[1] if "AS runtime" in text else text
    report.add(
        "D5",
        "运行阶段不含编译工具链",
        "build-essential" not in runtime_part,
        "runtime 段出现 build-essential" if "build-essential" in runtime_part else "",
    )

    report.add(
        "D6",
        "观测依赖与运行依赖分文件安装",
        "requirements-observability.txt" in text,
        "requirements.txt 保持 W1 基线不动",
    )


# ---------------------------------------------------------------------------
# compose
# ---------------------------------------------------------------------------
def _resolve(value: Any) -> str:
    """把 ``${VAR:-default}`` 归一化成实际生效的默认值（静态检查口径）。"""
    text = str(value or "").strip()
    match = re.fullmatch(r"\$\{[^:}]+:-([^}]*)\}", text)
    if match:
        return match.group(1).strip().lower()
    return text.lower()


def _service_env(service: dict[str, Any]) -> dict[str, Any]:
    env = service.get("environment") or {}
    if isinstance(env, list):  # "K=V" 形式
        parsed: dict[str, Any] = {}
        for item in env:
            key, _, value = str(item).partition("=")
            parsed[key] = value
        return parsed
    return dict(env)


def check_prod(report: Report) -> None:
    path = ROOT / "docker-compose.prod.yml"
    data = _load_yaml(path)
    services: dict[str, Any] = data.get("services") or {}
    report.add("P0", "docker-compose.prod.yml 可解析", bool(services), str(path))
    if not services:
        return

    required = {"mysql", "redis", "migrate", "backend", "mcp-http"}
    missing = sorted(required - set(services))
    report.add("P1", "服务齐全（mysql/redis/migrate/backend/mcp-http）", not missing, f"missing={missing}")

    for name in ("backend", "mcp-http"):
        service = services.get(name) or {}
        report.add(
            f"P2-{name}",
            f"{name} 具备健康探针",
            bool(service.get("healthcheck")),
            "healthcheck 缺失" if not service.get("healthcheck") else "",
        )

    backend_env = _service_env(services.get("backend") or {})
    mcp_env = _service_env(services.get("mcp-http") or {})
    for label, env in (("backend", backend_env), ("mcp-http", mcp_env)):
        backend_value = _resolve(env.get("SESSION_REGISTRY_BACKEND", ""))
        report.add(
            f"P3-{label}",
            f"{label} 会话后端为 redis",
            backend_value == "redis",
            f"SESSION_REGISTRY_BACKEND={backend_value or '<unset>'}",
        )

    for name in ("backend", "mcp-http"):
        service = services.get(name) or {}
        report.add(
            f"P4-{name}",
            f"{name} 未固定 container_name（可 --scale）",
            "container_name" not in service,
            "存在 container_name，扩容会冲突" if "container_name" in service else "",
        )

    backend_dep = (services.get("backend") or {}).get("depends_on") or {}
    migrate_dep = backend_dep.get("migrate") if isinstance(backend_dep, dict) else None
    report.add(
        "P5",
        "迁移与启动分离（backend 等 migrate 完成）",
        bool(migrate_dep) and str((migrate_dep or {}).get("condition")) == "service_completed_successfully",
        f"depends_on.migrate={migrate_dep}",
    )

    bad_ports: list[str] = []
    for name in ("backend", "mcp-http"):
        for mapping in (services.get(name) or {}).get("ports") or []:
            text = str(mapping)
            # `- "8000"` 只声明容器端口；`"8000:8000"` 会绑定固定宿主端口 → 无法扩容
            if ":" in text:
                bad_ports.append(f"{name}:{text}")
    report.add("P6", "端口仅声明容器端口（可多副本）", not bad_ports, f"fixed_host_ports={bad_ports}")

    collector = services.get("otel-collector") or {}
    has_profile = "observability" in (collector.get("profiles") or [])
    report.add("P7", "OTel Collector 通过 profile 按需启动", has_profile or not collector, "")


def check_dev(report: Report) -> None:
    path = ROOT / "docker-compose.dev.yml"
    data = _load_yaml(path)
    services: dict[str, Any] = data.get("services") or {}
    report.add("V0", "docker-compose.dev.yml 可解析", bool(services), str(path))
    if not services:
        return

    backend = services.get("backend") or {}
    command = " ".join(backend.get("command") or []) if isinstance(backend.get("command"), list) else str(backend.get("command") or "")
    volumes = [str(v) for v in (backend.get("volumes") or [])]
    mounted = any("./app" in v for v in volumes)
    report.add("V1", "开发态挂载源码 + --reload", mounted and "--reload" in command, f"mounted={mounted}")

    env = _service_env(backend)
    report.add(
        "V2",
        "开发态默认零外部依赖（memory 会话 / OTel 关）",
        _resolve(env.get("SESSION_REGISTRY_BACKEND", "")) == "memory" and _resolve(env.get("ENABLE_OTEL", "")) == "false",
        f"registry={env.get('SESSION_REGISTRY_BACKEND')} otel={env.get('ENABLE_OTEL')}",
    )


def check_workflow(report: Report) -> None:
    path = ROOT / ".github" / "workflows" / "ci-test.yml"
    text = _read(path)
    report.add("W0", "CI 工作流存在", bool(text), str(path))
    if not text:
        return

    report.add("W1", "CI 包含 ruff 检查", "ruff check" in text)
    report.add("W2", "CI 覆盖率门禁 ≥85%", "--cov-fail-under=85" in text)
    report.add("W3", "CI 覆盖 W4 校验脚本", "verify_seed.py" in text and "demo_three_flows.py" in text)
    report.add("W4", "CI 含 Docker 构建任务", "docker/build-push-action" in text)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验 W5-Part2 容器化与 CI 清单")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    report = Report()
    check_dockerfile(report)
    check_prod(report)
    check_dev(report)
    check_workflow(report)

    if args.json:
        print(json.dumps({c.code: c.as_dict() for c in report.checks}, ensure_ascii=False, indent=2))
    else:
        width = max(len(c.code) for c in report.checks)
        for check in report.checks:
            mark = "PASS" if check.passed else "FAIL"
            print(f"[{mark}] {check.code:<{width}} {check.title}" + (f"  | {check.detail}" if check.detail else ""))
        total = len(report.checks)
        passed = sum(1 for c in report.checks if c.passed)
        print(f"\n{passed}/{total} 项通过")
        print("DEPLOY MANIFEST OK" if report.ok else "DEPLOY MANIFEST FAILED")

    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
