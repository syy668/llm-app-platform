# API 参考 v1.1（人工审批 / 分层记忆 / 导出）

> 适用版本：`v1.1`（在 `v1.0.6` 基础上新增导出、知识库自检、Reviewer 可信度字段）
> 基址：`http://127.0.0.1:8000/api/v1`；完整机器可读定义见运行中服务的 `/openapi.json`
> 本文示例中的 token、任务号、memory_id 均来自本机真实调用，不是手写占位。

---

## 0. 通用约定

| 项 | 约定 |
|---|---|
| 响应信封 | `{"code":"OK","message":"ok","data":{...},"trace_id":"..."}`；失败时 `code` 为错误码（如 `FORBIDDEN`、`NOT_FOUND`） |
| 鉴权 | `Authorization: Bearer <access_token>`；SSE 端点额外接受 `?access_token=`（`EventSource` 不能设头） |
| 分页 | 请求 `page`（从 1 开始）、`size`；响应的分页字段**不统一**：`/approvals` 平铺 `{items,page,size,total}`，`/memories` 包在 `{items,meta}` 里 |
| 时间 | **全库 UTC naive**（无偏移）。序列化带 `+00:00` 的接口（日志、轨迹）前端需 `dayjs.utc(v).local()` 展示 |
| 角色 | `ADMIN` / `APPROVER` / `VIEWER`；违规返回 403 `FORBIDDEN` |
| 导出类接口 | 返回**文件流**（`Content-Disposition: attachment`），不套信封 |

---

## 1. 人工审批（FR-011 / FR-012）

### 1.1 待审批收件箱

```
GET /approvals?page=1&size=20
```

```bash
curl -s "http://127.0.0.1:8000/api/v1/approvals?page=1&size=2" \
  -H "Authorization: Bearer $TOKEN"
```

```json
{
  "code": "OK",
  "message": "ok",
  "data": {
    "items": [
      {
        "task_id": 10,
        "task_no": "AF20261022-0010",
        "goal": "统计上周各渠道 GMV 并推送到运营群",
        "status": "AWAITING_APPROVAL",
        "creator_id": 1,
        "approval_id": 4,
        "approval_type": "HIGH_RISK_TOOL",
        "reason": "高危动作需审批：#5 推送运营群",
        "risky_subtasks": [
          {
            "seq": 5,
            "name": "推送运营群",
            "tool": "notify",
            "tool_hint": "notify",
            "risk_level": "high",
            "description": "将 2026-10-12 至 2026-10-18 各渠道 GMV 汇总摘要推送至运营群"
          }
        ],
        "created_at": "2026-10-22T04:31:01",
        "expires_at": "2026-10-23T04:31:03"
      }
    ],
    "page": 1,
    "size": 2,
    "total": 1
  },
  "trace_id": "0b9fe9343d0e4c9ea72655142710b4a3"
}
```

> 字段口径：`risky_subtasks[].tool` 与 `tool_hint` 同值（`tool` 是对外契约，`tool_hint` 为前端历史兼容别名）；
> `risk_level` 为小写枚举串 `high` / `normal` / `low`。

### 1.2 已处理历史

```
GET /approvals?status=APPROVED,REJECTED,EXPIRED&page=1&size=20
```

`status` 支持逗号分隔多值；不传或传 `PENDING` 时返回待处理收件箱。

```json
{
  "code": "OK",
  "data": {
    "items": [
      {
        "approval_id": 9,
        "task_id": 15,
        "subtask_id": null,
        "type": "HIGH_RISK_TOOL",
        "reason": "高危动作需审批：#5 推送运营群",
        "status": "APPROVED",
        "payload": {
          "goal": "统计近7天各渠道销售额，并把汇总结果推送到运营群",
          "subtasks": [{ "seq": 5, "name": "推送运营群", "tool": "notify", "risk_level": "high" }],
          "time_range": { "start": "2026-10-15", "end": "2026-10-22" }
        },
        "decided_by": 1,
        "decide_comment": "重启后断点恢复验证",
        "decided_at": "2026-10-22T06:39:12"
      }
    ],
    "page": 1,
    "size": 2,
    "total": 4
  }
}
```

### 1.3 审批单详情

```
GET /approvals/{approval_id}
```

返回单条审批单（结构与上表元素一致），用于抽屉直开 / 深链分享。

### 1.4 查看挂起上下文

```
GET /tasks/{task_id}/interrupt
```

