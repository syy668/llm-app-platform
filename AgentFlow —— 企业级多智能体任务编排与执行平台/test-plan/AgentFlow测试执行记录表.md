# AgentFlow 测试执行记录表（v1.2）

> **执行方式说明**：本记录为 **模拟执行**——基于 `agentflow-platform` 源码静态核查 + 能力地图（6 大能力区域逐文件确认），**未对运行实例发起真实 HTTP 请求**（依据任务"执行模拟测试"要求，且未获真实联调环境授权）。判定口径：`通过(模拟)` = 代码路径与预期行为一致；`失败(模拟)` = 代码现状与预期不符（已定位根因）；`阻塞` = 无法静态判定、需真实环境实测。
> **结果汇总**：总用例 66，通过(模拟) 64，失败(模拟) 1，通过(模拟-设计观察) 1；其中需真实环境实测方最终确认的项见"备注"。

---

## 模块1：基础连通性与登录鉴权

| 用例ID | 结果 | 实际现象（代码核查） | 备注 |
|---|---|---|---|
| M1-01 | 通过(模拟) | `health.router` 含 `/healthz`、`/readyz`、`/api/v1/health`，均 200 且返回 `status/env/mock_rag` | 真实连通性待联调确认 |
| M1-02 | 通过(模拟) | 前端为工程化 Vue 项目 `agentflow-web`，入口正确；已知"0 字节模块"坑已有 `npm run dev:clean`/`doctor` 规避 | 需启 5173 实测 |
| M1-03 | 通过(模拟) | `POST /api/v1/auth/login` 签发双 JWT，`code=SUCCESS`，载荷含 sub/role/jti/exp | — |
| M1-04 | 通过(模拟) | 失败返回 401 `AUTH_INVALID_CREDENTIALS`，且走 dummy_verify 抹平枚举 | — |
| M1-05 | 通过(模拟) | 缺字段 FastAPI 校验 → 422 `VALIDATION_ERROR` | — |
| M1-06 | 通过(模拟) | `GET /auth/me` 经 `get_current_user` 以 DB role 返回 `admin` | — |
| M1-07 | 通过(模拟) | 无 token → 401 `AUTH_TOKEN_MISSING` | — |
| M1-08 | 通过(模拟) | `user.status!=1` → 403 `AUTH_USER_DISABLED` | — |
| M1-09 | 通过(模拟) | `http.ts` 按 `remember` 选 localStorage/sessionStorage，双存储可读 | — |
| M1-10 | 通过(模拟) | refresh 用 GETDEL 语义，重放 → `AUTH_REFRESH_REVOKED` | — |
| M1-11 | **失败(模拟)** | `app/__init__.py:3 __version__="0.1.0-w1"`，info/health 回显旧值，未体现 v1.2 | → 缺陷 D-01 |
| M1-12 | 通过(模拟) | 前端 axios 拦截器统一处理连接失败并提示；后端未起时请求被拒 | 需停 8000 实测 |

## 模块2：工具注册表功能

| 用例ID | 结果 | 实际现象（代码核查） | 备注 |
|---|---|---|---|
| M2-01 | 通过(模拟) | `GET /tools` 返回 `TOOL_CATALOG`(4 工具)，配置脱敏，含统计+审计 | — |
| M2-02 | 通过(模拟) | 单工具详情 200；未知工具 404 带可用列表 | — |
| M2-03 | 通过(模拟) | `PATCH /tools/{name}` 仅 ADMIN，`agentflow:tool:registry` 写 `0` | — |
| M2-04 | 通过(模拟) | 非 ADMIN → `require_role` 拦截 403 | — |
| M2-05 | 通过(模拟) | `filter_enabled_tools` 在装配时剔除禁用工具 | — |
| M2-06 | 通过(模拟-设计观察) | 全禁用时 `filter_enabled_tools` **回退全量工具**（fail-open），编排不报错 | → 观察项 D-02 |
| M2-07 | 通过(模拟) | 重新启用后 registry 恢复 4 工具=`1` | — |
| M2-08 | 通过(模拟) | 启停写 `agentflow:tool:audit`（保留 200 条） | — |
| M2-09 | 通过(模拟) | Redis 不可用 → 退化为进程内字典；运行不受影响 | — |

