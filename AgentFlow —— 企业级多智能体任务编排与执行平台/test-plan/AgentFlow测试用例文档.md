# AgentFlow 多智能体编排平台 — 测试用例文档（v1.2 交付范围）

> 测试对象：AgentFlow 企业级多智能体任务编排与执行平台
> 版本基线：`v1.2.0-wip`（CHANGELOG）；后端 `agentflow-platform`，端口 8000；前端 `agentflow-web`，端口 5173；外部依赖项目一知识库 8010
> 测试账号：`admin_user` / `Passw0rd!`（role=admin）
> 红线（仅验证行为，不改代码）：断点持久化 / 分层记忆 / SSE 流式 / 知识库适配器 / 基础 Reviewer 判定逻辑
> 用例格式：`用例ID | 测试标题 | 前置条件 | 操作步骤 | 预期结果 | 严重等级`

---

## 模块1：基础连通性与登录鉴权测试

| 用例ID | 测试标题 | 前置条件 | 操作步骤 | 预期结果 | 严重等级 |
|---|---|---|---|---|---|
| M1-01 | 后端健康检查连通性 | 后端 8000 已启动 | 依次 `GET /healthz`、`GET /readyz`、`GET /api/v1/health` | 三者均返回 200，body 含 `status=ok`、当前 `env`、`mock_rag` 开关；`/api/v1/health` 返回服务版本信息 | P0 |
| M1-02 | 前端静态资源可访问 | 前端 5173 已启动 | 浏览器访问 `http://127.0.0.1:5173/` | 返回登录页 HTML（200），无 404/空白页；控制台无 `does not provide an export` 类 0 字节模块报错 | P1 |
| M1-03 | 正常登录获取双 token | 后端 8000 已启动，账号存在 | `POST /api/v1/auth/login` body=`{username:"admin_user",password:"Passw0rd!",remember:false}` | 返回 200，body 信封 `code=SUCCESS`，`data` 含 `access_token` 与 `refresh_token`；两 token 均为 JWT（含 `sub/username/role/typ/jti/exp`） | P0 |
| M1-04 | 错误密码返回 401 | 后端 8000 已启动 | `POST /api/v1/auth/login` body 密码错误 | 返回 401，body `code=AUTH_INVALID_CREDENTIALS`、`message=用户名或密码错误`；响应时间无明显用户名枚举差异（走 dummy_verify 抹平） | P0 |
| M1-05 | 缺少字段返回 422 | 后端 8000 已启动 | `POST /api/v1/auth/login` body=`{username:"admin_user"}`（缺 password） | 返回 422，body `code=VALIDATION_ERROR`，errors 指向缺失字段 | P1 |
| M1-06 | 登录后获取角色信息 | M1-03 已登录拿到 access_token | `GET /api/v1/auth/me` 带 `Authorization: Bearer <access_token>` | 返回 200，`data.role="admin"`、`data.username="admin_user"`；role 以 DB 为准 | P0 |
| M1-07 | 无 token 访问受保护接口 | 后端 8000 已启动 | `GET /api/v1/auth/me`（不带 Authorization） | 返回 401，body `code=AUTH_TOKEN_MISSING` | P0 |
| M1-08 | 禁用用户登录返回 403 | 存在 `status!=1` 的测试账号 | 用该账号 `POST /api/v1/auth/login` | 返回 403，body `code=AUTH_USER_DISABLED` | P1 |
| M1-09 | token 双存储策略 | 登录页可操作 | 勾选"保持登录"登录 vs 不勾选登录，分别检查浏览器存储 | 勾选 → `af_access_token` 存于 `localStorage`；不勾选 → 存于 `sessionStorage`；`getAccessToken` 两种存储均可读取 | P2 |
| M1-10 | refresh token 一次性消费 | M1-03 已拿到 refresh_token | 用 refresh_token 调用刷新接口成功后，立即用同一 refresh_token 再次刷新 | 首次成功；重放返回 403/拒绝，body `code=AUTH_REFRESH_REVOKED` | P1 |
| M1-11 | 版本号对齐 v1.2 | 后端 8000 已启动 | `GET /api/v1/health` 或 info 端点查看 `version` 字段 | 版本号应体现 `v1.2.x`；若回显 `0.1.0-w1` 则视为**未对齐交付范围**（见缺陷 D-01） | P2 |
| M1-12 | 后端端口不通的前端可观测性 | 停止后端 8000 | 前端 5173 页面发起登录 | 前端给出明确错误提示（连接失败/网络错误），不出现白屏或卡死；错误可被用户理解 | P1 |