```json
{
  "code": "OK",
  "data": {
    "task_id": 15,
    "approval_id": 9,
    "paused": true,
    "node": "human_gate",
    "preview": {
      "goal": "统计近7天各渠道销售额，并把汇总结果推送到运营群",
      "time_range": { "start": "2026-10-15", "end": "2026-10-22" },
      "subtasks": [
        { "seq": 5, "name": "推送运营群", "tool": "notify", "tool_hint": "notify", "risk_level": "high", "description": "..." }
      ]
    }
  }
}
```

> `paused=false` 表示当前没有挂起的断点（任务已终态，或 checkpoint 已被清理）。

### 1.5 审批决策并续跑

```
POST /tasks/{task_id}/resume
```

```bash
curl -s -X POST "http://127.0.0.1:8000/api/v1/tasks/15/resume" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"decision":"APPROVED","comment":"同意推送"}'
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `decision` | 是 | `APPROVED` / `REJECTED`；`REJECTED` 会让 graph 进入 **replan** 分支重规划 |
| `comment` | 否 | 审批意见，落库到 `approvals.decide_comment` |

```json
{ "code": "OK", "data": { "task_id": 15, "approval_id": 9, "status": "RUNNING", "resumed": true } }
```

> 断点持久化：`approvals` 与 LangGraph checkpoint 都在 Redis（Redis Stack）里，
> **后端重启后 resume 仍可成功**；若返回 409 `INTERRUPT_NOT_FOUND`，说明该任务的断点已过期（TTL 72h）。

### 1.6 批量审批（失败隔离）

```
POST /approvals/batch
```

```bash
curl -s -X POST "http://127.0.0.1:8000/api/v1/approvals/batch" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"items":[{"task_id":7,"decision":"APPROVED","comment":"验收批量通过"},{"task_id":999999,"decision":"APPROVED"}]}'
```

```json
{
  "code": "OK",
  "data": {
    "succeeded": 1,
    "failed": 1,
    "results": [
      { "task_id": 7, "approval_id": 1, "ok": true, "status": "RUNNING" },
      { "task_id": 999999, "ok": false, "code": "NOT_FOUND", "message": "任务 999999 不存在" }
    ]
  }
}
```

> 语义：**逐条独立执行**，单条失败不影响其他条；调用方按 `results[].ok` 分组提示即可。
> 上限 50 条/次，超出返回 422。

---

## 2. 分层记忆（FR-014）

前置条件：`.env` 里 `ENABLE_LAYERED_MEMORY=true`，否则写接口返回 503、读接口返回空列表（设计内的总开关）。

### 2.1 能力元信息

```
GET /memories/meta
```

```json
{
  "code": "OK",
  "data": {
    "enabled": true,
    "store": "local-keyword",
    "fallback": "local-keyword",
    "short_ttl_seconds": 86400,
    "mid_ttl_days": 30,
    "long_min_review_score": 3.0,
    "layers": {
      "short": { "backend": "redis", "ttl_seconds": 86400, "summarized": false },
      "mid": { "backend": "mysql.memories", "ttl_days": 30, "summarized": true },
      "long": { "backend": "mysql.memories + rag", "min_review_score": 3.0, "ttl_days": null }
    },
    "viewer": { "user_id": 1, "role": "admin", "scope_filter": false }
  }
}
```

> `store=local-keyword` 表示向量检索走本地关键词打分（`MOCK_EXTERNAL_RAG=true` 时的实现），
> 不是假数据；切到真实 RAG 后该字段会变为项目一的向量库标识。

### 2.2 列表

```
GET /memories?scope=long&page=1&size=20&task_id=20
```

```json
{
  "code": "OK",
  "data": {
    "items": [
      {
        "id": 11,
        "scope": "long",
        "user_id": 1,
        "task_id": 20,
        "content": "- #1 取数：近7天各渠道订单量 [FAILED]: ... （任务结论摘要）",
        "importance": 4,
        "hit_count": 2,
        "created_at": "2026-10-22T07:12:03"
      }
    ],
    "meta": { "page": 1, "size": 20, "total": 7 }
  }
}
```

> 与 `/approvals` 不同，这里分页在 `data.meta` 里。

### 2.3 新增

```
POST /memories
```

```bash
curl -s -X POST "http://127.0.0.1:8000/api/v1/memories" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"scope":"long","content":"渠道销售汇总任务规范：GMV 需扣除退款金额，推送运营群时只发摘要不超过200字。","importance":5}'
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `scope` | 是 | `short` / `mid` / `long` |
| `content` | 是 | 记忆正文 |
| `importance` | 否 | 1~5；`scope=long` 归档要求 ≥ `long_min_review_score`（默认 3） |
| `task_id` | 否 | 关联任务（任务收尾自动归档时会带） |