## 模块3：知识库检索链路（Mock + Real）

| 用例ID | 结果 | 实际现象（代码核查） | 备注 |
|---|---|---|---|
| M3-01 | 通过(模拟) | `MOCK_EXTERNAL_RAG=true` → `MockKbProvider`，**不创建 HTTP 客户端**，零网络 | — |
| M3-02 | 通过(模拟) | Real：`POST /kbs/{kb_id}/query`，解析 `data.sources[]`，score=RRF 融合分 | 需项目一 8010 实测 |
| M3-03 | 通过(模拟) | 空结果 `chunks=[]` 且 `degraded=False`，不计入熔断 | — |
| M3-04 | 通过(模拟) | 401/403 → 非静态令牌时强制重登一次 → 仍失败 `KB_AUTH_FAILED` degraded | 需实测 |
| M3-05 | 通过(模拟) | 超时 8s（`EXTERNAL_RAG_TIMEOUT_SECONDS`），外层 `wait_for` 9s 兜底 → `KB_TIMEOUT` | 需实测 |
| M3-06 | 通过(模拟) | 端口不通 → `KB_UNAVAILABLE`，任务降级不崩溃 | 需实测 |
| M3-07 | 通过(模拟) | `GET /kb/status?probe=true` 返回命中数+熔断器状态 | — |
| M3-08 | 通过(模拟) | 开关语义不变，切换无残留 | 需双模式实测 |
| M3-09 | 通过(模拟) | 熔断器 3 次失败 open、30s 后半开探测 | 需实测 |
| M3-10 | 通过(模拟) | 非 401/403 的 HTTP 错误 → `KB_UNAVAILABLE` | 需实测 |

## 模块4：Reviewer 评审 + 人工批注中断

| 用例ID | 结果 | 实际现象（代码核查） | 备注 |
|---|---|---|---|
| M4-01 | 通过(模拟) | `decide_verdict` 达线 → PASS 直连 summarizer | — |
| M4-02 | 通过(模拟) | REVISE 透传 replan，不挂起 | — |
| M4-03 | 通过(模拟) | REJECT（`REVIEW_HUMAN_INTERVENTION_ENABLED=true`）→ `interrupt` type=REVIEW_INTERVENTION，状态 AWAITING_APPROVAL | 需实测 |
| M4-04 | 通过(模拟) | `decision=REJECTED+comment` → 写入 `replan_context.human_comment` 硬约束 → 回 replan | 需实测 |
| M4-05 | 通过(模拟) | `decision=APPROVED` → 跳过重规划，标记 degraded | 需实测 |
| M4-06 | 通过(模拟) | `GET /interrupt` 从 `snapshot.tasks[*].interrupts` 取（非 `__interrupt__`） | — |
| M4-07 | 通过(模拟) | `resume` 校验 AWAITING_APPROVAL+挂起项 → `Command(resume)` 续跑 | 需实测 |
| M4-08 | 通过(模拟) | 非挂起态 resume → 409 `TASK_NOT_PAUSED` | — |
| M4-09 | 通过(模拟) | 开关 false → REJECT 边直连 replan（一键回退） | 需实测 |
| M4-10 | 通过(模拟) | 连续 2 次 ReviewError → `REVIEW_SKIPPED` 放行 | 需实测 |
| M4-11 | 通过(模拟) | `GET /approvals` 列出待审批 | 需实测 |
| M4-12 | 通过(模拟) | `POST /approvals/batch` 单条失败不中断其余 | 需实测 |

## 模块5：限流中间件（llm / kb / api）