---

## 模块2：工具注册表功能测试

> 工具目录固定 4 个：`kb-search / sql-query / chart-render / notify`；状态存 Redis `agentflow:tool:registry`（`1`=启用/`0`=禁用）；`PATCH` 仅 ADMIN 可改。

| 用例ID | 测试标题 | 前置条件 | 操作步骤 | 预期结果 | 严重等级 |
|---|---|---|---|---|---|
| M2-01 | 工具目录正常返回 | admin 已登录 | `GET /api/v1/tools` | 返回 200，列出 4 个工具元信息，配置项脱敏（`config_keys` 可见但密钥值不回显），含启用/禁用统计与最近审计 | P1 |
| M2-02 | 单工具详情与未知工具 | admin 已登录 | `GET /api/v1/tools/kb-search`；再 `GET /api/v1/tools/not-exist` | 前者返回 200 与详情；后者返回 404，body 含可用工具列表 | P2 |
| M2-03 | ADMIN 禁用单个工具 | admin 已登录 | `PATCH /api/v1/tools/sql-query` body=`{enabled:false}` | 返回 200，返回变更后元信息；Redis `agentflow:tool:registry` 中 `sql-query=0` | P1 |
| M2-04 | 非 ADMIN 禁用被拒 | approver/viewer 账号已登录 | 用该账号 `PATCH /api/v1/tools/kb-search` body=`{enabled:false}` | 返回 403（require_role(ADMIN) 拦截），工具状态不变 | P1 |
| M2-05 | 禁用后编排不可见该工具 | M2-03 已禁用 sql-query | 创建并执行任务，观察 Planner 工具列表 / 轨迹 | `filter_enabled_tools` 生效，被禁用工具不进入模型工具列表，规划阶段不会调用该工具 | P1 |
| M2-06 | 边界：禁用全部工具 | admin 已登录，4 工具逐个禁用 | 依次 `PATCH` 禁用全部 4 个工具后创建任务 | **实际行为（fail-open）**：编排不报错，回退返回全量工具继续运行（见观察项 D-02）；不出现空计划崩溃 | P2 |
| M2-07 | 恢复启用工具 | M2-06 后 | `PATCH` 将全部工具重新 `enabled:true` | Redis `tool:registry` 恢复 4 工具均为 `1`；后续任务工具列表恢复完整 | P2 |
| M2-08 | 启停审计日志 | 完成若干 M2-03/M2-07 操作 | `GET /api/v1/tools` 查看审计段 | 启停操作写入 `agentflow:tool:audit`（保留最近 200 条），含操作人/时间/动作 | P2 |
| M2-09 | 异常：Redis 不可用降级 | 临时断开 Redis | 执行启停操作并创建任务 | 工具状态退化为进程内字典，不影响运行；Redis 恢复后状态以存储为准（注明：禁用不会热摘除 MCP 进程） | P2 |

---

## 模块3：知识库检索链路测试（Mock + Real）

> Mock 开关：`MOCK_EXTERNAL_RAG`（默认 `true` → `MockKbProvider`，零网络）。Real：`false` → `POST {EXTERNAL_RAG_BASE_URL}/api/v1/kbs/{kb_id}/query`（项目一 8010），超时 8s（外层 9s），401 强制重登一次，空结果 `degraded=False` 不计数熔断。

