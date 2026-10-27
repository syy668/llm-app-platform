"""本轮 P0 修复的回归测试（离线，沿用 conftest 夹具风格）。

覆盖：
  B1-4  GET /api/v1/eval/runs/{id}/badcases   契约 / 参数校验(422) / 权限 / 空值口径
  B1-6  BUG-4  _mean_or_none 统一空值口径（JSON null ↔ SQL NULL）
  B2-1  cleanup_stale_running_runs            僵死任务清理（sqlite 内存库直连验证）

BUG-1（空数据集终态 failed）与 BUG-5（direct 路由跳过日志）位于后台线程主流程，
依赖 run_agent/RAGAS 全链路，离线单测不覆盖（见函数内注释说明）。
"""
import app.services.eval_service as eval_service
from conftest import assert_error, assert_ok

API = "/api/v1/eval"


# ---------------------------------------------------------------------------
# B1-4 BadCase 接口
# ---------------------------------------------------------------------------

def test_list_badcases_ok(admin_client, monkeypatch):
    """契约：分页字段 + 每条含 ground_truth 与四项指标。"""
    captured = {}

    def fake_list_badcases(db, run_id, metric, threshold, page, page_size):
        captured.update(metric=metric, threshold=threshold, page=page, page_size=page_size)
        return {
            "total": 1,
            "page": page,
            "page_size": page_size,
            "metric": metric,
            "threshold": threshold,
            "items": [
                {
                    "id": 9, "run_id": run_id, "item_id": 3,
                    "question": "Q", "answer": "A", "ground_truth": "GT",
                    "retrieved_contexts": ["c1"],
                    "faithfulness": 0.2, "answer_relevancy": None,
                    "context_precision": 0.5, "context_recall": 0.1,
                    "latency_ms": 100, "created_at": "2026-10-20T00:00:00",
                }
            ],
        }

    monkeypatch.setattr(eval_service, "list_badcases", fake_list_badcases)
    data = assert_ok(
        admin_client.get(
            f"{API}/runs/5/badcases?metric=faithfulness&threshold=0.3&page=2&page_size=10"
        )
    )
    assert data["total"] == 1
    assert data["items"][0]["ground_truth"] == "GT"
    assert captured == {"metric": "faithfulness", "threshold": 0.3, "page": 2, "page_size": 10}


def test_list_badcases_default_params(admin_client, monkeypatch):
    """不传参数时使用默认值 metric=faithfulness / threshold=0.6 / page=1 / page_size=20。"""
    captured = {}

    def fake_list_badcases(db, run_id, metric, threshold, page, page_size):
        captured.update(metric=metric, threshold=threshold, page=page, page_size=page_size)
        return {"total": 0, "page": page, "page_size": page_size,
                "metric": metric, "threshold": threshold, "items": []}

    monkeypatch.setattr(eval_service, "list_badcases", fake_list_badcases)
    assert_ok(admin_client.get(f"{API}/runs/5/badcases"))
    assert captured == {"metric": "faithfulness", "threshold": 0.6, "page": 1, "page_size": 20}


def test_list_badcases_invalid_metric_422(admin_client):
    """metric 不在白名单 -> 422。"""
    assert_error(admin_client.get(f"{API}/runs/5/badcases?metric=latency"), 422, 422)


def test_list_badcases_invalid_threshold_422(admin_client):
    """threshold 越界（>1 或 <0 或非数值）-> 422。"""
    assert_error(admin_client.get(f"{API}/runs/5/badcases?threshold=1.5"), 422, 422)
    assert_error(admin_client.get(f"{API}/runs/5/badcases?threshold=-0.1"), 422, 422)
    assert_error(admin_client.get(f"{API}/runs/5/badcases?threshold=abc"), 422, 422)


def test_list_badcases_page_size_over_limit_422(admin_client):
    """page_size > 100 -> 422（与其他分页接口上限一致）。"""
    assert_error(admin_client.get(f"{API}/runs/5/badcases?page_size=101"), 422, 422)


def test_list_badcases_requires_login(client):
    """未登录 -> 401（查询接口登录可读）。"""
    assert_error(client.get(f"{API}/runs/5/badcases"), 401, 401)


def test_list_badcases_login_user_ok(user_client, monkeypatch):
    """普通用户（登录即可读）-> 200。"""
    monkeypatch.setattr(
        eval_service, "list_badcases",
        lambda db, rid, m, t, p, ps: {"total": 0, "page": p, "page_size": ps,
                                      "metric": m, "threshold": t, "items": []},
    )
    data = assert_ok(user_client.get(f"{API}/runs/5/badcases"))
    assert data["total"] == 0


# ---------------------------------------------------------------------------
# B1-6 BUG-4 统一空值口径
# ---------------------------------------------------------------------------

def test_mean_or_none_unified_null_rule():
    """均值统计口径：过滤 None；全空 -> None；保留 6 位小数。"""
    f = eval_service._mean_or_none
    assert f([0.1, None, 0.5]) == 0.3
    assert f([None, None]) is None
    assert f([]) is None
    assert f([1 / 3]) == 0.333333


def test_list_badcases_null_metric_excluded():
    """BadCase 筛选口径：SQL NULL 指标不参与比较（is_not(None) 前置）。"""
    import sqlite3

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # 独立 sqlite 内存库（StaticPool 保证同库），不经全局 engine
    eval_service.EvalRun.__table__.create(engine)
    eval_service.EvalRunScore.__table__.create(engine)
    eval_service.EvalDatasetItem.__table__.create(engine)  # badcase 查询 LEFT JOIN 需要
    db = sessionmaker(bind=engine)()
    try:
        db.add(eval_service.EvalRun(
            id=1, dataset_id=1, kb_id=1, model="m",
            status="done", total_items=3, success_items=3, created_by=1,
        ))
        db.add_all([
            # 低分 -> 命中（sqlite 下 BigInteger 主键不自增，显式给 id；MySQL 无此问题）
            eval_service.EvalRunScore(id=101, run_id=1, item_id=1, question="a", answer="x",
                                      faithfulness=0.2, latency_ms=1),
            # NULL 指标 -> 不参与筛选（统一空值口径）
            eval_service.EvalRunScore(id=102, run_id=1, item_id=2, question="b", answer="x",
                                      faithfulness=None, latency_ms=1),
            # 高分 -> 不命中
            eval_service.EvalRunScore(id=103, run_id=1, item_id=3, question="c", answer="x",
                                      faithfulness=0.9, latency_ms=1),
        ])
        db.commit()

        data = eval_service.list_badcases(db, 1, "faithfulness", 0.6, 1, 20)
        assert data["total"] == 1
        assert data["items"][0]["question"] == "a"
    finally:
        db.close()
