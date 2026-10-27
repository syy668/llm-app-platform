# API 参考 · v1.2 增补

> 面向 v1.2 新增的三块能力：**工具注册表**、**全局限流**、**评审 REJECT 人工批注**。
> 基础接口（鉴权 / 任务 / 审批 / 记忆 / 导出 / 知识库自检）见 [`API参考-v1.1.md`](API参考-v1.1.md)。
> 所有示例均为**真实响应**（本机 3307/6379/8000/8010 全链路跑出，敏感字段已替换）。

统一约定（与 v1.0 一致）：
- 成功判据是 `code === "OK"`，不是 HTTP 200；
- 响应信封 `{code, message, data, trace_id}`；
- 鉴权 `Authorization: Bearer <access_token>`；SSE 额外支持 `?access_token=`。

---

## 1. 工具注册表

### 1.1 `GET /api/v1/tools` — 工具目录

权限：**ADMIN / APPROVER**（VIEWER 403）。

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/api/v1/tools | python -m json.tool
```

```json
{
  "code": "OK",
  "message": "ok",
  "data": {
    "items": [
      {
        "name": "kb-search",
        "display_name": "知识库检索",
        "server": "kb-search",
        "category": "retrieval",
        "description": "检索企业知识库（历史电商经营周报），返回最相关的 1~5 条片段与得分。",
        "capability": "把用户目标里的症状词（断货 / 转化率 / 预算）转成检索语句……失败按「熔断 → mall 只读库」顺序兜底。",
        "risk_level": "low",
        "external": true,
        "input_schema": {
          "type": "object",
          "required": ["query"],
          "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 200, "description": "检索语句"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
            "tenant": {"type": "string", "maxLength": 64, "default": "agentflow"}
          }
        },
        "output_schema": {
          "chunks": "片段数组：doc_id / title / content(≤300字) / score / source",
          "latency_ms": "工具内耗时（毫秒）",
          "degraded": "是否降级（true 表示未拿到真实证据）",
          "error_code": "降级原因：KB_TIMEOUT / KB_UNAVAILABLE / KB_AUTH_FAILED"
        },
        "config_hints": [
          {"key": "MOCK_EXTERNAL_RAG", "value": "false", "purpose": "知识库 Mock 开关：true=内置周报语料，false=项目一真实检索", "sensitive": false},
          {"key": "EXTERNAL_RAG_BASE_URL", "value": "http://127.0.0.1:8010", "purpose": "项目一后端根地址（演示环境为 8010）", "sensitive": false}
        ],
        "notes": ["禁用后 Planner 不再规划 kb 检索，评审的「引用忠实度」维度会按无引用扣分"],
        "enabled": true,
        "updated_at": "2026-10-22T08:18:37+00:00",
        "updated_by": "admin_user"
      }
    ],
    "total": 4,
    "enabled": 4,
    "disabled": 0,
    "generated_at": "2026-10-22T08:25:10+00:00",
    "audit": [
      {"at": "2026-10-22T08:18:37+00:00", "tool": "kb-search", "enabled": false, "operator": "admin_user", "reason": "演示验证：临时下线知识库"}
    ]
  }
}
```

字段说明：

| 字段 | 含义 |
|---|---|
| `category` | `retrieval` / `database` / `visualization` / `notification` |
| `risk_level` | `low` / `medium` / `high`（`high` 会触发 human_gate 人工审批） |
| `external` | 是否依赖外部服务（当前仅 `kb-search` → 项目一） |
| `input_schema` | JSON Schema 子集（前端表格直接渲染，不做二次解析） |
| `output_schema` | 「字段名 → 说明」映射 |
| `config_hints` | 生效配置（密钥类一律脱敏，只暴露"是否已配置"） |
| `audit` | 最近 20 条变更（LPUSH 保留 200 条） |

### 1.2 `GET /api/v1/tools/{name}` — 单工具详情

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/tools/sql-query
```

未知工具返回 404：

