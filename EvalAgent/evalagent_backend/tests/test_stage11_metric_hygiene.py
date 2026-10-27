"""打分卫生（2026-10-27 真机跑黄金测试集 30 条后补的回归）。

三个真机暴露出来的问题，逐个钉住：
1. **拒答样本的 answer_relevancy 恒 0**：AR 是「由答案反推问题再比相似度」，
   拒答话术（"资料中未找到相关信息"）没有主题内容 → 反推问题必然跑偏。
   4 条拒答把整体 AR 从 0.924 压到 0.822，属于口径错误而不是质量下降。
2. **单指标 NaN 静默丢失**：ragas `raise_exceptions=False` 只吞掉坏的那一项，
   行 status 仍是 ok、error_msg 为空 → 前端只看到空单元格，无法区分
   「没算」和「真的 0 分」。
3. **单样本 300s 超时丢全部指标**：实测超时样本单独重打只要 20~40s 就能满分，
   一次网络抖动就让 10% 样本零指标 → 收尾前必须补跑。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import EvalDataset, EvalItem, EvalRun, EvalScore
from app.services import runner as runner_mod
from app.services.judge import JudgeOutcome
from app.services.rag_client import RagAnswer


# ============================ 造数 ============================
def _seed(db, items: list[tuple[str, str, str | None]]) -> tuple[int, int]:
    """items = [(question, reference_answer, stratum), ...]"""
    tag = f"T11-{int(time.time() * 1000)}-{len(items)}"
    ds = EvalDataset(name=f"打分卫生 {tag}", code=tag, target_type="kb", kb_id=6, item_count=len(items))
    db.add(ds)
    db.flush()
    for i, (q, ref, stratum) in enumerate(items, start=1):
        db.add(EvalItem(dataset_id=ds.id, seq=i, question=q, reference_answer=ref, stratum=stratum))
    db.flush()
    run = EvalRun(
        run_no=f"EV-{tag}", dataset_id=ds.id, target="kb", status="pending", total_items=len(items)
    )
    db.add(run)
    db.commit()
    ids = (ds.id, run.id)
    db.close()
    return ids


def _cleanup(dataset_id: int) -> None:
    db = SessionLocal()
    run_ids = list(db.execute(select(EvalRun.id).where(EvalRun.dataset_id == dataset_id)).scalars())
    for rid in run_ids:
        db.execute(EvalScore.__table__.delete().where(EvalScore.run_id == rid))
    db.execute(EvalRun.__table__.delete().where(EvalRun.dataset_id == dataset_id))
    db.execute(EvalItem.__table__.delete().where(EvalItem.dataset_id == dataset_id))
    db.execute(EvalDataset.__table__.delete().where(EvalDataset.id == dataset_id))
    db.commit()
    db.close()


def _patch_pipeline(monkeypatch, score_fn) -> None:
    async def fake_query(kb_id, question, top_k=None):  # noqa: ANN001
        return RagAnswer(question=question, answer=f"答-{question}", contexts=["ctx-1"], latency_ms=5)

    monkeypatch.setattr(runner_mod.rag_client, "query", fake_query)
    monkeypatch.setattr(runner_mod.judge, "preflight", lambda *a, **k: "stub-ok")
    monkeypatch.setattr(runner_mod.judge, "score", score_fn)


def _rows(run_id: int) -> list[EvalScore]:
    db = SessionLocal()
    rows = list(
        db.execute(
            select(EvalScore).where(EvalScore.run_id == run_id).order_by(EvalScore.item_seq)
        ).scalars()
    )
    db.close()
    return rows


# ============================ 用例 ============================
def test_refusal_sample_excluded_from_answer_relevancy(monkeypatch):
    """拒答样本的 AR 置空且不参与聚合；其余样本的 AR 原样保留。"""
    ds_id, run_id = _seed(
        db := SessionLocal(),
        [
            ("公司明年打算收购哪家公司？", "知识库中未找到相关信息", "拒答陷阱"),
            ("VPN密码忘了怎么弄？", "在门户的「账号安全」页重置VPN密码", "常规FAQ"),
        ],
    )
    db.close()

    def fake_score(question, answer, contexts, reference_answer, reference_contexts):  # noqa: ANN001
        return JudgeOutcome(
            scores={
                "faithfulness": 1.0,
                "answer_relevancy": 0.0,  # 拒答样本真机就是 0
                "context_precision": 1.0,
                "context_recall": 1.0,
            },
            raw={"stub": True},
            tokens=1,
        )

    try:
        _patch_pipeline(monkeypatch, fake_score)
        asyncio.run(runner_mod.execute_run(run_id))

        rows = _rows(run_id)
        refusal, normal = rows[0], rows[1]
        assert refusal.answer_relevancy is None, "拒答样本不得把 AR=0 计入"
        assert (refusal.metric_scores or {}).get("answer_relevancy_skipped")
        assert normal.answer_relevancy == 0.0, "非拒答样本必须原样保留 AR（哪怕是 0）"

        db = SessionLocal()
        run = db.get(EvalRun, run_id)
        # 只剩 1 条参与聚合 → 0.0；若把拒答也算进来同样是 0.0，故再断言参与条数
        detail = run.metric_detail or {}
        assert run.metrics["answer_relevancy"] == 0.0
        assert detail["by_stratum"]["total"] == 2
        db.close()
    finally:
        _cleanup(ds_id)


def test_refusal_detected_by_reference_when_stratum_missing(monkeypatch):
    """没有分层标签时，靠标准答案话术也能识别拒答题（手工建的数据集常见）。"""

    def fake_score(question, answer, contexts, reference_answer, reference_contexts):  # noqa: ANN001
        return JudgeOutcome(
            scores={"faithfulness": 1.0, "answer_relevancy": 0.0, "context_precision": 1.0, "context_recall": 1.0},
            raw={},
            tokens=1,
        )

    ds_id, run_id = _seed(SessionLocal(), [("公司明年打算收购哪家公司？", "资料中未找到相关信息", None)])
    try:
        _patch_pipeline(monkeypatch, fake_score)
        asyncio.run(runner_mod.execute_run(run_id))
        assert _rows(run_id)[0].answer_relevancy is None
    finally:
        _cleanup(ds_id)


def test_missing_metric_is_visible_in_error_msg(monkeypatch):
    """单指标 NaN 必须在行上留痕，不能只留一个空单元格。"""

    def fake_score(question, answer, contexts, reference_answer, reference_contexts):  # noqa: ANN001
        return JudgeOutcome(
            scores={"faithfulness": 0.9, "answer_relevancy": 0.8, "context_precision": None, "context_recall": 0.7},
            raw={"metrics_used": ["faithfulness", "answer_relevancy", "context_precision", "context_recall"],
                 "missing": ["context_precision"], "missing_retried": True},
            tokens=1,
        )

    ds_id, run_id = _seed(SessionLocal(), [("Q1", "A1", None)])
    try:
        _patch_pipeline(monkeypatch, fake_score)
        asyncio.run(runner_mod.execute_run(run_id))
        row = _rows(run_id)[0]
        assert row.status == "ok"
        assert row.context_precision is None
        assert "context_precision" in (row.error_msg or "")
        assert row.metric_scores["missing"] == ["context_precision"]
    finally:
        _cleanup(ds_id)


def test_timed_out_sample_is_rescued_before_finalize(monkeypatch):
    """超时样本收尾补跑：第一次打分超时，补跑成功 → 不该在报告里留 failed。"""
    monkeypatch.setattr(settings, "JUDGE_ITEM_TIMEOUT", 0.2)
    monkeypatch.setattr(settings, "RUN_RESCUE_BUDGET_SECONDS", 5.0)
    calls = {"n": 0}

    def fake_score(question, answer, contexts, reference_answer, reference_contexts):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:  # 只让第一条第一次卡住
            time.sleep(0.6)
        return JudgeOutcome(
            scores={"faithfulness": 0.9, "answer_relevancy": 0.8, "context_precision": 0.7, "context_recall": 0.6},
            raw={},
            tokens=2,
        )

    ds_id, run_id = _seed(SessionLocal(), [("Q1", "A1", None), ("Q2", "A2", None)])
    try:
        _patch_pipeline(monkeypatch, fake_score)
        asyncio.run(runner_mod.execute_run(run_id))

        db = SessionLocal()
        run = db.get(EvalRun, run_id)
        rows = _rows(run_id)
        db.close()
        assert run.status == "done"
        assert [r.status for r in rows] == ["ok", "ok"], "超时行应被补跑救回"
        assert run.failed_items == 0
        assert run.success_items == 2
        assert run.metrics["faithfulness"] == pytest.approx(0.9)
    finally:
        _cleanup(ds_id)


# ============================ 中文答案的切句（faithfulness 空值真凶）============================
def test_cn_segmenter_splits_markdown_answer():
    """中文 markdown 答案必须能切出陈述，且每句带终止符。

    ragas 默认英文断句器 + 「只保留以 . 。 ! ！ 结尾的句子」这个过滤器，
    会让「表格 + 加粗标题」结构的中文答案命中 **0 句** → 陈述列表为空 →
    faithfulness 直接 NaN（实测 7 条里 3 条如此）。这里钉住切句器的行为。
    """
    from app.services.judge import ChineseMarkdownSegmenter

    answer = (
        "根据知识库资料，年假天数按司龄核定，具体标准如下：\n\n"
        "| 司龄 | 年假天数 |\n"
        "|---|---|\n"
        "| 满1年 | 5天 |\n"
        "| 满3年 | 8天 |\n\n"
        "**补充说明**：年假当年有效，未休部分不折现\n"
    )
    parts = ChineseMarkdownSegmenter().segment(answer)

    assert parts, "中文 markdown 答案切出 0 句 = faithfulness 必空"
    assert all(p.endswith(("。", "！", "？", "；")) for p in parts), "每句要有终止符才能过 ragas 过滤器"
    joined = "".join(parts)
    assert "---" not in joined and "|" not in joined, "markdown 分隔线/竖线要清掉"
    assert any("满1年" in p for p in parts), "表格行要被当成陈述"


def test_segmenter_keeps_plain_chinese_sentences():
    from app.services.judge import ChineseMarkdownSegmenter

    parts = ChineseMarkdownSegmenter().segment("第一步先停机。第二步取出卡纸！第三步检查滚轮")
    assert len(parts) == 3
    assert parts[2].endswith("。"), "没有终止符的尾句要补「。」"


def test_judge_wires_cn_segmenter_into_faithfulness(monkeypatch):
    """_evaluate_metrics 必须把中文切句器挂到 faithfulness 上（否则补丁等于没接）。"""
    import pandas as pd

    import app.services.judge as J
    from ragas.metrics import faithfulness

    captured = {}

    class _FakeResult:
        def to_pandas(self):
            return pd.DataFrame([{"faithfulness": 0.5}])

    def fake_evaluate(dataset, metrics, llm, embeddings, raise_exceptions, run_config):  # noqa: ANN001
        captured["seg"] = metrics[0].sentence_segmenter
        return _FakeResult()

    monkeypatch.setattr(J, "evaluate", fake_evaluate)
    judge = J.RagasJudge()
    out = judge._evaluate_metrics([faithfulness], dataset=None, llm=object(), embeddings=object())

    assert isinstance(captured["seg"], J.ChineseMarkdownSegmenter)
    assert out["faithfulness"] == 0.5


def test_all_upstream_failures_do_not_blame_judge(monkeypatch):
    """上游 401 导致的零成功，文案必须指向上游，不能甩锅给裁判。

    真机事故（2026-10-27）：项目一账号被清空 → 7 条全 skipped → 界面报
    「全部样本未产出有效指标（请查裁判配置与上游可用性）」，排查方向被带偏。
    """
    from app.services.rag_client import UpstreamError

    async def boom(kb_id, question, top_k=None):  # noqa: ANN001
        raise UpstreamError("项目一业务错误 [401] 用户名或密码错误")

    monkeypatch.setattr(runner_mod.rag_client, "query", boom)

    def never_called(*a, **k):  # noqa: ANN001
        raise AssertionError("上游没取到答案就不该打分")

    monkeypatch.setattr(runner_mod.judge, "preflight", lambda *a, **k: "stub-ok")
    monkeypatch.setattr(runner_mod.judge, "score", never_called)

    ds_id, run_id = _seed(SessionLocal(), [("Q1", "A1", None), ("Q2", "A2", None)])
    try:
        asyncio.run(runner_mod.execute_run(run_id))
        db = SessionLocal()
        run = db.get(EvalRun, run_id)
        msg = run.error_msg or ""
        db.close()
        assert run.status == "failed"
        assert "上游" in msg and "401" in msg, msg
        assert "裁判配置" not in msg, "上游认证失败不该提示查裁判"
    finally:
        _cleanup(ds_id)
