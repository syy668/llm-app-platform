"""v1.13.2 大盘聚合统计（GET /eval/runs/stats）。

为什么单独一个文件：这个接口是**面试大屏**的数据源，一旦口径错了，
界面上四个漂亮的数字全是假的 —— 而且不会报错，没人会发现。
所以把它当「口径契约」来测，重点不是「有没有返回四个指标」，
而是「这个均值到底代表什么」。

覆盖：
  - 空库：四项全 None（不是 0，0 是「测出很差」，None 是「没测」）；
  - 口径：只统计 done，failed 里抢救出来的得分**不进**大盘；
  - 加权：按 success_items 加权，不是 run 级简单平均；
  - 独立分母：某指标为 None 时不进该项分母（四项分母可以不同）；
  - done 但 success_items=0（空数据集/全被跳过）不贡献权重，也不编数据；
  - metrics 为 None / 全 null / 非 dict 时不崩、不计入；
  - bool 不被当成 1/0 计入（int 子类防御）；
  - 计数：total / pending / running / done / failed / cancelled 与库一致；
  - 接口层：401 未登录、200 契约、路由不被 /runs/{run_id} 抢占（不能返回 422）。
"""
import pytest

import app.services.eval_service as eval_service
from app.models.eval_dataset import EvalDataset
from app.models.eval_run import (
    RUN_CANCELLED,
    RUN_DONE,
    RUN_FAILED,
    RUN_PENDING,
    RUN_RUNNING,
    EvalRun,
)
from conftest import assert_error, assert_ok

API = "/api/v1/eval"
METRICS = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")


@pytest.fixture(autouse=True)
def _no_global_table_setup(monkeypatch):
    """屏蔽 ensure_eval_tables 对全局 engine（真实 MySQL）的建表动作。"""
    monkeypatch.setattr(eval_service, "ensure_eval_tables", lambda: None)


def _make_dataset(db, name="统计集", kb_id=6, count=3):
    ds = EvalDataset(kb_id=kb_id, name=name, description=None, item_count=count, created_by=1)
    db.add(ds)
    db.commit()
    db.refresh(ds)
    return ds


