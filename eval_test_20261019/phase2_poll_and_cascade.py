# -*- coding: utf-8 -*-
"""脚本二：轮询后台任务至终态 + 删除级联验证 + MySQL直查。"""
import json, time, sys
import requests
import pymysql

BASE = "http://127.0.0.1:8000/api/v1"
RESULTS = []


def record(tc, module, steps, expected, actual, status, note=""):
    RESULTS.append({"tc": tc, "module": module, "steps": steps, "expected": expected,
                    "actual": actual, "status": status, "note": note})
    print(f"[{status}] {tc} {module} :: {actual[:200]}", flush=True)


def login(u, p):
    r = requests.post(f"{BASE}/auth/login", json={"username": u, "password": p}, timeout=15)
    return r.json()["data"]["access_token"]


def H(t):
    return {"Authorization": f"Bearer {t}"}


admin = login("admin", "admin123456")
ha = H(admin)
IDS = json.load(open("ids.json", encoding="utf-8"))
D30, DAB, DEMPTY, DTHR = IDS["D30"], IDS["DAB"], IDS["DEMPTY"], IDS["DTHR"]
RNO, RB, RFULL, RA, REMPTY, R30 = IDS["RNO"], IDS["RB"], IDS["RFULL"], IDS["RA"], IDS["REMPTY"], IDS["R30"]

db = pymysql.connect(host="127.0.0.1", port=3306, user="root", password="12345678",
                     database="kb_agent", charset="utf8mb4")
cur = db.cursor()

# ---------- TC4-3 幂等补列：直查列结构（重启验证另行标注） ----------
cur.execute("SHOW COLUMNS FROM eval_runs LIKE 'rag_config'")
row = cur.fetchone()
record("TC4-3a", "幂等补列", "直查MySQL SHOW COLUMNS eval_runs.rag_config", "存在 JSON NULL 列",
       f"SHOW COLUMNS => {row}", "PASS" if row and "json" in str(row[1]).lower() else "FAIL",
       "重启重复启动验证：见代码审查+最终结论（避免中断执行中任务）")

# ---------- 轮询所有后台任务 ----------
targets = {"RNO": RNO, "RB": RB, "RFULL": RFULL, "RA": RA, "REMPTY": REMPTY, "R30": R30}
states = {}
deadline = time.time() + 40 * 60
pending = dict(targets)
while pending and time.time() < deadline:
    for tag, rid in list(pending.items()):
        r = requests.get(f"{BASE}/eval/runs/{rid}", headers=ha, timeout=10)
        d = r.json()["data"]
        st = d["status"]
        if st in ("done", "failed"):
            states[tag] = d
            del pending[tag]
            print(f"run {tag}(id={rid}) => {st} metrics={d.get('metrics')} err={d.get('error_msg')}", flush=True)
    if pending:
        time.sleep(20)
for tag, rid in pending.items():
    print(f"TIMEOUT: run {tag}(id={rid}) not finished in 40min", flush=True)
    states[tag] = {"status": "timeout"}

# TC4-2 空数据集任务应 failed
d = states.get("REMPTY", {})
record("TC4-2", "异常-空数据集", "对0条样本数据集创建评测任务，等待终态", "status=failed（后台任务失败）",
       f"run id={REMPTY} status={d.get('status')} error_msg={d.get('error_msg')}",
       "PASS" if d.get("status") == "failed" else "FAIL")

# 其余任务 done
for tag in ["RNO", "RB", "RFULL", "RA", "R30"]:
    d = states.get(tag, {})
    record(f"TC-{tag}", "评测执行", f"run {tag}(id={ids_get:=IDS[tag]}) 等待终态" if False else f"run {tag} 等待终态", "status=done",
           f"run id={IDS[tag]} status={d.get('status')} total={d.get('total_items')} success={d.get('success_items')} metrics={d.get('metrics')}",
           "PASS" if d.get("status") == "done" else "FAIL")

# TC5-3 API侧：A/B两run指标与分数存在
def score_count(rid):
    cur.execute("SELECT COUNT(*) FROM eval_run_scores WHERE run_id=%s", (rid,))
    return cur.fetchone()[0]

ra_done = states.get("RA", {}).get("status") == "done"
rb_done = states.get("RB", {}).get("status") == "done"
cntA, cntB = score_count(RA), score_count(RB)
record("TC5-3a", "A/B对照-API侧", "runA(top_k=3)与runB(top_k=8)均done且各有得分明细", "两run done，scores数量=5/5",
       f"RA status={states.get('RA',{}).get('status')} scores={cntA}; RB status={states.get('RB',{}).get('status')} scores={cntB}",
       "PASS" if (ra_done and rb_done and cntA == 5 and cntB == 5) else "FAIL",
       "前端对齐与图表渲染在浏览器端验证")

# ---------- 删除级联（TC2-1/2-2） ----------
# 级联专用：先在 DTHR 上建一条 run 并等 done
r = requests.post(f"{BASE}/eval/runs", headers=ha, json={"dataset_id": DTHR}, timeout=15)
assert r.status_code == 200, r.text
RTHR = r.json()["data"]["id"]
print(f"cascade run id={RTHR} launched", flush=True)
while True:
    d = requests.get(f"{BASE}/eval/runs/{RTHR}", headers=ha, timeout=10).json()["data"]
    if d["status"] in ("done", "failed"):
        break
    time.sleep(15)
