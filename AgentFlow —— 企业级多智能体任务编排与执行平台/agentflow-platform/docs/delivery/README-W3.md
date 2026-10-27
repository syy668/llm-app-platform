# AgentFlow Platform — W3 交付说明（SSE 进度流 + 人机审批断点续跑 + 轨迹回放 + 最小 MVP 前端）

> 本文件只描述 **W3 阶段**。W1 脚手架见 `README-W1.md`，W2 编排内核见 `README-W2.md`。
> W3 未能覆盖的能力（评测脚本、项目一真实接口联调、A/B 压缩对比、Demo 录制，以及 P2 的
> MCP HTTP 传输 / FR-014 分层记忆）见文末「W4 边界」与「P2 占位」。

---

## 1. 本阶段完成什么

把 W2「能跑完但要等」的批处理链路，变成「**可订阅、可暂停、可续跑、可回放**」的可运营链路：

```
POST /api/v1/tasks/stream          （提交并拿到 stream_url）
        │
        ├── 后台协程：graph.astream_events(...)  ──┐
        │                                        │ 4 类事件
        │                                        ▼
        │                    Redis Stream（事件总线，MAXLEN ~ N + TTL 24h）
        │                                        │
        ▼                                        ▼
GET /tasks/{id}/stream  ◀──── SSE（text/event-stream，带 id/retry/心跳）
        │
        ├─ state_update ──▶ 前端进度条 / DAG 高亮
        ├─ tool_call    ──▶ 工具调用流水
        ├─ interrupt    ──▶ 自动弹审批弹窗（终止帧）
        └─ complete     ──▶ 收尾 + 报告（终止帧）

审批侧：GET /approvals（收件箱）→ POST /tasks/{id}/resume {decision}
        → Command(resume=...) → 从 Redis checkpoint 续跑（不重跑已完成节点）
```

四条硬性行为：

1. **进度可见**：4 类事件过去式推送，断线用 `Last-Event-ID` 续订，不丢不重；
2. **挂起可控**：`human_gate` 挂起时状态置 `AWAITING_APPROVAL`，前端从 `interrupt` 事件直接拿到
   `approval_id` / 阻断原因 / 高危子任务预览；
3. **续跑不重跑**：`resume` 走 `Command(resume=...)`，图从 checkpoint 的 `human_gate` 之后继续；
4. **过程可回放**：节点进出、工具调用、LLM 入参出参、checkpoint 快照全部落库，只读回放接口回放。

W1 / W2 代码**零改动**（只有两处 include_router 注册与一个 W1 占位类的实现填充，见 §2.5）。

---

## 2. 新增文件清单

### 2.1 配置与契约

| 文件 | 行数 | 作用 |
|---|---:|---|
| `app/core/w3_config.py` | 87 | W3 全量开关：`SSE_*` / `STREAM_*` / `TRACE_*` / `APPROVAL_*` / `CANCEL_*`，`w3` 单例；不侵入 W1 `config.py` 与 W2 `w2_config.py` |
| `app/core/w3_errors.py` | 91 | W3 错误码（字符串，不扩 W1 `ErrorCode` 枚举）+ `TERMINAL_STATUSES` / `RESUMABLE_STATUSES` + `normalize_status()`（`CANCELED→CANCELLED`、`CREATED→PLANNING`）+ `conflict/not_found/bad_request/unavailable` |
| `app/schemas/w3.py` | 242 | 协议层契约：`SseEventType`/`TERMINAL_EVENTS`/`SseEvent.to_frame()`/`SseHeartbeat`、`ResumeRequest`/`CancelRequest`/`InterruptContext`、`ApprovalInboxItem`/`ApprovalInboxData`、`TraceStep`/`CheckpointStep`/`TraceReplayData`/`SnapshotData`/`ResumeResult`/`CancelResult` |

### 2.2 服务层（`app/services/`）

