"""第三步端到端验证脚本（真实链路，无 mock）。

链路：
    本脚本 → POST /eval/runs（8200）
              → 后端 httpx 调项目一 POST /api/v1/kbs/{kb}/query 取 answer+contexts
              → 后端跑 RAGAS 裁判（deepseek-v4.1-flash-1017）
              → 写 eval_scores → 聚合回 eval_runs.metrics
    本脚本轮询 GET /eval/runs/{id}，再验证 badcases / stratum_metrics / compare / export

用法：
    .venv/Scripts/python.exe scripts/e2e_eval.py
    .venv/Scripts/python.exe scripts/e2e_eval.py --kb 6 --skip-ab   # 只跑单次
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8200"
API = f"{BASE}/api/v1"

# 全部取自 samples/企业运维知识库样例.md，参考答案按原文摘录（不是模型生成）
ITEMS = [
    {
        "question": "打印机卡纸时第一步该怎么做？",
        "reference_answer": "先按打印机面板上的停止键，等待 10 秒后再打开前盖，切勿在运转中强行抽纸。",
        "stratum": "easy",
    },
    {
        "question": "P1 工单的响应时限是多久？",
        "reference_answer": "P1（紧急）响应时限 15 分钟，24 小时全天候受理，值班电话 0571-8888-6666。",
        "stratum": "easy",
    },
    {
        "question": "收到一级告警后需要完成哪些处置步骤？",
        "reference_answer": (
            "第一步 5 分钟内在值班群确认认领；第二步按《应急预案手册》执行初步处置（重启、切流、扩容）；"
            "第三步 15 分钟内在告警平台更新处置状态；第四步故障恢复后 1 个工作日内提交故障复盘报告。"
        ),
        "stratum": "medium",
    },
    {
        "question": "内部系统密码复杂度有什么要求？",
        "reference_answer": (
            "长度不少于 10 位，须包含大小写字母、数字与特殊字符，且不得与最近 3 次历史密码重复；"
            "核心系统密码有效期为 180 天。"
        ),
        "stratum": "medium",
    },
    {
        "question": "工单超过响应时限后如何升级？",
        "reference_answer": (
            "P3 超 1 个工作日升级至组长，超 2 个工作日升级至 IT 经理；"
            "P2 超 4 小时升级至组长，超 8 小时升级至 IT 经理并抄送分管副总。"
        ),
        "stratum": "hard",
    },
    {
        "question": "三级告警需要多久内处理？",
        "reference_answer": "三级告警（Info）每日晨会集中 review，无需即时处理。",
        "stratum": "hard",
    },
    {
        "question": "公司年假有多少天？",
        "reference_answer": "知识库未包含年假制度相关内容，正确做法是明确说明资料中没有该信息。",
        "stratum": "out_of_scope",
    },
]

METRICS = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")


def call(method: str, path: str, **kwargs) -> dict:
    with httpx.Client(timeout=120) as client:
        resp = client.request(method, f"{API}{path}", **kwargs)
    try:
        body = resp.json()
    except ValueError:
        raise SystemExit(f"[FAIL] {path} 返回非 JSON（HTTP {resp.status_code}）：{resp.text[:200]}")
    if body.get("code") != 0:
        raise SystemExit(f"[FAIL] {method} {path} -> HTTP {resp.status_code} {body.get('message')}")
    return body["data"]


def wait_run(run_id: int, timeout: int = 900) -> dict:
    started = time.time()
    last = None
    while time.time() - started < timeout:
        run = call("GET", f"/eval/runs/{run_id}")
        progress = f"{run['processed_items']}/{run['total_items']}"
        if progress != last:
            print(f"     状态={run['status']:<8} 进度={progress}  成功={run['success_items']} 失败={run['failed_items']}")
            last = progress
        if run["status"] in ("done", "failed", "cancelled"):
            return run
        time.sleep(3)
    raise SystemExit(f"[FAIL] run {run_id} 超时未结束")


def fmt(value) -> str:
    return "  —  " if value is None else f"{value:.3f}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kb", type=int, default=6, help="项目一知识库 ID")
    parser.add_argument("--skip-ab", action="store_true", help="跳过第二次 run 与 A/B 对比")
    parser.add_argument("--name", default=None, help="评测集名称")
    args = parser.parse_args()

    print("=" * 78)
    print("EvalAgent 端到端验证".center(70))
    print("=" * 78)

    # 0) 健康检查
    health = call("GET", "/health")
    print(f"[0] 后端健康：v{health['version']} / {health['database']}")

    # 1) 建评测集
    code = f"E2E-{datetime.now():%Y%m%d%H%M%S}"
    ds = call(
        "POST",
        "/eval/datasets",
        json={
            "name": args.name or f"运维知识库 E2E {datetime.now():%H:%M}",
            "code": code,
            "description": "第三步端到端验证：真实项目一取数 + 真实 RAGAS 打分",
            "target_type": "kb",
            "kb_id": args.kb,
            "scenario": "IT运维",
            "agent_scope": [
                {"role": "retriever", "weight": 0.4},
                {"role": "generator", "weight": 0.6},
            ],
            "tags": ["e2e", "stage3"],
        },
    )
    print(f"[1] 评测集 #{ds['id']} {ds['name']}（kb_id={ds['kb_id']}）")

    imported = call("POST", f"/eval/datasets/{ds['id']}/items", json={"items": ITEMS})
    print(f"[2] 导入样本 {imported['imported']} 条（分层：easy/medium/hard/out_of_scope）")

    # 3) 触发 run A（top_k=3）
    t0 = time.time()
    run_a = call(
        "POST",
        "/eval/runs",
        json={
            "dataset_id": ds["id"],
            "variant": "baseline",
            "rag_config": {"top_k": 3, "search_mode": "bm25_vector_hybrid"},
            "callback_run_no": None,
        },
    )
    print(f"[3] 触发 run A：#{run_a['id']} {run_a['run_no']} variant=baseline top_k=3")
    run_a = wait_run(run_a["id"])
    elapsed_a = time.time() - t0
    print(f"    run A 结束：{run_a['status']}，耗时 {elapsed_a:.1f}s，token={run_a['total_tokens']}")

    if run_a["status"] != "done":
        print(f"[FAIL] run A 未成功：{run_a.get('error_msg')}")
        raise SystemExit(1)

    # 4) 汇总指标
    metrics_a = run_a["metrics"] or {}
    print("\n[4] 汇总指标（run A）")
    print("    " + "  ".join(f"{m}={fmt(metrics_a.get(m))}" for m in METRICS))

    # 5) 明细
    scores = call("GET", f"/eval/runs/{run_a['id']}/scores", params={"page": 1, "size": 100})
    print(f"\n[5] 样本明细（{scores['total']} 条）")
    print(f"    {'#':<3}{'分层':<14}{'F':<8}{'AR':<8}{'CP':<8}{'CR':<8}{'延迟ms':<8}{'可疑':<5}问题")
    for s in scores["items"]:
        print(
            f"    {s['item_seq']:<3}{(s['stratum'] or '未分层'):<14}"
            f"{fmt(s['faithfulness']):<8}{fmt(s['answer_relevancy']):<8}"
            f"{fmt(s['context_precision']):<8}{fmt(s['context_recall']):<8}"
            f"{s['latency_ms']:<8}{'是' if s['suspect'] else '否':<5}{s['question'][:26]}"
        )
    failed = [s for s in scores["items"] if s["status"] != "ok"]
    if failed:
        print("    失败样本（不静默吞掉，逐条留原因）：")
        for s in failed:
            print(f"      #{s['item_seq']} {s['question'][:20]} -> {s['error_msg']}")

    # 6) BadCase
    bad = call(
        "GET", f"/eval/runs/{run_a['id']}/badcases",
        params={"metric": "faithfulness", "threshold": 0.8, "page": 1, "page_size": 10},
    )
    print(f"\n[6] BadCase（faithfulness < 0.8）：{bad['total']} 条")
    for row in bad["items"]:
        print(f"    #{row['item_seq']} F={fmt(row['faithfulness'])}  {row['question'][:30]}")
        print(f"       标准答案：{(row['ground_truth'] or '')[:60]}")

    # 7) 分层聚合
    strata = call("GET", f"/eval/runs/{run_a['id']}/stratum_metrics")
    print(f"\n[7] 分层聚合（total={strata['total']}）")
    short = {"faithfulness": "F", "answer_relevancy": "AR",
             "context_precision": "CP", "context_recall": "CR"}
    for item in strata["items"]:
        m = item["metrics"]
        print(
            f"    {(item['stratum'] or '未分层'):<14}n={item['sample_count']:<3}"
            + "  ".join(f"{short[k]}={fmt(m.get(k))}" for k in METRICS)
        )

    # 8) A/B 对比（top_k=5）
    if not args.skip_ab:
        run_b = call(
            "POST",
            "/eval/runs",
            json={
                "dataset_id": ds["id"],
                "variant": "treatment",
                "rag_config": {"top_k": 5, "search_mode": "bm25_vector_hybrid"},
            },
        )
        print(f"\n[8] 触发 run B：#{run_b['id']} {run_b['run_no']} variant=treatment top_k=5")
        run_b = wait_run(run_b["id"])
        print(f"    run B 结束：{run_b['status']}，token={run_b['total_tokens']}")

        cmp = call("POST", "/eval/runs/compare", json={"run_id_a": run_a["id"], "run_id_b": run_b["id"]})
        print(f"\n[9] A/B 对比（阈值 {cmp['diff_threshold']}，{len(cmp['rows'])} 行对齐）")
        print(f"    {'指标':<20}{'A':<10}{'B':<10}{'Δ(B-A)':<10}")
        for row in cmp["summary"]:
            print(
                f"    {row['metric']:<20}{fmt(row['avg_a']):<10}{fmt(row['avg_b']):<10}{fmt(row['diff']):<10}"
            )
        diffs = [r for r in cmp["rows"] if r["is_diff"]]
        print(f"    显著差异样本：{len(diffs)} 条")
        for r in diffs[:3]:
            print(f"      · {r['question'][:30]}  " + " ".join(
                f"{short[k]}Δ={fmt(v)}" for k, v in r["diffs"].items() if v is not None
            ))

    # 10) CSV 导出
    with httpx.Client(timeout=60) as client:
        resp = client.get(f"{API}/eval/runs/{run_a['id']}/export")
    out_dir = Path(__file__).resolve().parent.parent / "logs"
    out_dir.mkdir(exist_ok=True)
    csv_path = out_dir / f"e2e_run_{run_a['run_no']}.csv"
    csv_path.write_bytes(resp.content)
    # 回答里带换行，不能按行数数记录，必须用 csv 解析
    import csv as _csv
    import io as _io

    parsed = list(_csv.reader(_io.StringIO(resp.content.decode("utf-8-sig"))))
    print(f"\n[10] CSV 导出：{csv_path.name}（{len(parsed) - 1} 行数据 / {len(parsed[0])} 列，含 BOM）")
    print("     " + ",".join(parsed[0]))
    print("     " + ",".join(parsed[1])[:110])

    summary = {
        "dataset_id": ds["id"],
        "run_a": {"id": run_a["id"], "run_no": run_a["run_no"], "metrics": metrics_a,
                  "tokens": run_a["total_tokens"], "elapsed_s": round(elapsed_a, 1)},
        "badcases": bad["total"],
        "strata": strata["items"],
    }
    print("\n" + "=" * 78)
    print("E2E 通过：" + json.dumps(summary, ensure_ascii=False)[:400])
    print("=" * 78)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
