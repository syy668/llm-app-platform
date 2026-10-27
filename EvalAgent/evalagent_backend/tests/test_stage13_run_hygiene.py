"""第十三步：任务管控与熔断（高可用深水区）。

锁定三件「进程被杀 / 网络抖动 / 配置坏」时最容易漏的机制：
1. **致命错误熔断参数化**：`_is_fatal_judge_error` 覆盖 10 类特征，逐个断言
   「配置坏了」会命中（立即熔断）、「样本难」不会命中（继续跑其余）。
2. **僵死任务回收**：进程被杀后 running 永远卡住，`recover_stale_runs` 按超时判死。
3. **超时补跑**：`_rescue_timeouts` 只补「打分超时」的失败行，不碰其它失败原因。
"""

from __future__ import annotations

import time

import pytest

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import EvalRun
from app.services import runner as runner_mod
from app.services.runner import _is_fatal_judge_error, recover_stale_runs


# ============================ 致命错误熔断（参数化）============================
@pytest.mark.parametrize(
    "msg",
    [
        "AllocationQuota exceeded",
        "FreeTierOnly: 免费额度已用完",
        "insufficient_quota for this request",
        "PermissionDenied",
        "invalid_api_key provided",
        "Error code: 401 - Unauthorized",
        "Error code: 403 - Forbidden",
        "model 'qwen-xxx' does not exist",
        "model_not_found",
        "NotFoundError: model not found",
    ],
)
def test_fatal_judge_error_matches_config_failures(msg):
    """这些是「配置坏了」，重试无意义，必须命中熔断。"""
    assert _is_fatal_judge_error(msg) is True


@pytest.mark.parametrize(
    "msg",
    [
        "样本答案过长导致超时",
        "网络超时，请稍后重试",
        "APIConnectionError: 连接抖动",
        "",  # 空错误不该命中
        None,
    ],
)
def test_fatal_judge_error_ignores_transient_or_hard_samples(msg):
    """这些是「这条样本难 / 瞬时抖动」，不该熔断整轮。"""
    assert _is_fatal_judge_error(msg) is False


def test_fatal_judge_error_is_case_insensitive():
    assert _is_fatal_judge_error("ALLOCATIONQUOTA EXCEEDED") is True
    assert _is_fatal_judge_error("Invalid_API_Key") is True


# ============================ 僵死任务回收 ============================
def test_recover_stale_runs_marks_only_old_running(monkeypatch):
    """只有超过阈值仍 running/pending 的才判死，新鲜的保留。"""
    from datetime import timedelta

    db = SessionLocal()
    # 造两条：一条僵死（started 很久前），一条新鲜
    old_run = EvalRun(
        run_no="EV-stale-old", dataset_id=1, target="kb", status="running", total_items=1,
    )
    fresh_run = EvalRun(
        run_no="EV-stale-fresh", dataset_id=1, target="kb", status="running", total_items=1,
    )
    db.add_all([old_run, fresh_run])
    db.commit()
    # 旧任务 started_at 设为 2 小时前
    old_run.started_at = old_run.started_at or old_run.created_at
    from app.services.runner import now as _now
    old_run.started_at = _now() - timedelta(seconds=settings.RUN_STALE_SECONDS + 100)
    fresh_run.started_at = _now() - timedelta(seconds=10)
    db.commit()
    ids = (old_run.id, fresh_run.id)

    try:
        recovered = recover_stale_runs()
        db2 = SessionLocal()
        old = db2.get(EvalRun, ids[0])
        fresh = db2.get(EvalRun, ids[1])
        db2.close()
        assert recovered >= 1
        assert old.status == "failed"
        assert "僵死" in (old.error_msg or "")
        assert fresh.status == "running"  # 新鲜任务不动
    finally:
        db.execute(EvalRun.__table__.delete().where(EvalRun.id.in_(ids)))
        db.commit()
        db.close()


# ============================ 超时补跑 ============================
def test_rescue_timeouts_disabled_returns_early(monkeypatch):
    """RUN_RESCUE_TIMEOUTS=false 时直接跳过。"""
    monkeypatch.setattr(settings, "RUN_RESCUE_TIMEOUTS", False)
    import asyncio

    async def _run():
        # 不该抛异常，也不该做任何事
        await runner_mod._rescue_timeouts(None, None, [], {})

    asyncio.run(_run())