| 文件 | 行数 | 作用 |
|---|---:|---|
| `event_bus.py` | 276 | **W1 占位 → W3 实现**：`EventBus.publish/read/last_id/size/clear`；Redis Stream `XADD MAXLEN ~` + `EXPIRE` + `XREAD` 带游标；`backend="auto"` 时 Redis 失败自动降级进程内内存队列（只告警一次）；`StreamEvent.to_sse()`、`id_greater()` |
| `stream_context.py` | 265 | ContextVar 绑定 `StreamContext`（task_id/trace_id/bus/trace/node/seq）；`emit()` 统一信封；`mcp_event_hook` 把 MCP 的 `tool.call/tool.result` 翻成 `tool_call`；`InstrumentedLLM` 透明代理 LLM 客户端并落 `llm.call` 轨迹 |
| `trace_service.py` | 205 | `record()` 写 `task_executions`（复用 W1 已有表，不新增 ORM）；`event` 增 `llm.call`；`_safe_payload` 先脱敏再截断（`TRACE_PAYLOAD_MAX_CHARS`）；`list_steps/count/stats` |
| `interrupt_service.py` | 327 | `load_snapshot` / `collect_interrupts`（读 `snapshot.tasks[*].interrupts`）/ `interrupt_context` / `ensure_resumable`（DB 状态 + 断点双校验）/ `clear_interrupts`（`aupdate_state(as_node=...)` 打死断点）/ `find_pending_approval` / `expire_pending_approvals` / `list_paused_tasks` |
| `cancel_service.py` | 105 | `CancelSignal`（Redis 标记 + TTL）；`_degraded` 一次性降级，避免无 Redis 环境刷屏告警 |
| `snapshot_service.py` | 132 | `SnapshotService.snapshot()`（纯 JSON 化，`messages` 只留最近 N 条）/ `checkpoints()`（`aget_state_history` 按 step 升序）/ `replay()` |
| `stream_service.py` | 649 | **核心**：`version_candidates()` + `iterate_graph_events()` 版本协商（首选 v3 → v2 → v1 并缓存）；`StreamingTaskService(TaskService)` 只覆盖 `run/resume/cancel`，`submit` 返回 `stream_url`；`_handle_event()` 事件翻译；`_apply_cancel()`；模块级 `sse_event_frames()` 生成器 |
| `w3_runtime.py` | 172 | `create_w3_runtime()`：`MCPClientPool.create_default(on_event=mcp_event_hook)` + `InstrumentedLLM` + 注入**幂等**审批建单器/更新器；`get_w3_runtime()` 单例 + `reset_w3_runtime()` |

### 2.3 路由（`app/routers/`）

| 文件 | 行数 | 路由 |
|---|---:|---|
| `tasks_ext.py` | 287 | `POST /tasks/stream`、`GET /tasks/{id}/stream`、`GET /tasks/{id}/interrupt`、`POST /tasks/{id}/resume`、`POST /tasks/{id}/cancel`、`GET /tasks/{id}/snapshot`、`GET /tasks/{id}/trace`；`sse_current_user()` 支持 `Bearer` 头与 `?access_token=`；`assert_visible()` 统一 404/403 |
| `approvals.py` | 49 | `GET /approvals`（收件箱，ADMIN 只看本人、APPROVER 全量）、`GET /approvals/{approval_id}` |

### 2.4 前端（`frontend/`，与后端严格分离，**无构建链**）

| 文件 | 行数 | 作用 |
|---|---:|---|
| `index.html` | 31 | Vue 3.4.38 CDN + `<script type="module" src="src/main.js">`；CDN 不可达时给出兜底提示 |
| `src/styles.css` | 251 | 设计系统：`.card`/`.badge`/`.console`/`.timeline`/`.modal`/`.dag-*`/`.toast` |
| `src/api.js` | 136 | `request()`（`code!=='OK'` 抛 `ApiError`）+ `api.*` 13 个方法 + `streamUrl(taskId,lastEventId)` |
| `src/util.js` | 93 | `STATUS_META`/`NODE_LABEL`/时间与 token 格式化/`eventLine()` 四类事件中文化 |
| `src/store.js` | 60 | `reactive` 全局 store + `notify()` toast + hash 路由 |
| `src/sse.js` | 102 | `openTaskStream()`：四类事件监听、`lastEventId` 游标、终止帧即 `close()`、最多 6 次重连、暴露 `close/reconnect` |
| `src/components/dag.js` | 92 | 纯 SVG 只读 DAG（8 节点 10 边，含分支标签坐标），`doneNodes`/`activeNode`/`status` props |
| `src/components/approval-modal.js` | 96 | 同意/驳回 + 意见（驳回必填）+ `TASK_NOT_PAUSED` 特判 |
| `src/components/event-console.js` | 25 | 事件流水渲染 |
| `src/pages/submit.js` | 241 | **页面一** 任务提交 + SSE 窗口（interrupt 自动弹审批、终止任务、协议说明） |
| `src/pages/inbox.js` | 131 | **页面二** 待审批收件箱（15s 轮询 + 阻断原因 + 高危子任务） |
| `src/pages/detail.js` | 297 | **页面四** 任务详情 + DAG + 子任务 + 报告 + 评审 + 轨迹三 tab + 快照（**页面三**审批弹窗在此复用） |
| `src/main.js` | 118 | 顶栏（API 地址 / Token / 演示登录）+ hash 路由分发三个页面 + toast |

