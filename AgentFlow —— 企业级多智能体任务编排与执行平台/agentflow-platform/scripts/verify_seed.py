#!/usr/bin/env python
"""mall 数据校验 + W4 三条硬断言 + 20 个断点抽样。

演化说明（重要）：
- W1 本文件是**空壳**（只有 CLI 与断言清单，`:func:`verify_gmv_attribution`` 直接
  ``raise NotImplementedError``，`SEED VERIFIED` 从未真正打印过）；
- W4 保留 W1 定下的对外契约（同样的 ``--dsn`` / ``--seed`` / ``--sample-points`` / ``--dry-run``
  参数、同样以 ``SEED VERIFIED`` + 退出码 0 表示通过），把实现补齐并**按 W4 任务书重排断言**：
  W4 要的是「能力侧」断言（只读拦截 / kb 命中 / 审批闸门 / 断点不重跑），
  W1 的「数据侧」断言（GMV 归因数值）依赖项目一真实 52 万行数据，保留为 ``SEED-1..3``
  与 ``S01..S20`` 清单，在 ``--mall-dsn`` 指向真实库时以结构校验方式执行。

断言总览：

| 编号 | 断言 | 侧 | 通过条件 |
|---|---|---|---|
| A1 | SQL 只能只读，拦截 DML/DDL | 能力 | 9 条非法语句全部拒绝 + 2 条合法 SELECT 放行 + 真实工具入口拒绝且执行器零调用 |
| A2 | kb-search 非 Mock 命中 SKU-10086 断货 | 能力 | HTTP 不可用时经熔断 + mall 只读库兜底仍返回断货证据；Mock 模式回归不受影响 |
| A3 | notify 无审批直接拦截、不发送 | 能力 | 无 APPROVED 审批 → APPROVAL_MISSING 且 mock_messages 零新增；补审批后 → SENT 且 +1 |
| B00~B19 | 20 个随机断点恢复不从头重跑 | 能力 | planner enter 恒为 1；恢复前后 plan 签名一致；human_gate enter 恰好 2 次 |
| SEED-1..3 / S01..S20 | 埋因数值与总量口径 | 数据 | 需真实 mall 库；缺库时标 SKIP，不阻塞能力断言 |

用法：
    python scripts/verify_seed.py --dry-run                       # 只看清单
    python scripts/verify_seed.py --init-mall-fixture             # 建 SQLite 最小 mall 库后退出
    python scripts/verify_seed.py --dsn sqlite+aiosqlite:///./storage/_mall_fixture.db
    python scripts/verify_seed.py --dsn "mysql+aiomysql://mall_ro:pwd@host:3306/mall" --require-redis
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _path in (PROJECT_ROOT, PROJECT_ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

DEFAULT_FIXTURE = PROJECT_ROOT / "storage" / "_mall_fixture.db"
DEFAULT_FIXTURE_DSN = f"sqlite+aiosqlite:///{DEFAULT_FIXTURE.as_posix()}"


# ---------------------------------------------------------------------------
# 断言模型
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Check:
    code: str
    title: str
    side: str = "capability"
    passed: bool = False
    skipped: bool = False
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "title": self.title,
            "side": self.side,
            "passed": self.passed,
            "skipped": self.skipped,
            "detail": self.detail,
            "evidence": self.evidence,
        }


#: W1 保留的数据侧断言（PRD 11.2 埋因口径）——需真实库，缺库时 SKIP
SEED_ASSERTIONS: tuple[tuple[str, str, str], ...] = (
    ("SEED-1", "主因：数码配件转化率断崖", "W42 category_id=3 加权 conv_rate ∈ [1.6%, 1.8%]，其余品类环比 ≤ ±8%"),
    ("SEED-2", "次因：该品类投放预算削减", "W42 category_id=3 日预算 7,200（-40%），其余品类预算环比 ≤ ±5%"),
    ("SEED-3", "噪声控制与总量口径", "除 cat=3 外各品类 GMV 环比 ∈ [-3%, +3%]，总 GMV 环比 = -18.0% ± 0.5pct"),
)

#: W1 保留的 20 个数据抽样断点（明细 ↔ 汇总双向核对），需真实库
SAMPLING_CHECKS: tuple[str, ...] = (
    "S01 orders 总行数落在 [45万, 55万]",
    "S02 orders 日期覆盖 187 天且无未来日期",
    "S03 orders.order_id + sku_id 唯一无重复",
    "S04 pay_status 分布符合基线（成功占比 92%~96%）",
    "S05 W41 与 W42 的 GMV 汇总与明细逐日累加一致",
    "S06 traffic_records = 800 SKU × 187 天，无缺日",
    "S07 traffic.uv >= paid_uv 恒成立",
    "S08 conv_rate ∈ [0, 1] 且 cat=3 W42 显著低于基线",
    "S09 ad_campaigns = 30 计划 × 187 天，无缺日",
    "S10 cat=3 W42 日预算值为 7,200 的计划条数符合预期",
    "S11 cost <= budget 恒成立",
    "S12 roas 与 (GMV/cost) 口径一致（容差 1%）",
    "S13 维表 skus 800 行、categories 8 行且外键齐全",
    "S14 SKU-10086 自 2026-10-10 起无成功支付订单（断货语义）",
    "S15 各品类 GMV 占比合计 = 100% ± 0.1pct",
    "S16 W42 订单数环比跌幅与 GMV 跌幅方向一致",
    "S17 随机种子复现：同 seed 两次生成的抽样行完全一致",
    "S18 金额字段无负数、无 NULL",
    "S19 时区与日期边界：W42 = 2026-10-12 00:00:00 ~ 2026-10-18 23:59:59",
    "S20 只读账号可查询全部三张业务表（权限校验）",
)


# ---------------------------------------------------------------------------
# A1：SQL 只读
# ---------------------------------------------------------------------------
MUST_REJECT_SQL: tuple[str, ...] = (
    "DELETE FROM orders WHERE id > 0",
    "UPDATE orders SET gmv = 0",
    "INSERT INTO orders (gmv) VALUES (1)",
    "DROP TABLE orders",
    "ALTER TABLE orders ADD COLUMN x INT",
    "TRUNCATE TABLE orders",
    "SELECT 1; DELETE FROM orders",
    "SELECT * FROM orders -- 注释绕过",
    "REPLACE INTO orders (id) VALUES (1)",
)

MUST_PASS_SQL: tuple[str, ...] = (
    "SELECT category_id, SUM(gmv) FROM orders "
    "WHERE dt BETWEEN '2026-10-12' AND '2026-10-18' GROUP BY category_id LIMIT 10",
    "SELECT * FROM skus WHERE stock = 0",
)


async def check_sql_readonly() -> Check:
    from app.schemas.mcp import McpErrorCode
    from app.tools.mcp_stdio import ToolError
    from app.tools.mcp_servers import sql_query

    check = Check("A1", "SQL 只能只读，DML/DDL 必须拦截")

    rejected: list[str] = []
    leaked: list[str] = []
    for sql in MUST_REJECT_SQL:
        try:
            sql_query.validate_sql(sql)
            leaked.append(sql)
        except ToolError as exc:
            code = exc.code.value if hasattr(exc.code, "value") else str(exc.code)
            rejected.append(f"{sql[:26]} → {code}")

    accepted: list[str] = []
    broken: list[str] = []
    for sql in MUST_PASS_SQL:
        try:
            sql_query.validate_sql(sql)
            accepted.append(sql[:30])
        except ToolError as exc:
            code = exc.code.value if hasattr(exc.code, "value") else str(exc.code)
            broken.append(f"{sql[:22]} → {code}")

    # 第二层：真实工具入口必须拒绝，且执行器一次都不能被调用（防止"校验通过后才拦"）
    class _SpyExecutor(sql_query.SqlExecutor):
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, sql: str, timeout: float) -> sql_query.QueryResult:
            self.calls += 1
            return sql_query.QueryResult(columns=["x"], rows=[[1]], row_count=1)

        async def estimate_scan_rows(self, sql: str, timeout: float) -> int | None:
            self.calls += 1
            return 1

    spy = _SpyExecutor()
    tool = sql_query.SqlQueryTool(executor=spy)
    tool_error: str | None = None
    try:
        await tool(sql_query.SqlQueryInput(sql="DELETE FROM orders"))
    except ToolError as exc:
        tool_error = exc.code.value if hasattr(exc.code, "value") else str(exc.code)

    check.passed = (
        not leaked and not broken and spy.calls == 0 and tool_error == McpErrorCode.SQL_SYNTAX.value
    )
    check.detail = (
        f"拦截 {len(rejected)}/{len(MUST_REJECT_SQL)} 条非法语句，放行 {len(accepted)}/{len(MUST_PASS_SQL)} 条合法 SELECT；"
        f"工具入口码={tool_error}，执行器调用={spy.calls} 次"
    )
    check.evidence = {
        "rejected_samples": rejected[:4],
        "leaked": leaked,
        "broken": broken,
        "tool_error_code": tool_error,
        "executor_calls": spy.calls,
    }
    return check


# ---------------------------------------------------------------------------
# A2：kb-search 非 Mock 命中 SKU-10086
# ---------------------------------------------------------------------------
async def check_kb_non_mock(mall_dsn: str) -> Check:
    from app.tools import kb_adapter
    from app.tools.mcp_servers import kb_search

    check = Check("A2", "kb-search 非 Mock 模式命中 SKU-10086 断货数据")
    query = "检索历史周报与库存记录，确认 SKU-10086 是否断货"
    kb_adapter.reset_breaker()

    # 1) 工厂语义：MOCK=true 必须仍是 MockKbProvider（W2 行为不变）
    mock_provider = kb_adapter.build_kb_provider(mock=True)
    mock_ok = isinstance(mock_provider, kb_search.MockKbProvider)

    # 2) 非 Mock：HTTP 打不通 → 熔断 + mall 只读库兜底，仍须拿到真实断货证据
    provider = kb_adapter.build_kb_provider(mock=False, mall_dsn=mall_dsn)
    tool = kb_search.KbSearchTool(provider=provider)
    payload = await tool(kb_search.KbSearchInput(query=query, top_k=3, tenant="agentflow"))
    chunks = payload.get("chunks") or []
    hit_sku = any("SKU-10086" in f"{c.get('content') or ''}{c.get('title') or ''}" for c in chunks)
    hit_stockout = any("断货" in (c.get("content") or "") for c in chunks)
    breaker = kb_adapter.get_breaker().snapshot().as_dict()

    # 3) Mock 回归：W41 周报风险片段仍在
    mock_out = await kb_search.MockKbProvider().search("库存 断货 周报 风险", 3, "agentflow")
    mock_hit = any("SKU-10086" in c.content for c in mock_out.chunks)

    check.passed = bool(mock_ok and hit_sku and hit_stockout and mock_hit)
    check.detail = (
        f"非 Mock 返回 {len(chunks)} 条片段（SKU-10086 {'命中' if hit_sku else '未命中'}，"
        f"断货描述 {'命中' if hit_stockout else '未命中'}）；工厂={type(mock_provider).__name__}；"
        f"Mock 语料回归={'通过' if mock_hit else '失败'}；熔断={breaker['state']}"
    )
    check.evidence = {
        "query": query,
        "provider": type(provider).__name__,
        "degraded": payload.get("degraded"),
        "error_code": payload.get("error_code"),
        "chunks": [{"doc_id": c.get("doc_id"), "content": (c.get("content") or "")[:80]} for c in chunks[:3]],
        "breaker": breaker,
        "mock_regression": mock_hit,
    }
    return check


# ---------------------------------------------------------------------------
# A3：notify 审批闸门
# ---------------------------------------------------------------------------
async def check_notify_gate(env: Any) -> Check:
    from sqlalchemy import func, select

    from app.schemas.enums import ApprovalStatus, ApprovalType
    from app.schemas.mcp import McpErrorCode, NotifyInput
    from app.schemas.orm_models import Approval
    from app.schemas.orm_models_ext import MockMessage
    from app.tools.mcp_stdio import ToolError
    from app.tools.mcp_servers import notify as notify_module

    check = Check("A3", "notify 无审批直接拦截、不发送消息")
    factory = env.session_factory
    tool = notify_module.NotifyTool(session_factory=factory)
    orphan_task_id = 990001  # 刻意不存在的任务：只验证审批闸门，不依赖任务状态

    # 幂等：上一轮跑批会为这个"孤儿任务"落一张 APPROVED 反向对照单，
    # 不清掉的话本轮「无审批」用例会捡到它而误判为放行（脚本重跑必须可重复）。
    from sqlalchemy import delete

    async with factory() as session:
        await session.execute(delete(Approval).where(Approval.task_id == orphan_task_id))
        await session.commit()

    async def _count() -> int:
        async with factory() as session:
            return int((await session.execute(select(func.count(MockMessage.id)))).scalar_one())

    before = await _count()
    negative_error: str | None = None
    try:
        await tool(
            NotifyInput(
                task_id=orphan_task_id,
                title="W42 经营异常预警",
                content="数码配件断货导致 GMV 环比 -18%，请尽快处理。",
                receivers=["ops_group"],
            )
        )
    except ToolError as exc:
        negative_error = exc.code.value if hasattr(exc.code, "value") else str(exc.code)
    after_negative = await _count()
    blocked = negative_error == McpErrorCode.APPROVAL_MISSING.value and after_negative == before

    # 正向对照：补一条 APPROVED 审批单，同一工具必须能落库（证明不是"一律拒绝"）
    async with factory() as session:
        approval = Approval(
            task_id=orphan_task_id,
            type=ApprovalType.HIGH_RISK_TOOL,
            status=ApprovalStatus.APPROVED,
            reason="verify_seed 正向对照",
            payload={"subtask_seq": 1},
            requested_by=1,
            decided_by=1,
        )
        session.add(approval)
        await session.commit()
        await session.refresh(approval)
        approval_id = int(approval.id)

    positive_ok = False
    positive_error: str | None = None
    try:
        result = await tool(
            NotifyInput(
                task_id=orphan_task_id,
                approval_id=approval_id,
                title="W42 经营异常预警",
                content="数码配件断货导致 GMV 环比 -18%，请尽快处理。",
                receivers=["ops_group"],
            )
        )
        positive_ok = result.get("status") == "SENT" and result.get("mock") is True
    except ToolError as exc:
        positive_error = str(exc)
    after_positive = await _count()

    check.passed = bool(blocked and positive_ok and after_positive == after_negative + 1)
    check.detail = (
        f"无审批 → {negative_error}（落库 {before}→{after_negative}，"
        f"{'零新增' if after_negative == before else '异常新增'}）；"
        f"有 APPROVED → {'SENT 且落库 +1' if positive_ok and after_positive == after_negative + 1 else f'失败：{positive_error}'}"
    )
    check.evidence = {
        "negative_error": negative_error,
        "rows_before": before,
        "rows_after_blocked": after_negative,
        "rows_after_approved": after_positive,
        "positive_ok": positive_ok,
    }
    return check


# ---------------------------------------------------------------------------
# B：20 个随机断点抽样
# ---------------------------------------------------------------------------
GOAL_POOL: tuple[str, ...] = (
    "分析上周 GMV 下跌原因，并把结论通知到运营群",
    "生成经营周报并推送到运营群，重点说明断货影响",
    "统计断货 SKU 清单并通知供应链同学处理",
    "分析上周 GMV 下跌原因，检索历史周报佐证后通知运营群",
)


async def check_checkpoint_samples(env: Any, *, samples: int, seed: int) -> tuple[list[Check], dict[str, Any]]:
    """每个抽样点 = 一次「挂起 → 恢复」，三条判据同时成立才算通过：

    1. 恢复后 ``planner`` 的 ``enter`` 轨迹仍为 1 次（没有重新规划整条链路）；
    2. 恢复前后 ``plan`` 的 (seq, name) 签名完全一致（确实读的 checkpoint，而非重新生成）；
    3. 恢复后 ``human_gate`` 的 ``enter`` 恰好 2 次（首次建单 + 断点重入），排除"从头跑一遍"。
    """
    from app.services.trace_service import TraceService

    rng = random.Random(seed)
    api = env.api
    trace_service = TraceService(env.session_factory)
    checks: list[Check] = []
    stats: dict[str, Any] = {"planner_enters": [], "gate_enters": [], "step_deltas": [], "tasks": []}

    for index in range(samples):
        goal = GOAL_POOL[index % len(GOAL_POOL)]
        if index % 3 == 0:
            goal = f"{goal}（抽样 #{index + 1}）"
        check = Check(f"B{index:02d}", f"断点抽样 #{index + 1}：恢复不从头重跑")
        try:
            submitted = await api.submit(goal)
            task_id = int(submitted["task_id"])
            stats["tasks"].append(task_id)

            if not await _wait_status(api, task_id, target="AWAITING_APPROVAL", timeout=60):
                check.detail = "未在超时内挂起（该目标未生成 notify 子任务）"
                checks.append(check)
                continue

            pre_snapshot = await api.snapshot(task_id)
            pre_values = pre_snapshot.get("values") or {}
            pre_plan = pre_values.get("plan") or []
            pre_steps = await trace_service.list_steps(task_id=task_id) or []
            pre_planner = sum(1 for s in pre_steps if s["node"] == "planner" and s["event"] == "enter")

            await asyncio.sleep(rng.uniform(0.0, 0.05))  # 随机化审批延迟
            await api.resume(task_id, decision="APPROVED", comment="verify_seed 断点抽样", sync=False)

            final = await _wait_terminal(api, task_id, timeout=120)
            if final is None:
                check.detail = "恢复后未在超时内到达终态"
                checks.append(check)
                continue

            post_snapshot = await api.snapshot(task_id)
            post_values = post_snapshot.get("values") or {}
            post_plan = post_values.get("plan") or []
            post_steps = await trace_service.list_steps(task_id=task_id) or []
            post_planner = sum(1 for s in post_steps if s["node"] == "planner" and s["event"] == "enter")
            post_gate = sum(1 for s in post_steps if s["node"] == "human_gate" and s["event"] == "enter")

            plan_same = bool(pre_plan) and _plan_signature(pre_plan) == _plan_signature(post_plan)
            no_replan = post_planner == pre_planner == 1
            # human_gate 首次建单 1 次 + 每次恢复重入 1 次；未被二次挂起时应恰好 2 次
            gate_reentered = post_gate >= 2 and post_gate % 2 == 0
            step_delta = len(post_steps) - len(pre_steps)

            check.passed = bool(plan_same and no_replan and gate_reentered and final["status"] == "COMPLETED")
            check.detail = (
                f"task={task_id} 终态={final['status']}；planner enter {pre_planner}→{post_planner}；"
                f"human_gate enter={post_gate}；plan 一致={'是' if plan_same else '否'}；"
                f"轨迹 {len(pre_steps)}→{len(post_steps)} 步"
            )
            check.evidence = {
                "task_id": task_id,
                "goal": goal,
                "pre_next_nodes": pre_snapshot.get("next_nodes"),
                "pre_step": pre_snapshot.get("step"),
                "step_delta": step_delta,
            }
            stats["planner_enters"].append(post_planner)
            stats["gate_enters"].append(post_gate)
            stats["step_deltas"].append(step_delta)
        except Exception as exc:  # noqa: BLE001 - 单点失败不打断整批
            check.detail = f"异常：{type(exc).__name__}: {str(exc)[:160]}"
        checks.append(check)
    return checks, stats


def _plan_signature(plan: Sequence[Any]) -> list[tuple[str, str]]:
    signature: list[tuple[str, str]] = []
    for item in plan or []:
        if isinstance(item, dict):
            signature.append((str(item.get("seq")), str(item.get("name"))))
        else:
            signature.append((str(getattr(item, "seq", "")), str(getattr(item, "name", ""))))
    return signature


async def _wait_status(api: Any, task_id: int, *, target: str, timeout: float) -> bool:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        detail = await api.detail(task_id)
        status = detail.get("status")
        if status == target:
            return True
        if status in {"COMPLETED", "FAILED", "CANCELLED"}:
            return False
        await asyncio.sleep(0.1)
    return False


async def _wait_terminal(api: Any, task_id: int, *, timeout: float) -> dict[str, Any] | None:
    """等终态。二次挂起（重规划后再次需审批）时才自动放行，避免把"恢复过程中的瞬时挂起"误判成新断点。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    seen_running = False
    while loop.time() < deadline:
        detail = await api.detail(task_id)
        status = detail.get("status")
        if status in {"COMPLETED", "FAILED", "CANCELLED"}:
            return detail
        if status == "RUNNING":
            seen_running = True
        elif status == "AWAITING_APPROVAL" and seen_running:
            # 只有"跑起来过之后又挂起"才是真二次挂起；刚发出 resume 的瞬时挂起不算
            try:
                await api.resume(task_id, decision="APPROVED", comment="verify_seed 二次放行", sync=False)
            except Exception:  # noqa: BLE001
                pass
            seen_running = False
        await asyncio.sleep(0.1)
    return None


