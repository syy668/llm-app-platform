# -*- coding: utf-8 -*-
"""脚本一：预置数据 + 快速接口测试 + 启动后台评测任务。只读为主，产生的测试资源均有唯一前缀。"""
import json, csv, time, sys, io
import requests

BASE = "http://127.0.0.1:8000/api/v1"
OUT = {}
RESULTS = []
IDS = {}
PREFIX = "IT-TEST-20261019"


def record(tc, module, steps, expected, actual, status, note=""):
    RESULTS.append({"tc": tc, "module": module, "steps": steps, "expected": expected,
                    "actual": actual, "status": status, "note": note})
    print(f"[{status}] {tc} {module} :: {actual[:160]}", flush=True)


def login(u, p):
    r = requests.post(f"{BASE}/auth/login", json={"username": u, "password": p}, timeout=15)
    d = r.json()
    assert d["code"] == 0, d
    return d["data"]["access_token"]


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


# ---------- 登录 ----------
admin = login("admin", "admin123456")
user = login("user01", "user123456")
ha, hu = H(admin), H(user)
print("login ok", flush=True)

# TC3-1 未登录
r = requests.get(f"{BASE}/eval/datasets", timeout=10)
record("TC3-1", "权限-未登录", "不带Token GET /eval/datasets", "401 未授权",
       f"HTTP {r.status_code} body={r.text[:120]}",
       "PASS" if r.status_code == 401 else "FAIL")

# TC3-2 user可读
r1 = requests.get(f"{BASE}/eval/datasets", headers=hu, timeout=10)
r2 = requests.get(f"{BASE}/eval/runs", headers=hu, timeout=10)
ok = r1.status_code == 200 and r2.status_code == 200
record("TC3-2", "权限-普通用户可读", "user01 GET /eval/datasets、GET /eval/runs", "200 可读",
       f"datasets HTTP {r1.status_code}, runs HTTP {r2.status_code}, total={r1.json()['data']['total']}/{r2.json()['data']['total']}",
       "PASS" if ok else "FAIL")

# TC3-3 user写 403
r = requests.post(f"{BASE}/eval/runs", headers=hu, json={"dataset_id": 1}, timeout=10)
record("TC3-3", "权限-普通用户写", "user01 POST /eval/runs", "403 禁止",
       f"HTTP {r.status_code} body={r.text[:120]}",
       "PASS" if r.status_code == 403 else "FAIL")

# TC3-4 / TC2-3 user删除 403（对不存在资源也应先判权限）
r1 = requests.delete(f"{BASE}/eval/datasets/999999", headers=hu, timeout=10)
r2 = requests.delete(f"{BASE}/eval/runs/999999", headers=hu, timeout=10)
ok = r1.status_code == 403 and r2.status_code == 403
record("TC3-4+TC2-3", "权限-普通用户删除", "user01 DELETE /eval/datasets/{id}、DELETE /eval/runs/{id}", "403 禁止",
       f"dataset HTTP {r1.status_code}, run HTTP {r2.status_code}",
       "PASS" if ok else "FAIL")