> `frontend/README.md` 是 W1 的空目录占位说明，按红线性约束**原样保留未改**；
> 前端实际运行方式见本文件 §7.2。

### 2.5 既有文件的最小改动（仅 3 处，不含业务逻辑）

| 文件 | 改动 | 说明 |
|---|---|---|
| `app/routers/api_router.py` | +2 行 `include_router` | 只注册 `tasks_ext` / `approvals`，W4 占位注释保留 |
| `app/services/event_bus.py` | W1 占位类 → 实现 | W1 只留了空壳接口，W3 填充实现，签名未变 |
| `.env.example` | 追加 W3 段（167~225 行） | 只新增注释与变量，W1/W2 段（1~166 行）一行未动 |

### 2.6 脚本与测试

| 文件 | 行数 | 说明 |
|---|---:|---|
| `tests/test_w3_e2e.py` | 777 | 18 条用例，见 §8 |
| `scripts/seed_w3_demo_users.py` | 155 | 幂等建 3 个演示账号（`admin_user` / `approver_user` / `viewer_user`，密码 `Passw0rd!`）。W1 未提供用户种子，前端「演示登录」按钮依赖此脚本 |

---

## 3. 断点续跑实现方案

### 3.1 落点：为什么"不重跑"

LangGraph 把每一步状态写入 checkpointer（W1 已接 `RedisSaver`，`thread_id = str(task_id)`）。
`human_gate` 用 `interrupt()` 挂起后：

- `snapshot.next = ("human_gate",)`，`snapshot.tasks[*].interrupts` 非空；
- `resume` 时传 `Command(resume={...})`，图**只重跑 `human_gate` 这一个节点**，其上游
  `planner` 的输出（`plan` / `subtasks` / 路由决策）全部从 checkpoint 读出，不重新调模型。

因此续跑的成本是「一个 gate 节点 + 下游节点」，而不是整条链路。

### 3.2 挂起判定（红线：禁止用 `__interrupt__`）

```python
snapshot = await graph.aget_state(config)          # interrupt_service.load_snapshot
interrupts = collect_interrupts(snapshot)          # 读 snapshot.tasks[*].interrupts
paused = bool(interrupts)                          # 唯一判定口径
```

`__interrupt__` 是 LangGraph 旧版本在 `ainvoke` 返回值里塞的临时字段，0.3.x 已不再保证存在，
且无法区分"多个待处理 interrupt"，故全链路不使用。

### 3.3 两次执行的事件口径一致

`run()` 与 `resume()` 都收敛到 `StreamingTaskService._drive(task_id, resume_payload=...)`：

| 差异点 | `run` / `submit` | `resume` |
|---|---|---|
| 图入参 | `None`（从 START 走） | `Command(resume=payload)` |
| 前置校验 | 无 | `ensure_resumable()`：DB 状态 ∈ `RESUMABLE_STATUSES` 且断点存在，否则 **409 `TASK_NOT_PAUSED`** |
| 事件 `state_update.phase=run` 的 `mode` | `fresh` | `resume` |

事件翻译、轨迹落库、终态判定全部共用 `_drive_bound()`，所以前端不需要区分"首跑"与"续跑"。

### 3.4 幂等：为什么必须