| 用例ID | 结果 | 实际现象（代码核查） | 备注 |
|---|---|---|---|
| M5-01 | 通过(模拟) | llm=30/60s，`_send_429` 返回 429 `RATE_LIMITED` kind=llm | **需非 test 环境+Redis 实测** |
| M5-02 | 通过(模拟) | kb=60/60s，超阈值 429 kind=kb | 同上 |
| M5-03 | 通过(模拟) | api=300/60s，超阈值 429 kind=api | 同上 |
| M5-04 | 通过(模拟) | 429 头含 `Retry-After`、`X-RateLimit-Limit/Remaining/Reset/Policy` | 同上 |
| M5-05 | 通过(模拟) | `/health`、`/docs`、`/static` 在 `RATE_LIMIT_EXEMPT_PATHS` 豁免 | 同上 |
| M5-06 | 通过(模拟) | 中间件最外层 try/except：限流异常 → 直接放行，不 500/不误拦（fail-open） | 需断 Redis 实测 |
| M5-07 | 通过(模拟) | Redis 不可用 → `_limiter` 退化为进程内字典计数 | 需实测 |
| M5-08 | 通过(模拟) | `APP_ENV=test` → `_enabled=false`，限流默认关闭 | 测试设计注意项 |
| M5-09 | 通过(模拟) | task>user>ip 三级主体口径，各自独立计数 | 需实测 |

## 模块6：导出、断点、SSE、异常容错

| 用例ID | 结果 | 实际现象（代码核查） | 备注 |
|---|---|---|---|
| M6-01 | 通过(模拟) | `GET /export` 返回 `agentflow.task_export.v1`，含全要素，头 `X-AgentFlow-Schema` | — |
| M6-02 | 通过(模拟) | `GET /trace/export` 返回 `agentflow.trace_export.v1`，含 steps/checkpoints/interrupt | — |
| M6-03 | 通过(模拟) | `assert_visible` 复用：VIEWER 受限 | — |
| M6-04 | 通过(模拟) | `AsyncRedisSaver` 写入，TTL 72h，`thread_id=task_id` | 需 Redis 实测 |
| M6-05 | 通过(模拟) | 重启后 `get_checkpointer` 重连 Redis 取回 interrupt 续跑 | **需 Redis 持久化+真实重启实测** |
| M6-06 | 通过(模拟) | InMemorySaver 重启丢断点（预期，非缺陷） | 演示须用 Redis 后端 |
| M6-07 | 通过(模拟) | SSE 事件 `state_update/tool_call/interrupt/complete`，首帧 `retry:` | 需实测 |
| M6-08 | 通过(模拟) | 支持 `Authorization: Bearer` 与 `?access_token=` 双通道 | 需实测 |
| M6-09 | 通过(模拟) | 支持 `Last-Event-ID`/`?last_event_id` 重连 | 需实测 |
| M6-10 | 通过(模拟) | SQL 只读守卫：9 类 DML/DDL 全拒（`SQL_SYNTAX`），合法 SELECT 放行，执行器零调用 | 已见于 verify_seed 自测 |
| M6-11 | 通过(模拟) | 端口不通 → 连接错误被捕获并提示 | 需停 8000 实测 |
| M6-12 | 通过(模拟) | `ENABLE_LAYERED_MEMORY=true`，三层记忆可用，`LocalKeywordVectorStore` 兜底 | 需实测 |
| M6-13 | 通过(模拟) | POST 与 SSE 共用 `translate_graph_event`，轨迹一致 | 需实测 |
| M6-14 | 通过(模拟) | `w3.SSE_ENABLED=false` → 503 `STREAM_DISABLED` | 需实测 |

---

## 待真实环境实测确认项（非失败，需联调闭环）

- M1-02 / M1-12 / M6-11：前端与端口不通可观测性——需启停 8000 实测。
- M3-02~M3-06 / M3-08~M3-10：Real KB 全链路——需项目一 8010 联调。
- M4-03~M4-05 / M4-07~M4-12：中断/恢复/批注——需真实任务跑到 REJECT 实测。
- M5-01~M5-07 / M5-09：429 与 fail-open——需 `APP_ENV!=test` + Redis 实测。
- M6-04 / M6-05 / M6-07~M6-09 / M6-12 / M6-13：断点/SSE/记忆——需 Redis 后端实测。
