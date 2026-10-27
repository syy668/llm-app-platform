"""v1.13 评测任务「真停止」+ 批量并发闸。

为什么单独一个文件：这两个能力都是**防事故**而不是加功能，回归了没人会发现 ——
「停止」按钮点了不生效比没有按钮更糟（用户以为停了，后台在扣费）；并发闸失效则让
批量执行从效率工具变成 429 放大器。所以把状态机和边界全部钉死。

覆盖：
  - cancel_run 状态机：pending/running 可停、done/failed 报 400、cancelled 幂等；
  - cancel_run 审计：operation_logs 落 eval_run_cancel；
  - delete_run 保护：pending/running 拒绝删除（否则后台线程写孤儿得分）；
  - create_run 并发闸：达上限报 429，未达上限放行；
  - _is_cancel_requested：cancelled 为真、running 为假、探测异常按未取消处理；
  - _run_eval_background 认领：只有 pending 才转 running，已取消的直接退出；
  - 接口层：成功契约、403、401、已结束任务 400。
"""
import pytest
from sqlalchemy.orm import sessionmaker

import app.services.eval_service as eval_service
from app.core.exceptions import BizError, NotFoundError
from app.models.eval_dataset import EvalDataset
from app.models.eval_run import (
    RUN_CANCELLED,
    RUN_DONE,
    RUN_FAILED,
    RUN_PENDING,
    RUN_RUNNING,
    EvalRun,
)
from app.models.operation_log import OperationLog
from app.schemas.eval import RunCreate
from conftest import assert_error, assert_ok, make_user
from datetime import datetime, timedelta

API = "/api/v1/eval"


@pytest.fixture(autouse=True)
def _no_global_table_setup(monkeypatch):
    """屏蔽 ensure_eval_tables 对全局 engine（真实 MySQL）的建表动作。"""
    monkeypatch.setattr(eval_service, "ensure_eval_tables", lambda: None)


@pytest.fixture
def patch_session_local(monkeypatch, sqlite_engine):
    """把服务里的 SessionLocal 指向测试 sqlite。

    `_is_cancel_requested` 刻意用**独立会话**探测取消标记（后台线程自己的会话在
    MySQL REPEATABLE READ 下读不到外部提交），所以测试必须给它一个能用的工厂，
    否则会去连真实 MySQL。
    """
    monkeypatch.setattr(eval_service, "SessionLocal", sessionmaker(bind=sqlite_engine))


def _make_dataset(db, name="测试集", kb_id=6, count=3):
    ds = EvalDataset(kb_id=kb_id, name=name, description=None, item_count=count, created_by=1)
    db.add(ds)
    db.commit()
    db.refresh(ds)
    return ds


