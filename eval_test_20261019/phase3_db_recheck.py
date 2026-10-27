# -*- coding: utf-8 -*-
"""脚本三：autocommit 直查复核（修正 phase2 的事务隔离误读）+ 二次级联测试。"""
import json, time
import requests
import pymysql

BASE = "http://127.0.0.1:8000/api/v1"
OUT = {}

db = pymysql.connect(host="127.0.0.1", port=3306, user="root", password="12345678",
                     database="kb_agent", charset="utf8mb4", autocommit=True)
cur = db.cursor()

def q(sql, args=None):
    cur.execute(sql, args or ())
    return cur.fetchall()

def login(u, p):
    r = requests.post(f"{BASE}/auth/login", json={"username": u, "password": p}, timeout=15)
    return r.json()["data"]["access_token"]

def H(t): return {"Authorization": f"Bearer {t}"}

admin = login("admin", "admin123456")
ha = H(admin)

print("== 1) 现存各run得分条数复核 (autocommit) ==")
for rid in [6, 7, 8, 9, 11]:
    n = q("SELECT COUNT(*) FROM eval_run_scores WHERE run_id=%s", (rid,))[0][0]
    st = q("SELECT status, total_items, success_items FROM eval_runs WHERE id=%s", (rid,))[0]
    print(f"run {rid}: status={st} scores={n}")

print("== 2) 已删除 dataset 9 的残留检查（TC2-1 复核） ==")
print("dataset 9 exists:", q("SELECT COUNT(*) FROM eval_datasets WHERE id=9"))
print("dataset 9 items:", q("SELECT COUNT(*) FROM eval_dataset_items WHERE dataset_id=9"))
print("runs on dataset 9:", q("SELECT id, status FROM eval_runs WHERE dataset_id=9"))

print("== 3) rag_config JSON 语义检查 ==")
for rid in [6, 7, 11, 1]:
    row = q("SELECT rag_config, JSON_TYPE(rag_config) FROM eval_runs WHERE id=%s", (rid,))
    print(f"run {rid}: rag_config={row[0][0]!r} json_type={row[0][1]}")

print("== 4) dataset 5 '11' item_count 复核 ==")
print(q("SELECT id, name, (SELECT COUNT(*) FROM eval_dataset_items i WHERE i.dataset_id=d.id) AS real_cnt FROM eval_datasets d WHERE id=5"))

print("== 5) 失败样本分析：run 11 的得分 vs 失败条目 ==")
scores = q("SELECT item_id, question FROM eval_run_scores WHERE run_id=11")
scored_ids = {s[0] for s in scores}
items = q("SELECT id, question FROM eval_dataset_items WHERE dataset_id=6")
missing = [i for i in items if i[0] not in scored_ids]
print(f"dataset6 items={len(items)}, scored={len(scores)}, missing={len(missing)}")
for m in missing[:8]:
    print("  missing:", m[0], m[1][:40])

print("== 6) 二次级联测试：新dataset+2items+run，等done后删除dataset ==")
r = requests.post(f"{BASE}/eval/datasets", headers=ha, json={"kb_id": 6, "name": "IT-CASCADE-RETEST", "description": "二次级联"}, timeout=10)
DID = r.json()["data"]["id"]
requests.post(f"{BASE}/eval/datasets/{DID}/items", headers=ha, json={"items": [
    {"question": "如何重置VPN密码", "reference_answer": "在门户-账号安全页重置"},
    {"question": "打印机卡纸怎么办", "reference_answer": "打开前盖取出卡纸"}]}, timeout=15)
r = requests.post(f"{BASE}/eval/runs", headers=ha, json={"dataset_id": DID, "rag_config": {"top_k": 3}}, timeout=15)
RID = r.json()["data"]["id"]
print(f"retest dataset={DID} run={RID}")
while True:
    st = requests.get(f"{BASE}/eval/runs/{RID}", headers=ha, timeout=10).json()["data"]["status"]
    if st in ("done", "failed"): break
    time.sleep(15)
print("retest run status:", st)
n_items = q("SELECT COUNT(*) FROM eval_dataset_items WHERE dataset_id=%s", (DID,))[0][0]
n_scores = q("SELECT COUNT(*) FROM eval_run_scores WHERE run_id=%s", (RID,))[0][0]
rd = requests.delete(f"{BASE}/eval/datasets/{DID}", headers=ha, timeout=10)
n_items_after = q("SELECT COUNT(*) FROM eval_dataset_items WHERE dataset_id=%s", (DID,))[0][0]
n_runs_after = q("SELECT COUNT(*) FROM eval_runs WHERE id=%s", (RID,))[0][0]
n_scores_after = q("SELECT COUNT(*) FROM eval_run_scores WHERE run_id=%s", (RID,))[0][0]
run_api = requests.get(f"{BASE}/eval/runs/{RID}", headers=ha, timeout=10)
print(f"BEFORE: items={n_items} scores={n_scores}")
print(f"DELETE dataset: HTTP {rd.status_code}")
print(f"AFTER: items={n_items_after} (expect 0), run_rows={n_runs_after} (expect 1保留), scores={n_scores_after} (保留), GET run HTTP {run_api.status_code}")

OUT["retest"] = {"dataset": DID, "run": RID, "items_before": n_items, "scores_before": n_scores,
                 "del_http": rd.status_code, "items_after": n_items_after, "run_rows_after": n_runs_after,
                 "scores_after": n_scores_after, "get_run_http": run_api.status_code, "run_status": st}
with open("phase3.json", "w", encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=1)
print("PHASE3 DONE")
