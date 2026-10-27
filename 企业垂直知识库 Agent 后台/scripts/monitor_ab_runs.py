"""监控评测 run 进度，完成后输出 A/B 四项指标对比。

用法（项目根目录执行）：
    cd 企业垂直知识库\ Agent\ 后台
    .venv/Scripts/python.exe -X utf8 scripts/monitor_ab_runs.py 19 20
不传参数时默认监控最近的两个 run。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import time

from sqlalchemy import text

from app.core.database import SessionLocal

run_ids = [int(a) for a in sys.argv[1:]] or [19, 20]
id_list = ",".join(str(i) for i in run_ids)

db = SessionLocal()
deadline = time.time() + 3600
while time.time() < deadline:
    rows = db.execute(
        text(f"SELECT id, status, success_items, total_items FROM eval_runs WHERE id IN ({id_list})")
    ).fetchall()
    st = {r._mapping["id"]: (r._mapping["status"], r._mapping["success_items"], r._mapping["total_items"]) for r in rows}
    print(time.strftime("%H:%M:%S"), st, flush=True)
    if all(v[0] in ("done", "failed") for v in st.values()):
        break
    time.sleep(60)

print("===== 最终结果 =====")
for r in db.execute(text(f"""
    SELECT run_id, COUNT(*) AS n,
           ROUND(AVG(faithfulness), 4) AS faithfulness,
           ROUND(AVG(answer_relevancy), 4) AS answer_relevancy,
           ROUND(AVG(context_precision), 4) AS context_precision,
           ROUND(AVG(context_recall), 4) AS context_recall,
           ROUND(AVG(latency_ms)) AS avg_latency_ms
    FROM eval_run_scores WHERE run_id IN ({id_list}) GROUP BY run_id
""")):
    print(dict(r._mapping))
db.close()