def _make_run(db, ds=None, status=RUN_RUNNING, success=0, total=3):
    if ds is None:
        ds = _make_dataset(db)
    run = EvalRun(
        dataset_id=ds.id,
        kb_id=ds.kb_id,
        model="deepseek-chat",
        status=status,
        total_items=total,
        success_items=success,
        # running 必须有 started_at：僵尸判定取 COALESCE(started_at, created_at)，
        # 而 created_at 是数据库 server_default 生成的（会话时区可能是 UTC），
        # 在测试里与 datetime.now() 不一定一致，会让"刚建的任务"被判成僵尸。
        # 生产里 started_at 由 Python 写入，天然同源。
        started_at=None if status == RUN_PENDING else datetime.now(),
        created_by=1,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


# ---------------------------------------------------------------------------
# 服务层：cancel_run 状态机
# ---------------------------------------------------------------------------

def test_cancel_running_run(eval_db):
    """核心场景：跑到一半点停止。"""
    run = _make_run(eval_db, status=RUN_RUNNING, success=1)

    data = eval_service.cancel_run(eval_db, run.id, make_user("admin"))

    assert data["status"] == RUN_CANCELLED
    # 已完成条数要带回去，前端据此提示「保留已完成 N 条」
    assert data["success_items"] == 1
    eval_db.expire_all()
    assert eval_db.get(EvalRun, run.id).status == RUN_CANCELLED


def test_cancel_pending_run_before_start(eval_db):
    """刚创建、线程还没认领就取消：必须也能停（否则用户会看到点了没反应）。"""
    run = _make_run(eval_db, status=RUN_PENDING)

    data = eval_service.cancel_run(eval_db, run.id)

    assert data["status"] == RUN_CANCELLED


def test_cancel_is_idempotent(eval_db):
    """重复调用不报错。

    前端可能因网络重试重复发请求；如果第二次回 400，用户会以为没停下而反复点。
    """
    run = _make_run(eval_db, status=RUN_RUNNING)

    first = eval_service.cancel_run(eval_db, run.id)
    second = eval_service.cancel_run(eval_db, run.id)

    assert first["status"] == RUN_CANCELLED
    assert second["status"] == RUN_CANCELLED


@pytest.mark.parametrize("status", [RUN_DONE, RUN_FAILED])
def test_cancel_finished_run_rejected(eval_db, status):
    """已结束的任务没有可停的东西，必须报 400 而不是静默成功。"""
    run = _make_run(eval_db, status=status)

    with pytest.raises(BizError) as exc:
        eval_service.cancel_run(eval_db, run.id)

    assert exc.value.code == 400
    assert status in exc.value.message or status in str(exc.value)


def test_cancel_missing_run_raises_not_found(eval_db):
    with pytest.raises(NotFoundError):
        eval_service.cancel_run(eval_db, 999999)


def test_cancel_writes_audit_log(eval_db):
    """取消是管理动作，必须留痕：谁停了哪个任务的什么状态。"""
    run = _make_run(eval_db, status=RUN_RUNNING, success=2, total=5)

    eval_service.cancel_run(eval_db, run.id, make_user("admin"))

    logs = eval_db.query(OperationLog).filter(OperationLog.action == "eval_run_cancel").all()
    assert len(logs) == 1
    assert logs[0].target_type == "eval_run"
    assert logs[0].target_id == str(run.id)
    assert logs[0].user_id == 1
    assert logs[0].detail["success_items"] == 2
    assert logs[0].detail["total_items"] == 5


def test_cancel_survives_audit_failure(eval_db, monkeypatch):
    """审计写失败不能回滚已提交的状态变更 —— 否则用户点了停止，任务还在跑。"""
    run = _make_run(eval_db, status=RUN_RUNNING)

    original_add = eval_db.add

    def _boom(obj):
        if isinstance(obj, OperationLog):
            raise RuntimeError("audit table broken")
        return original_add(obj)

    monkeypatch.setattr(eval_db, "add", _boom)

    data = eval_service.cancel_run(eval_db, run.id)

    assert data["status"] == RUN_CANCELLED
    monkeypatch.undo()
    eval_db.expire_all()
    assert eval_db.get(EvalRun, run.id).status == RUN_CANCELLED


# ---------------------------------------------------------------------------
# 服务层：delete_run 保护
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [RUN_PENDING, RUN_RUNNING])
def test_delete_run_rejected_while_active(eval_db, status):
    """运行中的任务不许删。

    删掉之后后台线程收尾时 db.get 拿到 None：既写不进最终状态，中途
    _flush_scores 还会往已删除的 run_id 插孤儿得分，是写不进的脏数据。
    """
    run = _make_run(eval_db, status=status)

    with pytest.raises(BizError) as exc:
        eval_service.delete_run(eval_db, run.id)

    assert exc.value.code == 400
    assert "停止" in exc.value.message


@pytest.mark.parametrize("status", [RUN_DONE, RUN_FAILED, RUN_CANCELLED])
def test_delete_run_allowed_after_terminal(eval_db, status):
    """终态任务（含 cancelled）可以删 —— 停止之后要能清理掉。"""
    run = _make_run(eval_db, status=status)

    data = eval_service.delete_run(eval_db, run.id)

    assert data["deleted_run_id"] == run.id
    eval_db.expire_all()
    assert eval_db.get(EvalRun, run.id) is None


# ---------------------------------------------------------------------------
# 服务层：create_run 并发闸
# ---------------------------------------------------------------------------