`Command(resume=...)` 会让 `human_gate` **整节点重跑**，节点内的"建审批单"代码会再执行一次。
若不幂等，每次 resume 都会多插一条 `approvals`，收件箱出现重复条目。

处理方式：`w3_runtime.make_idempotent_approval_creator()` 先
`find_pending_approval(task_id)`，命中则复用（返回已存在的 `approval_id`），未命中才 INSERT。

### 3.5 取消：如何"打死断点"

只把 `tasks.status` 改成 `CANCELLED` 是不够的——checkpoint 里仍有活断点，此时调 `resume`
会真的把任务继续跑起来。因此 `clear_interrupts()` 额外做一件事：

```python
await graph.aupdate_state(config, update, as_node=<snapshot.next[0]>)   # as_node=human_gate
```

实测效果：`snapshot.next == ()`、`snapshot.tasks[*].interrupts == []`、`aget_state_history()`
多出一条 `step=1` 的 `source=update` 记录。此后 `ensure_resumable()` 必然返回 409。

取消分两条路径：

| 场景 | 处理 | 结果 |
|---|---|---|
| 任务处于 `AWAITING_APPROVAL`（已挂起、无协程在跑） | `clear_interrupts` + 状态置 `CANCELLED` + 审批单 `EXPIRED` + 发 `complete` | 立即生效 |
| 任务处于 `RUNNING`（协程在跑） | 写 Redis 取消标记 `cancel:{task_id}`（TTL 24h），图在**节点边界**检查到标记后 `break`，再走 `_apply_cancel()` | 当前节点跑完即停 |

节点边界检查（而非强杀协程）是为了让"已经发出的工具调用"能收尾，不留半截产物。

---

## 4. SSE 事件协议

### 4.1 帧格式

```
retry: 3000            ← 仅首帧下发（SSE_RETRY_MS）
id: 1730000000000-3    ← Redis Stream ID，等价 Last-Event-ID 游标
event: state_update    ← 4 类之一
data: {"task_id":1,"trace_id":"...","seq":4,"node":"executor","ts":"...","elapsed_ms":812,...}
                       ← data 严格单行 JSON（换行已被拆分，不会破坏 SSE 帧）

: ping                 ← 心跳注释帧（不触发 EventSource 的 message 事件）
```

响应头：`Content-Type: text/event-stream; charset=utf-8`、`Cache-Control: no-cache`、
`X-Accel-Buffering: no`（禁用 Nginx 缓冲）。

### 4.2 统一信封（所有事件共有）

| 字段 | 类型 | 说明 |
|---|---|---|
| `task_id` | int | 任务 ID |
| `trace_id` | string | 链路 ID（与 `tasks.trace_id` 一致，便于跨日志对齐） |
| `seq` | int | 本任务内单增序号（前端排序兜底；Stream ID 才是去重依据） |
| `node` | string | 事件归属节点（`tool_call` 用确定性映射，见下） |
| `ts` | string | ISO8601 时间戳 |
| `elapsed_ms` | int | 距本次执行开始的毫秒数 |

### 4.3 四类事件

| `event` | 触发点 | 负载字段 | 终止帧 |
|---|---|---|:--:|
| `state_update` | 节点 `on_chain_start` / `on_chain_end` / `on_chain_error`；执行开始 | `phase`=`run`→`{mode}`；`enter`→`{node,status}`；`exit`→`{node,status,output_keys[]}`；`error`→`{node,status,error}` | ✗ |
| `tool_call` | MCP 层 `tool.call` / `tool.result`（经 `mcp_event_hook`） | `phase`=`call`→`{tool,server,params}`；`result`→`{tool,server,ok,latency_ms,error_code,result_digest}` | ✗ |
| `interrupt` | 图停在 `human_gate`（`snapshot.tasks[*].interrupts` 非空） | `phase="awaiting_approval"` + `paused/task_id/status/approval_id/type/reason/preview/interrupted_at/next_node/resumable/resume_endpoint` | ✓ |
| `complete` | 图正常结束 / 失败 / 被取消 | 正常：`status,token_usage,artifacts[],degraded,replan_count,elapsed_ms`；失败：`status=FAILED,error{code,message}`；取消：`status=CANCELLED` | ✓ |

