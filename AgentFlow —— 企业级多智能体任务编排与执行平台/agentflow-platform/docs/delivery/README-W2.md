# AgentFlow Platform — W2 交付说明（编排内核 + MCP 工具层 + 上下文工程）

> 本文件只描述 **W2 阶段**（LangGraph 编排内核 / Planner-Executor-Reviewer / MCP 工具层 / 上下文工程）。
> W1 的脚手架说明见 `README-W1.md`；W2 未覆盖的能力（SSE、审批 REST、前端、trace 回放）见文末「W3 边界」。

---

## 1. 本阶段完成什么

一条可在**无外部依赖**条件下跑通的主链路：

```
POST /api/v1/tasks
   │
   ▼
planner → (human_gate)? → executor → reviewer → (replan → executor)* → summarizer → (notifier)? → END
```

- **7 个节点固定拓扑**，条件边由纯函数路由（`app/agents/graph.py`）；
- **唯一挂起点**在 `human_gate`（`langgraph.types.interrupt`），`Command(resume=...)` 续跑；
- **4 个 MCP Server**（sql-query / chart-render / kb-search / notify）走 stdio JSON-RPC 2.0，仅实现
  `initialize` / `tools/list` / `tools/call`；
- 所有 LLM 输出经 Pydantic 强制校验，失败按「修复 2 次 + 备用模型 1 次」处理；
- 工具结果**先进上下文瘦身器**再进 LLM，超预算自动压缩（最多 2 层）。

全链路依赖均可切换为进程内实现，因此 `pytest` 与本地 `uvicorn` 都不需要 MySQL / Redis / 真实 LLM。

---

## 2. 新增文件清单

### 2.1 配置与 Schema

| 文件 | 作用 |
|---|---|
| `app/core/w2_config.py` | W2 全量可调参数（预算/白名单/超时/阈值），不侵入 W1 `config.py` |
| `app/schemas/agent_io.py` | Agent 入出参契约：`PlannerInput`/`PlanValidationReport`/`ExecutorInput`/`ToolCallRequest`/`ToolCallOutcome`/`ReviewInput`/`SummaryOutput`/`TaskCreateRequest` |
| `app/schemas/mcp.py` | JSON-RPC 信封 + 4 工具入出参 + `McpErrorCode` + 可重试/确定性错误集合 |
| `app/schemas/context.py` | `TokenEstimate` / `CompressionConfig` / `SlimmingConfig` / `SlimResult` / `ContextBuildResult` |
| `app/schemas/graph_state.py` | **扩展**：`add_messages`/`merge_dict`/`merge_token_ledger`/`add_artifacts` reducer 与运行期字段 |
| `app/schemas/orm_models_ext.py` | `mock_messages` 表（notify Mock 落库），不动 W1 的 7 张表 |

### 2.2 Agent 层（`app/agents/`）

| 文件 | 要点 |
|---|---|
| `llm.py` | OpenAI 兼容客户端（`/chat/completions`）+ `MockLLMClient` + 主备降级门面 |
| `structured.py` | `extract_json`（容忍围栏/废话/平衡括号）+ `complete_structured`（修复重试 + 备用模型） |
| `prompts.py` | 4 个 system 提示词（含 few-shot），首行带 `AGENTFLOW:TASK=xxx` 标记供 Mock 分流 |
| `common.py` | State 容错读取（Pydantic 或 dict）、记账、风险判定、`needs_approval` |
| `planner.py` | 规划主流程：预算 → 结构化 → DAG 校验 →（失败回喂重排，最多 2 轮）；`temperature=0.2` |
| `executor.py` | 子任务执行：参数构造 → 重试/降级矩阵 → 瘦身 → 产物；工具循环上限 8 |
| `human_gate.py` | 建单（幂等）→ `interrupt` → 解析决策 → 回写；REJECTED 写 `replan_context` |
| `reviewer.py` | 本地规则评分（离线）/ 外部 faithfulness + LLM；连续 2 次不合规 → `REVIEW_SKIPPED` |
| `replan.py` | 原因优先级（人工驳回 > 评审未过 > 工具失败）；保留 DONE，其余 SKIPPED 占位；上限 2 |
| `summarizer.py` | `SummaryOutput` 结构化报告 + `report` 产物 + 降级说明 |
| `notifier.py` | 审批前置闸门 + notify 调用 + 结果瘦身 |
| `graph.py` | 5 个纯函数路由 + `build_graph` + `create_runtime` + `pending_interrupts` |

### 2.3 MCP 工具层（`app/tools/`）