def test_create_run_rejected_when_active_limit_reached(eval_db, monkeypatch):
    """达上限返回 429，且报错信息里带上限值与当前值（前端才能提示清楚）。"""
    monkeypatch.setattr(eval_service.settings, "eval_max_active_runs", 2)
    monkeypatch.setattr(eval_service, "_run_eval_background", lambda run_id: None)
    ds = _make_dataset(eval_db)
    _make_run(eval_db, ds=ds, status=RUN_RUNNING)
    _make_run(eval_db, ds=ds, status=RUN_PENDING)

    with pytest.raises(BizError) as exc:
        eval_service.create_run(eval_db, make_user("admin"), RunCreate(dataset_id=ds.id))

    assert exc.value.code == 429
    assert "2" in exc.value.message
    assert "当前" in exc.value.message


def test_create_run_allowed_below_active_limit(eval_db, monkeypatch):
    monkeypatch.setattr(eval_service.settings, "eval_max_active_runs", 2)
    monkeypatch.setattr(eval_service, "_run_eval_background", lambda run_id: None)
    ds = _make_dataset(eval_db)
    _make_run(eval_db, ds=ds, status=RUN_RUNNING)

    data = eval_service.create_run(eval_db, make_user("admin"), RunCreate(dataset_id=ds.id))

    assert data["status"] == RUN_PENDING


def test_create_run_terminal_runs_do_not_count(eval_db, monkeypatch):
    """闸只数 pending/running：历史上一堆 done/failed 不能把新任务挡住。"""
    monkeypatch.setattr(eval_service.settings, "eval_max_active_runs", 1)
    monkeypatch.setattr(eval_service, "_run_eval_background", lambda run_id: None)
    ds = _make_dataset(eval_db)
    for status in (RUN_DONE, RUN_FAILED, RUN_CANCELLED):
        _make_run(eval_db, ds=ds, status=status)

    data = eval_service.create_run(eval_db, make_user("admin"), RunCreate(dataset_id=ds.id))

    assert data["status"] == RUN_PENDING


def test_create_run_missing_dataset_reports_404_not_429(eval_db, monkeypatch):
    """数据集校验要排在闸之前：数据集不存在应报 404，报 429 会误导排查方向。"""
    monkeypatch.setattr(eval_service.settings, "eval_max_active_runs", 1)
    ds = _make_dataset(eval_db)
    _make_run(eval_db, ds=ds, status=RUN_RUNNING)

    with pytest.raises(NotFoundError):
        eval_service.create_run(eval_db, make_user("admin"), RunCreate(dataset_id=999999))


# ---------------------------------------------------------------------------
# 服务层：_is_cancel_requested 探测
# ---------------------------------------------------------------------------

def test_is_cancel_requested_true_for_cancelled(eval_db, patch_session_local):
    run = _make_run(eval_db, status=RUN_CANCELLED)
    assert eval_service._is_cancel_requested(run.id) is True


@pytest.mark.parametrize("status", [RUN_PENDING, RUN_RUNNING, RUN_DONE, RUN_FAILED])
def test_is_cancel_requested_false_for_other_status(eval_db, patch_session_local, status):
    run = _make_run(eval_db, status=status)
    assert eval_service._is_cancel_requested(run.id) is False


def test_is_cancel_requested_false_when_run_missing(eval_db, patch_session_local):
    """任务被删了不该让后台线程误以为要取消（正常也走不到，兜底语义要明确）。"""
    assert eval_service._is_cancel_requested(999999) is False


def test_is_cancel_requested_false_when_probe_fails(eval_db, monkeypatch):
    """探测异常必须按「未取消」处理。

    反过来的代价是：一次数据库抖动就把正在正常执行的任务误杀，用户已付费的
    算力全丢。宁可多跑一会儿。
    """

    def _boom():
        raise RuntimeError("db is down")

    monkeypatch.setattr(eval_service, "SessionLocal", _boom)

    assert eval_service._is_cancel_requested(1) is False


# ---------------------------------------------------------------------------
# 服务层：后台线程认领（竞态）
# ---------------------------------------------------------------------------

def test_background_claim_skips_cancelled_pending_run(eval_db, monkeypatch, sqlite_engine):
    """竞态核心用例：pending 阶段被取消后，后台线程不能再把它改回 running。

    不能用「get 出来改字段再 commit」实现认领 —— 读到 pending 之后、写回之前
    用户可能已经改成 cancelled，再写 running 就把取消覆盖了，用户会看到
    明明点了停止任务还在跑。
    """
    monkeypatch.setattr(eval_service, "SessionLocal", sessionmaker(bind=sqlite_engine))
    run = _make_run(eval_db, status=RUN_CANCELLED)
    eval_db.commit()

    eval_service._run_eval_background(run.id)

    eval_db.expire_all()
    assert eval_db.get(EvalRun, run.id).status == RUN_CANCELLED