`interrupt` 与 `complete` 是**终止帧**：前端收到即 `EventSource.close()`；审批后由用户操作触发
重新 `openTaskStream()` 续订。

### 4.4 `tool_call` 的节点归属

图节点推进与工具事件是并发产生的，`ctx.node` 可能已经跑到下一个节点。为保证前端流水归属正确，
用确定性映射 `TOOL_NODE_HINT`：

| 工具 | 归属节点 |
|---|---|
| `sql-query` / `chart-render` / `kb-search` | `executor` |
| `notify` | `notifier` |

### 4.5 断线重连与去重

- 浏览器 `EventSource` 自动重连带 `Last-Event-ID` 头；**该头在自定义实现里不一定可控**，
  因此同时支持查询参数：`GET /tasks/{id}/stream?last_event_id=1730000000000-3&access_token=...`；
- `EventSource` 不能设置请求头 → 鉴权额外支持 `?access_token=<JWT>`（`sse_current_user()` 内
  Bearer 头优先、查询参数兜底）；
- 服务端 `XREAD` 以游标为起点，只返回 `id > last_event_id` 的事件（`id_greater()` 比较保证
  同毫秒内多事件不乱序），**天然去重**；
- 若订阅时任务已达终态，服务端补发一条 `complete` 后立即返回，不空转（`block_ms=0` 快路径）。

### 4.6 协议版本协商（为什么默认写 v3）

需求指定 `version="v3"`，但 **langgraph 0.3.34 只实现 v1/v2**，直接传 v3 抛
`NotImplementedError: Only versions "v1" and "v2" of the schema is currently supported.`。

实现为协商而非硬编码：

```python
def version_candidates(preferred=None) -> list[str]:   # ["v3", "v2", "v1"]
async def iterate_graph_events(graph, input, config, preferred=None):
    for version in candidates:                          # 逐个尝试
        try:
            async for ev in graph.astream_events(input, config, version=version):
                yield ev
            _RESOLVED_EVENTS_VERSION = version          # 成功后缓存，后续不再试错
            return
        except NotImplementedError:
            continue                                    # 退让到下一版本
```

升级 langgraph 后把 `SSE_EVENTS_VERSION` 改成 v3 即自动生效，无需改代码。

---

## 5. 状态流转

```
                    POST /tasks/stream
                            │
                            ▼
                      ┌───────────┐
                      │  CREATED  │   （W1 枚举值；入库即 PLANNING，两者等价归一）
                      └─────┬─────┘
                            │ run()
                            ▼
                      ┌───────────┐
              ┌──────▶│  RUNNING  │◀──────────┐
              │       └─────┬─────┘           │
              │             │                 │ APPROVE（Command(resume)）
              │   human_gate 触发 interrupt    │
              │             ▼                 │
              │   ┌────────────────────┐      │
              └───┤ AWAITING_APPROVAL  ├──────┘
                  └─────┬────────┬─────┘
              REJECT    │        │  CANCEL（清断点 + 审批单 EXPIRED）
        （写 replan_context）    │
                        ▼        ▼
                  ┌──────────┐  ┌───────────┐
                  │  RUNNING │  │ CANCELLED │ ← 终态
                  │ (replan) │  └───────────┘
                  └────┬─────┘
                       │ reviewer 分数达标 / replan 超限
             ┌─────────┴─────────┐
             ▼                   ▼
      ┌───────────┐       ┌──────────┐
      │ COMPLETED │       │  FAILED  │   ← 均终态
      └───────────┘       └──────────┘
```

要点：

- **`AWAITING_APPROVAL` 不是终态**：`TERMINAL_STATUSES = {COMPLETED, FAILED, CANCELLED}`；
- **可续跑集合** `RESUMABLE_STATUSES = {AWAITING_APPROVAL}`，其余一律 409；
- **驳回的真实走向**：`human_gate(REJECTED)` → `replan` → `executor` → `reviewer` → `summarizer`
  → `notifier` → `notify` 因审批未通过返回 `APPROVAL_MISSING` → **FAILED**（`replan_count=1`）。
  这是 W2 既有语义（审批是 notify 的硬闸门），W3 只如实反映，不做"驳回即成功"的粉饰；