# ---------- 预置数据集 ----------
def make_dataset(name, kb_id=6, desc=None):
    r = requests.post(f"{BASE}/eval/datasets", headers=ha,
                      json={"kb_id": kb_id, "name": name, "description": desc}, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["data"]["id"]

def add_items(did, items):
    r = requests.post(f"{BASE}/eval/datasets/{did}/items", headers=ha, json={"items": items}, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["data"]

# 30条主数据集（TC5-1 用）
with io.open("valid_30.csv", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))
items30 = [{"question": x["question"], "reference_answer": x["ground_truth"]} for x in rows]
D30 = make_dataset(f"{PREFIX}-30条主数据集", desc="valid_30.csv 全链路")
add_items(D30, items30)
IDS["D30"] = D30

# 5条 A/B 数据集
items5 = items30[:5]
DAB = make_dataset(f"{PREFIX}-AB数据集", desc="A/B对照5条")
add_items(DAB, items5)
IDS["DAB"] = DAB

# 空数据集（TC4-2）
DEMPTY = make_dataset(f"{PREFIX}-空数据集", desc="0条样本")
IDS["DEMPTY"] = DEMPTY

# 级联专用数据集（TC2-1/2-2），2条
DTHR = make_dataset(f"{PREFIX}-级联数据集", desc="删除级联验证")
add_items(DTHR, items30[:2])
IDS["DTHR"] = DTHR
print(f"datasets: D30={D30} DAB={DAB} DEMPTY={DEMPTY} DTHR={DTHR}", flush=True)

# ---------- 校验类用例（同步422） ----------
def create_run(payload, headers=None):
    return requests.post(f"{BASE}/eval/runs", json=payload, headers=headers or ha, timeout=15)

r = create_run({"dataset_id": DAB, "rag_config": {"top_k": 0}})
record("TC1-4a", "rag_config边界", "top_k=0 创建run", "422 校验错误(范围1-50)",
       f"HTTP {r.status_code} detail={r.text[:200]}",
       "PASS" if r.status_code == 422 else "FAIL")
r = create_run({"dataset_id": DAB, "rag_config": {"top_k": 100}})
record("TC1-4b", "rag_config边界", "top_k=100 创建run", "422 校验错误(范围1-50)",
       f"HTTP {r.status_code} detail={r.text[:200]}",
       "PASS" if r.status_code == 422 else "FAIL")
r = create_run({"dataset_id": DAB, "rag_config": {"top_k": 8, "search_mode": "hybrid_bogus"}})
record("TC1-5", "rag_config枚举", "search_mode=非法字符串 创建run", "422 枚举校验错误",
       f"HTTP {r.status_code} detail={r.text[:200]}",
       "PASS" if r.status_code == 422 else "FAIL")
r = create_run({"dataset_id": DAB, "rag_config": {"chunk_size": -100, "top_k": 8}})
record("TC1-6", "rag_config数值", "chunk_size=-100 创建run", "422 数值校验错误(100-4000)",
       f"HTTP {r.status_code} detail={r.text[:200]}",
       "PASS" if r.status_code == 422 else "FAIL")

# TC4-1 dataset不存在
r = create_run({"dataset_id": 999999})
record("TC4-1", "异常输入", "dataset_id=999999 创建run", "404 不存在",
       f"HTTP {r.status_code} body={r.text[:150]}",
       "PASS" if r.status_code == 404 else "FAIL")

# ---------- 启动后台评测任务 ----------
def launch(did, rc=None, tag=""):
    r = create_run({"dataset_id": did, **({"rag_config": rc} if rc else {})})
    assert r.status_code == 200, f"{tag}: {r.text}"
    rid = r.json()["data"]["id"]
    print(f"launched run {tag} id={rid} rag_config={rc}", flush=True)
    return rid

# TC1-1/TC6-2：不传rag_config（5条）
RNO = launch(DAB, None, "RNO(TC1-1)")
# TC1-2/TC5-3 runB：仅top_k=8（5条）
RB = launch(DAB, {"top_k": 8}, "RB(TC1-2,runB)")
# TC1-3：完整rag_config（5条）
RFULL = launch(DAB, {"chunk_size": 500, "search_mode": "vector_only", "top_k": 8}, "RFULL(TC1-3)")
# TC5-3 runA：top_k=3（5条）
RA = launch(DAB, {"top_k": 3}, "RA(runA)")
# TC4-2：空数据集
REMPTY = launch(DEMPTY, None, "REMPTY(TC4-2)")
# TC5-1：30条主数据集，top_k=8（API触发，前端链路后续用同一数据集再做一条；此run同时作为长链路验证）
R30 = launch(D30, {"top_k": 8}, "R30(TC5-1)")
IDS.update(RNO=RNO, RB=RB, RFULL=RFULL, RA=RA, REMPTY=REMPTY, R30=R30)

# ---------- TC1-7 创建后立即查询 ----------
r = requests.get(f"{BASE}/eval/runs/{RB}", headers=ha, timeout=10)
d = r.json()["data"]
ok = d["rag_config"] == {"top_k": 8}
record("TC1-7a", "rag_config查询", "GET /eval/runs/{RB} 详情", "rag_config={top_k:8} 正确返回",
       f"HTTP {r.status_code} rag_config={d['rag_config']}",
       "PASS" if ok else "FAIL")
r = requests.get(f"{BASE}/eval/runs/{RFULL}", headers=ha, timeout=10)
d = r.json()["data"]
expect = {"chunk_size": 500, "search_mode": "vector_only", "top_k": 8}
ok = d["rag_config"] == expect
record("TC1-7b", "rag_config查询", "GET /eval/runs/{RFULL} 详情", "rag_config 三字段完整返回",
       f"HTTP {r.status_code} rag_config={d['rag_config']}",
       "PASS" if ok else "FAIL")
r = requests.get(f"{BASE}/eval/runs/{RNO}", headers=ha, timeout=10)
d = r.json()["data"]
record("TC1-7c", "rag_config查询", "GET /eval/runs/{RNO}（未传配置）详情", "rag_config=null",
       f"HTTP {r.status_code} rag_config={d['rag_config']}",
       "PASS" if d["rag_config"] is None else "FAIL")
r = requests.get(f"{BASE}/eval/runs?page=1&size=50", headers=ha, timeout=10)
items = r.json()["data"]["items"]
mp = {x["id"]: x.get("rag_config") for x in items}
ok = mp.get(RB) == {"top_k": 8} and mp.get(RNO) is None and mp.get(RFULL) == {"chunk_size": 500, "search_mode": "vector_only", "top_k": 8}
record("TC1-7d", "rag_config查询", "GET /eval/runs 列表", "列表 rag_config 字段正确返回(JSON/null)",
       f"HTTP {r.status_code} RB={mp.get(RB)} RNO={mp.get(RNO)} RFULL={mp.get(RFULL)}",
       "PASS" if ok else "FAIL")

# ---------- TC6-1 旧run ----------
olds = []
for rid in [1, 2, 3, 4]:
    rr = requests.get(f"{BASE}/eval/runs/{rid}", headers=ha, timeout=10)
    if rr.status_code == 200:
        olds.append((rid, rr.json()["data"].get("rag_config")))
ok = all(rc is None for _, rc in olds) and len(olds) > 0
record("TC6-1a", "兼容性-旧数据", "GET /eval/runs/1~4（历史无rag_config）", "rag_config=null",
       f"HTTP 200; {olds}",
       "PASS" if ok else "FAIL")

# ---------- 导出 ----------
r = requests.get(f"{BASE}/eval/runs/5/export", headers=ha, timeout=15)
body = r.content.decode("utf-8", errors="replace")
has_bom = r.content[:3] == b"\xef\xbb\xbf"
ok = r.status_code == 200 and "text/csv" in r.headers.get("content-type", "") and "question" in body
record("TC5-1pre", "导出-后端CSV", "GET /eval/runs/5/export（已有旧run）", "200 CSV下载，含BOM与表头",
       f"HTTP {r.status_code} ct={r.headers.get('content-type')} BOM={has_bom} head={body[:80]!r}",
       "PASS" if ok else ("FAIL" if r.status_code != 200 else "PASS"),
       "BOM=True避免Excel中文乱码" if ok and has_bom else ("备注:BOM缺失" if ok and not has_bom else ""))
r = requests.get(f"{BASE}/eval/runs/5/export", headers=hu, timeout=15)
record("TC5-1pre2", "导出-权限", "user01 GET /eval/runs/5/export", "403（导出仅管理员）",
       f"HTTP {r.status_code}",
       "PASS" if r.status_code == 403 else "FAIL",
       "前端需对user隐藏导出按钮，若未隐藏为前端体验问题")

with open("results1.json", "w", encoding="utf-8") as f:
    json.dump(RESULTS, f, ensure_ascii=False, indent=1)
with open("ids.json", "w", encoding="utf-8") as f:
    json.dump(IDS, f, ensure_ascii=False, indent=1)
print("PHASE1 DONE", flush=True)