| 用例ID | 测试标题 | 前置条件 | 操作步骤 | 预期结果 | 严重等级 |
|---|---|---|---|---|---|
| M3-01 | Mock 模式正常检索 | `MOCK_EXTERNAL_RAG=true`，后端 8000 启动 | 执行触发 KB 检索的任务 | 返回非空 `chunks`，全程**零外部网络请求**；响应标记 mock 来源 | P0 |
| M3-02 | Real 模式正常检索 | `MOCK_EXTERNAL_RAG=false`，项目一 8010 启动且 KB 有数据，凭据正确 | 执行触发 KB 检索的任务 | 返回 `data.sources[]`（含 `chunk_id/document_title/content/score`），score 为 RRF 融合分（0.01~0.03 量级，非 0~1 相关度） | P0 |
| M3-03 | 空结果处理 | Real 模式，查询无命中文档 | 执行检索 | 返回 `chunks=[]` 且 `degraded=False`；任务不因"库中空"而熔断或失败 | P1 |
| M3-04 | 401 鉴权失败 | Real 模式，项目一返回 401 且重登后仍 401 | 执行检索 | 首次 401 → 强制重登一次重试；仍失败 → `KB_AUTH_FAILED`、`degraded=True`；任务降级而非崩溃 | P1 |
| M3-05 | 检索超时 | Real 模式，项目一响应 >8s | 执行检索 | 触发 `KB_TIMEOUT`（外层 9s 兜底）；任务降级给出可观测错误，不无限挂起 | P1 |
| M3-06 | 服务端口不通 | 停止项目一 8010 | 执行检索 | 返回 `KB_UNAVAILABLE`；任务降级继续（不抛出未捕获异常导致 500） | P1 |
| M3-07 | KB 状态自检 | admin 已登录 | `GET /api/v1/kb/status?probe=true` | 返回 200，含真实命中条数与熔断器状态（closed/open/half_open） | P2 |
| M3-08 | Mock/Real 兼容性切换 | 分别置 `true`/`false` 并重启后端 | 各执行一次检索任务，对比轨迹 | 两种模式均可独立产出结果，切换无残留状态污染；`mock_rag` 开关在 info 端点正确反映 | P1 |
| M3-09 | 熔断器行为 | Real 模式，连续制造 3 次失败 | 第 1~3 次失败后观察；等待 30s 后再试 | 连续 3 次失败 → 熔断器 open；30s 后放行一次 half_open 探测；空结果不计入失败 | P2 |
| M3-10 | 其他 HTTP 错误 | Real 模式，项目一返回 500 | 执行检索 | 返回 `KB_UNAVAILABLE`（`degraded=True`），任务降级 | P2 |

---

## 模块4：Reviewer 评审 + 人工批注中断测试

> 三态判定（`decide_verdict`）：总分 <2.5 → REJECT；未达通过线 3.5 或 confidence<0.55 → REVISE；否则 PASS。`REVIEW_HUMAN_INTERVENTION_ENABLED=true` 时 REJECT 才 `interrupt`。恢复端点：`GET /tasks/{id}/interrupt`、`POST /tasks/{id}/resume`。