- 状态写入统一走 W2 的 `TaskService._finalize/_resolve_status`，W3 只新增 `AWAITING_APPROVAL`
  与 `CANCELLED` 两个落点。

---

## 6. 关键设计决策

| # | 决策 | 理由 |
|---|---|---|
| 1 | **子类覆盖 `run`，不改 W2 `TaskService`** | 复用 `create_task/_finalize/_resolve_status`；红线要求 W2 零改动 |
| 2 | **ContextVar 传执行上下文** | `MCPClient._emit(on_event=...)` 与 `LLMClient.complete` 签名冻结，钩子只能靠 ContextVar 拿到 task_id/node |
| 3 | **`interrupt`/`complete` 必须在 bind 存活期内发布** | 首版在 `finally: unbind()` 之后 emit，ContextVar 已清空 → 终止帧丢失。修法：`_drive` 只做 bind/unbind，真正跑图放 `_drive_bound()` |
| 4 | **Redis Stream 而非 pub/sub** | pub/sub 无回放、无游标；Stream 的 ID 天然就是 `Last-Event-ID`，多客户端各自 XREAD 即广播语义，无需消费组 |
| 5 | **不用消费组** | 消费组是"分工"语义，SSE 是"广播"语义（多标签页/多客户端都要看全量） |
| 6 | **事件发布失败不打断执行** | `emit()` 内 catch 全部异常只告警；进度是旁路，业务正确性优先 |
| 7 | **取消用"节点边界检查"而非强杀** | 强杀会留下半截工具调用与不一致 artifact |
| 8 | **轨迹复用 `task_executions`，不新增表** | W1 表已够用（`node/event/tool_name/payload/tokens/latency`），避免动 ORM |
| 9 | **先脱敏再截断** | 顺序反了会留下被截断的敏感串（如脱敏前截断半截手机号） |
| 10 | **reset 而非 close：单测用 fakeredis + 内存 SQLite；W3 e2e 用文件型 SQLite + WAL** | 图执行与轨迹写入并发，`StaticPool` 单连接库会报 `cannot commit transaction - SQL statements in progress` |
| 11 | **`app.state` 承载 bus / stream_service** | 测试可替换（`app.state.event_bus = w3_bus`），无需 patch 全局变量 |

---

## 7. 本地运行

### 7.1 后端

```bash
cd agentflow-platform
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt

# 1) 环境变量（无 LLM_API_KEY / 无外部 RAG 也能跑）
#    MOCK_EXTERNAL_RAG=true
#    MOCK_EXTERNAL_EVAL=true
#    LLM_MOCK=true                （W2 已支持）
#    STREAM_BACKEND=auto          （无 Redis 会自动降级进程内内存）

# 2) 建表
.venv/Scripts/python.exe -m alembic upgrade head

# 3) 建演示账号（W1 没有用户种子，前端"演示登录"依赖它）
.venv/Scripts/python.exe scripts/seed_w3_demo_users.py

# 4) 起服务
.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000
```

无 MySQL 想纯本地看效果时，可用**独立演示库**（不动主库）：

```bash
.venv/Scripts/python.exe scripts/seed_w3_demo_users.py \
    --dsn "sqlite+aiosqlite:///./storage/w3_demo.db" --create-tables
# 注意：app 的 DATABASE_URL 由 MYSQL_* 派生，跑 uvicorn 仍需 MySQL；
#       该命令仅用于验证脚本本身与 SQLite 演示数据。
```

### 7.2 前端（纯静态，无 npm / 无构建）

```bash
cd agentflow-platform/frontend
python -m http.server 5173
# 浏览器打开 http://127.0.0.1:5173/
```

- 顶栏填 API 地址（默认 `http://127.0.0.1:8000`），点「用演示账号获取 Token」即可登录；
- 演示账号：`admin_user` / `approver_user` / `viewer_user`，密码统一 `Passw0rd!`；
- 三个页面用 hash 路由：`#/submit`（任务提交 + SSE）、`#/inbox`（待审批收件箱）、
  `#/tasks/{id}`（详情 + 轨迹回放）；审批弹窗是跨页面复用的组件。

### 7.3 两条演示路径

