#!/usr/bin/env python
"""RAG 评测基线跑批脚本（独立脚本，不修改任何业务代码）。

流程：
    1. 读取 test_datasets/baseline_qa.csv（question / ground_truth）
    2. 登录后端 API，逐条调用 POST /api/v1/kbs/{kb_id}/query 获取 answer 与 retrieved_contexts
    3. 使用 ragas 计算 faithfulness 与 context_recall（评测 LLM 从项目 .env 读取，
       与业务 LLM_PROVIDER 解耦：当前业务为 mock 回显时仍可用真实 LLM 做指标评审）
    4. 终端打印每条明细与汇总指标，并生成 Markdown 报告 docs/RAG_BASELINE_REPORT.md

用法：
    .venv/Scripts/python.exe scripts/run_baseline_eval.py [--kb-id 6] [--limit N]
        [--csv test_datasets/baseline_qa.csv] [--output docs/RAG_BASELINE_REPORT.md]
        [--api-base http://127.0.0.1:8000] [--username admin] [--password ***]

依赖（本项目 .venv 已全部具备，重装可执行）：
    .venv/Scripts/python.exe -m pip install "ragas>=0.4.3" datasets langchain-openai python-dotenv requests

注意：
    - ragas 0.4.3 与 langchain 1.x 存在导入不兼容（langchain_community.chat_models.vertexai
      已被移出），脚本顶部做了进程内 shim 兼容，不改动任何依赖与库文件。
    - 业务侧 LLM_PROVIDER=mock 时 answer 为检索内容回显，faithfulness 会系统性偏高；
      基线结论仅代表当前配置下的检索+生成链路，切换 LLM_PROVIDER=openai 后应重跑。
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
import types
import warnings
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

# ---------------------------------------------------------------------------
# ragas 0.4.3 x langchain 1.x 兼容 shim（仅本进程生效，不改依赖/不改库）
# ---------------------------------------------------------------------------
def _patch_langchain_shims() -> None:
    missing = {
        "langchain_community.chat_models.vertexai": ["ChatVertexAI"],
        "langchain_community.chat_models": ["ChatAnthropic"],
    }
    for name, attrs in missing.items():
        try:
            __import__(name)
        except ModuleNotFoundError:
            mod = types.ModuleType(name)
            for attr in attrs:
                setattr(mod, attr, type(attr, (), {}))
            sys.modules[name] = mod


_patch_langchain_shims()
warnings.filterwarnings("ignore", category=DeprecationWarning)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE_DIR / ".env")

import requests  # noqa: E402

DEFAULT_API_BASE = "http://127.0.0.1:8000"
DEFAULT_KB_ID = 6  # 企业运维知识库样例


# ---------------------------------------------------------------------------
# 后端 API 调用
# ---------------------------------------------------------------------------
def login(api_base: str, username: str, password: str) -> str:
    resp = requests.post(
        f"{api_base}/api/v1/auth/login",
        json={"username": username, "password": password},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"登录失败: {data}")
    return data["data"]["access_token"]


def rag_query(api_base: str, token: str, kb_id: int, question: str, top_k: int | None):
    """调用现有 RAG 问答 API，返回 (answer, retrieved_contexts)。"""
    body = {"question": question}
    if top_k:
        body["top_k"] = top_k
    resp = requests.post(
        f"{api_base}/api/v1/kbs/{kb_id}/query",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"问答接口返回异常: {data}")
    payload = data["data"]
    answer = payload.get("answer", "")
    contexts = [h.get("content", "") for h in payload.get("sources", []) if h.get("content")]
    return answer, contexts


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="RAG 评测基线跑批（ragas: faithfulness + context_recall）")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin123456")
    parser.add_argument("--kb-id", type=int, default=DEFAULT_KB_ID)
    parser.add_argument("--csv", default=str(BASE_DIR / "test_datasets" / "baseline_qa.csv"))
    parser.add_argument("--output", default=str(BASE_DIR / "docs" / "RAG_BASELINE_REPORT.md"))
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0=全部）")
    parser.add_argument("--top-k", type=int, default=0, help="覆盖检索条数（0=使用知识库默认）")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"[ERROR] 测试集不存在: {csv_path}")
        return 1
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("question")]
    if args.limit > 0:
        rows = rows[: args.limit]
    print(f"[1/3] 已加载测试集 {len(rows)} 条（来源: {csv_path.name}），目标知识库 kb_id={args.kb_id}")

    token = login(args.api_base, args.username, args.password)
    print(f"[2/3] 登录成功，开始逐条调用问答接口 {args.api_base}/api/v1/kbs/{args.kb_id}/query ...")

    records: list[dict] = []
    for idx, row in enumerate(rows, start=1):
        question = row["question"].strip()
        ground_truth = (row.get("ground_truth") or "").strip()
        t0 = time.time()
        try:
            answer, contexts = rag_query(args.api_base, token, args.kb_id, question, args.top_k or None)
            err = ""
        except Exception as exc:  # 单条失败不中断跑批，记为空结果并标注
            answer, contexts, err = "", [], str(exc)
        records.append(
            {
                "id": idx,
                "question": question,
                "reference": ground_truth,
                "answer": answer,
                "contexts": contexts,
                "latency": time.time() - t0,
                "error": err,
            }
        )
        flag = "ERR" if err else "OK"
        print(f"  [{idx:>2}/{len(rows)}] {flag}  {question[:24]}...  ctx={len(contexts)}  {time.time() - t0:.2f}s")

    ok_records = [r for r in records if not r["error"]]
    if not ok_records:
        print("[ERROR] 所有问答调用均失败，无法评测。请确认后端服务与知识库可用。")
        return 2

    # ---------------- ragas 评测 ----------------
    print("[3/3] 调用 ragas 计算 faithfulness / context_recall（评测 LLM 来自 .env 配置）...")
    from langchain_openai import ChatOpenAI
    from ragas import EvaluationDataset, SingleTurnSample, evaluate
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import context_recall, faithfulness
    from ragas.run_config import RunConfig

    eval_llm = LangchainLLMWrapper(
        ChatOpenAI(
            model=os.getenv("LLM_MODEL", "deepseek-chat"),
            base_url=os.getenv("LLM_API_BASE") or None,
            api_key=os.getenv("LLM_API_KEY"),
            temperature=0,
            timeout=120,
        )
    )

    samples = []
    for r in records:
        contexts = r["contexts"] or ["（无检索结果）"]
        samples.append(
            SingleTurnSample(
                user_input=r["question"],
                retrieved_contexts=contexts,
                response=r["answer"] or "（接口调用失败，无回答）",
                reference=r["reference"],
            )
        )

    result = evaluate(
        EvaluationDataset(samples),
        metrics=[faithfulness, context_recall],
        llm=eval_llm,
        run_config=RunConfig(max_workers=4, timeout=600),
    )
    df = result.to_pandas()

    summary = {
        "faithfulness": float(df["faithfulness"].mean()),
        "context_recall": float(df["context_recall"].mean()),
    }
    print("\n================ 评测汇总 ================")
    print(f"  样本数          : {len(records)}（成功 {len(ok_records)} / 失败 {len(records) - len(ok_records)}）")
    print(f"  faithfulness    : {summary['faithfulness']:.4f}")
    print(f"  context_recall  : {summary['context_recall']:.4f}")
    print("==========================================\n")

    print("---- 每条明细 ----")
    for r in records:
        row = df.iloc[r["id"] - 1]
        faith = row.get("faithfulness")
        recall = row.get("context_recall")
        fmt = lambda v: f"{float(v):.4f}" if v is not None and v == v else "N/A"
        print(f"  #{r['id']:>2} faithfulness={fmt(faith)}  context_recall={fmt(recall)}  ctx={len(r['contexts'])}  Q: {r['question'][:30]}")

    # ---------------- 生成 Markdown 报告 ----------------
    lines = [
        "# RAG 评测基线报告（RAGAS）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 测试集：`{csv_path.name}`（{len(records)} 条：企业报销流程 10 条 + IT 运维手册 10 条）",
        f"- 目标知识库：kb_id={args.kb_id}",
        f"- 问答接口：`POST {args.api_base}/api/v1/kbs/{{kb_id}}/query`（混合检索 + LLM）",
        "- 评测指标：ragas faithfulness / context_recall（评测 LLM 见 `.env` 配置）",
        "",
        "## 一、汇总指标",
        "",
        "| 指标 | 得分 | 说明 |",
        "| --- | --- | --- |",
        f"| faithfulness | **{summary['faithfulness']:.4f}** | 回答与检索上下文的事实一致性（越高说明越少幻觉） |",
        f"| context_recall | **{summary['context_recall']:.4f}** | 检索上下文对标准答案要点的覆盖度（越高说明检索越全） |",
        "",
        "> ⚠️ 前提说明：当前业务配置 `LLM_PROVIDER=mock`（回答为检索内容回显），faithfulness 会系统性偏高。",
        "本基线反映的是**当前配置下的端到端链路水平**；将 `LLM_PROVIDER` 切换为 `openai`（DeepSeek）并重启后端后应重跑以获得生成质量基线。",
        "",
        "## 二、逐条明细",
        "",
        "| # | faithfulness | context_recall | 检索块数 | 耗时(s) | 问题 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in records:
        row = df.iloc[r["id"] - 1]
        faith, recall = row.get("faithfulness"), row.get("context_recall")
        fmt = lambda v: f"{float(v):.4f}" if v is not None and v == v else "N/A"
        lines.append(
            f"| {r['id']} | {fmt(faith)} | {fmt(recall)} | {len(r['contexts'])} | {r['latency']:.2f} | {r['question']} |"
        )
    lines += [
        "",
        "## 三、失败/异常记录",
        "",
    ]
    errs = [r for r in records if r["error"]]
    if errs:
        lines += [f"- #{r['id']} {r['question']}：{r['error']}" for r in errs]
    else:
        lines.append("- 无（全部问答调用成功）")
    lines += [
        "",
        "## 四、结论与复跑方式",
        "",
        f"- 本次基线：faithfulness={summary['faithfulness']:.4f}，context_recall={summary['context_recall']:.4f}（n={len(records)}）。",
        "- 复跑命令：`.venv/Scripts/python.exe scripts/run_baseline_eval.py --kb-id 6`",
        "- 切换真实 LLM 生成后重跑，可与本基线对比生成质量差异。",
        "",
    ]
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[OK] Markdown 报告已保存: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
