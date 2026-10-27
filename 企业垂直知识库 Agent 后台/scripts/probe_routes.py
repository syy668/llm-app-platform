"""验证五路路由实际可触发，并记录 route 字段。

用法：./.venv/Scripts/python.exe scripts/probe_routes.py
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8000/api/v1"

CASES = [
    ("规则短路 rag", "如何申请服务器权限？"),
    ("LLM rag", "公司 VPN 账号锁定了怎么办"),
    ("report", "帮我统计一下本周的工单数量"),
    ("db_query", "现在有多少台设备需要更换"),
    ("parallel_compare", "对比研发部和销售部的住宿标准"),
    ("direct", "你好，介绍一下你自己"),
    ("操作词+统计词", "如何统计本周的告警数量"),
]


def call(path: str, payload: dict | None, token: str, method: str = "POST") -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=180) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


def main() -> int:
    token = call("/auth/login", {"username": "admin", "password": "123456"}, "")["data"]["access_token"]
    conv = call("/conversations", {"title": "路由验证临时会话"}, token)["data"]
    conv_id = conv["id"]
    print(f"临时会话 id={conv_id}\n")
    try:
        for tag, q in CASES:
            try:
                data = call(f"/conversations/{conv_id}/chat", {"question": q, "kb_id": 6}, token)["data"]
                ans = (data.get("answer") or "").replace("\n", " ")[:70]
                print(f"[{tag}] {q}\n    answer={ans}")
            except Exception as exc:  # noqa: BLE001
                print(f"[{tag}] {q}\n    ERROR {exc}")

        # agent_route 落在 messages 表上，回查一次拿真实路由
        msgs = call(f"/conversations/{conv_id}/messages", None, token, method="GET")["data"]
        items = msgs.get("items") if isinstance(msgs, dict) else msgs
        print("\n---- messages.agent_route 实测 ----")
        questions = [q for _, q in CASES]
        for msg in items or []:
            if msg.get("role") != "assistant":
                continue
            idx = sum(1 for m in (items or []) if m.get("id", 0) < msg.get("id", 0) and m.get("role") == "user") - 1
            prompt = questions[idx] if 0 <= idx < len(questions) else "?"
            print(f"  route={str(msg.get('agent_route')):18} trace={str(msg.get('trace_id'))[:10]} | {prompt}")
    finally:
        call(f"/conversations/{conv_id}", None, token, method="DELETE")
        print(f"已删除临时会话 {conv_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
