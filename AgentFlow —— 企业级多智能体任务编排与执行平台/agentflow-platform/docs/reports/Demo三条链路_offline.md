# W4 Demo 三条链路执行报告

- 生成时间：20261021-145124
- 运行模式：`embedded`（db=sqlite:_w4_embedded.db / redis=fakeredis / checkpointer=InMemorySaver / mcp=InMemoryTransport）
- LLM：`mock`
- 结果：**3/3 条链路通过**

> 离线模式使用 Mock LLM + SQLite + fakeredis + InMemoryTransport，编排、SSE、审批、断点、取消全部走真实实现；只有模型输出是确定性模板。

## 汇总

| 链路 | 标题 | task_id | 终态 | 步骤 | 耗时(ms) | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 审批闭环：提交 → SSE → 审批 → 续跑 → 完成 | 51 | COMPLETED | 8/8 | 496 | 通过 |
| 2 | 驳回重规划：驳回 → replan → 无审批再拦截 | 52 | FAILED | 5/5 | 561 | 通过 |
| 3 | 取消冻结：审批点取消 → 状态收口 → 断点清理 | 53 | CANCELLED | 8/8 | 287 | 通过 |

## 链路 1：审批闭环：提交 → SSE → 审批 → 续跑 → 完成

- 目标（goal）：`分析上周数码配件 GMV 下滑原因，并推送运营群通知`
- 任务 ID：`51`；终态：`COMPLETED`；耗时：496 ms

### 执行步骤

| 步骤 | 动作 | 结果 | 耗时(ms) | 细节 |
| --- | --- | --- | --- | --- |
| 1 | 提交高风险任务 | PASS | 32 | task_id=51 status=PLANNING |
| 2 | 任务挂起在审批点 | PASS | 171 | status=AWAITING_APPROVAL |
| 3 | SSE 第一段推送（收尾于 interrupt） | PASS | 15 | frames=5 tail=interrupt 事件分布={'state_update': 4, 'interrupt': 1} |
| 4 | 挂起上下文可查（含高危子任务预览） | PASS | 0 | reason=高危动作需审批：#4 推送运营群通知 approval_id=20 |
| 5 | 审批待办已进收件箱 | PASS | 14 | approval_id=20 |
| 6 | 审批通过 → 断点续跑 → 完成 | PASS | 231 | status=COMPLETED tokens=3256 |
| 7 | SSE 第二段续订（收尾于 complete） | PASS | 7 | frames=16 tail=complete 事件分布={'state_update': 11, 'tool_call': 4, 'complete': 1} |
| 8 | 轨迹回放（步骤 / 断点 / 节点序列） | PASS | 17 | steps=20 checkpoints=8 nodes=planner→human_gate→executor→reviewer→executor→notifier→summarizer→notifier→summarizer→notifier |

### SSE 推送序列

- **第一段（提交→审批挂起）**：5 帧，收尾事件 `interrupt`，分布 interrupt×1, state_update×4
- **第二段（审批通过→完成）**：16 帧，收尾事件 `complete`，分布 complete×1, state_update×11, tool_call×4

第一段事件时间轴（截取前 20 帧）：

| # | 事件 | 节点 | phase | status |
| --- | --- | --- | --- | --- |
| 1 | state_update | planner | run | RUNNING |
| 2 | state_update | planner | enter | RUNNING |
| 3 | state_update | planner | exit | PLANNING |
| 4 | state_update | human_gate | enter | RUNNING |
| 5 | interrupt | human_gate | awaiting_approval | AWAITING_APPROVAL |

### 轨迹回放

- 步骤数：20；断点数：8
- 节点序列：`planner → human_gate → executor → reviewer → executor → notifier → summarizer → notifier → summarizer → notifier`
- 记录的事件类型：enter, exit, llm.call, tool.call, tool.result

### 终态明细

```json
{
  "status": "COMPLETED",
  "reason": "COMPLETED",
  "error": null,
  "replan_count": 0,
  "notify_status": null,
  "node_tokens": {
    "planner": 1795,
    "executor": 448,
    "reviewer": 0,
    "summarizer": 1013,
    "notifier": 0
  },
  "total_tokens": 3256
}
```

## 链路 2：驳回重规划：驳回 → replan → 无审批再拦截

- 目标（goal）：`分析上周数码配件 GMV 下滑原因，并推送运营群通知`
- 任务 ID：`52`；终态：`FAILED`；耗时：561 ms

### 执行步骤

