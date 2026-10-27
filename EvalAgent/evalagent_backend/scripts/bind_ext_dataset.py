"""把「外部系统数据集编码」绑定到本中台的一个真实评测集（幂等）。

场景：项目三 AgentFlow 触发评测时，``EvalRunCreateRequest.dataset_id`` 是**字符串语义**
（默认 ``"agentflow-30"``），代表它侧的数据集标识。跨系统集成不能用自增主键绑定
（换库就错位），只能用业务编码 —— 因此本中台约定：``eval_datasets.code`` 就是外部引用键。

本脚本做三件事（全部走 HTTP，不直连数据库，保证和前端/项目三完全同一条路径）：
  1. 若无 ``code=agentflow-30`` 的评测集 → 创建（target_type=agentflow，绑定 kb_id）
  2. 若该集无样本 → 从 ``--source-code`` 指定的现有评测集复制样本（含 stratum 分层）
  3. 打印最终绑定结果，供人工核对

用法：
  .venv/Scripts/python.exe scripts/bind_ext_dataset.py
  .venv/Scripts/python.exe scripts/bind_ext_dataset.py --code agentflow-30 --kb-id 6
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import httpx

BASE = "http://127.0.0.1:8200/api/v1"


def call(method: str, path: str, **kwargs: Any) -> Any:
    url = f"{BASE}{path}"
    with httpx.Client(timeout=60) as client:
        resp = client.request(method, url, **kwargs)
    try:
        body = resp.json()
    except ValueError:
        raise SystemExit(f"{method} {path} 返回非 JSON（HTTP {resp.status_code}）：{resp.text[:300]}")
    if resp.status_code >= 400 or body.get("code") not in (0, None):
        raise SystemExit(f"{method} {path} 失败（HTTP {resp.status_code}）：{body}")
    return body["data"]


def find_by_code(code: str) -> dict[str, Any] | None:
    page = call("GET", "/eval/datasets", params={"page": 1, "size": 200})
    for row in page["items"]:
        if row.get("code") == code:
            return row
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", default="agentflow-30", help="外部系统使用的数据集编码")
    parser.add_argument("--source-code", default="E2E", help="复制样本来源（code 前缀匹配）")
    parser.add_argument("--kb-id", type=int, default=6, help="绑定项目一的知识库 id")
    parser.add_argument("--name", default="AgentFlow 回归评测集（RAG 侧）")
    args = parser.parse_args()

    ds = find_by_code(args.code)
    if ds is not None:
        print(f"[跳过创建] code={args.code} 已存在 → id={ds['id']} name={ds['name']} items={ds['item_count']}")
    else:
        ds = call(
            "POST",
            "/eval/datasets",
            json={
                "name": args.name,
                "code": args.code,
                "description": (
                    "项目三 AgentFlow 触发评测时使用的外部编码绑定集；"
                    "样本取自企业运维知识库真实问答对（含 1 条越界难例）"
                ),
                "target_type": "agentflow",
                "kb_id": args.kb_id,
                "scenario": "运维",
                "tags": ["agentflow", "regression", "external-binding"],
            },
        )
        print(f"[创建] code={args.code} → id={ds['id']} name={ds['name']}")

    if ds["item_count"] > 0:
        print(f"[跳过导入] 该评测集已有 {ds['item_count']} 条样本")
    else:
        page = call("GET", "/eval/datasets", params={"page": 1, "size": 200})
        source = next(
            (r for r in page["items"] if r["id"] != ds["id"] and (r.get("code") or "").startswith(args.source_code)),
            None,
        )
        if source is None:
            print(f"[失败] 找不到样本来源（code 以 {args.source_code} 开头的评测集）", file=sys.stderr)
            return 1
        items_page = call("GET", f"/eval/datasets/{source['id']}/items", params={"page": 1, "size": 200})
        items = [
            {
                "question": it["question"],
                "reference_answer": it["reference_answer"],
                "reference_contexts": it.get("reference_contexts"),
                "stratum": it.get("stratum"),
            }
            for it in items_page["items"]
        ]
        imported = call("POST", f"/eval/datasets/{ds['id']}/items", json={"items": items})
        print(f"[导入] 从 id={source['id']}({source.get('code')}) 复制 {imported['imported']} 条样本")

    final = call("GET", f"/eval/datasets/{ds['id']}")
    print("\n最终绑定：")
    print(f"  dataset_id(主键) = {final['id']}")
    print(f"  dataset_code     = {final['code']}   ← 项目三用它触发评测")
    print(f"  target_type      = {final['target_type']}")
    print(f"  kb_id            = {final['kb_id']}")
    print(f"  item_count       = {final['item_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