### 2.4 召回

```
POST /memories/recall
```

```bash
curl -s -X POST "http://127.0.0.1:8000/api/v1/memories/recall" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"goal":"汇总近7天各渠道销售额并推送运营群","top_k":2}'
```

```json
{
  "code": "OK",
  "data": {
    "enabled": true,
    "goal": "汇总近7天各渠道销售额并推送运营群",
    "store": "local-keyword",
    "hits": [
      {
        "memory_id": 7,
        "scope": "long",
        "score": 0.4065,
        "content": "- #1 取数：近7天各渠道订单销售额 [DONE]: 列: ['channel','sales_amount','order_cnt'] ..."
      }
    ]
  }
}
```

> 召回在任务里的用法：创建任务时 `use_memory=true` → graph 先跑 `memory_recall` 节点，
> 命中片段注入 planner 的上下文，并在轨迹里留下 `memory_recall enter/exit` 两条记录。

### 2.5 修改 / 删除

```
PATCH  /memories/{memory_id}     # 改 content / importance
DELETE /memories/{memory_id}
```

仅本人或 ADMIN 可操作；跨用户返回 403。

---

## 3. 任务与轨迹导出（v1.1 新增，缺口 G10）

两个端点都是**只读**，直接返回 JSON 文件，`Content-Disposition` 带 ASCII 文件名。

### 3.1 任务归档

```
GET /tasks/{task_id}/export
```

```bash
curl -s -OJ "http://127.0.0.1:8000/api/v1/tasks/18/export" -H "Authorization: Bearer $TOKEN"
# 落盘：task-18-export-20261022T073635Z.json
```

响应头：

```
HTTP/1.1 200 OK
content-type: application/json; charset=utf-8
content-disposition: attachment; filename="task-18-export-20261022T073635Z.json"
x-agentflow-schema: agentflow.task_export.v1
```

文件结构：

```json
{
  "schema": "agentflow.task_export.v1",
  "exported_at": "2026-10-22T07:36:35+00:00",
  "exported_by": { "user_id": 1, "username": "admin_user", "role": "admin" },
  "task": { "task_id": 18, "task_no": "AF20261022-0018", "status": "COMPLETED", "goal": "...", "token_usage": 9136 },
  "subtasks": [{ "id": 61, "seq": 1, "name": "取数", "tool_hint": "sql-query", "status": "DONE", "retry_count": 1 }],
  "review": { "scores": { "evidence": 4, "faithfulness": 4 }, "total": 4.0, "verdict": "PASS", "confidence": 0.82 },
  "eval_score": { "total": 4.0, "dims": {}, "confidence": 0.82, "verdict": "PASS" },
  "summary": "【渠道销售汇总】...",
  "approvals": { "status": "APPROVED", "approval_id": 9 },
  "artifacts": [{ "kind": "chart", "url": "/static/charts/chart_xxx.png" }],
  "trace": { "trace_id": "4b1f...", "total_steps": 68, "truncated": false, "steps": [], "interrupt": null },
  "metrics": {
    "duration_s": 35.0,
    "token_usage": 9136,
    "replan_count": 2,
    "subtask_total": 5,
    "subtask_done": 4,
    "tool_calls": 22
  }
}
```

> `trace.steps` 最多保留 2000 条，超出时 `truncated=true`（完整轨迹用 3.2 单独导出）。

### 3.2 轨迹导出

```
GET /tasks/{task_id}/trace/export
```

```json
{
  "schema": "agentflow.trace_export.v1",
  "task_id": 18,
  "trace_id": "4b1f...",
  "total_steps": 68,
  "truncated": false,
  "steps": [
    {
      "span_id": 1201,
      "parent_span_id": null,
      "node": "memory_recall",
      "event": "exit",
      "subtask_id": null,
      "started_at": "2026-10-22T06:58:11+00:00",
      "payload": { "hits": 2 }
    }
  ],
  "checkpoints": [{ "checkpoint_id": "...", "step": 3, "source": "loop" }],
  "interrupt": null
}
```

权限与 `GET /tasks/{id}` 一致：`VIEWER` 仅能导出已完成任务，`ADMIN` 仅本人任务。

---

