"""W6 接口测试（W5-Part2 欠账收口）：对照 docs/W6接口设计文档.md §5 验收清单。

覆盖：
1. viewer GET /tasks 仅本人任务 + creator_name=None + 传他人 creator_id 静默覆盖（验收 1）
2. GET /tasks 分页 / 状态过滤 / 关键词 / 排序 / 非法参数 422
3. GET /dashboards/summary 与种子数据对账 + viewer token 脱敏（验收 2/3）
4. GET /dashboards/tools 按 task_executions 聚合
5. /eval/runs 列表过滤 + viewer 403（验收 3）
6. /eval/runs/summary A/B 聚合与 token_saving_rate 口径（验收 4）
7. /eval/metrics 按天趋势
8. POST /eval/runs mock 立即 DONE（验收 5）+ 非 mock 空地址 502 且本地 FAILED（验收 6）
9. 401 / 信封 trace_id 非空（验收 7）
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.schemas.enums import (
    EvalRunStatus,
    EvalSource,
    EvalVariant,
    ExecutionNode,
    TaskStatus,
    UserRole,
)
from app.schemas.orm_models import EvalRun, Task, TaskExecution, User
from app.utils.ids import new_trace_id
from app.utils.time import utcnow

TEST_PASSWORD = "Passw0rd!"


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------
async def _login(client: AsyncClient, username: str) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": TEST_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["access_token"]


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_task(
    session: AsyncSession,
    creator_id: int,
    *,
    goal: str,
    status: TaskStatus,
    created_at,
    finished_at=None,
    token_usage: int = 1000,
    task_no: str | None = None,
    trace_id: str | None = None,
    error: dict | None = None,
) -> Task:
    task = Task(
        task_no=task_no or f"AFTEST{len(goal):04d}-{creator_id}-{created_at.strftime('%H%M%S%f')[:6]}",
        creator_id=creator_id,
        goal=goal,
        time_range={"start": "2026-10-01", "end": "2026-10-21"},
        status=status,
        replan_count=0,
        token_usage=token_usage,
        trace_id=trace_id or new_trace_id(),
        error=error,
        created_at=created_at,
        finished_at=finished_at,
    )
    session.add(task)
    await session.flush()
    return task


@pytest_asyncio.fixture
async def seeded_tasks(session_factory: async_sessionmaker[AsyncSession], seeded_users: dict[str, int]):
    """种子任务：admin 2 条（1 完成 1 待审批）、approver 1 条完成、viewer 2 条（1 完成 1 失败）。

    完成任务带 finished_at（= created_at + 100s），便于 duration 断言。
    """
    base = utcnow().replace(microsecond=0)
    rows: list[Task] = []
    async with session_factory() as session:
        admin = await session.get(User, seeded_users["admin_user"])
        approver = await session.get(User, seeded_users["approver_user"])
        viewer = await session.get(User, seeded_users["viewer_user"])

        rows.append(
            await _seed_task(
                session, admin.id, goal="分析华南大促退货率异常", status=TaskStatus.COMPLETED,
                created_at=base - timedelta(hours=2), finished_at=base - timedelta(hours=2) + timedelta(seconds=100),
                token_usage=3200, task_no="AF20261021-0001",
            )
        )
        rows.append(
            await _seed_task(
                session, admin.id, goal="生成运营群周报通知", status=TaskStatus.AWAITING_APPROVAL,
                created_at=base - timedelta(hours=1), token_usage=4018, task_no="AF20261021-0002",
            )
        )
        rows.append(
            await _seed_task(
                session, approver.id, goal="绘制华东 GMV 趋势图", status=TaskStatus.COMPLETED,
                created_at=base - timedelta(minutes=30), finished_at=base - timedelta(minutes=30) + timedelta(seconds=200),
                token_usage=2800, task_no="AF20261021-0003",
            )
        )
        rows.append(
            await _seed_task(
                session, viewer.id, goal="查询退货明细数据", status=TaskStatus.COMPLETED,
                created_at=base - timedelta(minutes=20), finished_at=base - timedelta(minutes=20) + timedelta(seconds=50),
                token_usage=1500, task_no="AF20261021-0004",
            )
        )
        rows.append(
            await _seed_task(
                session, viewer.id, goal="扫描全表导出报表", status=TaskStatus.FAILED,
                created_at=base - timedelta(minutes=10), token_usage=800, task_no="AF20261021-0005",
                error={"code": "TASK_EXECUTION_ERROR", "message": "工具失败", "span_id": "span-x"},
            )
        )
        await session.commit()
    return rows


# ---------------------------------------------------------------------------
# §1 GET /tasks（验收 1 + 参数行为）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tasks_viewer_forced_own_and_masked(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession],
    seeded_users: dict[str, int], seeded_tasks: list[Task],
):
    """验收 1：viewer 仅本人任务、creator_name=None、传他人 creator_id 被静默覆盖。"""
    token = await _login(client, "viewer_user")
    admin_id = seeded_users["admin_user"]

    response = await client.get(
        "/api/v1/tasks",
        params={"creator_id": admin_id},  # 试图看 admin 的任务
        headers=_auth_header(token),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "OK" and body["trace_id"]
    items = body["data"]["items"]
    assert len(items) == 2  # 静默覆盖：只看到本人 2 条
    assert all(item["creator_id"] == seeded_users["viewer_user"] for item in items)
    assert all(item["creator_name"] is None for item in items)  # 脱敏第 2 条
    # duration_s 服务端算好
    completed = [item for item in items if item["status"] == "COMPLETED"]
    assert completed and completed[0]["duration_s"] == 50
    # error 只含 code/message
    failed = [item for item in items if item["status"] == "FAILED"]
    assert failed and failed[0]["error"] == {"code": "TASK_EXECUTION_ERROR", "message": "工具失败"}


@pytest.mark.asyncio
async def test_tasks_admin_filter_pagination(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession],
    seeded_users: dict[str, int], seeded_tasks: list[Task],
):
    token = await _login(client, "admin_user")
    viewer_id = seeded_users["viewer_user"]

    # 按创建人过滤 + creator_name 展示
    response = await client.get("/api/v1/tasks", params={"creator_id": viewer_id}, headers=_auth_header(token))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["meta"]["total"] == 2
    assert data["meta"]["page"] == 1
    assert all(item["creator_name"] == "viewer_user" for item in data["items"])

    # 状态多值过滤
    response = await client.get("/api/v1/tasks", params={"status": "COMPLETED,FAILED"}, headers=_auth_header(token))
    assert response.status_code == 200
    assert response.json()["data"]["meta"]["total"] == 4

    # 关键词
    response = await client.get("/api/v1/tasks", params={"keyword": "退货"}, headers=_auth_header(token))
    assert response.status_code == 200
    assert response.json()["data"]["meta"]["total"] == 2  # 退货率异常 / 退货明细

    # 排序
    response = await client.get("/api/v1/tasks", params={"order": "created_asc", "size": 2}, headers=_auth_header(token))
    items = response.json()["data"]["items"]
    assert items[0]["task_no"] == "AF20261021-0001"
    assert response.json()["data"]["meta"]["total"] == 5


@pytest.mark.asyncio
async def test_tasks_validation_errors(
    client: AsyncClient, seeded_tasks: list[Task],
):
    """§1.5：status/order 非法值 422；creator_id 不存在 422（不做 404）。"""
    token = await _login(client, "admin_user")
    headers = _auth_header(token)

    response = await client.get("/api/v1/tasks", params={"status": "NO_SUCH"}, headers=headers)
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"

    response = await client.get("/api/v1/tasks", params={"order": "goal_desc"}, headers=headers)
    assert response.status_code == 422

    response = await client.get("/api/v1/tasks", params={"creator_id": 999999}, headers=headers)
    assert response.status_code == 422

    # 未登录 401
    response = await client.get("/api/v1/tasks")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# §2 dashboards（验收 2/3）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dashboard_summary_reconciliation(
    client: AsyncClient, seeded_tasks: list[Task],
):
    """验收 2：各指标与种子数据对账。种子：total=5，终态 4（完成 3 + 失败 1），
    完成率 0.75，待审批 1，token 总 3200+4018+2800+1500+800=12318，均值 2463.6。

    显式传窗口（种子时刻前后各一天）：默认窗口是"今日 UTC 日切"，
    本地 0:00~2:00（UTC 16:00~18:00 前一天之后）跑测试时 `base - 2h` 的种子
    会落进昨天，导致 task_total 随运行时刻漂移——对账测试不应依赖时钟。
    """
    now = utcnow()
    window = {
        "start": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"),
        "end": (now + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S"),
    }
    token = await _login(client, "admin_user")
    response = await client.get("/api/v1/dashboards/summary", params=window, headers=_auth_header(token))
    assert response.status_code == 200
    data = response.json()["data"]

    assert data["task_total"] == 5
    assert data["completion_rate"] == 0.75
    assert data["pending_approvals"] == 1
    assert data["token_usage_total"] == 12318
    assert data["avg_token_per_task"] == 2463.6
    # 终态均值：(100 + 200 + 50) / 3 ≈ 116.67
    assert data["avg_duration_s"] == pytest.approx(116.67, abs=1)
    assert data["status_breakdown"]["COMPLETED"] == 3
    assert data["status_breakdown"]["FAILED"] == 1
    assert data["status_breakdown"]["AWAITING_APPROVAL"] == 1
    assert data["status_breakdown"]["PLANNING"] == 0  # 六键恒齐
    assert data["window"]["start"] and data["window"]["end"]


@pytest.mark.asyncio
async def test_dashboard_summary_viewer_masked(
    client: AsyncClient, seeded_tasks: list[Task],
):
    """验收 3：viewer 调 summary 时 token 字段为 null，其余指标正常。
    显式传窗口，理由同 test_dashboard_summary_reconciliation（防 UTC 日切漂移）。"""
    now = utcnow()
    window = {
        "start": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"),
        "end": (now + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S"),
    }
    token = await _login(client, "viewer_user")
    response = await client.get("/api/v1/dashboards/summary", params=window, headers=_auth_header(token))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["token_usage_total"] is None
    assert data["avg_token_per_task"] is None
    assert data["task_total"] == 5
    assert data["pending_approvals"] == 1


@pytest.mark.asyncio
async def test_dashboard_summary_window_validation(
    client: AsyncClient, seeded_tasks: list[Task],
):
    token = await _login(client, "admin_user")
    response = await client.get(
        "/api/v1/dashboards/summary",
        params={"start": "2026-10-21T00:00:00", "end": "2026-10-20T00:00:00"},
        headers=_auth_header(token),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_dashboard_tools_aggregation(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], seeded_tasks: list[Task],
):
    """§2.3：按 task_executions.tool_name 聚合，call_count 降序。"""
    async with session_factory() as session:
        base = utcnow().replace(microsecond=0)
        rows = []
        plan = [("sql-query", 8, True), ("sql-query", 6, False), ("chart-render", 5, True), ("notify", 2, True)]
        for index, (tool, latency, ok) in enumerate(plan):
            rows.append(
                TaskExecution(
                    trace_id=seeded_tasks[0].trace_id,
                    span_id=f"span-w6-{index}",
                    task_id=seeded_tasks[0].id,
                    node=ExecutionNode.EXECUTOR,
                    event="tool.result",
                    tool_name=tool,
                    tool_error=None if ok else "TOOL_TIMEOUT",
                    latency_ms=latency,
                    started_at=base - timedelta(minutes=index),
                )
            )
        session.add_all(rows)
        await session.commit()

    token = await _login(client, "admin_user")
    response = await client.get("/api/v1/dashboards/tools", headers=_auth_header(token))
    assert response.status_code == 200
    data = response.json()["data"]
    by_tool = {item["tool_name"]: item for item in data["items"]}

    assert by_tool["sql-query"]["call_count"] == 2
    assert by_tool["sql-query"]["success_rate"] == 0.5
    assert by_tool["sql-query"]["avg_latency_ms"] == 7.0
    assert by_tool["chart-render"]["success_rate"] == 1.0
    assert by_tool["notify"]["avg_latency_ms"] == 2.0
    # call_count 降序
    counts = [item["call_count"] for item in data["items"]]
    assert counts == sorted(counts, reverse=True)


# ---------------------------------------------------------------------------
# §3 评测接口组（验收 3/4/5/6）
# ---------------------------------------------------------------------------
async def _seed_eval_runs(session: AsyncSession) -> None:
    base = utcnow().replace(microsecond=0)
    seeds = [
        # (run_no, source, variant, dataset, status, metrics, detail, created)
        ("AFRUN20261021-0001", EvalSource.REGRESSION, EvalVariant.COMPRESS_ON, "agentflow-30",
         EvalRunStatus.DONE, {"task_completion": 0.93, "plan_f1": 0.87, "tool_acc": 0.91,
                              "token_cost": 1_400_000, "latency_p50": 180, "latency_p95": 400},
         {"samples_count": 30, "finished_at": base.isoformat()}, base - timedelta(days=1)),
        ("AFRUN20261021-0002", EvalSource.REGRESSION, EvalVariant.COMPRESS_ON, "agentflow-30",
         EvalRunStatus.DONE, {"task_completion": 0.91, "plan_f1": 0.85, "tool_acc": 0.89,
                              "token_cost": 1_600_000, "latency_p50": 190, "latency_p95": 420},
         {"samples_count": 30, "finished_at": base.isoformat()}, base - timedelta(days=1)),
        ("AFRUN20261021-0003", EvalSource.REGRESSION, EvalVariant.COMPRESS_ON, "agentflow-30",
         EvalRunStatus.DONE, {"task_completion": 0.95, "plan_f1": 0.89, "tool_acc": 0.93,
                              "token_cost": 1_200_000, "latency_p50": 170, "latency_p95": 380},
         {"samples_count": 30, "finished_at": base.isoformat()}, base),
        ("AFRUN20261021-0004", EvalSource.REGRESSION, EvalVariant.COMPRESS_OFF, "agentflow-30",
         EvalRunStatus.DONE, {"task_completion": 0.92, "plan_f1": 0.86, "tool_acc": 0.90,
                              "token_cost": 2_000_000, "latency_p50": 175, "latency_p95": 390},
         {"samples_count": 30, "finished_at": base.isoformat()}, base),
        ("AFRUN20261021-0005", EvalSource.REGRESSION, EvalVariant.COMPRESS_OFF, "agentflow-30",
         EvalRunStatus.RUNNING, None, {}, base),
    ]
    for run_no, source, variant, dataset, status, metrics, detail, created in seeds:
        session.add(
            EvalRun(
                run_no=run_no, source=source, dataset_id=dataset, variant=variant,
                ext_run_id=None, metrics=metrics, status=status, detail=detail,
                created_at=created,
            )
        )
    await session.commit()


@pytest.mark.asyncio
async def test_eval_runs_list_and_viewer_forbidden(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], seeded_users: dict[str, int],
):
    await _seed_eval_runs(session_factory())

    # 验收 3：viewer 调 GET /eval/runs 得 403
    viewer_token = await _login(client, "viewer_user")
    response = await client.get("/api/v1/eval/runs", headers=_auth_header(viewer_token))
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"

    admin_token = await _login(client, "admin_user")
    response = await client.get("/api/v1/eval/runs", headers=_auth_header(admin_token))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["meta"]["total"] == 5
    done_item = next(item for item in data["items"] if item["status"] == "DONE")
    assert {"run_id", "run_no", "source", "variant", "dataset_id", "status", "metrics", "sample_count"} <= set(done_item)
    assert done_item["sample_count"] == 30

    # 多值过滤
    response = await client.get(
        "/api/v1/eval/runs",
        params={"variant": "compress_off", "status": "DONE"},
        headers=_auth_header(admin_token),
    )
    data = response.json()["data"]
    assert data["meta"]["total"] == 1
    assert data["items"][0]["variant"] == "compress_off"


@pytest.mark.asyncio
async def test_eval_runs_summary_ab(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], seeded_users: dict[str, int],
):
    """验收 4：A/B 数值与逐条均值一致；token_saving_rate 以 compress_off 为基线。"""
    await _seed_eval_runs(session_factory())

    admin_token = await _login(client, "admin_user")
    response = await client.get("/api/v1/eval/runs/summary", headers=_auth_header(admin_token))
    assert response.status_code == 200
    groups = {g["variant"]: g for g in response.json()["data"]["groups"]}

    on_group, off_group = groups["compress_on"], groups["compress_off"]
    assert on_group["run_count"] == 3
    assert on_group["avg_task_completion"] == pytest.approx((0.93 + 0.91 + 0.95) / 3, abs=1e-3)
    assert on_group["avg_token_cost"] == pytest.approx((1_400_000 + 1_600_000 + 1_200_000) / 3, abs=1)
    # saving = 1 - 1_400_000 / 2_000_000 = 0.3
    assert on_group["token_saving_rate"] == pytest.approx(0.3, abs=1e-3)
    assert off_group["run_count"] == 1
    assert off_group["token_saving_rate"] is None  # 基线组自身 None


@pytest.mark.asyncio
async def test_eval_runs_summary_viewer_masked(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], seeded_users: dict[str, int],
):
    await _seed_eval_runs(session_factory())
    viewer_token = await _login(client, "viewer_user")
    response = await client.get("/api/v1/eval/runs/summary", headers=_auth_header(viewer_token))
    assert response.status_code == 200  # V 的「汇总」入口
    for group in response.json()["data"]["groups"]:
        assert group["avg_token_cost"] is None
        assert group["token_saving_rate"] is None
        assert group["avg_task_completion"] is not None


@pytest.mark.asyncio
async def test_eval_metrics_trend(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], seeded_users: dict[str, int],
):
    await _seed_eval_runs(session_factory())
    token = await _login(client, "admin_user")
    response = await client.get(
        "/api/v1/eval/metrics",
        params={"start": (utcnow() - timedelta(days=3)).isoformat(), "end": (utcnow() + timedelta(days=1)).isoformat()},
        headers=_auth_header(token),
    )
    assert response.status_code == 200
    points = response.json()["data"]["points"]
    assert len(points) == 2  # 两个自然日（第三条 compress_on 的 RUNNNING 不计入）
    dates = [p["date"] for p in points]
    assert dates == sorted(dates)  # 日期升序
    assert sum(p["run_count"] for p in points) == 4  # 仅 DONE
    assert all(p["avg_token_cost"] is not None for p in points)


@pytest.mark.asyncio
async def test_eval_post_run_mock_immediate_done(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], seeded_users: dict[str, int],
):
    """验收 5：POST 200 立即返回；mock 模式 metrics 落库、列表可见 DONE。"""
    token = await _login(client, "approver_user")  # A/R 可触发
    response = await client.post(
        "/api/v1/eval/runs",
        json={"dataset_id": "agentflow-30", "variant": "compress_on", "note": "W6 集成测试"},
        headers=_auth_header(token),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["trace_id"]
    data = body["data"]
    assert data["status"] == "DONE"
    assert data["run_no"].startswith("AFRUN")

    # 列表轮询立即可见
    response = await client.get("/api/v1/eval/runs", params={"status": "DONE"}, headers=_auth_header(token))
    items = response.json()["data"]["items"]
    match = [item for item in items if item["run_id"] == data["run_id"]]
    assert match and match[0]["metrics"]["task_completion"] > 0
    assert match[0]["sample_count"] == 30

    # 并发约束：预置同 dataset+variant 的 PENDING 记录后再触发 → 409
    async with session_factory() as session:
        session.add(
            EvalRun(
                run_no="AFRUN20261021-9001", source=EvalSource.REGRESSION,
                dataset_id="agentflow-30", variant=EvalVariant.COMPRESS_ON,
                status=EvalRunStatus.PENDING, detail={},
            )
        )
        await session.commit()
    response = await client.post(
        "/api/v1/eval/runs",
        json={"dataset_id": "agentflow-30", "variant": "compress_on"},
        headers=_auth_header(token),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "EVAL_RUN_DUPLICATE"

    # viewer 不可触发
    viewer_token = await _login(client, "viewer_user")
    response = await client.post("/api/v1/eval/runs", json={}, headers=_auth_header(viewer_token))
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_eval_post_run_upstream_unreachable(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession],
    seeded_users: dict[str, int], monkeypatch: pytest.MonkeyPatch,
):
    """验收 6：非 mock 且上游不可达 → 502 EVAL_UPSTREAM_UNAVAILABLE 且本地记录 FAILED。"""
    monkeypatch.setattr(settings, "MOCK_EXTERNAL_EVAL", False)
    monkeypatch.setattr(settings, "EVALAGENT_BASE_URL", "")

    token = await _login(client, "admin_user")
    response = await client.post(
        "/api/v1/eval/runs",
        json={"dataset_id": "agentflow-30", "variant": "compress_off"},
        headers=_auth_header(token),
    )
    assert response.status_code == 502
    body = response.json()
    assert body["code"] == "EVAL_UPSTREAM_UNAVAILABLE"
    assert body["trace_id"]

    # 本地记录置 FAILED。v1.4 起失败信息取真实原因：两处 BASE_URL 均空 → 配置缺失文案
    # （旧断言 == "upstream unreachable" 是 EVALAGENT 直连时代的契约，已被项目一转发取代）。
    async with session_factory() as session:
        runs = (await session.execute(__import__("sqlalchemy").select(EvalRun))).scalars().all()
        assert len(runs) == 1
        assert runs[0].status == EvalRunStatus.FAILED
        assert "未配置真实评测后端" in runs[0].detail["error"]


@pytest.mark.asyncio
async def test_eval_post_run_bad_variant(
    client: AsyncClient, seeded_users: dict[str, int],
):
    """§3.5：variant 非法 → 422。"""
    token = await _login(client, "admin_user")
    response = await client.post(
        "/api/v1/eval/runs",
        json={"dataset_id": "agentflow-30", "variant": "turbo"},
        headers=_auth_header(token),
    )
    assert response.status_code == 422
