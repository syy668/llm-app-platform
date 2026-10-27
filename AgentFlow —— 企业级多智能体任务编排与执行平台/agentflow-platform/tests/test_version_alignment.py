"""D-01 回归防线：版本号必须与 CHANGELOG「当前版本」对齐。

背景（缺陷 D-01，P2）：``app/__init__.py`` 长期停留在 W1 骨架的 ``0.1.0-w1``，
导致 ``/``、``/healthz``、``/api/v1/health`` 与 /docs 回显的版本号无法体现真实交付范围。

本文件只做**一致性校验**，不覆盖任何业务逻辑：

1. ``__version__`` 与 CHANGELOG「当前版本」声明一致（CHANGELOG 侧的 ``v`` 前缀与
   ``-wip`` 等文档标记在比较时忽略，避免文档标记变化就误报）；
2. 三个对外端点真实回显该版本，而不是各自硬编码的旧值。

这样后续任何一次发版忘了改版本号，CI 会直接失败，而不是等到交付评审才发现。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from app import __version__

#: CHANGELOG 位于 agentflow-platform/ 下（tests/ -> agentflow-platform/）；
#: 兼容它将来被移到仓库根目录的情况，两处都找一遍。
_ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = next(
    (candidate for candidate in (_ROOT / "CHANGELOG.md", _ROOT.parent / "CHANGELOG.md") if candidate.is_file()),
    _ROOT / "CHANGELOG.md",
)

_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


def _normalize(raw: str) -> str:
    """把版本串规整为 ``X.Y.Z``：去掉 ``v`` 前缀与 ``-wip`` 之类的文档后缀。"""
    text = raw.strip().lstrip("vV")
    match = _VERSION_RE.match(text)
    return match.group(0) if match else text


def _changelog_version() -> str | None:
    """从 CHANGELOG 的「当前版本」行解析版本；文件缺失或没有该行时返回 None。"""
    if not CHANGELOG.is_file():
        return None
    for line in CHANGELOG.read_text(encoding="utf-8").splitlines():
        if "当前版本" not in line:
            continue
        match = re.search(r"`v?([0-9]+(?:\.[0-9]+)+)", line)
        if match:
            return _normalize(match.group(1))
    return None


def test_version_is_semver() -> None:
    """版本号必须可解析为 X.Y.Z，避免再次出现 ``0.1.0-w1`` 这类阶段标签。"""
    assert _VERSION_RE.fullmatch(__version__), f"__version__={__version__!r} 不是 X.Y.Z 语义化版本"


def test_version_matches_changelog() -> None:
    """版本号与 CHANGELOG「当前版本」一致（缺陷 D-01 的直接防线）。"""
    declared = _changelog_version()
    if declared is None:
        pytest.skip("未找到 CHANGELOG.md 或其中没有「当前版本」声明")
    assert _normalize(__version__) == declared, (
        f"版本回显落后：app.__version__={__version__} 但 CHANGELOG 声明 {declared}"
    )


@pytest.mark.asyncio
async def test_health_endpoints_echo_current_version(client: AsyncClient) -> None:
    """对外端点回显的 version 必须等于 ``__version__``，且已体现当前主版本。

    这里**不要**写死前缀（v1.2 时这里硬编码过 ``startswith("1.2")``，
    发版改到 1.3.0 时直接误报 —— 测试自己要防的正是"版本号写死"这类问题）。
    期望前缀从 ``__version__`` 推导，真正守住的是上面那条等值断言：
    端点若各自硬编码旧值，等值断言必红。
    """
    major_minor = ".".join(__version__.split(".")[:2])
    for path in ("/healthz", "/api/v1/health"):
        response = await client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code}"
        body: dict[str, Any] = response.json()
        assert body["version"] == __version__, f"{path} 回显 {body['version']}，与 __version__ 不一致"
        assert body["version"].startswith(major_minor), (
            f"{path} 回显 {body['version']}，未体现当前主版本 v{major_minor}"
        )


@pytest.mark.asyncio
async def test_root_echoes_current_version(client: AsyncClient) -> None:
    """服务信息端点同样回显当前版本。"""
    response = await client.get("/")
    assert response.status_code == 200
    assert response.json()["version"] == __version__
