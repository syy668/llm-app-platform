"""任务 17：stdio 大响应行修复。

生产实锤（任务 44 / AF20261025-0019）：分周趋势图 56 行×多列的 JSON-RPC 响应单行
超过 asyncio 管道默认 64KB 行限制，stdout.readline() 抛
"Separator is found, but chunk is longer than limit" → 子任务 EXECUTOR_ERROR。
修复：create_subprocess_exec 显式传 limit=4MB；行超限兜底映射为 MCP_PROTOCOL_ERROR
（可被健康哨兵识别重启），不再以裸 ValueError 冒泡。
"""

from __future__ import annotations

import json
import sys

import pytest

from app.tools.mcp_client import MCPTransportError, StdioTransport


def _big_response_code() -> str:
    """子进程脚本：输出一条 result 超过 64KB 的合法 JSON-RPC 响应行。"""
    big_text = "x" * 200_000
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": big_text}]}},
        ensure_ascii=False,
    )
    return (
        "import sys, time\n"
        f"sys.stdout.write({payload!r} + '\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(5)\n"
    )


async def test_stdio_transport_reads_large_response_line(tmp_path) -> None:
    # Windows 命令行长度有限，200KB 内联脚本会炸 WinError 206 → 先落临时文件
    script = tmp_path / "big_line_child.py"
    script.write_text(_big_response_code(), encoding="utf-8")
    transport = StdioTransport(
        [sys.executable, str(script)],
        server_name="big-line-test",
        cwd=".",
    )
    try:
        await transport.start()
        resp = await transport.request("tools/call", {}, timeout=10.0)
        text = resp["result"]["content"][0]["text"]
        assert len(text) == 200_000
    finally:
        await transport.close()


async def test_oversized_line_maps_to_protocol_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """行超限不再裸抛 ValueError，而是 MCP_PROTOCOL_ERROR（哨兵可识别重启）。"""
    transport = StdioTransport([sys.executable, "-c", "import time; time.sleep(5)"], cwd=".")
    await transport.start()
    try:
        async def fake_readline() -> bytes:
            raise ValueError("Separator is found, but chunk is longer than limit")

        assert transport._proc is not None and transport._proc.stdout is not None
        monkeypatch.setattr(transport._proc.stdout, "readline", fake_readline)
        with pytest.raises(MCPTransportError) as ei:
            await transport.request("tools/call", {}, timeout=5.0)
        assert "行超限" in str(ei.value)
    finally:
        await transport.close()