| 用例ID | 测试标题 | 前置条件 | 操作步骤 | 预期结果 | 严重等级 |
|---|---|---|---|---|---|
| M4-01 | PASS 分支正向 | 构造高质交付物 | 执行任务至 Reviewer | 评分 ≥3.5 且 confidence ≥0.55 → 直接路由 summarizer，无中断；轨迹出现 `verdict=PASS` | P0 |
| M4-02 | REVISE 分支 | 构造中质交付物（未达线/置信不足） | 执行任务至 Reviewer | 判定 REVISE → 直接透传 replan（**不挂起**），轨迹 `verdict=REVISE` | P1 |
| M4-03 | REJECT 触发人工中断 | 构造低质交付物（总分<2.5） | 执行任务至 Reviewer | 判定 REJECT → 触发 `REVIEW_INTERVENTION` 类型 interrupt；任务状态置 `AWAITING_APPROVAL` | P0 |
| M4-04 | 人工批注注入重规划 | M4-03 已挂起 | `GET /tasks/{id}/interrupt` 取 ctx；`POST /tasks/{id}/resume` body=`{decision:"REJECTED",comment:"请补充数据源X"}` | 批注写入 `replan_context.human_comment` 作为**硬约束**；任务回 replan 并按批注重规划；轨迹可见 human_comment | P0 |
| M4-05 | 人工批准跳过修订 | M4-03 已挂起 | `POST /tasks/{id}/resume` body=`{decision:"APPROVED"}` | 跳过重规划直接 summarizer，结果标记 `degraded=True`；状态转为完成 | P1 |
| M4-06 | 中断上下文可查 | M4-03 已挂起 | `GET /api/v1/tasks/{id}/interrupt` | 返回挂起项，数据取自 checkpoint `snapshot.tasks[*].interrupts`（**非** `__interrupt__` 字段）；含 payload type=REVIEW_INTERVENTION | P0 |
| M4-07 | 恢复续跑 | M4-04/M4-05 已提交 resume | 观察任务状态 | `resume` 校验 DB=`AWAITING_APPROVAL` 且有挂起项后 `_drive(Command(resume=payload))` 续跑；最终到达完成态 | P0 |
| M4-08 | 未挂起任务 resume 拦截 | 取一个运行/完成态任务 | `POST /tasks/{id}/resume` | 返回 409，body `code=TASK_NOT_PAUSED` | P1 |
| M4-09 | 干预开关关闭回退 | `REVIEW_HUMAN_INTERVENTION_ENABLED=false` | 构造 REJECT 场景执行 | REJECT 边直连 replan（不挂起），与 v1.2 前行为等价（一键回退校验） | P2 |
| M4-10 | 评审连续失败跳过 | 连续制造 2 次 ReviewError | 执行至 Reviewer | 连续 2 次错误 → `REVIEW_SKIPPED` 放行（不阻断主流程） | P2 |
| M4-11 | 审批收件箱 | admin/approver 已登录，存在挂起任务 | `GET /api/v1/approvals` | 返回待审批列表，含 M4-03 挂起任务 | P1 |
| M4-12 | 批量审批隔离 | 存在多条挂起任务 | `POST /api/v1/approvals/batch` 含一条非法 id | 合法项正常 resume；非法/失败项不影响其余（单条失败不中断批量） | P2 |

---

## 模块5：限流中间件测试（llm / kb / api 三类配额）

> 配额（窗口 60s）：`api=300`、`llm=30`、`kb=60`、`task=240`。429 响应头：`Retry-After` + `X-RateLimit-Limit/Remaining/Reset/Policy`。`APP_ENV=test` 默认关闭限流；Redis 不可用时退化为进程内本地限流（非完全放开）；中间件整体异常 fail-open。