cur.execute("SELECT COUNT(*) FROM eval_dataset_items WHERE dataset_id=%s", (DTHR,))
items_before = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM eval_run_scores WHERE run_id=%s", (RTHR,))
scores_before = cur.fetchone()[0]

# TC2-1 删除dataset：items级联删，runs保留
r = requests.delete(f"{BASE}/eval/datasets/{DTHR}", headers=ha, timeout=10)
cur.execute("SELECT COUNT(*) FROM eval_dataset_items WHERE dataset_id=%s", (DTHR,))
items_after = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM eval_runs WHERE id=%s", (RTHR,))
run_kept = cur.fetchone()[0]
rd = requests.get(f"{BASE}/eval/runs/{RTHR}", headers=ha, timeout=10)
ok = r.status_code == 200 and items_before == 2 and items_after == 0 and run_kept == 1 and rd.status_code == 200
record("TC2-1", "删除级联-dataset", "管理员DELETE /eval/datasets/{DTHR}；查items计数与run保留情况",
       "dataset_items级联删除(2→0)；eval_runs保留可查",
       f"del HTTP {r.status_code}; items {items_before}->{items_after}; run kept={run_kept}, GET run HTTP {rd.status_code}",
       "PASS" if ok else "FAIL")
# 同数据集的旧runs（RNO/RB/RFULL/RA挂在DAB上，此处验证DTHR）——补查历史runs dataset_id=DTHR 的其它记录
cur.execute("SELECT COUNT(*) FROM eval_runs WHERE dataset_id=%s", (DTHR,))
runs_on_dthr = cur.fetchone()[0]
record("TC2-1b", "删除级联-dataset", "删除数据集后 eval_runs 中该dataset的记录数", "≥1（保留不删除）",
       f"eval_runs where dataset_id={DTHR}: {runs_on_dthr} 条（删除前创建 {RTHR}）",
       "PASS" if runs_on_dthr >= 1 else "FAIL")

# TC2-2 删除run：scores级联删，dataset...已删，验证scores删除+其它dataset不受影响
cur.execute("SELECT COUNT(*) FROM eval_dataset_items WHERE dataset_id=%s", (D30,))
d30_items = cur.fetchone()[0]
r = requests.delete(f"{BASE}/eval/runs/{RTHR}", headers=ha, timeout=10)
cur.execute("SELECT COUNT(*) FROM eval_run_scores WHERE run_id=%s", (RTHR,))
scores_after = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM eval_runs WHERE id=%s", (RTHR,))
run_gone = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM eval_dataset_items WHERE dataset_id=%s", (D30,))
d30_items_after = cur.fetchone()[0]
ok = r.status_code == 200 and scores_before > 0 and scores_after == 0 and run_gone == 0 and d30_items == d30_items_after
record("TC2-2", "删除级联-run", "管理员DELETE /eval/runs/{RTHR}；查scores计数",
       "eval_run_scores级联删除；其他dataset数据不受影响",
       f"del HTTP {r.status_code}; scores {scores_before}->{scores_after}; run存在={run_gone}; D30 items {d30_items}->{d30_items_after}",
       "PASS" if ok else "FAIL")

# 删除不存在资源
r1 = requests.delete(f"{BASE}/eval/datasets/{DTHR}", headers=ha, timeout=10)
r2 = requests.delete(f"{BASE}/eval/runs/{RTHR}", headers=ha, timeout=10)
record("TC2-1c", "删除级联-重复删除", "对已删除资源再次DELETE", "404",
       f"dataset HTTP {r1.status_code}; run HTTP {r2.status_code}",
       "PASS" if r1.status_code == 404 and r2.status_code == 404 else "FAIL")

# TC6-2 DB侧：RNO 的 rag_config 为 NULL
cur.execute("SELECT rag_config FROM eval_runs WHERE id=%s", (RNO,))
rc = cur.fetchone()[0]
record("TC6-2", "兼容性-无配置新建", "不携带rag_config新建run，直查DB", "eval_runs.rag_config IS NULL，评测用知识库原生top_k",
       f"DB rag_config={rc!r}",
       "PASS" if rc is None else "FAIL")

# TC1-3 DB侧：RFULL 三字段落库
cur.execute("SELECT rag_config FROM eval_runs WHERE id=%s", (RFULL,))
rc = cur.fetchone()[0]
import json as _j
ok = rc and isinstance(rc, (dict, str)) and _j.loads(rc) if isinstance(rc, str) else rc
parsed = _j.loads(rc) if isinstance(rc, str) else rc
ok = parsed == {"chunk_size": 500, "search_mode": "vector_only", "top_k": 8}
record("TC1-3a", "rag_config落库", "完整rag_config创建run，直查DB", "三字段完整JSON落库",
       f"DB rag_config={rc!r}", "PASS" if ok else "FAIL")

db.close()
with open("results2.json", "w", encoding="utf-8") as f:
    json.dump(RESULTS, f, ensure_ascii=False, indent=1)
with open("states.json", "w", encoding="utf-8") as f:
    json.dump({k: {kk: vv for kk, vv in v.items() if kk in ("status", "metrics", "total_items", "success_items", "error_msg", "rag_config")} for k, v in states.items()},
              f, ensure_ascii=False, indent=1)
print("PHASE2 DONE", flush=True)