def test_background_claim_promotes_pending_to_running(eval_db, monkeypatch, sqlite_engine):
    """正常路径回归：没被取消的 pending 必须能被认领并推进到 running。

    数据集里没有样本，函数会在「空数据集」分支直接置 failed 返回 —— 这里只
    验证认领那一步真的把 pending 推进了（否则整个评测都起不来）。
    """
    monkeypatch.setattr(eval_service, "SessionLocal", sessionmaker(bind=sqlite_engine))
    run = _make_run(eval_db, status=RUN_PENDING, total=0)
    eval_db.commit()

    eval_service._run_eval_background(run.id)

    eval_db.expire_all()
    fresh = eval_db.get(EvalRun, run.id)
    # 空数据集走 failed 分支，证明认领成功且后续流程真的跑到了
    assert fresh.status == RUN_FAILED
    assert "数据集为空" in (fresh.error_msg or "")
    assert fresh.started_at is not None


# ---------------------------------------------------------------------------
# 服务层：僵尸任务清理（并发闸的配套自救）
# ---------------------------------------------------------------------------
# 背景：真实库里实测发现 13 个停在 running 的历史任务（批量执行后进程重启，
# daemon 线程随之消失）。它们永久占着并发闸名额 —— 不清理的话，上线这个闸
# 等于把所有人挡在门外，而且报错信息看不出是僵尸导致的。

def test_cleanup_marks_stale_pending_as_failed(eval_db, monkeypatch, sqlite_engine):
    """启动期（clear_pending=True）残留 pending 一律清理。

    新进程不会接管旧线程，此刻还停在 pending 的必然是僵尸 —— 它们占着并发闸
    名额，不清掉用户就永远建不了新任务。这里刻意**不看时间**：created_at 由
    数据库 server_default 生成，会话时区可能与 Python 不一致。
    """
    monkeypatch.setattr(eval_service, "SessionLocal", sessionmaker(bind=sqlite_engine))
    ds = _make_dataset(eval_db)
    run = _make_run(eval_db, ds=ds, status=RUN_PENDING)

    cleaned = eval_service.cleanup_stale_running_runs(600, clear_pending=True)

    assert cleaned == 1
    eval_db.expire_all()
    fresh = eval_db.get(EvalRun, run.id)
    assert fresh.status == RUN_FAILED
    assert "未能启动" in fresh.error_msg
    assert fresh.finished_at is not None


def test_cleanup_keeps_fresh_pending(eval_db, monkeypatch, sqlite_engine):
    """运行期（默认 clear_pending=False）绝不动 pending。

    这是防误杀的关键：create_run 撞闸自救时会调本函数，若默认就清 pending，
    会把「刚触发、线程正准备认领」的正常任务当成僵尸杀掉。
    """
    monkeypatch.setattr(eval_service, "SessionLocal", sessionmaker(bind=sqlite_engine))
    ds = _make_dataset(eval_db)
    run = _make_run(eval_db, ds=ds, status=RUN_PENDING)

    cleaned = eval_service.cleanup_stale_running_runs(600)

    assert cleaned == 0
    eval_db.expire_all()
    assert eval_db.get(EvalRun, run.id).status == RUN_PENDING


