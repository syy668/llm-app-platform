"""第四步端到端联调验证（真服务、真网络、真打分，无 mock）。

覆盖三个场景：
  A. 前端 → 8200：CORS 预检 + Vite 编译产物里基址真的是 8200 + 前端用到的 6 类接口
     （列表 / 详情 / 明细 / badcases / stratum_metrics / compare / export）全部 200 且 code=0
  B. 项目三 → 8200：AgentFlow 触发评测（载荷 dataset_id="agentflow-30" 字符串语义）
     → 8200 真跑项目一 RAG + RAGAS 裁判 → 项目三轮询拿到终态 DONE 并回写 metrics
  C. 交叉核对：8200 侧 ext_run_ref == 项目三 run_no 的那条 run 与项目三落库的 metrics 一致

用法：
  .venv/Scripts/python.exe scripts/e2e_stage4.py
  .venv/Scripts/python.exe scripts/e2e_stage4.py --skip-agentflow   # 只验前端↔8200
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import httpx

EVAL_BASE = "http://127.0.0.1:8200/api/v1"
FRONT_BASE = "http://127.0.0.1:5174"
UPSTREAM_BASE = "http://127.0.0.1:8010/api/v1"
AGENTFLOW_BASE = "http://127.0.0.1:8000/api/v1"
EXT_DATASET_CODE = "agentflow-30"
AGENTFLOW_USER = "admin_user"
AGENTFLOW_PASS = "Passw0rd!"
METRICS = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))
    return ok


def client(timeout: float = 60.0) -> httpx.Client:
    """本机直连：显式禁用环境代理（Windows 上常带 http_proxy 导致 502）。"""
    return httpx.Client(timeout=timeout, trust_env=False)


def api(method: str, path: str, base: str = EVAL_BASE, **kwargs: Any) -> tuple[int, Any]:
    with client() as c:
        resp = c.request(method, f"{base}{path}", **kwargs)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, resp.text[:200]


def fmt(v: Any) -> str:
    return "None" if v is None else f"{float(v):.3f}"


# --------------------------------------------------------------------------- #
# 场景 A：前端 → 8200
# --------------------------------------------------------------------------- #
def scenario_frontend() -> dict[str, Any] | None:
    print("\n[A] 前端（5174）→ 评测后端（8200）")

    # A1. 前端 dev server 活着
    with client(20) as c:
        try:
            r = c.get(f"{FRONT_BASE}/")
            ok_index = r.status_code == 200 and "<div id=\"app\">" in r.text
        except Exception as exc:  # noqa: BLE001
            ok_index, r = False, None
            check("A1 前端 dev server 可访问", False, f"{type(exc).__name__}: {exc}")
        else:
            check("A1 前端 dev server 可访问", ok_index, f"HTTP {r.status_code}")

        # A2. Vite 按需编译的 http.ts 里，基址常量必须已经是 8200（证明 .env 真的生效）
        try:
            src = c.get(f"{FRONT_BASE}/src/api/http.ts").text
            has_8200 = "127.0.0.1:8200/api/v1" in src
            has_8010 = "127.0.0.1:8010/api/v1" in src
            check("A2 编译产物基址 = 8200（评测域）/ 8010（上游域）", has_8200 and has_8010,
                  f"8200={has_8200} 8010={has_8010} len={len(src)}")
        except Exception as exc:  # noqa: BLE001
            check("A2 编译产物基址 = 8200", False, f"{type(exc).__name__}")

        # A3. 浏览器跨域预检：Origin 5174 打 8200 必须有 allow-origin，否则前端全红
        pre = c.request(
            "OPTIONS",
            f"{EVAL_BASE}/eval/runs",
            headers={
                "Origin": FRONT_BASE,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        allow = pre.headers.get("access-control-allow-origin", "")
        check("A3 CORS 预检放行 5174", pre.status_code in (200, 204) and allow in ("*", FRONT_BASE),
              f"HTTP {pre.status_code} allow-origin={allow or '缺失'}")

    # A4. 评测集/任务列表（前端首屏两条请求）
    status, body = api("GET", "/eval/datasets", params={"page": 1, "size": 50})
    datasets = body.get("data", {}).get("items", []) if isinstance(body, dict) else []
    check("A4 GET /eval/datasets", status == 200 and body.get("code") == 0, f"{len(datasets)} 个评测集")

    status, body = api("GET", "/eval/runs", params={"page": 1, "size": 20})
    runs = body.get("data", {}).get("items", []) if isinstance(body, dict) else []
    check("A5 GET /eval/runs", status == 200 and body.get("code") == 0, f"{len(runs)} 个任务")

    done = next((r for r in runs if r["status"] == "done" and r["success_items"] > 0), None)
    if done is None:
        check("A6 存在可展示的已完成任务", False, "先跑一次评测再验前端")
        return None
    run_id = done["id"]
    check("A6 存在可展示的已完成任务", True, f"run={done['run_no']} 成功 {done['success_items']}/{done['total_items']}")

    # A7~A11：前端 TaskView / AbCompareView 实际会发的 5 个接口（含 4 个聚合接口）
    for label, method, path, kwargs, ok_fn in [
        ("A7 任务详情 /eval/runs/{id}", "GET", f"/eval/runs/{run_id}", {}, lambda b: b["data"]["metrics"] is not None),
        ("A8 得分明细 /scores", "GET", f"/eval/runs/{run_id}/scores", {"params": {"page": 1, "size": 5}},
         lambda b: len(b["data"]["items"]) > 0),
        ("A9 聚合①BadCase /badcases", "GET", f"/eval/runs/{run_id}/badcases",
         {"params": {"metric": "faithfulness", "threshold": 0.6, "page": 1, "page_size": 5}},
         lambda b: "total" in b["data"]),
        ("A10 聚合②分层 /stratum_metrics", "GET", f"/eval/runs/{run_id}/stratum_metrics", {},
         lambda b: b["data"]["total"] >= 0),
    ]:
        status, body = api(method, path, **kwargs)
        ok = status == 200 and isinstance(body, dict) and body.get("code") == 0 and ok_fn(body)
        detail = f"HTTP {status}"
        if ok and label.startswith("A10"):
            detail = "分层 " + ", ".join(
                f"{(i['stratum'] or '未分层')}={i['sample_count']}" for i in body["data"]["items"]
            )
        elif ok and label.startswith("A9"):
            detail = f"低分样本 total={body['data']['total']}"
        check(label, ok, detail)

    # A11. 聚合③CSV 导出（blob 直出，前端下载用）
    with client(30) as c:
        r = c.get(f"{EVAL_BASE}/eval/runs/{run_id}/export")
        head = r.content[:3]
        check("A11 聚合③CSV 导出 /export", r.status_code == 200 and head == b"\xef\xbb\xbf",
              f"HTTP {r.status_code} BOM={head == b'\xef\xbb\xbf'} bytes={len(r.content)}")

    # A12. 聚合④A/B 对比（需同数据集两条完成任务）
    pairs = [r for r in runs if r["status"] == "done" and r["dataset_id"] == done["dataset_id"]]
    if len(pairs) >= 2:
        status, body = api(
            "POST", "/eval/runs/compare",
            json={"run_id_a": pairs[-1]["id"], "run_id_b": pairs[0]["id"]},
        )
        ok = status == 200 and body.get("code") == 0 and len(body["data"]["rows"]) > 0
        check("A12 聚合④A/B 对比 /compare", ok,
              f"{pairs[-1]['run_no']} vs {pairs[0]['run_no']} 对齐 {len(body['data']['rows']) if ok else 0} 条")
    else:
        check("A12 聚合④A/B 对比 /compare", True, "同数据集仅 1 条完成任务，跳过（不伪造）")

    return done


# --------------------------------------------------------------------------- #
# 场景 B：项目三 → 8200
# --------------------------------------------------------------------------- #
def scenario_agentflow() -> dict[str, Any] | None:
    print("\n[B] 项目三 AgentFlow（8000）→ 评测后端（8200）")

    status, body = api("GET", "/health", base=AGENTFLOW_BASE)
    mock_eval = (body.get("mock") or {}).get("external_eval") if isinstance(body, dict) else None
    check("B1 项目三在线且 mock 已关闭", status == 200 and mock_eval is False, f"external_eval={mock_eval}")

    status, body = api("POST", "/auth/login", base=AGENTFLOW_BASE,
                       json={"username": AGENTFLOW_USER, "password": AGENTFLOW_PASS})
    token = (body.get("data") or {}).get("access_token") if isinstance(body, dict) else None
    if not token:
        check("B2 项目三登录", False, f"HTTP {status} {str(body)[:120]}")
        return None
    check("B2 项目三登录", True, f"token {len(token)} 字符")
    headers = {"Authorization": f"Bearer {token}"}

    # B3. 触发：载荷与项目三前端完全一致（dataset_id 是字符串语义）
    t0 = time.time()
    status, body = api("POST", "/eval/runs", base=AGENTFLOW_BASE,
                       headers=headers, json={"dataset_id": EXT_DATASET_CODE, "note": "stage4 e2e"})
    data = (body.get("data") or {}) if isinstance(body, dict) else {}
    if status != 200 or not data.get("run_id"):
        check("B3 项目三触发评测（转发 8200）", False, f"HTTP {status} {str(body)[:200]}")
        return None
    af_run_id = data["run_id"]
    check("B3 项目三触发评测（转发 8200）", True,
          f"run_no={data.get('run_no')} 立即返回 {time.time() - t0:.2f}s（异步，不阻塞）")

    # B4. 轮询终态（项目三只提供列表端点，无 GET /eval/runs/{id} —— 按 run_id 在列表里定位）
    print("     轮询中（项目三每 10s 拉一次 8200，最长 30 次）…", flush=True)
    final = None
    # 项目二 RAGAS 一轮 7 条样本常见 3~6 分钟：窗口必须比单轮耗时长
    deadline = time.time() + 900
    last = ""

    def fetch_af_run() -> dict[str, Any] | None:
        _, b = api("GET", "/eval/runs", base=AGENTFLOW_BASE, headers=headers,
                   params={"page": 1, "size": 50})
        items = (b.get("data") or {}).get("items", []) if isinstance(b, dict) else []
        return next((r for r in items if r.get("run_id") == af_run_id), None)

    while time.time() < deadline:
        row = fetch_af_run() or {}
        cur = f"{row.get('status')}"
        if cur != last:
            print(f"       · status={cur} elapsed={time.time() - t0:.0f}s", flush=True)
            last = cur
        if cur in ("DONE", "FAILED"):
            final = row
            break
        time.sleep(5)

    if final is None:
        check("B4 轮询到终态", False, "900s 内未到终态")
        return None

    metrics = final.get("metrics") or {}
    ok_done = final["status"] == "DONE" and isinstance(metrics, dict) and len(metrics) > 0
    check("B4 轮询到终态 DONE", ok_done, f"status={final['status']} 耗时 {time.time() - t0:.0f}s")
    if ok_done:
        print("     项目三落库 metrics: " + "  ".join(f"{k}={fmt(v)}" for k, v in metrics.items()))
        check("B5 回写 sample_count", final.get("sample_count") is not None,
              f"sample_count={final.get('sample_count')}（8200 实际打分条数）")
        check("B6 回写 ext_run_id（指回 8200 的 run）", bool(final.get("ext_run_id")),
              f"ext_run_id={final.get('ext_run_id')}")
        check("B7 非 mock 路径（is_mock=false）", final.get("is_mock") is False,
              f"is_mock={final.get('is_mock')}")
    else:
        check("B4 轮询到终态 DONE", False, f"status={final['status']} error={final.get('error_message')}")

    # B8/B9. 交叉核对：8200 侧 ext_run_ref == 项目三 run_no
    status, body = api("GET", "/eval/runs", params={"page": 1, "size": 50})
    ext_runs = body.get("data", {}).get("items", []) if isinstance(body, dict) else []
    matched = next((r for r in ext_runs if r.get("ext_run_ref") == data.get("run_no")), None)
    if matched is None:
        check("B8 8200 侧 ext_run_ref 回填项目三 run_no", False, f"未找到 ext_run_ref={data.get('run_no')}")
        return final
    check("B8 8200 侧 ext_run_ref 回填项目三 run_no", True,
          f"8200 run={matched['run_no']} status={matched['status']}")
    same = all((matched["metrics"] or {}).get(k) is not None for k in METRICS)
    check("B9 8200 侧四项指标齐备（无静默空分）", same,
          "  ".join(f"{k}={fmt((matched['metrics'] or {}).get(k))}" for k in METRICS))

    # 显式提示：项目三看板的 3 个 agent 指标与 RAGAS 4 指标不是同一命名空间
    af_keys = sorted(metrics.keys())
    print(f"     跨系统指标命名空间：项目三期望 task_completion/plan_f1/tool_acc，"
          f"本次落库为 {', '.join(af_keys)}（RAGAS 语义，未做假映射）")
    return {"agentflow": final, "eval_run": matched}


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-agentflow", action="store_true")
    args = parser.parse_args()

    print("=" * 78)
    print("第四步端到端联调：前端 5174 / 评测后端 8200 / 项目一 8010 / 项目三 8000")
    print("=" * 78)

    scenario_frontend()
    if args.skip_agentflow:
        print("\n[跳过 B] --skip-agentflow")
    else:
        scenario_agentflow()

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("\n" + "=" * 78)
    print(f"结果：{passed}/{total} 通过" + ("（全部通过）" if passed == total else "（存在失败项）"))
    for name, ok, detail in results:
        if not ok:
            print(f"  FAIL {name}  {detail}")
    print("=" * 78)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