| 用例ID | 测试标题 | 前置条件 | 操作步骤 | 预期结果 | 严重等级 |
|---|---|---|---|---|---|
| M5-01 | llm 配额触发 429 | `APP_ENV!=test` 且限流开启、Redis 可用 | 60s 内高频 `POST /api/v1/tasks` >30 次 | 超出后返回 429，`body.code=RATE_LIMITED`，`data.detail.kind="llm"` | P0 |
| M5-02 | kb 配额触发 429 | 同上 | 60s 内高频 `GET /api/v1/kb/status` 或 KB 检索类 >60 次 | 超出后返回 429，`kind="kb"` | P1 |
| M5-03 | api 配额触发 429 | 同上 | 60s 内高频普通 API（如 `/auth/me`）>300 次 | 超出后返回 429，`kind="api"` | P1 |
| M5-04 | 429 响应头完整 | M5-01 触发时抓响应头 | 检查响应头 | 含 `Retry-After`（秒）、`X-RateLimit-Limit`、`X-RateLimit-Remaining`、`X-RateLimit-Reset`、`X-RateLimit-Policy` | P1 |
| M5-05 | 豁免路径不限流 | 限流开启 | 高频 `GET /healthz`、`/docs`、`/static` | 这些路径不被计入限流，不因高频返回 429 | P2 |
| M5-06 | Redis 故障 fail-open | 限流开启，主动断开 Redis | 高频请求 llm/api 类接口 | 限流中间件自身异常 → **直接放行**，请求既不 500 也不被误拦；业务正常返回 | P0 |
| M5-07 | 限流本地降级 | Redis 不可用 | 高频 llm 请求 | 退化为进程内字典计数（多副本各算各）；单进程内仍可在超阈值时 429 | P2 |
| M5-08 | 测试环境默认关闭 | `APP_ENV=test` | 高频请求 | 限流不生效（避免用例互相污染）；**真实 429 必须在非 test 环境观测**（测试设计注意项） | P2 |
| M5-09 | 用户/任务级区分计数 | 限流开启，两不同 task id | 分别高频请求各自 task 路径 | 不同 task id / 不同 user 各自独立计数，互不影响配额 | P2 |

---

## 模块6：导出功能、断点持久化、SSE 流式、异常容错测试

> 导出：`GET /tasks/{id}/export`（`agentflow.task_export.v1`）、`GET /tasks/{id}/trace/export`（`agentflow.trace_export.v1`）。断点：`AsyncRedisSaver` TTL 72h，thread_id=task_id；重启恢复依赖 Redis 持久化。SSE：`GET /tasks/{id}/stream`，事件 `state_update/tool_call/interrupt/complete`，支持 `?access_token=` 与 `Last-Event-ID` 重连。SQL 只读：DML/DDL 被拒（`SQL_SYNTAX`）。

| 用例ID | 测试标题 | 前置条件 | 操作步骤 | 预期结果 | 严重等级 |
|---|---|---|---|---|---|
| M6-01 | 任务导出 | admin 已登录，存在完成任务 | `GET /api/v1/tasks/{id}/export` | 返回 200，JSON 含 `schema=agentflow.task_export.v1`，含基本信息/子任务/评审/审批/轨迹（截断 ≤2000 步）；响应头 `X-AgentFlow-Schema` | P1 |
| M6-02 | 轨迹导出 | 同上 | `GET /api/v1/tasks/{id}/trace/export` | 返回 200，JSON `schema=agentflow.trace_export.v1`，含 `steps/checkpoints/interrupt` | P1 |
| M6-03 | 导出权限控制 | viewer 账号已登录 | viewer 访问他人未完成任务导出 | 被 `assert_visible` 拒绝（仅看已完成/VIEWER 受限），返回 403/404 | P2 |
| M6-04 | 断点持久化 | Redis 可用，任务执行中 | 任务运行中查询 Redis `agentflow:checkpoint:*` | 快照写入 Redis（`AsyncRedisSaver`），TTL≈72h；`thread_id=task_id` | P0 |
| M6-05 | 重启恢复断点 | M4-03 任务挂起（AWAITING_APPROVAL），Redis 有持久化 | **关闭后端 8000 → 重新启动 → `resume` 续跑** | 重启后 `get_checkpointer` 重连 Redis，取回 interrupt 并成功续跑至完成（验证断点不丢） | P0 |
| M6-06 | InMemory 模式重启丢失 | `CHECKPOINTER_BACKEND=memory`/Redis 不可用 | 挂起后重启后端 | 断点不可恢复（预期行为，非缺陷）；需注明演示须用 Redis 后端 | P2 |
| M6-07 | SSE 流式输出 | 任务执行中 | `GET /api/v1/tasks/{id}/stream`（Bearer 或 `?access_token=`） | `text/event-stream`；依次收到 `state_update`/`tool_call`/`interrupt`/`complete` 事件帧；首帧含 `retry:` | P0 |
| M6-08 | SSE 双通道鉴权 | 任务执行中 | 分别用 `Authorization: Bearer` 与 `?access_token=` 建立 EventSource | 两种方式均可建立流（浏览器 EventSource 不能自定义头，靠 query 参数） | P1 |
| M6-09 | SSE 断线重连 | 流进行中 | 断开后用 `Last-Event-ID` / `?last_event_id` 重连 | 从中断事件后续传，不重复已发事件 | P2 |
| M6-10 | SQL 危险语句安全拦截 | 任务触发 sql-query 工具 | 构造 DML/DDL（DELETE/UPDATE/INSERT/DROP/ALTER/TRUNCATE/多语句/注释绕过/REPLACE）由 LLM 生成并调用 | 全部被拒，返回业务码 `SQL_SYNTAX`；合法 `SELECT` 放行；执行器零调用；LLM 错误/危险 SQL 被安全拦截 | P0 |
| M6-11 | 服务端口不通容错 | 停止后端 8000 | 前端/调用方发起请求 | 返回可观测连接错误（非 200 空响应），前端给出错误提示，无未捕获崩溃 | P1 |
| M6-12 | 分层记忆回归 | `ENABLE_LAYERED_MEMORY=true` | 跨任务执行，验证记忆检索可用 | 三层记忆在任务间可用；向量走本地关键词（`LocalKeywordVectorStore`，mock 模式） | P1 |
| M6-13 | SSE 与 POST 轨迹一致 | 同任务分别走 `POST /tasks` 与 SSE | 对比两条路径轨迹 | 共用 `translate_graph_event`，事件序列一致（回归历史稳定链路） | P2 |
| M6-14 | SSE 关闭返回 503 | `w3.SSE_ENABLED=false` 重启 | `GET /tasks/{id}/stream` | 返回 503，`body.code=STREAM_DISABLED` | P2 |