```json
{"code": "NOT_FOUND", "message": "未注册的工具：not-exist",
 "data": {"detail": {"available": ["chart-render", "kb-search", "notify", "sql-query"]}},
 "trace_id": "..."}
```

### 1.3 `PATCH /api/v1/tools/{name}` — 启用 / 禁用

权限：**仅 ADMIN**（APPROVER 403）。

```bash
curl -s -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"enabled": false, "reason": "演示：临时下线知识库"}' \
  http://127.0.0.1:8000/api/v1/tools/kb-search
```

```json
{"code": "OK", "message": "工具 kb-search 已禁用（下一次新建任务生效）",
 "data": {"name": "kb-search", "enabled": false, "updated_by": "admin_user",
          "updated_at": "2026-10-22T08:18:37+00:00", "...": "..."},
 "trace_id": "..."}
```

**生效范围（重要）**：

- 只影响**后续新建任务**：被禁用的工具不会出现在 Planner 的 `tools` 列表里；
- **不影响已挂起/运行中任务**的断点续跑（MCP 连接池不摘除工具进程）；
- 全部禁用时后端**退回全量**，避免任务必然产出空计划（安全兜底）。

---

## 2. 全局限流

### 2.1 配额与分类

| 配额 | 触发条件 | 默认额度（每 60s） | 可调变量 |
|---|---|---:|---|
| `llm` | `POST /tasks`、`POST /tasks/stream`、`POST /tasks/{id}/resume`、`POST /approvals/batch` | 30 | `RATE_LIMIT_LLM_PER_MINUTE` |
| `kb` | `/api/v1/kb/*`（知识库自检等） | 60 | `RATE_LIMIT_KB_PER_MINUTE` |
| `api` | 其余所有 `/api/*`（兜底总闸，与上面同时计数） | 300 | `RATE_LIMIT_API_PER_MINUTE` |
| `task` | 路径中带 `/tasks/{id}` 的请求（按任务维度） | 240 | `RATE_LIMIT_TASK_PER_MINUTE` |

维度：优先按**用户**（从令牌解出 `user_id`），`task` 配额按**任务**；未登录/令牌无效时按 IP
（`RATE_LIMIT_PER_IP_ENABLED=true`）。健康检查 / 文档 / 静态资源免限流。

### 2.2 正常响应头

```http
HTTP/1.1 200 OK
X-RateLimit-Limit: 2
X-RateLimit-Remaining: 1
X-RateLimit-Reset: 19
X-RateLimit-Policy: kb:user
```

### 2.3 超限响应（HTTP 429）

```bash
# 低配额实例（RATE_LIMIT_KB_PER_MINUTE=2）第 3 次请求
curl -si -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8021/api/v1/kb/status?probe=false"
```

```http
HTTP/1.1 429 Too Many Requests
retry-after: 18
x-ratelimit-limit: 2
x-ratelimit-remaining: 0
x-ratelimit-reset: 18
x-ratelimit-policy: kb:user
```

```json
{
  "code": "RATE_LIMITED",
  "message": "请求过于频繁（kb 配额 2/60s），请 18s 后重试",
  "data": {
    "detail": {
      "kind": "kb",
      "scope": "user",
      "limit": 2,
      "used": 3,
      "window_seconds": 60,
      "retry_after": 18
    }
  },
  "trace_id": "3340f5df9e2643dd936ee2e5de831e15"
}
```

### 2.4 日志埋点

```json
{"level":"WARNING","msg":"rate_limit_exceeded","kind":"llm","scope":"user","subject":"u:3",
 "used":2,"limit":1,"window":60,"retry_after":8,"trace_id":"..."}
{"level":"WARNING","msg":"rate_limit_rejected","path":"/api/v1/approvals/batch","method":"POST",
 "kind":"llm","subject":"u:3","trace_id":"..."}
```

排障建议：`subject` 直接告诉你"是谁打满了配额"（`u:<id>` / `task:<id>` / `ip:<addr>`）。

---

## 3. 评审 REJECT 人工批注

### 3.1 挂起形态