**A. 跑通 + 审批通过**

1. `#/submit` 填目标（如「分析 2026-10-12~10-18 GMV 下跌原因」），点提交；
2. 事件窗口依次出现 `state_update(planner/enter/exit)` → `interrupt`（弹审批框）；
3. `#/inbox` 能看到这条待审批（含阻断原因与高危子任务）；
4. 点「同意」→ 事件窗口续接 `state_update(executor…)` → `tool_call(sql-query)` →
   `tool_call(notify)` → `complete(status=COMPLETED)`；
5. 进 `#/tasks/{id}` 看 DAG 高亮、报告、轨迹三 tab（步骤 / 工具调用 / 快照）。

**B. 驳回 → 重规划 → 失败（W2 语义）**

1. 同上挂起后点「驳回」并填意见；
2. 事件窗口出现 `state_update(replan)` → `executor` → `reviewer` → `summarizer` → `notifier`
   → `complete(status=FAILED, error.code=APPROVAL_MISSING)`；
3. 详情页可见 `replan_count=1` 与驳回意见原文。

**C. 中途取消**

- 挂起态取消：状态直接 `CANCELLED`，断言再 `resume` 必 409；
- 运行中取消：当前节点跑完后停，`complete(status=CANCELLED)`。

---

## 8. 测试覆盖

```
pytest（全量）→ 150 passed
  ├── W1 鉴权            12 条（未改动）
  ├── W2 编排/MCP/上下文  120 条（未改动）
  └── W3 e2e            18 条（新增）
```

`tests/test_w3_e2e.py` 18 条：

| # | 用例 | 覆盖 |
|---:|---|---|
| 1 | `test_execution_pauses_at_human_gate` | 提交后状态 → `AWAITING_APPROVAL`，断点存在 |
| 2 | `test_sse_stream_protocol` | 帧格式（`retry`/`id`/`event`/`data`）、事件序列、终止帧 |
| 3 | `test_sse_requires_token` | 无 token → 401 |
| 4 | `test_approve_resumes_from_checkpoint_and_completes` | **审批通过续跑至 COMPLETED**，节点集合含 `human_gate/executor/reviewer/summarizer/notifier` 且**不含 `replan`** |
| 5 | `test_reject_triggers_replan_and_blocks_unapproved_notify` | **驳回触发 replan**，`replan_count=1`，`APPROVAL_MISSING` |
| 6 | `test_resume_non_paused_task_conflicts` | 非暂停 resume → **409 `TASK_NOT_PAUSED`** |
| 7 | `test_resume_missing_task_returns_404` | 不存在 → 404 |
| 8 | `test_cancel_paused_task_then_resume_conflicts` | 挂起态取消 → `CANCELLED`，再 resume → 409 |
| 9 | `test_cancel_finished_task_conflicts` | 终态取消 → 409 `TASK_NOT_CANCELABLE` |
| 10 | `test_cancel_running_task_stops_at_node_boundary` | 运行中取消 → 节点边界停机 |
| 11 | `test_sse_reconnect_dedups_by_last_event_id` | **断线重连去重**（带游标不重复） |
| 12 | `test_sse_stops_when_client_disconnects` | 客户端断开 → 生成器收尾 |
| 13 | `test_sse_returns_immediately_when_stream_already_terminal` | 终态补发即返回，不空转 |
| 14 | `test_snapshot_and_trace_replay` | 快照 JSON 化 + 回放（checkpoint 数 / step 数） |
| 15 | `test_approval_inbox_lists_blocking_reason` | 收件箱含阻断原因 |
| 16 | `test_viewer_cannot_read_running_snapshot` | 越权 → 403 |
| 17 | `test_admin_cannot_approve_others_task_inbox` | ADMIN 只看本人（角色矩阵） |
| 18 | `test_events_version_falls_back_to_v2` | **版本协商降级**到 v1/v2 |

---

## 9. 红线自查