| 文件 | 要点 |
|---|---|
| `mcp_stdio.py` | 协议底座：`MCPServerApp`（3 方法 + 通知静默）+ stdio 逐行 JSON 主循环 |
| `mcp_client.py` | `StdioTransport` / `InMemoryTransport`、`MCPClient`、`MCPClientPool`（动态路由） |
| `mcp_servers/sql_query.py` | 五道闸门：只读账号 / sqlglot AST / 表白名单 / 强制 LIMIT / EXPLAIN 扫描量 |
| `mcp_servers/chart_render.py` | 字段正则白名单 + 行数上限 + ECharts option + matplotlib(Agg) 出图（进程池 + 超时） |
| `mcp_servers/kb_search.py` | Mock 周报样本 / HTTP Provider（重试 + 401 分类 + `degraded=true` 不抛异常） |
| `mcp_servers/notify.py` | 前置 `APPROVED` 校验，否则 `APPROVAL_MISSING`；成功写 `mock_messages`，恒不出网 |

### 2.4 服务层（`app/services/`）

| 文件 | 要点 |
|---|---|
| `context_service.py` | `plan_context`（预算/压缩判定）、`compress`（保留 3 轮 + 2 层）、`slim_tool_result`（分工具瘦身） |
| `task_service.py` | 任务创建（注入拦截 + 时间窗校验）、`run`（图执行 + 挂起判定 + 落终态）、`get_detail` |
| `checkpoint_service.py` | RedisSaver TTL=72h（分钟换算）+ 失败降级 `InMemorySaver` + `make_resumer` |
| `plan_service.py` | 计划 upsert（seq→id）、结果同步、dump/load |
| `approval_service.py` | 建单（24h 过期）、幂等裁决、`find_approved` |
| `eval_gateway.py` / `rag_gateway.py` | 项目二 / 项目一网关（Mock 与 HTTP 两路，失败降级不抛） |

### 2.5 路由与工具函数

| 文件 | 要点 |
|---|---|
| `app/routers/tasks.py` | `POST /api/v1/tasks`（ADMIN）、`GET /api/v1/tasks/{id}`（ADMIN/APPROVER/VIEWER 字段裁剪） |
| `app/routers/api_router.py` | 注册 tasks 路由（其余保留 W3/W4 注释） |
| `app/utils/tokens.py` | Token 估算（CJK 1 字≈1、ASCII 4 字符≈1）、截断、轮数统计 |
| `app/utils/dag.py` | 拓扑排序、环检测、`validate_plan`、`ready_subtasks`、`blocked_seqs` |
| `app/db/checkpointer.py` | **改写**为委托 `checkpoint_service`，状态输出补 `backend`/`checkpoint_ttl_hours` |

W1 文件仅两处必要改动：`app/schemas/graph_state.py`（加 reducer 与 W2 字段）、`app/db/checkpointer.py`（委托新服务）。
鉴权、DB 模型、全局中间件、docker-compose **未改动**。

---

## 3. 关键设计决策

| 决策 | 原因 |
|---|---|
| W2 参数独立放 `w2_config.py` | 硬性约束：不改 W1 `config.py`；同时满足 NFR-M3「零硬编码魔数」 |
| `InMemoryTransport` 与 `StdioTransport` 同一接口 | 生产走子进程，单测走进程内，业务代码零分支 |
| 工具业务错误用 `result.isError` + content JSON | 保留 PRD 错误码（`SQL_TOO_LARGE` 等），客户端才能做重试/降级分流 |
| 结构化校验失败先"回喂错误再生成" | 比直接重试成功率高，且错误信息可进 trace |
| 上下文压缩"无收益即放弃" | 历史很短时模板摘要反而更长，越压越大没有意义（`compress_no_gain`） |
| `pending_interrupts()` 查快照而非返回值 | `ainvoke` 的返回值**不含** `__interrupt__`，否则挂起任务会被误判为终态 |
| replan 用 SKIPPED 占位而非删 key | State 的 `subtask_results` 是合并语义，删不掉；SKIPPED 让 executor 重跑并覆盖 |
| notify 双重闸门（节点 + Server） | 任何一侧漏判都不会真正发消息；W2 恒 Mock 不出网 |

---

## 4. 本地运行

```bash
# 1) 依赖（已含 sqlglot / matplotlib）
pip install -r requirements-dev.txt

# 2) 环境变量：不填 LLM_API_KEY 也能跑（LLM_MOCK=true）
cp .env.example .env

# 3) 建表（W1 的 7 张表 + W2 的 mock_messages）
python -m scripts.verify_seed   # 或 alembic upgrade head

# 4) 起服务
uvicorn app.main:app --reload    # /docs 可试 POST /api/v1/tasks
```