当 Reviewer 判定 `verdict=REJECT` 且仍有重规划预算时，任务停在 `review_gate`：

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/tasks/23/interrupt
```

```json
{
  "code": "OK",
  "data": {
    "paused": true,
    "task_id": 23,
    "status": "AWAITING_APPROVAL",
    "approval_id": null,
    "type": "REVIEW_INTERVENTION",
    "reason": "评审判定 REJECT（总分 3.8/5，可信度 49%），请人工批注后重新规划",
    "preview": {
      "verdict": "REJECT",
      "total": 3.8,
      "confidence": 0.49,
      "confidence_reason": "评分 3.80/5（权重0.55）× 证据覆盖 56%（权重0.45） × 来源系数 0.82；本地降级 ×0.9",
      "scores": {"evidence": 3, "faithfulness": 4, "consistency": 3, "completeness": 4, "format": 5},
      "suggestions": [{"seq": 1, "advice": "补全未完成子任务后重新汇总"}],
      "replan_count": 0,
      "subtask_count": 10,
      "report_excerpt": "- #1 取数：近两周数码配件品类 GMV 与订单 [FAILED] …"
    },
    "next_node": ["review_gate"],
    "node": "review_gate",
    "resume_endpoint": "/api/v1/tasks/23/resume"
  }
}
```

> 与高危审批的区别：`type` 是 `REVIEW_INTERVENTION` 而不是 `HIGH_RISK_TOOL`，且 **`approval_id=null`**
> （这条断点不落 approvals 表）。前端据此渲染"批注卡片"而不是审批抽屉。

### 3.2 提交批注并重规划

复用审批恢复接口，语义由 `decision` 决定：

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"decision":"REJECTED","comment":"请改用 skus 表直接取库存周转与库存量，按品类汇总；把断货 SKU 单独列出","sync":false}' \
  http://127.0.0.1:8000/api/v1/tasks/23/resume
```

```json
{"code":"OK","data":{"task_id":23,"resumed":true,"status":"RUNNING","approval_id":null,
 "decision":"REJECTED","next_nodes":[],"sync":false}}
```

| `decision` | 行为 |
|---|---|
| `REJECTED` + `comment` | 批注写入 `replan_context.human_comment`，作为 **Planner 重规划的硬约束**（优先级高于评审建议） |
| `APPROVED` | 人工放行：跳过重规划，直接汇总并按降级交付（`degraded=true`） |

### 3.3 效果验证（真实任务 task#23）

批注内容要求"改用 skus 表、单列断货 SKU"，重规划后的计划逐字响应：

```
#1 取数：skus 表按品类汇总库存周转与库存量        sql_query
#2 取数：断货 SKU 明细清单                        sql_query
#3 取数：近两周数码配件流量与投放对比              sql_query
```

最终人工放行后任务 `COMPLETED`，详情里保留干预记录：

```json
{"status":"COMPLETED","replan_count":1,
 "review":{"verdict":"REJECT","total":3.8,"confidence":0.49},
 "review_intervention":{"status":"APPROVED","decision":"APPROVED",
   "comment":"数据源缺列，人工确认按现有结论降级交付","decided_by":1,"verdict":"REJECT"}}
```

### 3.4 开关

| 变量 | 默认 | 说明 |
|---|---|---|
| `REVIEW_HUMAN_INTERVENTION_ENABLED` | `true` | `false` 时 `reviewer` 的 replan 出边直连 `replan`，回到 v1.1 行为 |

---

## 4. 错误码速查（v1.2 新增部分）

| code | HTTP | 场景 |
|---|---:|---|
| `RATE_LIMITED` | 429 | 超出 `api`/`llm`/`kb`/`task` 任一配额 |
| `NOT_FOUND` | 404 | `/tools/{name}` 未注册 |
| `FORBIDDEN` | 403 | VIEWER 读工具目录、APPROVER 切开关 |
| `TASK_NOT_PAUSED` | 409 | 对未挂起的任务（含已恢复的 review_gate）调用 resume |