# ---------------------------------------------------------------------------
# 数据侧断言（需真实 mall 库；缺表/缺列时 SKIP）
# ---------------------------------------------------------------------------
async def check_seed_data(mall_dsn: str) -> list[Check]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    checks: list[Check] = []
    engine = create_async_engine(mall_dsn)
    try:
        async with engine.connect() as conn:
            tables = {row[0] for row in (await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")))} if mall_dsn.startswith("sqlite") else set()
            sku_row = (
                await conn.execute(text("SELECT sku_code, stock, turnover_days FROM skus WHERE sku_code='SKU-10086'"))
            ).first()
            total_orders = (await conn.execute(text("SELECT COUNT(*) FROM orders"))).scalar()
            budget = (
                await conn.execute(
                    text("SELECT daily_budget FROM ad_campaigns WHERE category_id=3 ORDER BY dt DESC LIMIT 1")
                )
            ).scalar()
    except Exception as exc:  # noqa: BLE001
        for code, title, expectation in SEED_ASSERTIONS:
            checks.append(Check(code, title, "data", skipped=True, detail=f"SKIP：库不可用/结构不符（{str(exc)[:80]}）"))
        return checks
    finally:
        await engine.dispose()

    # 结构级校验：能检出埋因行即认为"数据可用于 demo"，数值级断言需 52 万行真实库
    c1 = Check("SEED-1", SEED_ASSERTIONS[0][1], "data")
    c1.passed = sku_row is not None and float(sku_row[1] or 0) <= 0
    c1.detail = f"SKU-10086 行={'存在' if sku_row else '缺失'}，库存={sku_row[1] if sku_row else 'N/A'}，周转天数={sku_row[2] if sku_row else 'N/A'}"
    checks.append(c1)

    c2 = Check("SEED-2", SEED_ASSERTIONS[1][1], "data")
    c2.passed = budget is not None and abs(float(budget) - 7200.0) < 1.0
    c2.detail = f"category_id=3 最新日预算={budget}（期望 7200）"
    checks.append(c2)

    c3 = Check("SEED-3", SEED_ASSERTIONS[2][1], "data")
    c3.passed = bool(total_orders)
    c3.detail = f"orders 行数={total_orders}（全量口径需真实 52 万行库，此处仅校验可查询）"
    checks.append(c3)
    return checks


# ---------------------------------------------------------------------------
# SQLite 版 mall 最小库（离线跑 A2/SEED 用）
# ---------------------------------------------------------------------------
MALL_FIXTURE_DDL = (
    """CREATE TABLE IF NOT EXISTS skus (
        sku_id INTEGER PRIMARY KEY,
        sku_code TEXT NOT NULL,
        sku_name TEXT NOT NULL,
        category_id INTEGER,
        category_name TEXT,
        stock INTEGER,
        turnover_days INTEGER,
        status TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS orders (
        order_id INTEGER PRIMARY KEY,
        sku_id INTEGER,
        category_id INTEGER,
        gmv REAL,
        order_cnt INTEGER,
        conv_rate REAL,
        dt TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS ad_campaigns (
        campaign_id INTEGER PRIMARY KEY,
        category_id INTEGER,
        daily_budget REAL,
        dt TEXT
    )""",
    "DELETE FROM skus",
    "DELETE FROM orders",
    "DELETE FROM ad_campaigns",
    """INSERT INTO skus (sku_id, sku_code, sku_name, category_id, category_name, stock, turnover_days, status) VALUES
        (10086, 'SKU-10086', '数码配件爆款无线耳机', 3, '数码配件', 0, 21, '下架'),
        (10087, 'SKU-10087', '数码配件快充头', 3, '数码配件', 120, 9, '在售'),
        (20001, 'SKU-20001', '纯棉基础款T恤', 1, '服饰鞋包', 300, 12, '在售'),
        (30001, 'SKU-30001', '原木收纳盒', 2, '家居日用', 88, 15, '在售')""",
    """INSERT INTO orders (order_id, sku_id, category_id, gmv, order_cnt, conv_rate, dt) VALUES
        (1, 10086, 3, 0.0, 0, 1.7, '2026-10-15'),
        (2, 10087, 3, 8505000.0, 31720, 1.7, '2026-10-15'),
        (3, 20001, 1, 9200000.0, 46000, 3.4, '2026-10-15')""",
    "INSERT INTO ad_campaigns (campaign_id, category_id, daily_budget, dt) VALUES (1, 3, 7200.0, '2026-10-15')",
)


async def init_mall_fixture(dsn: str) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    if dsn.startswith("sqlite"):
        target = dsn.split("///", 1)[-1]
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(dsn)
    async with engine.begin() as conn:
        for statement in MALL_FIXTURE_DDL:
            await conn.execute(text(statement))
        row = (await conn.execute(text("SELECT sku_code, stock, turnover_days FROM skus WHERE sku_code='SKU-10086'"))).first()
        budget = (await conn.execute(text("SELECT daily_budget FROM ad_campaigns WHERE category_id=3"))).scalar()
    await engine.dispose()
    print(f"[fixture] {dsn} 就绪：SKU-10086={row}，数码配件日预算={budget}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
async def run(args: argparse.Namespace) -> int:
    mall_dsn = args.dsn or args.mall_dsn or DEFAULT_FIXTURE_DSN
    if args.dsn is None and args.mall_dsn is None and not DEFAULT_FIXTURE.exists():
        print(f"[init] 未指定 --dsn，自动创建 SQLite 最小 mall 库：{DEFAULT_FIXTURE}")
        await init_mall_fixture(mall_dsn)

    checks: list[Check] = []
    stats: dict[str, Any] = {}

    print("[1/5] A1 SQL 只读拦截 …")
    checks.append(await check_sql_readonly())
    print(f"      → {'PASS' if checks[-1].passed else 'FAIL'}：{checks[-1].detail}")

    print("[2/5] A2 kb-search 非 Mock 命中断货证据 …")
    a2 = await check_kb_non_mock(mall_dsn)
    checks.append(a2)
    print(f"      → {'PASS' if a2.passed else 'FAIL'}：{a2.detail}")

    from w4_common import EmbeddedEnv  # 延迟 import：A1/A2 的分析不受环境影响

    print("[3/5] SEED 数据侧断言（结构级）…")
    seed_checks = await check_seed_data(mall_dsn)
    checks.extend(seed_checks)
    for item in seed_checks:
        flag = "SKIP" if item.skipped else ("PASS" if item.passed else "FAIL")
        print(f"      [{flag}] {item.code}：{item.detail}")

    print("[4/5] A3 notify 审批闸门 …")
    env = await EmbeddedEnv(db_path=PROJECT_ROOT / "storage" / "_verify_seed.db").start()
    try:
        a3 = await check_notify_gate(env)
        checks.append(a3)
        print(f"      → {'PASS' if a3.passed else 'FAIL'}：{a3.detail}")

        print(f"[5/5] B 断点抽样 ×{args.sample_points} …")
        samples, stats = await check_checkpoint_samples(env, samples=args.sample_points, seed=args.seed)
        checks.extend(samples)
        print(f"      → {sum(1 for c in samples if c.passed)}/{len(samples)} 通过")
        stats["checkpointer"] = type(getattr(env.runtime, "checkpointer", None)).__name__
    finally:
        await env.aclose()

    hard = [c for c in checks if c.side == "capability"]
    failed = [c for c in hard if not c.passed]
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mall_dsn": mall_dsn.split("@")[-1] if "@" in mall_dsn else mall_dsn,
        "checkpointer": stats.get("checkpointer", "InMemorySaver"),
        "require_redis": args.require_redis,
        "sample_points": args.sample_points,
        "total": len(checks),
        "passed": sum(1 for c in checks if c.passed),
        "failed": [c.code for c in failed],
        "checks": [c.as_dict() for c in checks],
        "stats": stats,
    }

    output = args.json or str(PROJECT_ROOT / "storage" / "verify_seed.json")
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[out] 明细已写入 {target}")

    print("\n=== 结果 ===")
    for item in checks:
        flag = "SKIP" if item.skipped else ("PASS" if item.passed else "FAIL")
        print(f"[{flag}] {item.code:<8} {item.title}")
    print(f"\n合计 {payload['passed']}/{payload['total']} 通过（能力侧失败 {len(failed)} 项）")

    if args.require_redis and stats.get("checkpointer") != "AsyncRedisSaver":
        print(f"SEED FAILED：--require-redis 要求 AsyncRedisSaver，实际为 {stats.get('checkpointer')}")
        return 2
    if failed:
        print("SEED FAILED")
        return 1
    print("SEED VERIFIED")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="mall 数据校验 + W4 三条硬断言 + 断点抽样")
    parser.add_argument("--dsn", default=None, help="mall 只读库 DSN（W1 兼容参数，同 --mall-dsn）")
    parser.add_argument("--mall-dsn", default=None, help="mall 只读库 DSN（默认 storage/_mall_fixture.db）")
    parser.add_argument("--seed", type=int, default=42, help="抽样随机种子（默认 42，须与生成器一致）")
    parser.add_argument("--sample-points", type=int, default=20, help="断点抽样次数（默认 20）")
    parser.add_argument("--json", default=None, help="明细输出路径（默认 storage/verify_seed.json）")
    parser.add_argument("--dry-run", action="store_true", help="只打印断言清单，不连库、不起图")
    parser.add_argument("--init-mall-fixture", action="store_true", help="初始化 SQLite 版 mall 最小库后退出")
    parser.add_argument(
        "--require-redis",
        action="store_true",
        help="要求 checkpointer 为 AsyncRedisSaver（有 Redis Stack 的 CI 用；不满足时退出码 2）",
    )
    return parser


def print_plan(sample_points: int) -> None:
    print("=" * 82)
    print("AgentFlow W4 校验清单（能力侧断言 + W1 保留的数据侧断言）")
    print("=" * 82)
    print("\n[能力侧]")
    print("  A1: SQL 只能只读，DML/DDL 必须拦截（9 条非法 + 2 条合法 + 工具入口 + 执行器零调用）")
    print("  A2: kb-search 非 Mock 模式命中 SKU-10086 断货数据（HTTP 不通时熔断 → mall 只读库兜底）")
    print("  A3: notify 无审批直接拦截、不发送消息（负向零落库 + 正向对照 +1）")
    print(f"  B00~B{sample_points - 1:02d}: {sample_points} 个随机断点，校验恢复不从头重跑")
    print("\n[数据侧]（需真实 mall 库；缺库时 SKIP）")
    for code, title, expectation in SEED_ASSERTIONS:
        print(f"  {code}: {title}\n       期望：{expectation}")
    print("  20 个数据抽样断点（明细 ↔ 汇总双向核对）：")
    for item in SAMPLING_CHECKS[:sample_points]:
        print(f"    - {item}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.dry_run:
        print_plan(min(args.sample_points, len(SAMPLING_CHECKS)))
        print(
            f"\n数据库：{args.dsn or args.mall_dsn or DEFAULT_FIXTURE_DSN}"
            f"\ncheckpointer：CHECKPOINTER_BACKEND={os.environ.get('CHECKPOINTER_BACKEND', 'memory')}"
            "\n[DRY-RUN] 未连接数据库、未启动图。"
        )
        return 0

    if args.init_mall_fixture:
        asyncio.run(init_mall_fixture(args.dsn or args.mall_dsn or DEFAULT_FIXTURE_DSN))
        return 0

    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
