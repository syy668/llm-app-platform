"""批量跑一组检索验证题，输出命中来源，用于生成测试用例清单的"实测命中"列。

用法：./.venv/Scripts/python.exe scripts/probe_test_questions.py
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8000/api/v1"
KB_ID = 6
USER = ("admin", "123456")

QUESTIONS = [
    "研发部员工去成都出差，住宿上限是多少？谁审批？",
    "员工出差的住宿标准是多少钱一晚？",
    "VPN 连接不上怎么排查？",
    "打印机卡纸怎么办？",
    "集团整体退货率是多少？",
    "抖音平台 3C 数码品类这次大促的毛利额是多少？",
    "一级故障多久响应、多久上报、RTO 是多少？",
    "年假有几天？司龄满 5 年呢？",
    "公司年会预算是多少？",
    "对比研发部和销售部的住宿标准",
]


def post(path: str, payload: dict, token: str | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


def main() -> int:
    login = post("/auth/login", {"username": USER[0], "password": USER[1]})
    token = login["data"]["access_token"]

    for idx, q in enumerate(QUESTIONS, 1):
        try:
            result = post(f"/kbs/{KB_ID}/query", {"question": q}, token)
        except Exception as exc:  # noqa: BLE001
            print(f"[{idx}] {q}\n    ERROR {exc}\n")
            continue
        data = result.get("data") or {}
        route = data.get("route")
        answer = (data.get("answer") or "").replace("\n", " ")[:80]
        print(f"[{idx}] {q}")
        print(f"    route={route} answer={answer}")
        for rank, src in enumerate((data.get("sources") or [])[:5], 1):
            body = (src.get("content") or "").replace("\n", " ")
            print(
                f"    {rank}. {src.get('score'):.4f} {src.get('document_title')}"
                f" 块{src.get('chunk_index')} | {body[:70]}"
            )
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