| 步骤 | 动作 | 结果 | 耗时(ms) | 细节 |
| --- | --- | --- | --- | --- |
| 1 | 提交高风险任务 | PASS | 14 | task_id=52 |
| 2 | 任务挂起在审批点 | PASS | 107 | status=AWAITING_APPROVAL |
| 3 | 驳回后按预期失败（未放行发送） | PASS | 427 | status=FAILED code=APPROVAL_MISSING replan_count=1 |
| 4 | 轨迹含 replan 节点（重排发生） | PASS | 11 | replan_enter=1 notify_enter=1 nodes=planner→human_gate→replan→human_gate→replan→executor→replan→executor→reviewer→summarizer→notifier |
| 5 | 二次走到 notify 仍无审批 → APPROVAL_MISSING | PASS | 0 | code=APPROVAL_MISSING replan_count=1 |

### 轨迹回放

- 步骤数：21；断点数：9
- 节点序列：`planner → human_gate → replan → human_gate → replan → executor → replan → executor → reviewer → summarizer → notifier`
- replan enter 次数：1；notify enter 次数：1

### 终态明细

```json
{
  "status": "FAILED",
  "reason": "APPROVAL_MISSING",
  "error": {
    "code": "APPROVAL_MISSING",
    "message": "notify 前置条件不满足：审批未通过，已阻断发送",
    "span_id": null
  },
  "replan_count": 1,
  "total_tokens": 5206
}
```

## 链路 3：取消冻结：审批点取消 → 状态收口 → 断点清理

- 目标（goal）：`分析上周数码配件 GMV 下滑原因，并推送运营群通知`
- 任务 ID：`53`；终态：`CANCELLED`；耗时：287 ms

### 执行步骤

| 步骤 | 动作 | 结果 | 耗时(ms) | 细节 |
| --- | --- | --- | --- | --- |
| 1 | 提交高风险任务 | PASS | 46 | task_id=53 |
| 2 | 任务挂起在审批点 | PASS | 134 | status=AWAITING_APPROVAL |
| 3 | 取消前：断点存在且挂起 1 个 interrupt | PASS | 17 | interrupt_count=1 next=['human_gate'] step=1 approval=PENDING |
| 4 | 取消成功 → 状态 CANCELLED | PASS | 52 | status=CANCELLED code=TASK_CANCELED |
| 5 | 断点已冻结：状态/计划留存，挂起清空 | PASS | 5 | status=CANCELLED interrupt_count=0 plan_size=4 |
| 6 | 取消后再 resume 被拒绝 | PASS | 6 | HTTP 409: {"code":"TASK_NOT_PAUSED","message":"任务当前不在等待审批状态，无法恢复","data":{"detail":{"task_id":53,"st |
| 7 | 审批单已置 EXPIRED 且退出待办列表 | PASS | 16 | approval_id=22 status=EXPIRED 仍在收件箱=0 全局待办=0 |
| 8 | 轨迹与断点时间轴仍可回放（冻结不等于删除） | PASS | 8 | steps=5 checkpoints=4 nodes=planner→human_gate→executor |

### 轨迹回放

- 步骤数：5；断点数：4
- 节点序列：`planner → human_gate → executor`

**取消前快照**

```json
{
  "status": "PLANNING",
  "next_nodes": [
    "human_gate"
  ],
  "interrupt_count": 1,
  "step": 1,
  "plan_size": 4,
  "approval_status": "PENDING"
}
```

**取消后快照**

```json
{
  "status": "CANCELLED",
  "next_nodes": [
    "executor"
  ],
  "interrupt_count": 0,
  "step": 2,
  "values_keys": [
    "approvals",
    "artifacts",
    "context_digest",
    "context_meta",
    "creator_id",
    "goal",
    "memory_hits",
    "plan",
    "plan_assumptions",
    "recent_messages",
    "replan_context",
    "replan_count"
  ],
  "plan_size": 4,
  "messages_count": 1
}
```

取消后再 resume 的返回：`HTTP 409: {"code":"TASK_NOT_PAUSED","message":"任务当前不在等待审批状态，无法恢复","data":{"detail":{"task_id":53,"status":"CANCELLED"}},"trace_id":"d8369304d4b54d47800959cd01e3`

## 复现

```bash
# 离线（默认）：三条链路全跑
python scripts/demo_three_flows.py

# 只跑某一条
python scripts/demo_three_flows.py --flow 1

# 浏览器里手动点一遍（起服务 + 前端；前端是纯静态，无 npm/无构建）
docker compose up -d && alembic upgrade head && python -m app.main
python -m http.server -d frontend 5173   # 浏览器打开 http://127.0.0.1:5173/
```

## 与生产链路的差异（演示可外推的边界）

1. 存储：SQLite 文件库替代 MySQL；事件总线 / 取消标记用 fakeredis；checkpointer 用 `InMemorySaver`；
2. MCP：`InMemoryTransport` 替代 stdio 子进程，**工具实现与协议完全一致**（同一份 server 代码）；
3. LLM：Mock 输出为确定性模板，因此「报告内容质量」不可用于验收；编排 / SSE / 审批 / 断点 / 取消的**行为**可验收；
4. `--live` 模式直连真实服务（MySQL + Redis + 真实 LLM），除基建外脚本逻辑不变。