def _make_run(db, ds=None, status=RUN_DONE, success=0, total=3, metrics=None):
    if ds is None:
        ds = _make_dataset(db)
    run = EvalRun(
        dataset_id=ds.id,
        kb_id=ds.kb_id,
        model="deepseek-chat",
        status=status,
        total_items=total,
        success_items=success,
        metrics=metrics,
        created_by=1,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _all_metric(v: float | None) -> dict:
    """四项指标取同一个值，方便断言。"""
    return {k: v for k in METRICS}


# ---------------------------------------------------------------------------
# 空值与降级
# ---------------------------------------------------------------------------


def test_empty_db_returns_none_not_zero(eval_db):
    """没有任何任务时四项必须是 None。

    这是全局最重要的一条：前端把 None 渲染成灰色「—」，把 0 渲染成 0.0000。
    若后端偷懒返回 0，界面会显示"平均 Faithfulness 0.0000"，看起来像系统烂透了，
    实际是一条数据都没有 —— 面试现场这种数字是致命的。
    """
    stats = eval_service.run_stats(eval_db)

    assert stats["total"] == 0
    assert stats["metrics"] == {k: None for k in METRICS}
    assert all(v == 0 for v in stats["metric_items"].values())
    assert stats["metric_runs"] == 0
    assert stats["scope"] == "done_weighted"


def test_metrics_none_or_all_null_yields_none(eval_db):
    """历史任务 metrics 可能是 None 或 {k: null}，都不能被算成 0。"""
    ds = _make_dataset(eval_db)
    _make_run(eval_db, ds=ds, status=RUN_DONE, success=5, metrics=None)
    _make_run(eval_db, ds=ds, status=RUN_DONE, success=5, metrics=_all_metric(None))

    stats = eval_service.run_stats(eval_db)

    assert stats["done"] == 2
    assert stats["metrics"] == {k: None for k in METRICS}
    assert stats["metric_runs"] == 0  # 两个都没贡献任何一项


# ---------------------------------------------------------------------------
# 统计口径：done-only
# ---------------------------------------------------------------------------


def test_failed_runs_excluded_from_metrics(eval_db):
    """failed 的 run 不参与大盘均值 —— 这是本接口最核心的口径决定。

    场景：一个 30 条任务跑到第 10 条就挂了，失败的 20 条里有一堆 0 分。
    如果把它混进大盘，整体分数会被"没跑完"这件事本身拉低，而系统能力没变。
    我们自己在 error_msg 里就写着「指标仅代表已完成样本，不可做 A/B 对比」，
    大盘必须守同一条线。
    """
    ds = _make_dataset(eval_db)
    _make_run(eval_db, ds=ds, status=RUN_DONE, success=10, metrics=_all_metric(0.8))
    _make_run(eval_db, ds=ds, status=RUN_FAILED, success=10, metrics=_all_metric(0.1))
    _make_run(eval_db, ds=ds, status=RUN_CANCELLED, success=10, metrics=_all_metric(0.1))

    stats = eval_service.run_stats(eval_db)

    # 只有 done 那个 0.8 参与；failed/cancelled 的 0.1 完全不影响
    assert stats["metrics"]["faithfulness"] == pytest.approx(0.8)
    assert stats["metric_items"]["faithfulness"] == 10
    assert stats["metric_runs"] == 1
    # 但计数上它们必须都在，不能被"隐藏"
    assert stats["done"] == 1
    assert stats["failed"] == 1
    assert stats["cancelled"] == 1
    assert stats["total"] == 3


def test_pending_and_running_counted_but_not_scored(eval_db):
    """pending/running 只计数，绝不进均值（它们还没有任何指标）。"""
    ds = _make_dataset(eval_db)
    _make_run(eval_db, ds=ds, status=RUN_DONE, success=4, metrics=_all_metric(0.6))
    _make_run(eval_db, ds=ds, status=RUN_RUNNING, success=2, metrics=_all_metric(0.0))
    _make_run(eval_db, ds=ds, status=RUN_PENDING, success=0, metrics=None)

    stats = eval_service.run_stats(eval_db)

    assert stats["metrics"]["faithfulness"] == pytest.approx(0.6)
    assert stats["running"] == 1
    assert stats["pending"] == 1
    assert stats["total"] == 3


# ---------------------------------------------------------------------------
# 加权：样本级，不是 run 级
# ---------------------------------------------------------------------------


def test_weighted_by_success_items_not_simple_average(eval_db):
    """按成功样本数加权。

    run A：20 条样本，faithfulness 1.0
    run B：2 条样本，faithfulness 0.0（冒烟任务）
    简单平均 = 0.5（错，把 2 条的冒烟和 20 条的正经任务等权）
    加权平均 = (1.0*20 + 0.0*2) / 22 ≈ 0.909（对，样本级真实水平）
    """
    ds = _make_dataset(eval_db)
    _make_run(eval_db, ds=ds, status=RUN_DONE, success=20, total=20, metrics=_all_metric(1.0))
    _make_run(eval_db, ds=ds, status=RUN_DONE, success=2, total=2, metrics=_all_metric(0.0))

    stats = eval_service.run_stats(eval_db)

    expected = round((1.0 * 20 + 0.0 * 2) / 22, 6)
    assert stats["metrics"]["faithfulness"] == pytest.approx(expected)
    assert stats["metric_items"]["faithfulness"] == 22
    assert stats["metric_runs"] == 2
    # 显式反证：不能等于 run 级简单平均
    assert stats["metrics"]["faithfulness"] != pytest.approx(0.5)


def test_per_metric_denominator_independent(eval_db):
    """context_recall 为 None 的样本不进 recall 的分母，其他三项照常。

    真实场景：样本缺参考答案 → context_recall 算不出来（null），
    但 faithfulness/relevancy/precision 都算得出来。四项分母本来就该不同。
    """
    ds = _make_dataset(eval_db)
    _make_run(
        eval_db,
        ds=ds,
        status=RUN_DONE,
        success=10,
        metrics={
            "faithfulness": 0.9,
            "answer_relevancy": 0.9,
            "context_precision": 0.9,
            "context_recall": None,
        },
    )
    _make_run(
        eval_db,
        ds=ds,
        status=RUN_DONE,
        success=10,
        metrics={
            "faithfulness": 0.5,
            "answer_relevancy": 0.5,
            "context_precision": 0.5,
            "context_recall": 0.5,
        },
    )

    stats = eval_service.run_stats(eval_db)

    assert stats["metric_items"]["context_recall"] == 10      # 只有第二个 run
    assert stats["metric_items"]["faithfulness"] == 20        # 两个 run
    # recall 只由第二个 run 决定 → 0.5；faith 两项都算 → 0.7
    assert stats["metrics"]["context_recall"] == pytest.approx(0.5)
    assert stats["metrics"]["faithfulness"] == pytest.approx(0.7)


def test_done_run_without_success_items_contributes_nothing(eval_db):
    """done 但 success_items=0（空数据集 / 全部被 direct 路由跳过）不贡献权重。

    此时 metrics 可能有值也可能为 null；没有样本数就没有权重，
    不能补一个默认权重 1 硬塞进去 —— 那是编数据。
    """
    ds = _make_dataset(eval_db)
    _make_run(eval_db, ds=ds, status=RUN_DONE, success=0, total=0, metrics=_all_metric(0.99))
    _make_run(eval_db, ds=ds, status=RUN_DONE, success=10, total=10, metrics=_all_metric(0.4))

    stats = eval_service.run_stats(eval_db)

    # 0 样本那个 0.99 不得稀释结果
    assert stats["metrics"]["faithfulness"] == pytest.approx(0.4)
    assert stats["metric_items"]["faithfulness"] == 10
    assert stats["metric_runs"] == 1
    assert stats["done"] == 2  # 计数仍然算它一个 done


def test_non_numeric_and_bool_values_ignored(eval_db):
    """脏数据防御：字符串、bool 不能被当成数字混进均值。

    bool 是 int 的子类，`isinstance(True, int)` 为真 —— 不显式排除的话
    一个 True 会被当成 1.0 加进去，把均值抬到 1 附近。
    """
    ds = _make_dataset(eval_db)
    _make_run(
        eval_db,
        ds=ds,
        status=RUN_DONE,
        success=10,
        metrics={
            "faithfulness": True,
            "answer_relevancy": "0.9",
            "context_precision": 0.8,
            "context_recall": None,
        },
    )

    stats = eval_service.run_stats(eval_db)

    assert stats["metrics"]["faithfulness"] is None       # bool 被排除
    assert stats["metrics"]["answer_relevancy"] is None   # 字符串被排除
    assert stats["metrics"]["context_precision"] == pytest.approx(0.8)
    assert stats["metric_runs"] == 1                       # 有一项有效即算贡献


def test_metrics_non_dict_ignored(eval_db):
    """metrics 列被写成数组/字符串等异常类型时不崩、不计入。"""
    ds = _make_dataset(eval_db)
    _make_run(eval_db, ds=ds, status=RUN_DONE, success=5, metrics=["faithfulness", 0.9])
    _make_run(eval_db, ds=ds, status=RUN_DONE, success=5, metrics=_all_metric(0.5))

    stats = eval_service.run_stats(eval_db)

    assert stats["metrics"]["faithfulness"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 接口层
# ---------------------------------------------------------------------------


def test_api_requires_login(client):
    """未登录不给看大盘（与 list_runs 同权限：登录可读）。"""
    assert_error(client.get(f"{API}/runs/stats"), 401, 401)


def test_api_route_not_shadowed_by_run_id_route(admin_client, monkeypatch):
    """/runs/stats 必须被自己的路由接住，不能被 /runs/{run_id} 当成 run_id。

    回归点：如果路由声明顺序反了，FastAPI 会先匹配 /runs/{run_id}，
    然后 int 转换 "stats" 失败 → **422**。这个错很隐蔽：接口"能用"，
    只是永远返回 422，前端大盘一直显示「—」。

    接口层按项目惯例 monkeypatch 服务层，不依赖真实表（auth 与路由是这层的关注点）。
    """
    payload = {
        "total": 3,
        "pending": 1,
        "running": 1,
        "done": 1,
        "failed": 0,
        "cancelled": 0,
        "metrics": {k: 0.5 for k in METRICS},
        "metric_items": {k: 7 for k in METRICS},
        "metric_runs": 1,
        "scope": "done_weighted",
    }
    monkeypatch.setattr(eval_service, "run_stats", lambda db: payload)

    data = assert_ok(admin_client.get(f"{API}/runs/stats"))

    assert data["scope"] == "done_weighted"
    for key in ("total", "pending", "running", "done", "failed", "cancelled"):
        assert isinstance(data[key], int)
    assert set(data["metrics"]) == set(METRICS)
    assert set(data["metric_items"]) == set(METRICS)
    assert data["total"] == 3


def test_api_returns_null_metrics_untouched(admin_client, monkeypatch):
    """服务层给 null 时接口必须原样透出 null —— 不能在 API 层被 `or 0` 兜成 0。"""
    monkeypatch.setattr(
        eval_service,
        "run_stats",
        lambda db: {
            "total": 0,
            "pending": 0,
            "running": 0,
            "done": 0,
            "failed": 0,
            "cancelled": 0,
            "metrics": {k: None for k in METRICS},
            "metric_items": {k: 0 for k in METRICS},
            "metric_runs": 0,
            "scope": "done_weighted",
        },
    )

    data = assert_ok(admin_client.get(f"{API}/runs/stats"))

    assert data["metrics"] == {k: None for k in METRICS}
    assert all(v is None for v in data["metrics"].values())