```bash
# 5) 测试：不依赖 MySQL / Redis / 真实 LLM
pytest                           # 132 passed
pytest tests/test_sql_query_security.py -v   # 安全用例单跑
```

任务示例（ADMIN token）：

```json
{
  "goal": "分析上周 GMV 下跌原因，检索历史周报佐证，并通知运营群",
  "time_range": {"start": "2026-10-12", "end": "2026-10-18"},
  "sync": false
}
```

含「通知」→ 规划出 notify 子任务 → 任务停在 `AWAITING_APPROVAL`（审批 REST 属 W3）。

---

## 5. 测试覆盖（120 个 W2 用例 + 12 个 W1 用例）

| 文件 | 覆盖 |
|---|---|
| `tests/test_planner.py` | 正常规划、围栏 JSON、**解析失败修复重试**、DAG 环修复、修复用尽报错、温度 0.2、重规划入口 |
| `tests/test_executor.py` | 成功瘦身、确定性错误改写 3 次、可重试退避序列 `[2,4,4]`、循环上限、图表表格兜底、拒绝编造数据 |
| `tests/test_sql_query_security.py` | DML/DDL/多语句/注释/`SELECT INTO`/`FOR UPDATE` 拦截、白名单、跨库、LIMIT 追加与改写、扫描量超限、超时分类 |
| `tests/test_mcp_client.py` | 动态发现、成功调用、业务错误码与 retryable、协议 `-32601`、坏 server 降级、notify 审批闸门 |
| `tests/test_context_service.py` | 分工具瘦身、预算触发压缩、保留 3 轮、2 层压缩、无收益放弃压缩 |
| `tests/test_replan.py` | 原因优先级、上限 `REPLAN_EXHAUSTED`、DONE 保留 / FAILED 重排 |
| `tests/test_graph_routing.py` | 5 个路由纯函数全部分支 + 7 节点装配 |
| `tests/test_w2_e2e.py` | 无审批跑通至 `COMPLETED`；notify → interrupt → 批准 → 完成；驳回 → replan → `APPROVAL_MISSING` |

---

## 6. W3 边界（本阶段**未实现**，不要误判为已完成）

| 项 | 说明 | 依据 |
|---|---|---|
| **SSE 进度流** | `GET /api/v1/tasks/{id}/events`、节点级事件推送、前端订阅协议 | W3 |
| **审批 REST** | `GET /api/v1/approvals`、`POST /api/v1/approvals/{id}/approve|reject` | W3 |
| **恢复 / 取消** | `POST /api/v1/tasks/{id}/resume`（`Command(resume=...)`）、`/cancel`（`ainterrupt`/状态置 CANCELLED） | W3 |
| **trace / 回放** | `GET /api/v1/tasks/{id}/trace`（span 树、工具耗时瀑布图） | W3 |
| **前端** | 任务列表 / 详情 / 审批台 / 报告渲染（`frontend/` 仍为空壳） | W3 |
| **MCP HTTP 传输** | 仅 stdio；HTTP/SSE 传输为 P2 | 硬性约束 3 |
| **分层记忆 FR-014** | **仅注释占位**（`memory_hits` 字段 + 注入位），不编码实现 | 硬性约束 8 |
| **真实联调** | 真实 MySQL 只读库、RedisSaver、DeepSeek/SiliconFlow、项目一 RAG、项目二 Eval（当前全部 Mock 或进程内） | 硬性约束 2 |
| **多租户 / 配额** | 租户隔离、LLM 成本配额、限流 | W4 |

---

## 7. 已知限制

1. `LLM_MOCK=true` 时规划质量不代表真实效果（Mock 只按关键词拼最小可用 DAG），验收规划质量需配真实 Key。
2. `RedisSaver` 未在 CI 覆盖：单测统一走 `MemorySaver`，Redis 路径需 `CHECKPOINTER_BACKEND=redis` 手工验证。
3. `chart-render` 走进程池时首次渲染受 matplotlib 字体缓存影响，冷启动约 1~3s。
4. `notify` 恒 Mock：`NOTIFY_DRY_RUN=true` 且只写 `mock_messages` 表，绝不会出网。
5. `kb-search` 的 Mock 语料仅 4 条周报片段，用于验证"引用缺失/降级"链路，不代表真实召回质量。