| 约束 | 结论 |
|---|---|
| W1 鉴权 / 全局中间件 / ORM / docker-compose / 12 条鉴权用例 | **一行未改**，`pytest tests/test_auth.py` 12 passed |
| W2 编排（7 节点）/ 4 个 stdio MCP Server / SQL 安全校验 / 上下文工程 / 132 条用例 | **完全保留，只新增文件**；`pytest` 132 → 132 passed |
| `MOCK_EXTERNAL_RAG=true` / `MOCK_EXTERNAL_EVAL=true` | 沿用，未引入真实外部依赖 |
| MCP HTTP 传输 / FR-014 分层记忆 | **未编码**，仅在 `api_router.py` 与 `w3_runtime.py` 留注释占位 |
| 挂起判断 | 全部走 `snapshot.tasks[*].interrupts`；`grep -rn "__interrupt__" app/` 的 5 处命中**全部是「禁止使用」的注释/文档字符串**，无一处代码读取 |
| 断点持久化 | 继续用 `RedisSaver`；单测用 `fakeredis` + 内存 SQLite + `InMemoryTransport`，不启真实子进程 |
| 前端目录 | 严格隔离在 `frontend/`，后端 `app/` 内无任何前端产物；前端无构建链、无 npm 依赖 |

---

## 10. W4 边界（本阶段**未实现**，不要误判为已完成）

| # | W4 待实现 | 当前状态 | 说明 |
|---|---|---|---|
| 1 | **评测脚本** | 未开始 | FR-013 的离线评测集跑批：`scripts/eval_runner.py`（读 `分层难例评测集-manifest.json` → 批量提交 → 汇总 faithfulness / 任务成功率 / 平均 token / P95 时延），产出可入库的评测报告；`MOCK_EXTERNAL_EVAL=true` 的 Mock 分支已在 W2 留好 |
| 2 | **项目一真实接口联调** | 未开始 | 当前 `sql-query` 走本地 mock 库；W4 需接项目一（mall 模拟数据，约 52 万行）的真实 MySQL，跑通 `scripts/verify_seed.py` 的 3 条硬断言 + 20 个抽样断点，并验证 GMV 下跌主因/次因归因结论 |
| 3 | **A/B 压缩对比实验** | 未开始 | W2 已实现上下文瘦身 + 2 层压缩，但缺**量化对比**：同一批任务在「开压缩 / 关压缩」下的 token 消耗、信息损失（关键字段召回率）、LLM 调用次数、成本差异 |
| 4 | **Demo 录制** | 未开始 | 按 §7.3 三条路径录制演示视频：提交 → SSE 实时进度 → 挂起 → 审批 → 续跑 → 驳回重规划 → 取消 → 轨迹回放 |
| 5 | 登录页 | 未做（有意） | MVP 前端直接复用后端 token，不做登录页；W4 若要对外开放再补 |
| 6 | 事件总线多实例 | 未验证 | Redis Stream 天然支持多实例，但未做压测与 `XADD` 失败重试的容量验证 |

### P2 占位（按需求本阶段不编码）

| 项 | 现状 |
|---|---|
| MCP HTTP 传输 | `mcp_client.py` 仅 stdio；`w3_runtime.py` 注释标注 P2 扩展点 |
| FR-014 分层记忆（短期/会话/长期） | 未实现；`graph_state.py` 现有 `messages` 为单层短期记忆 |

---

## 11. 已知限制

1. **驳回即失败**：驳回后 `replan` 会重跑，但 `notify` 的审批闸门使最终落 `FAILED`
   （W2 语义）。若产品期望"驳回后自动再审批一次"，属 W4 需求变更；
2. **运行中取消非强杀**：需等当前节点结束，最坏情况等一个 LLM 调用 + 一次工具调用的时长；
3. **SSE 单连接上限**：未做连接数限流（`SSE_MAX_DURATION_SECONDS` 只做时间兜底），
   生产需在网关层加并发限制；
4. **无 Redis 时的降级总线是进程内的**：多 worker 部署必须用 Redis（`STREAM_BACKEND=redis`）；
5. **轨迹表会随任务线性增长**：`TRACE_PAYLOAD_MAX_CHARS` 只控单条大小，未做归档/分区；
6. **`starlette` 的 `EventSourceResponse` 未使用**：为保证 `id`/`retry`/心跳完全可控，
   手写 `StreamingResponse` 生成器，需自行保证 `is_disconnected` 检查（已在 `sse_event_frames` 内做）。