---

## 覆盖场景对照（需求追溯）

| 需求覆盖维度 | 对应用例 |
|---|---|
| 正常正向流程 | M1-03/M1-06、M2-01/M2-03、M3-01/M3-02、M4-01/M4-04、M5-01、M6-01/M6-04/M6-07/M6-10 |
| 边界：禁用全部工具 | M2-06（fail-open 回退全量） |
| 边界：知识库返回空文档 | M3-03 |
| 边界：LLM 错误/危险 SQL 被拦截 | M6-10（DML/DDL → `SQL_SYNTAX`） |
| 异常：接口 401 | M1-04/M1-07、M3-04 |
| 异常：服务端口不通 | M1-12、M3-06、M6-11 |
| 异常：Redis 断开 | M2-09、M5-06/M5-07、M6-06 |
| 异常：高频触发 429 限流 | M5-01/M5-02/M5-03 |
| 中断：跑到人工审批中断 + 关闭后端重启恢复 | M4-03/M4-04/M4-06/M4-07、M6-05 |
| 兼容性：Mock 离线 / Real 真实双模式 | M3-01/M3-02/M3-08、M6-12 |
| 回归：分层记忆 | M6-12 |
| 回归：SSE 流式输出 | M6-07/M6-08/M6-13 |

---

## 测试执行说明（重要）

- 本文档为 **v1.2 交付范围**对齐版，不覆盖本版本不存在的功能。
- 限流 429 类用例（M5-01~M5-03、M5-09）**必须**在 `APP_ENV != test` 且 Redis 可用的环境执行（测试环境默认关闭限流）。
- 断点恢复类用例（M6-05）**必须**使用 Redis 后端（`CHECKPOINTER_BACKEND=auto/redis`），否则断点不可恢复属预期。
- 所有"预期结果"均以可观测现象描述：HTTP 状态码、业务码 `code`、响应头、Redis key、SSE 事件类型、轨迹字段。
- 红线能力（断点/记忆/SSE/KB 适配器/Reviewer 判定）仅做行为验证，**不修改任何源码**。