## 4. 知识库工具自检（v1.1 新增）

```
GET /kb/status?probe=true&q=库存断货风险&top_k=3
```

```json
{
  "code": "OK",
  "data": {
    "mock": false,
    "contract": "project1_kb_query",
    "base_url": "http://127.0.0.1:8010",
    "kb_id": 1,
    "auth_mode": "service_account",
    "tenant": "agentflow",
    "breaker": { "state": "closed", "failures": 0 },
    "probe": {
      "ok": true,
      "degraded": false,
      "chunks": 3,
      "provider_latency_ms": 210,
      "total_latency_ms": 214
    },
    "samples": [{ "doc_id": "101", "title": "W41 电商经营周报 第12块", "score": 0.0163, "source": "project1:kb1:doc3" }]
  }
}
```

用途：把"项目一到底通不通"从「要跑一个任务才知道」变成一次显式探测；`probe=false` 只回配置。
权限：`ADMIN` / `APPROVER`。

检索契约说明：

| `EXTERNAL_RAG_CONTRACT` | 实际调用 | 说明 |
|---|---|---|
| `project1_kb_query`（默认） | `POST /api/v1/kbs/{kb_id}/query` | 项目一**真实**接口，取响应的 `data.sources[]` |
| `legacy_kb_search` | `POST /api/v1/kb/search` | PRD 假设 A-01 的接口，项目一暂未实现 |
| `mall_only` | — | 只用 mall 只读库兜底 |

---

## 5. Reviewer 结果字段（v1.1 变更）

`GET /tasks/{id}` 的 `review` 与 `eval_score` 新增两个服务端字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `confidence` | 0~1 | 结果可信度。`0.55×分数 + 0.45×证据覆盖率`，再乘来源系数（外部评测 1.0 / LLM 直评 0.82 / 本地降级 0.72），降级 ×0.9，跳过上限 0.4 |
| `verdict` | 枚举 | `PASS` 可交付 / `REVISE` 回 replan / `REJECT` 硬性拒绝 |
| `confidence_reason` | 字符串 | 可信度构成说明，便于排查"为什么分高但不可信" |

判定规则：

```
total < REVIEW_REJECT_THRESHOLD(2.5)              → REJECT
total < REVIEW_PASS_THRESHOLD(3.5)                → REVISE
total ≥ 3.5 但 confidence < 0.55                  → REVISE（分高但无证据）
其余                                              → PASS
```

`REVISE` / `REJECT` 且 `replan_count < REPLAN_MAX_COUNT(2)` → 走 **replan** 分支；预算用尽则降级交付，
`review.verdict` 仍保留 `REJECT`，供看板与导出文件标注。

---

## 6. SSE 事件（速查）

```
GET /tasks/{task_id}/stream?last_event_id=0
```

| 事件 | 触发 | 关键字段 |
|---|---|---|
| `state_update` | 节点进入/退出 | `node`、`status`、`seq` |
| `tool_call` | 工具调用与返回 | `tool_name`、`phase`(call/result)、`subtask_seq`、`result_digest` |
| `interrupt` | 停在 human_gate | `approval_id`、`preview` |
| `complete` | 任务终态 | `status`、`token_usage`、`artifacts` |

- 帧格式 `id: <seq>\nevent: <type>\ndata: <json>\n\n`，`seq` 单调递增；
- 断线重连带 `Last-Event-ID`（或 `?last_event_id=`）从断点续传，不会重复推已收事件；
- 空闲超时由 `SSE_IDLE_TIMEOUT_SECONDS` 控制（**全项目唯一定义处**，默认 300s）。

---

## 7. 错误码速查

| code | HTTP | 场景 |
|---|---|---|
| `OK` | 200 | 成功 |
| `FORBIDDEN` | 403 | 角色不足 / 非本人任务 / VIEWER 看未完成任务 |
| `NOT_FOUND` | 404 | 任务 / 审批单 / 记忆不存在 |
| `INTERRUPT_NOT_FOUND` | 409 | 断点不存在或已过期（checkpoint TTL 72h） |
| `MEMORY_DISABLED` | 503 | `ENABLE_LAYERED_MEMORY=false` 时调用记忆写接口 |
| `VALIDATION_ERROR` | 422 | 参数校验失败（如批量审批超过 50 条） |
| `KB_AUTH_FAILED` | — | 项目一服务账号未配置 / 令牌失效（出现在 `kb/status.probe` 与工具结果里，不抛 HTTP 错误） |