def test_create_run_does_not_silently_cleanup_on_block(eval_db, monkeypatch, sqlite_engine):
    """撞闸时**不许**偷偷清理别的任务，只能在报错里引导用户显式清理。

    反例（实现过又撤掉的版本）：撞闸自动把超阈值的 running 标成 failed。问题是一个
    30 样本的任务跑过 10 分钟（默认阈值）很正常，它的线程其实还活着，跑完又把状态
    写回 done，界面就会「失败了 → 又完成了」。在创建任务的路径上改别的任务状态，
    违反最小惊讶原则，也违反「停止/清理必须是显式动作」这条底线。
    """
    monkeypatch.setattr(eval_service, "SessionLocal", sessionmaker(bind=sqlite_engine))
    monkeypatch.setattr(eval_service.settings, "eval_max_active_runs", 1)
    monkeypatch.setattr(eval_service.settings, "eval_stale_run_seconds", 600)
    monkeypatch.setattr(eval_service, "_run_eval_background", lambda run_id: None)
    ds = _make_dataset(eval_db)
    zombie = _make_run(eval_db, ds=ds, status=RUN_RUNNING)
    eval_db.query(EvalRun).filter(EvalRun.id == zombie.id).update(
        {"started_at": datetime.now() - timedelta(seconds=7200)}
    )
    eval_db.commit()

    with pytest.raises(BizError) as exc:
        eval_service.create_run(eval_db, make_user("admin"), RunCreate(dataset_id=ds.id))

    assert exc.value.code == 429
    # 报错必须告诉用户怎么自救，否则只会看到「上限已满」而无从下手
    assert "cleanup-stale" in exc.value.message
    eval_db.expire_all()
    # 关键断言：僵尸任务的状态**没有被这次调用改动**
    assert eval_db.get(EvalRun, zombie.id).status == RUN_RUNNING


def test_create_run_still_blocked_by_genuinely_active_run(eval_db, monkeypatch, sqlite_engine):
    """自救只清僵尸：真正在跑的任务不能被当成僵尸放过（否则闸形同虚设）。"""
    monkeypatch.setattr(eval_service, "SessionLocal", sessionmaker(bind=sqlite_engine))
    monkeypatch.setattr(eval_service.settings, "eval_max_active_runs", 1)
    monkeypatch.setattr(eval_service.settings, "eval_stale_run_seconds", 600)
    monkeypatch.setattr(eval_service, "_run_eval_background", lambda run_id: None)
    ds = _make_dataset(eval_db)
    _make_run(eval_db, ds=ds, status=RUN_RUNNING)  # started_at = 刚建，不算僵尸

    with pytest.raises(BizError) as exc:
        eval_service.create_run(eval_db, make_user("admin"), RunCreate(dataset_id=ds.id))

    assert exc.value.code == 429


# ---------------------------------------------------------------------------
# 接口层
# ---------------------------------------------------------------------------

def test_api_cancel_run_ok(admin_client, eval_db, monkeypatch):
    run = _make_run(eval_db, status=RUN_RUNNING, success=1)
    monkeypatch.setattr(
        "app.api.v1.eval.eval_service.cancel_run",
        lambda db, run_id, user=None: {"id": run.id, "status": RUN_CANCELLED, "success_items": 1},
    )

    data = assert_ok(admin_client.post(f"{API}/runs/{run.id}/cancel"))

    assert data["status"] == RUN_CANCELLED


def test_api_cancel_run_requires_admin(user_client, eval_db):
    run = _make_run(eval_db, status=RUN_RUNNING)

    assert_error(user_client.post(f"{API}/runs/{run.id}/cancel"), 403, 403)


def test_api_cancel_run_requires_login(client, eval_db):
    run = _make_run(eval_db, status=RUN_RUNNING)

    assert_error(client.post(f"{API}/runs/{run.id}/cancel"), 401, 401)


def test_api_cancel_finished_run_400(admin_client, eval_db, monkeypatch):
    """接口层把服务的 BizError(400) 透成 400 —— 而不是 500。"""
    run = _make_run(eval_db, status=RUN_DONE)

    def _raise(db, run_id, user=None):
        raise BizError(400, "任务已结束（done），无法停止")

    monkeypatch.setattr("app.api.v1.eval.eval_service.cancel_run", _raise)

    assert_error(admin_client.post(f"{API}/runs/{run.id}/cancel"), 400, 400)


def test_api_cleanup_stale_ok(admin_client, monkeypatch):
    """显式清理入口：返回清理条数，0 也是成功（不是错误）。"""
    monkeypatch.setattr(
        "app.api.v1.eval.eval_service.cleanup_stale_running_runs", lambda: 3
    )

    data = assert_ok(admin_client.post(f"{API}/runs/cleanup-stale"))

    assert data["cleared"] == 3


def test_api_cleanup_stale_requires_admin(user_client):
    assert_error(user_client.post(f"{API}/runs/cleanup-stale"), 403, 403)
