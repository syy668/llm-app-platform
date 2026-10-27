# Changelog

本文件记录 AgentFlow Platform 自 W1 起每一轮的**新增文件、功能与修复**。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

- **当前版本**：`v1.3.0-wip`（任务归档 + 产物导出 + 记忆长文本单元格统一 + 审批超时收口 + 评测 Mock 去伪装）
- **用例基线**：412 passed / 0 failed（W6 后：426 / 缺口收口后：438 / 一致性审计后：457 / redis-stack 后：457 / v1.1 后：476 / **v1.2 后：492** / **v1.3 后：689** / 审批收口后：699）
- **阶段划分**：W1 → W2 → W3 → W4 → W5-Part1 → W5-Part2 → P2 收尾 → W6 欠账收口 → v1.0 缺口收口 → G8 三开关 → redis-stack 贯通 → v1.1 能力补全 → v1.2 运维与干预

---

## [v1.3.0-wip] — 2026-10-26 · 审批超时收口 + 评测 Mock 去伪装

> 定位：**这次修的是"PRD 写了但没实现"和"代码里藏着假数据"两类静默缺陷**。
> PRD L98/L245/L382 三处都写明"审批挂起超 24h → approval EXPIRED + task FAILED + 可重新发起"，
> 实际代码里 `expires_at` 写入后再无人读取——于是出现了 5 张超期 3 天的僵尸单，
> 其中 task#10 更已死锁（断点被 72h TTL 清空，批准必 409，又永不消失，侧边栏红点常亮）。
> 评测页则是另一个方向：`MOCK_EXTERNAL_EVAL=true` 时 `_mock_metrics()` 用 `random.randrange`
> 造指标，页面却用绿色徽标宣告"接口已接入"——这在交付现场是致命的。
> 全量回归：689 → **699 passed / 0 failed**（新增 10 例审批收口用例）。

### 1) L1 定时收口：`sweep_expired_approvals()`

- 新增于 `app/services/interrupt_service.py`，由 `app/main.py::lifespan` 拉起后台协程
  （`start_approval_sweeper` / `stop_approval_sweeper`，默认 300s，启动即扫一次）。
- 收两类脏单，均置 `EXPIRED` 并写 `decided_at`（否则「已处理」列表里 NULL 会全沉到末尾）：
  1. **超时单** `expires_at < now` → 审批 EXPIRED **且** 任务置 FAILED / `error.code=APPROVAL_EXPIRED`；
  2. **孤儿单**（任务已不在 AWAITING_APPROVAL，审批却还 PENDING）→ **只关审批，不覆盖任务状态**。
- 级联清理：对第 1 类任务直接删 Redis 断点键（`checkpoint` / `checkpoint_blob` / `checkpoint_write`
  三族，按 `:{task_id}:` 精确前缀）。为什么不复用 `clear_interrupts`：它要 graph 实例，
  而后台定时器里 `get_w3_runtime()` 会把 4 个 MCP 子进程拉起来（每 5 分钟一次不可接受）。
  语义上等价——终态任务本就被 `ensure_resumable` 拦在 409，删键额外保证
  `GET /tasks/{id}/interrupt` 不再对已失败任务报 `paused=true`（僵尸视图）。
- **为什么必须是定时器，不能靠"打开页面时惰性收口"**：审批 TTL 24h 短于断点 TTL 72h，
  中间 48h 是唯一还能"从最近 checkpoint 重发"的窗口；惰性收口一旦拖过 72h，任务永久救不回来。
- 配置收口在 `app/core/w3_config.py`：`APPROVAL_SWEEP_ENABLED` / `APPROVAL_SWEEP_INTERVAL_SECONDS`
  / `APPROVAL_SWEEP_PURGE_CHECKPOINT`。

### 2) L2 批准前二次校验

- `approval_service.apply_decision`：PENDING 且 `expires_at < now` → 顺手落 EXPIRED 并抛
  `409 APPROVAL_EXPIRED`（新错误码）。定时器最长有一个扫描周期的滞后，这段窗口必须靠这道闸拦。

### 3) L3 收件箱口径（红点能真正归零）

- `list_paused_tasks` 只返回 **未过期** 的 PENDING 单（`expires_at IS NULL` 视为无期限，仍算待办）。
  侧边栏红点用 `data.total`，因此过期单不会再让红点常亮。
- 每条新增 `resumable`：Redis 断点已丢时为 `false`（死锁）。探活 fail-open——非 Redis checkpointer、
  客户端缺失、扫描异常、键数超上限，一律按"可恢复"处理，宁可让用户点进去看真实报错，也不凭空禁用入口。
- 前端：新增「已过期」只读页签（`?status=EXPIRED`）；`resumable=false` 的行内「审批」置灰并提示
  "任务已死锁，请走一键重发或归档"；批量通过/全选自动跳过死锁行（避免制造"成功 3 失败 2"的噪音）。

### 4) 存量修复脚本

- 新增 `scripts/cleanup_expired_approvals.py`：默认 dry-run 打印"哪张单会被 kill、哪个任务会被置 FAILED"，
  `--apply` 才落库；直接复用 `sweep_expired_approvals`，避免"脚本一套口径、服务另一套口径"。
- 实测收口 5 张超期单（#4~#8）：`approvals` 全部 EXPIRED，task#10 置 FAILED/`APPROVAL_EXPIRED`，
  待办 total 从 5 → **0**。

### 5) 评测 Mock 去伪装

- `app/schemas/w6.py::EvalRunItem` 新增 `is_mock`（`detail.mock`），随行下发。
- 前端评测页：页头绿标「W6 评测接口已接入」在 Mock 模式下改为黄色警示条
  「⚠️ 当前为 Mock 评测数据 · 仅供接口联调，不可用于业务结论」；运行列表逐行打红色「Mock」标；
  所有指标数字置灰并带 `title="模拟数据…"`。
- A/B 面板：`groups` 为空时显式空态「暂无分组实验数据，请创建带 variant 的评测任务」。
  根因是触发评测时前端不带 `variant`，6 条 run 的 `variant` 全为 null，后端按 variant 分组必然为空——
  这不是"暂时没数据"，是结构性为空。

---

## [v1.3.0-wip] — 2026-10-26 · 任务归档（第一阶段）

> 定位：**归档不是删除**。用户要的是"别让它占着我的列表"，不是"抹掉审计证据"。
> 本阶段只做可逆归档，`deleted_at` 软删除与级联清理（Redis checkpoint / SSE / 图表文件）
> 留到第二阶段，等归档口径在真实数据上验证过再动。
> 全量回归：676 → **689 passed / 0 failed**（新增 13 例归档用例）。

### 1) 归档字段（手工迁移，幂等）

- `tasks` 新增 `archived_at DATETIME NULL`（+ 索引 `ix_tasks_archived_at`）：
  `NULL` = 未归档，有值 = 归档时间。
- 新增 `scripts/migrate_add_task_archive.py`：`create_all` 只补表不补列，已有库必须手工 ALTER。
  脚本对 MySQL（读 `.env` 的 `MYSQL_*`，默认 3307）与 SQLite 演示库都支持，可 `--dry-run` 预演。
- `app/schemas/orm_models.py::Task` 同步加列；`app/schemas/w6.py::TaskListItem` 新增 `archived_at`。

### 2) `POST /tasks/{task_id}/archive`

- 新增 `app/routers/task_archive.py`：`{"archived": true|false}`，省略 body 视为归档。
- **只允许终态**（COMPLETED / FAILED / CANCELLED）：执行中的任务被归档会让人以为"任务消失了但还在跑"，
  返回 `409 TASK_STATE_CONFLICT`（新错误码，`ErrorCode` + `_STATUS_MAP` 同步）。
- **幂等**：目标状态已满足时返回 `changed=false`，不刷新时间戳。
- **权限（刻意与详情接口不对齐）**：
  - 详情页仍保持仅创建人可见（防止越权读取隐私）—— `GET /tasks/{id}` 对 ADMIN 之外的他人任务仍 403；
  - 但**归档操作赋予 ADMIN 全局治理权**：ADMIN / APPROVER 均可归档**任意**终态任务，不限创建人。
    理由是管理员在「全部任务」列表里能看到所有人的任务，若按钮点下去 403，这个能力就是摆设；
    归档不读正文、不改数据、可随时取消，越权成本仅为"从列表里暂时收起来"。
  - VIEWER 无此能力（访客只能看已完成任务，没有整理列表的语义）。
- 归档/取消归档写审计日志 `task_archived` / `task_unarchived` / `task_archive_noop`。

### 3) 列表口径

- `GET /tasks` 新增 `archived` 三态查询参数：不传=**排除已归档**（默认）、`true`=只看已归档、`false`=只看未归档。
- 过滤放在 `task_query_service.list_tasks` 的 `conditions`，`total` 与列表同源，不会出现"第 2 页空白"。
- 详情 / 导出 / 看板聚合**不受归档影响**：归档只是列表整理，按 ID 打开仍可看，审计链完整。
- **看板口径（明确决策，勿"顺手优化"）**：`/dashboards/*` 4 处聚合继续**包含已归档任务**。
  列表是工作视图（我想第一屏看什么），大盘是事实视图（任务确实跑过、确实烧了 token）；
  抠掉会让完成率 / 平均耗时 / token 总量随"谁点了归档"漂移。若要"活跃口径"，
  应以显式参数开启，而不是改默认值。唯一的例外是第二阶段软删除（`deleted_at`），
  届时**所有**读路径统一过滤、一次性对齐，不在归档阶段埋半套。
- 新增 [`docs/API参考-v1.3.md`](docs/API参考-v1.3.md)：归档接口完整说明（真实响应、三态列表对账、
  权限口径、看板口径、迁移脚本）。

### 4) 前端

- 工作台任务表：标题区加「全部任务 / 已归档」切换；操作列新增「归档 / 取消归档」（行级 loading + 确认弹窗）；
  已归档行在状态列旁挂弱化的「已归档」标。
- 按钮可见性与后端一致（终态 + `isAdmin || isApprover`，不限创建人），避免"点得到但 403"。
- 「产物与报告」Tab 新增交付物导出条：**报告 Markdown**（概况表 + 摘要 + 审阅 + 子任务表 + 产物清单）、
  完整归档 JSON、轨迹 JSON（后两者复用既有 `/export`、`/trace/export`）。
- Markdown 导出会剥掉 `<untrusted_document>` 这类提示词隔离标签 —— 它们是给 LLM 看的边界标记，
  留在交付文档里既无意义，又会被 Markdown 渲染器当 HTML 标签吞掉正文。
- 记忆正文单元格抽成 `components/MemoryContentCell.vue`（记忆管理页 + 任务详情页记忆面板共用）：
  表格内单行省略 → 悬停限高 280px 可滚动浮层 → 点击抽屉看全文。
  原先用表格自带 `:tooltip`，正文 4000 字时浮层高度超过视口，顶部被裁。

---

## [v1.2.0-wip] — 2026-10-22 · 工具注册面板 + 全局限流 + 评审人工批注 + 演示脚本 + 完整编排

> 约束：**断点持久化 / 分层记忆 / SSE 流式 / 知识库适配器 / Reviewer 判定逻辑**五条已验收链路一行未动。
> 限流用**纯 ASGI 中间件**（不包 body，不碰 SSE 背压）；评审人工介入只新增节点与边，
> `route_after_review` / `decide_verdict` / `compute_confidence` 原样复用。
> 改动前后各跑一次全量回归：476 → **492 passed / 0 failed**。

### 1) MCP 工具注册面板（前后端）

- 新增 `app/services/tool_registry.py`：4 个内置工具（kb-search / sql-query / chart-render / notify）的
  元信息目录（入参 JSON Schema、出参字段说明、能力说明、风险级别、依赖配置项），
  启停状态存 Redis Hash `agentflow:tool:registry`，变更写审计 `agentflow:tool:audit`（保留 200 条）；
  无 Redis 时退回进程内字典（页面试用不 500）。
- 新增 `app/routers/tools.py` + `app/schemas/tool.py`：
  - `GET /api/v1/tools`（ADMIN/APPROVER）：目录 + 启用/禁用统计 + 最近变更；
  - `GET /api/v1/tools/{name}`：单工具详情（未知工具 404 + 可用列表）；
  - `PATCH /api/v1/tools/{name}`（**仅 ADMIN**）：启用/禁用，返回变更后的工具元信息。
- **运行时真生效**：`create_runtime` 装配 graph 前调用 `filter_enabled_tools()`，被禁用的工具不再进入
  Planner 的可用工具集（模型看不到即不会规划）；过滤函数对任何异常退回全量，且"全部禁用"时也退回全量
  ——避免注册表故障或误操作让任务必然产出空计划。连接池不摘除工具进程，**已挂起任务的断点续跑不受影响**。
- 前端：新增 `views/tool/ToolRegistryView.vue`（统计卡 + 工具表 + 启停开关 + 详情抽屉：入参/出参/配置/注意事项）、
  `api/tools.ts`、`types/tool.ts`，导航「系统 → 工具注册表」，路由 roles=`admin/approver`。

### 2) 全局限流（按用户 / 按任务，三类配额）

- 新增 `app/core/rate_config.py`（`RateLimitSettings`）与 `app/core/rate_limit.py`：
  - `RateLimiter`：固定窗口计数（Redis `INCRBY + EXPIRE`；Redis 故障降级进程内计数），
    支持用户维度（`u:<id>`）、任务维度（`task:<id>`）、IP 兜底（`ip:<addr>`）；
  - `classify_request`：`llm`（POST /tasks、/tasks/stream、/tasks/{id}/resume、/approvals/batch）→
    `kb`（/kb/*）→ `api`（兜底总闸），每次请求至少计 api 一次；
  - `RateLimitMiddleware`：**纯 ASGI**（不包装 body），进站判配额、出站回写 `X-RateLimit-*`；
    Redis/限流自身异常 **fail-open**，绝不把业务打成 500。
- 默认配额（`.env` 可调）：api 300 / llm 30 / kb 60 / task 240 每分钟，窗口 60s；
  健康检查 / 文档 / 静态资源免限流；测试环境（`APP_ENV=test`）默认不生效，避免用例互相污染。
- 超限返回标准信封：HTTP **429** + `{"code":"RATE_LIMITED", ...}` + `Retry-After` + `X-RateLimit-Limit/Remaining/Reset/Policy`；
  日志埋点 `rate_limit_exceeded`（kind/scope/subject/used/limit）与 `rate_limit_rejected`（path/method/subject/trace_id）。

### 3) 评审 REJECT 人工批注分支（批注注入 Planner）

- 新增 `app/agents/review_gate.py`：`review_gate` 节点 + `route_after_review_gate`。
  `reviewer` 的 `"replan"` 出边在开启时串到 `review_gate` → 仅 `verdict=REJECT` 时 `interrupt`，
  `REVISE` 直接放行到 replan（不拖慢常规路径）；`REVIEW_HUMAN_INTERVENTION_ENABLED=false` 时边直连 replan，
  与 v1.2 之前完全等价（一键回退）。
- 恢复复用既有 `POST /tasks/{id}/resume`：`decision=REJECTED` + `comment` → 批注写入
  `replan_context.human_comment`（replan 的首要约束）；`decision=APPROVED` → 人工放行，跳过重规划直接汇总并标 `degraded`。
- `TaskState` 新增 `review_intervention`（status ANNOTATED/APPROVED + comment + decided_by），
  `get_detail` 原样回传，供前端展示"谁批注了什么"。
- 前端 `TaskDetailView`：挂起时按 `interrupt.type` 分流 —— `REVIEW_INTERVENTION` 用页内批注卡片
  （分数/可信度/评审建议/结论摘要 + 批注框 + 两个动作），不再错误地弹审批抽屉（该断点没有 `approval_id`）。
- 新增 `NODE_NAMES` 成员 `review_gate`（轨迹/SSE 能正常记录该节点进出）；`route_after_review` 零改动。

### 4) 演示一键脚本

- 新增 `scripts/demo_up.py`（纯 stdlib）：体检 Redis Stack / MySQL / 后端 / 项目一 / 前端五个端口，
  只启动缺失组件；Redis 缺失且有 Docker 时自动拉起 `redis/redis-stack-server`；
  `--mock/--real` 通过**子进程环境变量**切换知识库数据源（不写 .env）；`--status` 只体检，`--down` 只停自己记录的 PID。
- Redis 体检用最小 RESP 协议直连（不依赖 redis-cli），并校验 `RediSearch` + `RedisJSON` 模块在位。
- README 新增「0. 演示快速启动」章节：组件/端口表、三条命令、MOCK 开关对照、知识库自检 curl。

### 5) 完整 docker-compose 编排

- 重写 `docker-compose.yml`：`mysql`（宿主 **3307**，避开系统 MySQL80）/ `redis`（改 **redis-stack-server**，
  healthcheck 校验 search 模块）/ `backend`（8000，`alembic upgrade head` 后启动，注入 v1.2 全部开关）。
- 两个 profile：`frontend`（nginx 托管 `agentflow-web/dist`，8080，SSE 关闭缓冲）、
  `project1`（项目一 8010:8000，独立库 `kb_agent`，**不改项目一代码**）。
- 新增 `deploy/nginx.conf`：静态托管 + `/api` 反代（`proxy_buffering off` 保证 SSE 边收边发）+ `/healthz` + `/static`。

### 6) 验证证据

| 项 | 证据 |
|---|---|
| 工具注册表 | `GET /tools` 返回 4 工具 / 配置脱敏（`MOCK_EXTERNAL_RAG=false`）；PATCH 后 Redis `tool:registry` 变为 `kb-search=0`，`filter_enabled_tools` 实际只剩 3 个工具；恢复后 4 个 |
| 全局限流 | 低配额实例（kb=2/llm=1）：KB 第 3 次请求 **429**（`kind=kb, limit=2, retry_after=18`）；LLM 第 2 次请求 **429**（`kind=llm`）；日志出现 `rate_limit_exceeded` / `rate_limit_rejected` |
| 评审人工批注 | task#23 真实链路：REJECT（3.8 / 可信度 0.49）→ `interrupt.type=REVIEW_INTERVENTION` → 提交批注 → 新计划子任务名变为「skus 表按品类汇总库存周转」「断货 SKU 明细清单」（**逐字对应批注**）→ 再次 REJECT → 人工放行 → **COMPLETED**（`review_intervention.status=APPROVED`，summary 2114 字） |
| 演示脚本 | `demo_up.py --status`：Redis Stack（PONG + RediSearch + RedisJSON）/ MySQL 3307 / 后端 8000 / 项目一 8010 / 前端 5173 全部 OK |
| 编排 | `yaml.safe_load` 校验通过：5 个服务（3 必需 + 2 profile）、6 个数据卷、端口 3307/6379/8000/8080/8010 |
| 回归 | **492 passed / 0 failed**；前端 `vue-tsc` 0 error、`vite build` 9.14s |

### 7) 模拟执行缺陷修复（D-01 / D-02）

- **D-01（P2 真实缺陷，已修）**：`app/__init__.py` 的 `__version__` 由 W1 遗留的 `0.1.0-w1`
  更新为 `1.2.0`，与 CHANGELOG「当前版本」对齐；`/`、`/healthz`、`/api/v1/health`、/docs 与启动日志
  回显同步修正。同时修正 `create_app()` 里过期的 Swagger 描述（原为「W1 工程骨架…业务 Agent / MCP
  工具尚未实现」，与现状不符）。
  新增 `tests/test_version_alignment.py`（4 条）作为 CI 校验：版本号必须为 `X.Y.Z`、必须与 CHANGELOG
  声明一致、三个端点必须回显同一版本 —— 防止版本号再次落后。
- **D-02（P2 设计观察项，采纳建议 b）**：**不改变** `filter_enabled_tools` 的 fail-open 语义
  （全部禁用时回退全量是有意的安全兜底），只在 `GET /tools` 响应新增 `fail_open` 布尔字段
  （`app/schemas/tool.py` + `app/routers/tools.py`），并在前端工具注册表页
  （`views/tool/ToolRegistryView.vue` + `types/tool.ts`）于「全部禁用」时显示醒目告警，
  说明"已自动回退为全量工具"，避免运维误判。是否改为 fail-close 仍待产品确认。

---

## [v1.1.0-wip] — 2026-10-22 · 项目一知识库对接 + Reviewer 可信度 + 导出 + 配置去重

> 约束：已验证的**断点持久化 / 分层记忆 / SSE 流式**三条链路一行未动；改动集中在新增模块与
> 只增字段的契约扩展上，改动前后各跑一次全量回归（457 → 476 passed，0 failed）。

### 1) 项目一知识库真实对接（内置工具 kb-search）

**背景（重要）**：PRD 假设 A-01 认为项目一暴露 `POST /api/v1/kb/search`，实测项目一**没有**该端点
（返回 `Cannot POST /api/v1/kb/search`）。项目一真实可用的检索入口是
`POST /api/v1/kbs/{kb_id}/query`，响应 `data.sources[]` 携带
`chunk_id / document_id / document_title / chunk_index / content / score`。

- 新增 `app/tools/project1_kb.py::ProjectOneKbProvider`：按真实契约取 `sources` 作为证据片段，
  丢弃项目一的 `answer`（AgentFlow 有自己的 planner/summarizer，不需要二次生成）；
  鉴权支持静态令牌与服务账号两种方式，令牌进程内缓存 `EXTERNAL_RAG_TOKEN_TTL_SECONDS`（默认 1500s），
  遇 401 自动重登一次；空结果视为「库中无相关内容」而非故障（不污染熔断计数）。
- `app/tools/kb_adapter.py::build_kb_provider`：按 `EXTERNAL_RAG_CONTRACT` 选择
  `project1_kb_query`（默认）/ `legacy_kb_search` / `mall_only`；`ResilientKbProvider` 的失败判定
  从「空结果也算失败」改为「仅 degraded 算失败」——避免正常空结果把断路器打开。
- 新增 `GET /api/v1/kb/status?probe=true`（ADMIN/APPROVER）：把"项目一通不通"变成一次显式探测，
  返回契约、地址、鉴权方式、熔断器快照；`probe=true` 时真实检索并回传耗时与命中标题。
- 配置项：`EXTERNAL_RAG_BASE_URL` 默认值由 `http://localhost:8000` 改为 `http://127.0.0.1:8010`
  （**8000 是 AgentFlow 自己的端口，原默认值会造成自我调用**），新增 `EXTERNAL_RAG_CONTRACT` /
  `EXTERNAL_RAG_KB_ID` / `EXTERNAL_RAG_USERNAME` / `EXTERNAL_RAG_PASSWORD` / `EXTERNAL_RAG_TOKEN_TTL_SECONDS`。
- `.env` 已切到真实检索：`MOCK_EXTERNAL_RAG=false`、`EXTERNAL_RAG_KB_ID=6`（项目一「企业运维知识库样例」，
  5 文档 / 18 切片）、`EXTERNAL_RAG_USERNAME=agentflow_svc`（用项目一自带 `scripts/seed.py` 新建的服务账号，
  **未改项目一任何代码**）。若要回到离线演示，把 `MOCK_EXTERNAL_RAG` 改回 `true` 即可。

### 2) Reviewer 校验逻辑（可信度打分 + 拒绝重规划分支）

- `ReviewOutput` 新增 `confidence`（0~1）、`confidence_reason`、`verdict`（PASS/REVISE/REJECT），
  **由服务端计算，不采信 LLM 自报值**。
- 新增纯函数 `compute_confidence()`：`0.55×分数 + 0.45×证据覆盖率`，乘来源系数
  （external 1.0 / local 0.82 / local_fallback 0.72），降级 ×0.9，跳过评审上限 0.4。
- 新增 `decide_verdict()`：`total < 2.5` → REJECT；`< 3.5` → REVISE；`≥3.5 但 confidence < 0.55` → REVISE
  （防"分高但无证据"）；其余 PASS。
- `graph.route_after_review()`：以 `verdict` 为主判据走**拒绝重规划分支**——REVISE/REJECT 且
  `replan_count < 2` 回 replan，预算用尽降级交付；无 `verdict` 的历史 checkpoint 仍按阈值判断（向后兼容）。
- 配置：`REVIEW_REJECT_THRESHOLD=2.5`、`REVIEW_CONFIDENCE_FLOOR=0.55`。
- 前端 `TaskDetailView`：审阅区改为展示 `verdict` 标签 + `总分/可信度`，并新增「可信度构成」「放行原因」两行；
  `types/task.ts::ReviewResult` 补 `total/pass/confidence/verdict`（此前前端读 `decision/score`，后端从不下发，恒显示 `—`）。

### 3) 任务与轨迹导出（缺口 G10）

- 新增 `app/routers/task_export.py`：
  - `GET /tasks/{id}/export` → `agentflow.task_export.v1`：任务 + 子任务 + 评审 + 审批 + 产物 + 轨迹 + metrics；
  - `GET /tasks/{id}/trace/export` → `agentflow.trace_export.v1`：steps + checkpoints + interrupt。
  两者均返回文件流（ASCII 文件名 + `X-AgentFlow-Schema` 头），权限复用 `assert_visible`，steps 上限 2000 条并标 `truncated`。
- 前端：`utils/download.ts`（blob 下载 + Content-Disposition 解析）、`api/tasks.ts` 两个导出函数、
  详情页「导出」下拉（任务归档 / 轨迹 JSON）。**替换**原先"浏览器侧拼接已加载数据"的假导出。

### 4) 配置去重

- `.env` 的 `SSE_IDLE_TIMEOUT_SECONDS` 原有两处（187 行 900、301 行 20，后者生效），现只保留一处并置 `300`
  （需同时满足 w6 的 `[1,600]` 校验），注释说明两个消费方（`stream_service` / `resilience_guard`）。
- 同段的重复 `SSE_HEARTBEAT_SECONDS` 一并去掉（值相同、无行为变化），避免同类"改了上面不生效"的坑。

### 5) 文档

- 新增 `docs/API参考-v1.1.md`：通用约定 + 人工审批（收件箱/已处理/详情/interrupt/resume/批量）+ 分层记忆
  （meta/列表/新增/召回/改删）+ 导出 + 知识库自检 + Reviewer 新字段 + SSE 速查 + 错误码，
  示例响应取自本机真实调用。

### 验收

- 后端 `pytest tests/` → **476 passed / 0 failed**（含新增 `tests/test_v11_new_features.py` 19 项）
- 前端 `vue-tsc --noEmit` 0 error；`vite build` 8.32s 成功
- **项目一真实检索（打通）**：`GET /kb/status?probe=true&q=数据备份` →
  `mock=false`、`auth_mode=service_account`、`kb_id=6`、`chunks=3`、`provider_latency_ms=818`，
  片段标题「企业运维知识库样例.md 第6块」、`score=0.032787`（RRF 原始量纲）、`source=project1:kb6:doc6`
- **任务内工具节点（打通）**：task#22「检索企业知识库，说明公司数据备份策略与信息安全要求」→ `COMPLETED`，
  3 个 `kb_search` 子任务全部 `DONE`，Trace 6 条 kb-search 事件（call/result 各 3），
  `result_digest` 里是项目一真实切片（doc_id 28/26，含"数据库每日凌晨 3 点全量备份，保留 30 天"），
  评审 `total=4.6 / verdict=PASS / confidence=0.71`，summary 明确标注证据 doc_id
- 真实任务 task#21（use_memory=true）：`review.total=4.2`、`verdict=PASS`、`confidence=0.62`，
  `confidence_reason="评分 4.20/5（权重0.55）× 证据覆盖 83%（权重0.45）× 来源系数 0.82；本地降级 ×0.9"`
- 导出真实下载：`task-18-export-*.json`（68 步、metrics.duration_s=35.0、tool_calls=22）、
  `task-18-trace-*.json`（68 steps / 14 checkpoints）
- `GET /kb/status` 配置与鉴权方式如实回显，VIEWER 访问返回 403

---

## [v1.0.5-wip] — 2026-10-22 · 前后端一致性审计（功能可用性修复）

> 触发：全量核对「后端已实现的接口 ↔ 前端页面实际调用」，确认无 mock / 占位数据被当作真实数据渲染。
> 结论：31 条路由与 10 个 api 模块一一对齐，九个页面均走真实接口；本轮只修**字段形态与交互缺口**，
> 不动任何已正确的调用链与业务逻辑。

### 后端修复（1 处，真实数据形态错误）

- `app/agents/human_gate.py::_preview`：审批单 `payload.subtasks[].risk_level` 由 `str(Enum)` 改为
  取 `.value` —— `str(SubtaskRiskLevel.HIGH)` 会序列化成 `"SubtaskRiskLevel.HIGH"`，
  前端按 `=== 'high'` 判断恒为 false，审批抽屉的「高危」标签从未显示过；同时补 `tool_hint` 别名
  （与既有 `tool` 同值，前者已被 `tests/test_w3_e2e.py` 断言为契约，故只做新增不改名）

### 前端修复（3 处）

- `components/ApprovalDrawer.vue`：高危子任务预览改为 `tool ?? tool_hint` 取工具名、
  `String(risk_level).toLowerCase().includes('high')` 判高危 —— 兼容历史脏数据（旧审批单里存的是枚举全名）
- `views/approval/ApprovalInboxView.vue`：待审批表格补服务端分页（`page` / `size` 此前是死变量，
  超过 20 条无法翻页）；批量处理后当前页为空自动回退到最后一页
- `components/DagGraph.vue`：G8 分支节点上图 —— `use_memory` 打开时绘制 `memory_recall → planner`，
  `compress` 打开时绘制 `planner → context_compress`，`human_gate` 前驱随之改接压缩节点；
  此前图上只有固定五节点 + 子任务，开关打开后 DAG 与实际执行链不一致
- `views/task/TaskDetailView.vue` + `types/task.ts`：详情页头部补「记忆召回 / 上下文压缩 / 评测标签」标签，
  `TaskDetail` 补 `compress` / `use_memory` / `eval_tag`（后端 `get_detail` 本就下发，前端类型原先漏了）

### 验收

- 真跑 task#10（新建后命中 notify）：`GET /tasks/10/interrupt` 的 `preview.subtasks[0]` 为
  `{"tool":"notify","tool_hint":"notify","risk_level":"high"}`，前端高危/工具标签均可渲染
- 后端：`pytest tests/test_v1_gaps.py tests/test_g8_switches.py tests/test_graph_routing.py tests/test_w3_e2e.py` → 66 passed
- 前端：`vue-tsc --noEmit` 0 error；`vite build` 成功（8.29s）

### 已知限制（非缺陷，环境所致）—— 均已解除

- ~~本地 `Memurai` 不支持 RedisJSON / RediSearch（`FT.INFO` 未知命令），checkpointer 按 `auto` 策略降级为
  `InMemorySaver`：后端进程重启后此前挂起的审批断点即丢失，任务是 `AWAITING_APPROVAL` 但 resume 会 409~~
  → 2026-10-22 环境切换为 **redis-stack 容器**（Redis 7.4.7 + RedisJSON + RediSearch 均可用），
  checkpointer 稳定落在 `AsyncRedisSaver`，重启后断点可恢复（见 v1.0.6）。
- ~~`ENABLE_LAYERED_MEMORY=false`（默认关闭）：记忆管理页只读空列表、写操作 503~~
  → 已置 `true`，三层记忆读写链路全部打通（见 v1.0.6）。

---

## [v1.0.6-wip] — 2026-10-22 · redis-stack 环境下的四项能力贯通

> 触发：环境由 Memurai 升级为 redis-stack 容器（RedisJSON / RediSearch 可用），原先"降级 InMemorySaver"
> 与"记忆总开关关闭"两条环境约束解除。本轮把四项能力做**真实可用性验证**，并修掉验证中暴露的 4 个缺陷。
> 全程未改动项目一（`evalagent_vue`）、项目二（`企业垂直知识库 Agent 后台`）任何代码。

### 缺陷修复（4 处）

- **`app/services/redis_saver_patch.py`（新增）+ `checkpoint_service._build_redis_saver`**：
  修正 `langgraph-checkpoint-redis 0.0.8` 的 checkpoint key 错位缺陷 —— 其 `AsyncRedisSaver.aput`
  用 config 里的**父快照 id** 作为新快照的 Redis key / 顶层 `checkpoint_id` 字段，而本次快照真实 id 是
  `checkpoint["id"]`。后果致命且隐蔽：①每次保存覆盖上一条快照（`checkpoint:11:...` 只剩 3 条且互相错位）；
  ②`channel="__interrupt__"` 的挂起写入（`checkpoint_write:*:...:-3`）找不到所属快照；
  ③`aget_state().tasks[].interrupts` 恒空 → `pending_interrupts()` 判定失败 → **任务在 `approval_created`
  之后立刻被写成 `FAILED`**（`error` 字段还是 null）；④重启后 resume 也不可能成功。
  修法是调用原实现前把 config 的 `checkpoint_id` 换成 `checkpoint["id"]`，不改库文件、可随依赖升级移除。
- **`app/agents/graph.py::create_runtime`**：构建 MCP 池时补 `on_event=mcp_event_hook`。
  此前只有 `w3_runtime`（流式入口）传钩子，`TaskService` 走的这个通用入口不传，导致普通
  `POST /tasks` 创建的任务 **tool.call / tool.result 一条都不落 `task_executions`**。
- **`app/services/task_service.py::run` + `_bind_offline_context` / `_emit_terminal`**：
  非流式入口补齐"上下文绑定 + 事件流消费 + 终止帧"。此前 `run` 用 `ainvoke` 一次性跑完且不 bind，
  于是 ①轨迹表全空（详情页轨迹时间线整页空白）；②SSE 订阅者永远收不到 `complete`/`interrupt`，
  只能等 20s 空闲超时断开，前端一直停在"运行中"。现改为消费 `astream_events` 并复用统一的
  `translate_graph_event`，收尾按挂起与否补发 `interrupt` / `complete`。
- **`app/services/stream_service.py`**：把 `_handle_event` 的翻译逻辑抽成模块级
  `translate_graph_event(event, ctx)`，SSE 与普通入口共用一套语义（含 G8 分支节点的
  `xxx_node` 名称放宽匹配）；`_artifact_dicts` 增公开别名 `artifact_dicts` 供跨模块复用。

### 分层记忆接线（此前模块完整但运行期未装配）

- `task_service._finalize_memory`（新增）：任务收尾统一归档 ——
  **短期层**把终态 `messages` 逐条 `append_short` 进 Redis List（TTL 24h、超 400 条裁剪）；
  **中期层** `write_mid` 写摘要（30 天 TTL）；**长期层**按 `review_score >= MEMORY_LONG_MIN_REVIEW_SCORE`
  归档并向量化。全部包在 `try/except` 内且受 `ENABLE_LAYERED_MEMORY` 总开关控制，
  关闭时行为与 W1~W4 完全一致（`test_w5_layered_memory` 25 例仍全绿）。

### 验收（全部真实数据，非模拟）

- **断点持久化**：task#15 挂起 `AWAITING_APPROVAL`（approval_id=9）→ **kill 后端进程** →
  Redis 中 3 条独立快照仍在 → 重启 → `GET /tasks/15/interrupt` 返回 `preview` → `POST /resume`
  通过 → `COMPLETED`，5 个子任务全 `DONE`。
- **分层记忆**：`POST /memories` 建 long/mid 各 1 条；`POST /memories/recall` 对相关 goal 命中
  `score=0.4065`、无关 goal 返回空；task#18（`use_memory=true`）Trace 出现 `memory_recall enter/exit`，
  exit payload 带真实命中片段（命中上一轮归档的 memory#5）；任务结束后
  `agentflow:memory:short:18` 长度 14（TTL 86389s），并新增 mid#6 / long#7。
- **轨迹完整性**：task#18 轨迹 **68 步**，节点分布 planner 2 / executor 8 / reviewer 6 / replan 4 /
  summarizer 2 / memory_recall 2 / context_compress 2；44 条 tool 行 `subtask_id` **全部非空**（G7 精确归属）。
- **SSE**：task#20 收到 21 帧（state_update 8 + tool_call 12 + complete 1），`seq` 单调 1→21，
  节点序 `planner → executor → reviewer → summarizer`；断线重连带 `Last-Event-ID` 从断点续传 4 条且不重复。
- **回归**：后端 `pytest tests/` → **457 passed / 0 failed**；前端 `vue-tsc --noEmit` 0 error、
  `vite build` 成功（18.60s）。

### 配置变更

- `.env`：`ENABLE_LAYERED_MEMORY=false → true`（总开关打开，记忆三层生效）。

### 遗留（不影响使用）

- `.env` 中 `SSE_IDLE_TIMEOUT_SECONDS` 出现两次（900 / 20），后者生效为 **20s**：长任务在
  "LLM 思考中暂无事件"时会被断开，前端 `EventSource` 自动重连 + `Last-Event-ID` 续传，功能可用；
  若要减少重连次数，把末尾那行改为 900 即可（本轮未改，避免动既有演示配置）。
- `MOCK_EXTERNAL_RAG=true` 时记忆向量检索走 `LocalKeywordVectorStore`（本地关键词打分），
  接入真实 RAG 服务后自动切 `HttpRagVectorStore`。

---

## [v1.0.4-wip] — 2026-10-22 · 缺口收口前端接入 + G8 轨迹可见性修复

> 范围（用户确认）：类别 A 全补、类别 B 仅 G9、G8 三开关完整实现；G6 配置侧与 G10 排入 v1.1；
> 类别 C（子任务编辑）跳过。前端改动均在 `agentflow-web/`，后端仅做**打通性修复**，不改既有业务逻辑。

### 后端修复（三处，均属"开关开了但看不到效果"的链路断点）

- `app/schemas/enums.py`：`ExecutionNode` 补 `memory_recall` / `context_compress`
  —— 此前 `trace_service.VALID_NODES` 由该枚举派生，两个 G8 节点被白名单直接跳过（`logger.debug: trace_node_skipped`），
  Trace 里永远看不到这两个节点
- MySQL Schema（一次性 DDL，无数据变更）：
  `ALTER TABLE task_executions MODIFY COLUMN node ENUM(...,'memory_recall','context_compress') NOT NULL`
- `app/services/stream_service.py`：`_handle_event` 的节点名匹配由 `name == node` 放宽为
  `name in {node, f"{node}_node"}` —— G8 节点的 Runnable 名是函数名 `xxx_node`，原判断会把整段事件丢弃
- `app/services/stream_context.py`：新增 `current_subtask_seq` ContextVar + `bind_subtask` / `unbind_subtask`，
  工具与 LLM 轨迹行 payload 追加 `subtask_seq`（执行期计划未落库，拿不到 `subtasks.id`）
- `app/agents/executor.py`：`run_pass` 在每次 `run_subtask` 前后 bind/unbind 子任务序号
- `app/services/task_service.py`：新增 `_backfill_subtask_ids`，`persist_plan` 拿到 seq→id 映射后
  回填 `task_executions.subtask_id`（仅补 `subtask_id IS NULL`），G7 由此真正可用
- `tests/test_v1_gaps.py`：修正 `test_dashboard_trend` 的窗口写法（`end` 传纯日期=当天 00:00，
  会把当天 01~02 点的种子任务排除，属测试自身 bug，与接口无关）

### 前端接入（`agentflow-web/`）

- `views/task/TaskCreateView.vue`：P03 高级选项三开关从 disabled 占位改为可用（`compress` / `use_memory` /
  「标记为评测样本」+ 标签输入 ≤64），提交时写入请求体，并在成功提示里回显已启用项
- `views/approval/ApprovalInboxView.vue`：新增「已处理」Tab（`GET /approvals?status=`，多选状态 + 服务端分页，
  展示决策结果/决策人/意见/决策时间）；「批量通过」改调 `POST /approvals/batch`，按 `results[].ok` 分组提示
- `views/task/TaskDetailView.vue` + `components/TraceTimeline.vue`：子任务 ↔ 工具调用改按 `subtask_id`
  精确归属（旧数据回落 `tool_name`），Trace 详情展示 `span_id` / `parent_span_id` / `subtask_id`
- `api/approvals.ts` / `types/approval.ts` / `types/trace.ts` / `types/task.ts` / `utils/status.ts`：
  补齐批量审批、已处理列表、G8 三开关、Span 三字段与节点文案

### 验收

- 后端：`pytest tests/test_v1_gaps.py tests/test_g8_switches.py tests/test_graph_routing.py` → 48 passed
- 真跑（DeepSeek，task#6 带三开关）：Trace 出现 `memory_recall` / `context_compress` 各 enter+exit，
  工具行 `subtask_id` 精确为 24/26；`eval_tag=v10-g8-check` 落库
- 真跑（task#7 含 notify）：`AWAITING_APPROVAL` → `POST /approvals/batch` 成功 1 / 失败隔离 1
  （`NOT_FOUND: 任务 999999 不存在`）；`GET /approvals?status=APPROVED` 返回决策人/意见/时间
- 前端：`vue-tsc --noEmit` 与 `vite build` 均通过

---

## [v1.0.3-wip] — 2026-10-22 · G8 任务级三开关完整实现

> 依据前端 P03 高级选项三开关（`compress` / `use_memory` / `eval_tag`），从「预埋参数」升级为**完整实现**。
> 保护规则：三开关默认 false，原有任务执行链路作为默认分支**保持不变**，仅用户主动勾选时进入对应分支。

### 功能

1. **入参接收 / 校验 / 入库**：`TaskCreateRequest` 新增 `compress` / `use_memory` / `eval_tag` 三字段
   （`eval_tag` 最长 64）；`Task` 表新增三列（`compress` / `use_memory` 默认 false，`eval_tag` 可空索引）；
   `create_task` / `submit` 落库；`get_detail` 透出。
2. **LangGraph 条件分支**（`build_graph` 始终注册节点 + 条件边路由，图拓扑固定、单例安全）：
   - `use_memory=True`：`START → memory_recall → planner`（召回长期记忆并入 `memory_hits`，planner/replan 已消费）；
   - `compress=True`：`planner → context_compress → human_gate/executor`（显式压缩上下文入 `context_digest` 并记账）；
   - 默认 false：`START → planner`、`planner → human_gate/executor` 直连，与原链路完全等价。
3. **eval_tag 绑定评测**：`EvalRun` 表新增 `eval_tag` 列；`POST /eval/runs` 请求新增 `eval_tag` 透传落库；
   `GET /eval/runs` 支持 `eval_tag` 过滤，供后续评测筛选。

### 新增文件

- `app/agents/context_steps.py` —— `memory_recall` / `context_compress` 两个可选步骤节点工厂
  （开关关=空操作，失败降级不阻断主流程）
- `tests/test_g8_switches.py` —— 13 条用例覆盖入参校验 / 落库 / state 注入 / 节点行为 / eval_tag 绑定与筛选

### 存量文件改动

- `app/schemas/agent_io.py`：`TaskCreateRequest` 追加三字段
- `app/schemas/orm_models.py`：`Task` 加 3 列、`EvalRun` 加 `eval_tag` 列
- `app/schemas/graph_state.py`：`TaskState` 追加 `compress` / `use_memory` / `eval_tag`
- `app/services/task_service.py`：`create_task` 落库三开关、`build_initial_state` 注入、`get_detail` 透出
- `app/services/stream_service.py`：`submit` 透传三开关
- `app/routers/tasks.py` / `tasks_ext.py`：创建路由透传三开关
- `app/agents/graph.py`：新增 `memory_recall` / `context_compress` 节点 + 条件边，`NODE_NAMES` 扩为 9，
  新增 `route_from_start` / `route_after_compress`
- `app/schemas/w6.py`：`EvalRunCreateRequest` / `EvalRunItem` 加 `eval_tag`
- `app/services/eval_run_service.py`：`create_run` 落库 `eval_tag`、`list_runs` 支持过滤
- `app/routers/eval.py`：`GET /runs` 加 `eval_tag` 过滤参数
- `tests/test_graph_routing.py`：`NODE_NAMES` 断言更新 + 新增 6 条 G8 路由用例

### 数据库迁移（无 alembic 迁移文件，按既有 create_all 约定，附 SQL）

```sql
ALTER TABLE tasks
  ADD COLUMN compress BOOLEAN NOT NULL DEFAULT FALSE COMMENT '上下文压缩开关',
  ADD COLUMN use_memory BOOLEAN NOT NULL DEFAULT FALSE COMMENT '长期记忆检索开关',
  ADD COLUMN eval_tag VARCHAR(64) NULL COMMENT '评测标签（绑定 eval_run 筛选）';
CREATE INDEX ix_tasks_eval_tag ON tasks (eval_tag);

ALTER TABLE eval_runs
  ADD COLUMN eval_tag VARCHAR(64) NULL COMMENT '评测标签，用于筛选';
CREATE INDEX ix_eval_runs_eval_tag ON eval_runs (eval_tag);
```

---

## [v1.0.2-wip] — 2026-10-22 · v1.0 缺口接口收口

> 依据前端 `agentflow-web` 预留点位 + PRD §7.1，补齐「前端已预留、后端缺失」的 5 项缺口。
> 范围经用户确认：类别 A 全补（趋势/日志/G7 字段/已处理审批），类别 B 仅破例 G9 批量审批，
> 类别 C（子任务编辑 P1）跳过。原则：只新增文件 + 存量文件仅追加，复用单条 resume 逻辑。

### 新增文件

- `app/routers/logs.py` —— `POST /logs/search` 日志检索（缺口 G6）：基于 `task_executions` 结构化执行记录，
  支持 `trace_id` / `task_id` / `event` / `level` / `start,end` 时间窗过滤 + 分页；`level` 由 `tool_error` 非空
  或 `event=error` 判定 ERROR；不引入外部日志平台（日志本身走 stdout JSON）
- `tests/test_v1_gaps.py` —— 12 条用例，覆盖 G4/G7/G9/趋势/日志 五个缺口

### 存量文件改动（仅追加/扩展，不改写已闭环逻辑）

- `app/routers/approvals.py`：
  - `GET /approvals` 增加 `status` 过滤（缺口 G4）：`APPROVED/REJECTED/EXPIRED` → 已处理列表（含 `decided_by` /
    `decide_comment` / `task_no` / `goal`）；缺省仍走待处理收件箱（原逻辑不变）
  - 新增 `POST /approvals/batch`（缺口 G9）：批量审批，逐条复用 `StreamingTaskService.resume`，
    单条失败不中断其余，返回 `{succeeded, failed, results[]}` 逐条结果
- `app/routers/dashboards.py`：新增 `GET /dashboards/trend` 任务完成趋势（按天聚合 task_total / completed /
  completion_rate，空桶补齐保证折线连续）
- `app/services/interrupt_service.py`：新增 `list_processed_approvals`（已处理审批列表查询，ADMIN 仅本人）
- `app/services/trace_service.py`：`_to_dict` 出参补 `span_id` / `parent_span_id` / `subtask_id`（缺口 G7，
  表列已存在，零迁移）
- `app/schemas/w6.py`：追加 `TrendPoint` / `TrendData` / `LogSearchRequest` / `LogItem` / `LogSearchData`
- `app/routers/api_router.py`：注册 `logs.router` 一行

---

## [v1.0.1-wip] — 2026-10-21 · W6：W5-Part2 欠账收口

> 依据 `docs/W6接口设计文档.md` 补齐 PRD §7.1 已承诺、`api_router.py` 注册位已留的 3 组接口。
> 原则：**只新增文件 + 存量文件仅追加**（错误码 / 配置 / 注册行 / 索引），不建新表，不动主干架构。

### 新增文件

- `app/routers/tasks_w6.py` —— `GET /tasks` 任务分页列表（缺口 G1）：A/R 可查全部（可按创建人过滤），
  V 仅本人；viewer 传他人 `creator_id` 被静默覆盖为本人、`creator_name` 恒 `null`（PRD 脱敏两条）；
  支持 `status` 多值 / `keyword` LIKE / `start,end` 闭区间 / `order` 排序；
  `error` 仅下发 `{code,message}`，`duration_s` 服务端计算
- `app/routers/dashboards.py` —— 大盘聚合（缺口 G2）：
  - `GET /dashboards/summary`：P02 五张指标卡（`task_total` / `completion_rate` / `pending_approvals`（与
    `GET /approvals` 的 total 同源，不限定窗口）/ `avg_duration_s` / token 两项）；viewer token 字段脱敏 `null`；
    `status_breakdown` 六键恒齐（SUMMARIZING 短瞬态以额外键透出，保证与 task_total 可对账）
  - `GET /dashboards/tools`：`task_executions` 按 `tool_name` 聚合（真实执行记录，非规划意图），
    `success_rate` 以 `tool_error IS NULL` 判定，`call_count` 降序
- `app/routers/eval.py` —— 评测接口组（缺口 G3，FR-016 P1）：
  - `GET /eval/runs`（A/R；viewer 403）：分页 + source/variant/status 多值过滤，created_at 降序
  - `GET /eval/runs/summary`（A/R/V）：按 variant 聚合 DONE run；`token_saving_rate` 以 compress_off 组为基线
    （`1 - 该组/基线`，基线组自身 null，无基线组两组均 null）；viewer token 字段脱敏
  - `GET /eval/metrics`（A/R/V）：窗口内按自然日聚合 DONE run，按日期升序，无数据日不出点
  - `POST /eval/runs`（A/R）：触发回归评测——本地插 `PENDING` 立即 200；mock 模式（`MOCK_EXTERNAL_EVAL=true`，
    dev_serve 默认态）内嵌 runner 生成 metrics 落库置 DONE；非 mock 走 PRD L704-706：转发项目二 → 后台每
    `EVAL_POLL_INTERVAL_S` 轮询 → DONE 回写 / 连续失败 `EVAL_POLL_MAX_RETRY` 次置 FAILED；
    同 dataset_id+variant 存在 PENDING/RUNNING → 409 `EVAL_RUN_DUPLICATE`
- `app/services/task_query_service.py` / `app/services/eval_run_service.py` —— 上述两组的查询聚合与触发链路服务层
- `app/schemas/w6.py` —— W6 响应/请求模型（信封复用 `ApiResponse`/`PageData`）
- `app/utils/query_parsing.py` —— 多值枚举 / ISO8601 区间 / 排序白名单解析（非法值统一 422）
- `tests/test_w6_interfaces.py` —— 14 条用例，对照 W6 文档 §5 验收清单 7 项全覆盖

### 存量文件改动（仅追加，零改写）

- `app/core/exceptions.py`：W2+ 预留段追加 `EVAL_UPSTREAM_UNAVAILABLE`（502）/ `EVAL_RUN_NOT_FOUND`（404）/
  `EVAL_RUN_DUPLICATE`（409），并补 `_STATUS_MAP`
- `app/core/config.py`：追加 `EVALAGENT_BASE_URL`（空 + 非 mock 时触发 502）/ `EVAL_POLL_INTERVAL_S=10` /
  `EVAL_POLL_MAX_RETRY=30`
- `app/routers/api_router.py`：启用预留注册位 3 行（文档约定的存量改动位）
- `app/schemas/orm_models.py`：`tasks` 追加单列索引 `ix_tasks_created_at`（W6 §1.4 纯时间范围扫描）
- `CHANGELOG.md`：本条目

### 说明

- `eval_runs` 表无 `finished_at` 列，响应 `finished_at` 取自 `detail.finished_at`（状态回写时写入），零表结构改动
- mock 模式下评测 run 同步置 DONE，无「进行中」窗口，409 并发约束在非 mock 或预置 PENDING 记录时触发
- 前端对接映射见 W6 文档 §4（agentflow-web 三类占位 G1/G2/G3 均可接入）

---

## [v1.0.0] — 2026-10-21 · W1~W5 全量交付完成

> 本版本为**功能冻结版**：主干 `main` 锁定，此后仅接受 hotfix。
> 本版本不新增业务能力，只做工程化收口：文档归档、版本锁定、交付包核验。

### 归档整理（本轮）

- **新增** `docs/` 文档归档目录，按用途分四类收拢全部 W1~W5 文档：
  - `docs/delivery/` —— `README-W1.md` / `README-W2.md` / `README-W3.md` / `README-W4.md` /
    `README-W5-Part1.md` / `README-W5.md` / `W1-W4项目总览与总结.md`（7 份，原根目录）
  - `docs/checklists/` —— `W2/W3/W4/W5-Part1/W5-Part2-自测核对清单.md` + `P2收尾-自测核对清单.md`（6 份）
  - `docs/deploy/production-deploy-guide.md`（1 份）
  - `docs/reports/` —— `AB实验报告模板.md` / `AB实验报告_full.md` / `eval_report_full.md` / `Demo三条链路_offline.md`（4 份）
- **新增** `docs/INDEX.md`：文档总索引 + 完整目录树（排除 venv / `__pycache__` / 覆盖率缓存 / 镜像缓存）
- **新增** 根 `README.md`：架构图、W1~W5 里程碑、技术栈、全套命令、已知限制、后续方向
- **新增** 本文件 `CHANGELOG.md`
- **新增** `CONTRIBUTING.md`：主干保护规则、PR 流程、hotfix 流程、提交规范
- **移动** `deploy/Dockerfile.w1-single-stage` → `deploy/legacy/Dockerfile.w1-single-stage`
- **删除** `deploy/docker-compose.w1.yml` —— 与根 `docker-compose.yml` **逐字节相同**的冗余备份
- **清理** `__pycache__`（10 个目录）/ `*.pyc`（72 个）/ `.pytest_cache` / `.ruff_cache` / `.coverage` /
  `storage/charts/*.png`（20 张运行期出图）
- **更新** `.gitignore`：新增 `storage/charts/*`（保留 `.gitkeep`）、`dist/`（本地交付包）
- **更新** `.dockerignore`：文档目录 `docs/`、评测产物 `eval_output/`、`storage/charts` 不入镜像

### Git / 版本

- 仓库初始化，主干 `main`
- 附注标签 `v1.0.0` —— `Release v1.0.0 W1-W5全量交付完成`
- 主干保护：禁止直接 push，所有变更走 PR（配置方式见 `CONTRIBUTING.md`）

---

## [P2 收尾] — RAG 字段对齐 + 记忆生命周期（+105 用例）

> 约束：W1~W5 业务代码只读，**仅新增文件**；`layered_memory.py` 一行未改。
> 基线 307 → **412 passed**，零回归。

### 新增文件

| 文件 | 规模 | 职责 |
|---|---:|---|
| `app/memory/rag_schema.py` | 410 语句 | RAG 六字段唯一契约 + 契约校验 + 打分对齐 + mock 兜底 + 契约客户端 |
| `app/memory/memory_lifecycle.py` | 360 语句 | 语义去重 + `w=exp(-λΔt)` 衰减 + 长期 TTL + 运行期方法包装 |
| `tests/test_p2_rag_schema.py` | 59 用例 | 字段缺失校验、打分对齐、mock 零网络、HTTP 降级 |
| `tests/test_p2_memory_lifecycle.py` | 46 用例 | 重复合并、衰减打分、过期过滤、开关关闭 |
| `docs/checklists/P2收尾-自测核对清单.md` | A~I 段 | 逐项核对 + 验证命令 |

### 功能

- **RAG 六字段契约**：`chunk_id / doc_id / score / text / spans / source` 为唯一出入参 schema；
  别名归一化（`content→text`、`id→chunk_id`、`similarity→score`）；`chunk_id` 缺失按 `doc_id#序号` **幂等派生**；
  `doc_id` 可从 `chunk_id` 前缀还原（兼容 Chroma 直出）
- **契约校验与修补分离**：`validate_chunk(strict=True)` 只判断不修补、一次报全缺失字段；
  `normalize_chunk()` 尽力修补；`check_kb_contract()` 对整包响应体检并回传 `missing_fields`
- **相似度打分对齐** `align_score()`：`93→0.93`、`"87%"→0.87`、`-0.2→0.4`（余弦）、`1.5→1.0`（越界截断）、`nan/inf→0.0`
- **`spans` 三形态兼容**：`[{start,end}]` / `[[s,e]]` / `"0-3,5:9"`，非法条目跳过不报错
- **`MOCK_EXTERNAL_RAG` 兜底**：开启时**不创建 HTTP 客户端**（零网络请求，可离线/CI 运行）
- **记忆语义去重**：切词 → crc32 特征哈希 128 维 → L2 归一化 → 余弦，阈值 0.88；
  `set_embedder(fn)` 可注入项目一真实向量；合并规则「正文取更长 / importance 取更高 / hit_count+1 / 刷新 TTL」；
  **跨用户绝不合并**
- **时间衰减**：`w = max(0.05, exp(-0.01·Δt))`，半衰期 69.31 天；`importance` 每条 +6% 微调；
  召回按衰减分重排，默认只降权不过滤（`MEMORY_DECAY_REFILTER=true` 才二次过滤）
- **TTL 自动清理**：长期默认 180 天（`0` = 永久，保持原语义），归档即写、合并刷新、召回期过滤；
  `purge_expired()` 一并清 mid+long，明细走新增 `purge_expired_details()`
- **运行期包装**：`install_memory_lifecycle()` 替换 `archive_long / recall / purge_expired / create_memory` 四个方法，
  **签名 / 返回类型 / 异常语义逐项相等**（`inspect.signature` 断言）；另提供 `LifecycleMemoryService` 子类共用同一组实现
- **开关关闭零执行**：`ENABLE_MEMORY_LIFECYCLE=false`（默认）时 `install()` 返回 `False`、方法身份不变、
  `stats()["archive_calls"]==0`、归档后 `expires_at is None`

### 修复

- `align_score()` 对 `1.5` 这类越界值原会原样返回 1.5 分 → 改为按越界截断到 `1.0`
- `normalize_chunk()` 原在 `chunk_id` 缺失时才派生，但**显式传入空串**时会用 `doc_id` 覆盖已有 id → 改为仅「字段真缺失」才派生
- `check_kb_contract()` 宽松模式原只丢弃坏块、不报缺失字段 → 改为同时收集 `missing_fields` 供联调定位
- 契约客户端测试原用 `request.json()` 断言，httpx 无 content-type 时会抛 → 改用 `json.loads(request.content)`

### 存量改动（仅配置模板）

- `.env.example` 末尾追加 25 行 P2 开关（`ENABLE_MEMORY_LIFECYCLE` / `MEMORY_DEDUP_*` / `MEMORY_DECAY_*` /
  `MEMORY_LONG_TTL_DAYS` / `MEMORY_TTL_CLEANUP_ENABLED` / `MEMORY_EMBED_DIM` / `RAG_CONTRACT_STRICT`）

---

## [W5-Part2] — 容器化 + 分布式 SSE 会话 + OTel 全链路 + CI（+100 用例）

> 约束：W1~W5-Part1 只读；`sse_event_stream()` 生成器**一行未改**。基线 207 → **307 passed**。

### 新增文件

| 文件 | 规模 | 职责 |
|---|---:|---|
| `app/core/w6_config.py` | 185 行 | `W6Settings`：会话后端 / OTel / SSE 加固开关，默认全「零影响」 |
| `app/mcp/session_backend.py` | 233 行 | `RedisSessionQueue`（鸭子类型 `put/get/qsize/aclose`）+ `RedisSessionRegistry` + `build_session_registry()` |
| `app/observability/otel_tracing.py` | 681 行 | TracerProvider 装配、指标注册表、四类旁路埋点、降级导出 |
| `app/core/tracing_middleware.py` | 86 行 | `OtelTracingMiddleware` + `install_tracing(app)` |
| `app/core/resilience_guard.py` | 183 行 | `sse_guard()` / `@async_guard` / `@guard` / `release_task_resources()` / `check_task_consistency()` |
| `app/bootstrap.py` | 103 行 | 生产装配层：`/health` `/ready` `/metrics` `/observability/status` + 可选 OTel + 可选 MCP HTTP |
| `Dockerfile` | — | **重写**为多阶段（builder venv → runtime），非 root `appuser:10001`，内置 `HEALTHCHECK` |
| `docker-compose.prod.yml` | — | mysql / redis / migrate / backend×2 / mcp-http×2 / otel-collector（profile） |
| `docker-compose.dev.yml` | — | 源码挂载 + `--reload` 热重载 + 独立 mcp-http |
| `deploy/otel-collector-config.yaml` | — | OTLP 4317/4318 + `spanmetrics` connector + Prometheus 8889 |
| `.github/workflows/ci-test.yml` | — | 5 job：quality → test×4 分片 → coverage → scripts → docker |
| `pyproject.toml` | — | ruff（line-length 140，**不启用 `UP`** + 22 个基线文件精确豁免）+ coverage 配置 |
| `requirements-observability.txt` | — | 仅 SDK + OTLP exporter + semantic-conventions，不引 `instrumentation-*` |
| `scripts/verify_deploy_manifest.py` | — | 26 项部署不变量静态校验（无 Docker 也能跑） |
| `tests/test_w6_session_backend.py` | 14 用例 | 双 registry 共享 `FakeServer`、跨副本 `drop` 不复活、队列哨兵 |
| `tests/test_w6_otel.py` | 13 用例 | 节点 span 去重、`graph.run` 深度守卫、指标聚合、开关快路径 |
| `tests/test_w6_resilience.py` | 21 用例 | 心跳/空闲/超时/异常/取消五路径、资源释放、一致性校验 |
| `tests/test_w6_bootstrap.py` | 12 用例 | 探针、`/metrics`、lifespan 关停 |
| `tests/test_w6_platform_extra.py` | 40 用例 | 配置派生、导出降级、补丁回滚等边界 |

### 功能

- **多副本 SSE 会话共享**：会话表从进程内 `dict` 外置到 Redis，`GET /sse` 落 Pod-A、`POST /messages` 落 Pod-B 也能命中；
  Pod 重启不丢会话。**事件流生成器零改动**——只换 `session.queue` 的实现（鸭子类型）
- **`SESSION_REGISTRY_BACKEND=memory/redis`**：默认 `memory`，本地测试不依赖外部 Redis；Redis 抖动自动降级进程内表，不返回 500
- **元数据同步客户端 + 队列异步客户端**：注册表接口是 Part1 冻结的同步签名，故元数据走 `redis.Redis`（`socket_timeout=50ms` 有界阻塞），消息队列走 `redis.asyncio`
- **OTel 五层埋点（全旁路）**：HTTP 中间件 / LangGraph `BaseCallbackHandler` / `MCPClient.call_tool` 包装 / `LLMClient.complete` 包装 / SQLAlchemy 引擎事件
- **span 去重**：① 节点 span —— LangGraph 对节点与外层 `RunnableSequence` 各发一次 `on_chain_start`，用活跃节点计数去重；② `graph.run` —— `ainvoke` 内部调 `astream`，用 ContextVar 深度守卫只建一个根 span
- **指标**：`MetricsRegistry` 自实现（counter + deque 直方图 + `total()/percentile()/render()`），不引 `prometheus_client`；
  新增 MCP 调用成功率、P95 时延、token 消耗计数器
- **导出降级**：`_probe_otlp()` TCP 探测（0.4s）→ 不可达且允许降级时切 `ConsoleSpanExporter`；
  `_FallbackExporter` 连续 3 次失败自动切换
- **CI 门禁**：测试失败 / ruff 报错 / 新增模块覆盖率 <85% 阻断 PR；main/tag 触发 GHCR 镜像构建 + 容器 `/health` 冒烟
- **SSE 边界加固**：`sse_guard()` 心跳帧、空闲超时、连接上限、`retry:` 首帧、`event: close` 收尾；
  用**泵任务 + 队列**而非 `wait_for(anext(inner))`，超时不会杀死内层生成器
- **任务取消资源释放**：清 `cancel:{id}` / `events:{id}` / `memory:short:{id}`，**保留 `ckpt:{id}`**（断点是审计资产）
- **多副本一致性校验**：`check_task_consistency()` 校验 thread_id / plan 签名 / next_nodes / interrupt_count / step 滞后

### 修复

- **OTel SDK 导入失败** —— SDK 1.45 把 `sdk.trace.samplers` 改名 `sdk.trace.sampling` → 加双路径 `try/except ModuleNotFoundError` 兼容
- **每个节点产生两个 span** —— LangGraph 对节点与外层 sequence 各发一次 `on_chain_start` → 引入活跃节点计数，内层写占位不开 span
- **`graph.run` 出现两次** —— `ainvoke` 内部调 `astream`，两层 patch 各开根 span → 新增 `_GRAPH_DEPTH_VAR` 深度守卫
- **`metrics.counter_value("mcp_calls_total")` 恒 0** —— 带 label 写入、无 label 读取 key 对不上 → 新增 `total()` 跨 series 求和
- **`histogram_values(name)` 返回 `[]`** —— 同上，`labels=None` 时 key 为空元组 → 改为聚合所有 series
- **跨副本 `drop` 后会话「复活」** —— 本地缓存直读让已关闭会话重新出现 → 删除缓存直读，每次 `HGETALL` + `_touch_remote`
- **`verify_deploy_manifest` V2 误报** —— compose 值是 `${VAR:-memory}`，字符串比较失败 → 加 `_resolve()` 归一化
- **`pyproject.toml` TOML 重复 key** —— `transport_http.py` 出现在两处 `per-file-ignores` → 合并为一条
- **ruff 规则要求改写冻结基线** —— 启用 `UP` 会强改 19 处引号注解 / 16 处 `TimeoutError` 别名 / 7 处 `isinstance` →
  不启用 `UP`，改为 22 个基线文件的精确豁免清单
- **测试 `NameError: create_production_app`** —— 漏 import → 补 `from app.bootstrap import create_production_app`
- **`test_graph_and_mcp_fast_path_after_runtime_toggle` 失败** —— MCP span 用「全量有/无」断言 → 改为前后差额比较
- **ruff F811 `boom` 重定义**（路由函数与局部变量同名）/ **B010** `setattr` 常量属性 / **F841** `heartbeat` 未使用 → 分别改名、改直接赋值、改 `remaining = min(...)`

### 存量改动（仅追加）

- `.env.example` 末尾追加 49 行 W5-P2 配置
- `.gitignore` 末尾追加容器 / 覆盖率 / 评测产物规则
- `Dockerfile` 重写（旧版备份至 `deploy/legacy/Dockerfile.w1-single-stage`）

---

## [W5-Part1] — MCP HTTP 传输 + FR-014 分层记忆（+57 用例）

> 约束：W1~W4 只读。基线 150 → **207 passed**。

### 新增文件

| 文件 | 行数 | 职责 |
|---|---:|---|
| `app/core/w5_config.py` | 147 | W5 专项配置（与 `w2_config.py` 同构，不侵入 W1 `config.py`） |
| `app/mcp/__init__.py` | 9 | 包声明 |
| `app/mcp/transport_http.py` | 744 | HTTP 服务端路由 + SSE 长会话 + HTTP 客户端传输 + 连接池 + 熔断 + 独立进程入口 |
| `app/mcp/http_entry.py` | 54 | 单进程「双传输」入口（主应用 + `/mcp/v1` 共存） |
| `app/memory/__init__.py` | 27 | 包导出 |
| `app/memory/layered_memory.py` | 1001 | 三层记忆 + 相似度召回 + 向量库抽象/兜底 + 图入口包装 |
| `app/schemas/w5.py` | 33 | 记忆管理 API 入参 |
| `app/routers/memory_router.py` | 189 | 记忆管理 API（鉴权保护） |
| `tests/test_w5_mcp_http.py` | 435 | 26 用例（HTTP 传输） |
| `tests/test_w5_layered_memory.py` | 602 | 31 用例（分层记忆） |

### 功能

- **MCP HTTP 传输（第二套）**：`HttpTransport` 实现 W2 的 `MCPTransport` 抽象，因此 `MCPClient` / `MCPClientPool`
  **一行不改**就能跑 HTTP；服务端只调 `MCPServerApp.handle`，**4 个工具实现零改动**；stdio 与 HTTP 可同时服务同一实例
- **端点**：`GET /mcp/v1/sse`、`POST /mcp/v1/messages`、`POST /mcp/v1`（一次性调用）；`?server=` 支持单进程多 Server 路由
- **连接池 / 超时 / 熔断**：`MCP_HTTP_*` 参数化，401 分类重试，熔断打开后快速失败
- **FR-014 三层记忆**：短期 L1（Redis 24h）/ 中期 L2（压缩摘要落 MySQL `scope='mid'` 30 天）/
  长期 L3（Reviewer ≥3 分归档，向量化或本地关键词兜底）
- **注入链路不侵入内核**：复用 W2 预留的 `TaskState.memory_hits` + 提示词「长期记忆（参考，可裁剪）」插槽
- **管理 API**：`GET/POST/DELETE /api/v1/memories`（鉴权 + 用户隔离）
- **开关默认 false**：`MCP_HTTP_ENABLED` / `ENABLE_LAYERED_MEMORY` —— 这是 207 条基线零回归的前提

---

## [W4] — 评测体系 + 种子校验 + KB 适配层 + 压缩 A/B + Demo（脚本，0 用例）

> 基线保持 **150 passed**（W4 交付物为脚本与数据集，不改业务逻辑）。

### 新增文件

| 文件 | 行数 | 职责 |
|---|---:|---|
| `eval_datasets/分层难例评测集-manifest.json` | 512 | 30 条分层难例（L1~L6），含 `question / ground_truth / risk_level / case_type / expected_gmv / expected_keywords / needs_notify` |
| `scripts/w4_common.py` | 1021 | 嵌入式 harness（SQLite+WAL / fakeredis / InMemoryTransport / InMemorySaver / ASGITransport）、`TaskApi`、指标函数、`JudgeClient`、输出工具 |
| `scripts/eval_runner.py` | 571 | 评测跑批：提交 → 轮询终态 → 采集指标 → 聚合 → 出 csv/json/md |
| `scripts/verify_seed.py` | 711 | 种子数据 3 条硬断言 + 20 个随机断点抽样恢复校验 |
| `scripts/ab_compression.py` | 611 | 压缩 A/B：微基准 + 端到端图级对照 + 指标汇总表 + markdown 报告 |
| `scripts/demo_three_flows.py` | 823 | 三条演示链路（审批闭环 / 驳回重规划 / 取消冻结） |
| `app/tools/kb_adapter.py` | 475 | `CircuitBreaker` / `MallKbProvider` / `ResilientKbProvider` / `build_kb_provider` / `default_mall_dsn` |
| `docs/checklists/W4-自测核对清单.md` | A~J 段 | 九段逐条核对 |
| `docs/reports/AB实验报告模板.md` | 126 | 通用 A/B 报告模板 |
| `docs/delivery/W1-W4项目总览与总结.md` | 282 | 目录总览 + 已完成功能 + P2 清单 |

### 功能

- **30 条分层难例评测**：L1~L6，指标含成功率 / 重规划率 / GMV 四项 / 分节点 token / P95 / faithfulness / 召回；
  超时重试 2 次、失败标 `timeout` 不打断批量；生成/评审模型分离，无 key 降级本地规则
- **种子数据硬校验**：3 条硬断言（SQL 只读 / KB 命中断货 / notify 无审批拦截）+ 20 个随机断点抽样
  （不从头重跑 / 计划签名不变 / 挂起-放行成对）→ **26/26 · SEED VERIFIED**
- **KB 适配层**：HTTP 主路 + 熔断 + mall 只读兜底，**不回退 mock 语义**（兜底也走真实 SQLite 查询）
- **压缩 A/B**：微基准 + 端到端 + 16 项指标对照，压缩率 **73.2%**，人读报告 1 份
- **Demo 三链路**：审批闭环 / 驳回重规划 / 取消冻结 → **3/3 链路、21/21 步骤**

### 产物

`eval_output/` 下 10 份运行期产物（json/csv 可重建不入库；4 份人读 md 已归档至 `docs/reports/`）。

---

## [W3] — SSE 进度流 + 人机审批断点续跑 + 轨迹回放 + 最小 MVP 前端（+18 用例）

> 基线 132 → **150 passed**。

### 新增文件

| 文件 | 行数 | 职责 |
|---|---:|---|
| `app/core/w3_config.py` | 87 | `SSE_*` / `STREAM_*` / `TRACE_*` / `APPROVAL_*` / `CANCEL_*`，`w3` 单例 |
| `app/core/w3_errors.py` | 91 | W3 错误码 + `TERMINAL_STATUSES` / `RESUMABLE_STATUSES` + `normalize_status()` |
| `app/schemas/w3.py` | 242 | SSE 事件契约 / `ResumeRequest` / `InterruptContext` / `ApprovalInbox*` / `TraceStep` / `TraceReplayData` / `SnapshotData` |
| `app/services/event_bus.py` | 276 | **W1 占位 → 实现**：Redis Stream `XADD MAXLEN ~` + `EXPIRE` + `XREAD` 游标；`auto` 模式自动降级内存队列 |
| `app/services/stream_context.py` | 265 | ContextVar `StreamContext` + `emit()` 统一信封 + `mcp_event_hook` + `InstrumentedLLM` |
| `app/services/trace_service.py` | 205 | 轨迹落 `task_executions`（复用 W1 表）；`_safe_payload` 先脱敏再截断 |
| `app/services/interrupt_service.py` | 327 | `collect_interrupts`（读快照，禁 `__interrupt__`）/ `ensure_resumable` / `clear_interrupts` / `expire_pending_approvals` |
| `app/services/cancel_service.py` | 105 | `CancelSignal`（Redis 标记 + TTL + 一次性降级） |
| `app/services/snapshot_service.py` | 132 | `snapshot()` 纯 JSON 化 / `checkpoints()` 按 step 升序 / `replay()` |
| `app/services/stream_service.py` | 649 | **核心**：版本协商 v3→v2→v1、`StreamingTaskService(TaskService)`、事件翻译、`sse_event_frames()` |
| `app/services/w3_runtime.py` | 172 | `create_w3_runtime()` 装配（MCP 池 + InstrumentedLLM + 幂等审批建单器） |
| `app/routers/tasks_ext.py` | 287 | `/tasks/stream`、`/{id}/stream|interrupt|resume|cancel|snapshot|trace` |
| `app/routers/approvals.py` | 49 | `GET /approvals`（收件箱）、`GET /approvals/{id}` |
| `frontend/index.html` + `src/*`（11 文件） | 1454 | Vue3 CDN 最小 MVP：提交 / 收件箱 / 详情 / 审批弹窗 / DAG / 事件控制台 |
| `tests/test_w3_e2e.py` | 777 | 18 用例 |
| `scripts/seed_w3_demo_users.py` | 155 | 幂等建 3 个演示账号（`admin_user` / `approver_user` / `viewer_user`） |

### 功能

- **SSE 四类事件**：`state_update` / `tool_call` / `interrupt` / `complete`，统一信封 + 协议版本协商（默认 v3）
- **断线重连**：`Last-Event-ID` 游标去重，前端最多 6 次重连
- **审批断点续跑**：`human_gate` interrupt → `Command(resume=...)`，**不从头重跑**；审批单幂等（节点重跑不重复建单）
- **挂起判定红线**：只认快照 `interrupts`，禁止 `__interrupt__`
- **取消**：挂起态立即收口 / 运行态节点边界收口；**冻结 ≠ 删除**（保留快照）
- **轨迹回放**：step 序列 + checkpoint 时间轴 + interrupt 现场三 tab

---

## [W2] — LangGraph 编排内核 + MCP 工具层 + 上下文工程（+120 用例）

> 基线 12 → **132 passed**。

### 新增文件

**配置与 Schema**

| 文件 | 职责 |
|---|---|
| `app/core/w2_config.py` | W2 全量可调参数（预算 / 白名单 / 超时 / 阈值） |
| `app/schemas/agent_io.py` | Agent 入出参契约（Planner / Executor / Review / Summary） |
| `app/schemas/mcp.py` | JSON-RPC 信封 + 4 工具入出参 + `McpErrorCode` |
| `app/schemas/context.py` | `TokenEstimate` / `CompressionConfig` / `SlimmingConfig` / `ContextBuildResult` |
| `app/schemas/graph_state.py` | **扩展**：`add_messages` / `merge_dict` / `merge_token_ledger` 等 reducer |
| `app/schemas/orm_models_ext.py` | `mock_messages` 表（notify Mock 落库），不动 W1 的 7 张表 |

**Agent 层 `app/agents/`**：`llm.py`（OpenAI 兼容 + Mock + 主备降级门面）、`structured.py`（容忍围栏/废话的 JSON 提取 + 修复重试）、
`prompts.py`（4 个版本化 system prompt）、`common.py`（State 容错读取 + 记账 + 风险判定）、`planner.py`、`executor.py`、
`human_gate.py`、`reviewer.py`、`replan.py`、`summarizer.py`、`notifier.py`、`graph.py`（5 个纯函数路由 + `build_graph` + `create_runtime`）

**MCP 工具层 `app/tools/`**：`mcp_stdio.py`（`MCPServerApp` + stdio 主循环）、`mcp_client.py`（`StdioTransport` / `InMemoryTransport` /
`MCPClient` / `MCPClientPool`）、`mcp_servers/sql_query.py`（五道安全闸门）、`chart_render.py`（字段白名单 + 进程池 + 超时）、
`kb_search.py`（Mock 样本 + HTTP Provider + 降级）、`notify.py`（前置 `APPROVED` 校验，恒不出网）

**服务层**：`context_service.py`、`task_service.py`、`checkpoint_service.py`、`plan_service.py`、`approval_service.py`、
`eval_gateway.py` / `rag_gateway.py`

**其他**：`app/routers/tasks.py`、`app/utils/tokens.py`、`app/utils/dag.py`、`app/db/checkpointer.py`（改写为委托 `checkpoint_service`）、
`tests/test_planner.py`(12) / `test_executor.py`(12) / `test_replan.py`(8) / `test_graph_routing.py`(17) /
`test_context_service.py`(17) / `test_mcp_client.py`(15) / `test_sql_query_security.py`(35) / `test_w2_e2e.py`(4)

### 功能

- **7 节点图**：`planner → (human_gate) → executor → reviewer → (replan) → summarizer → notifier`
- **SQL 五道闸门**：只读账号 / sqlglot AST / 表白名单 / 强制 LIMIT / EXPLAIN 扫描量估算
- **上下文工程**：分层预算、历史压缩（保留最近 N 轮 + 摘要 + 数字白名单）、分工具结果瘦身
- **双 Mock 开关**：`MOCK_EXTERNAL_RAG` / `MOCK_EXTERNAL_EVAL`，离线零外部调用
- **Replan 上限 2 次**，超限 `REPLAN_EXHAUSTED`；审批驳回写 `replan_context` 触发重规划

---

## [W1] — 工程骨架与安全底座（12 用例）

### 新增文件

`app/main.py`（FastAPI 入口）、`app/core/`（`config` / `context` / `deps` / `exceptions` / `logging` / `redis_client` /
`security` / `trace`）、`app/db/`（`base` / `session` / `checkpointer` / `types`）、`app/routers/`（`api_router` / `auth` / `health`）、
`app/schemas/`（`auth` / `common` / `enums` / `orm_models`）、`app/services/auth_service.py`、
`app/utils/`（`ids` / `masking` / `time` / `tokens`）、`alembic/` + `alembic.ini`、`tests/conftest.py` + `tests/test_auth.py`、
`Dockerfile`、`docker-compose.yml`、`requirements.txt` / `requirements-dev.txt`、`pytest.ini`、`.env.example`、`.gitignore`、`.dockerignore`

### 功能

- 分层目录骨架（routers / services / agents / tools / schemas / core / db / utils / scripts / frontend）
- 8 张 MySQL 表 ORM 模型 + Alembic 迁移体系
- **双 Token 鉴权**：access 2h / refresh 7d + Redis 黑名单 + 轮换 + bcrypt 哈希
- `require_role` 角色校验依赖（admin / approver / viewer）
- TraceID 中间件 + JSON 结构化日志（trace_id 贯穿全链路）
- 全局异常处理器 + 统一响应包 `{code, message, data, trace_id}`
- 健康检查三档（liveness / readiness / 运行态摘要）
- docker-compose 单机栈（mysql + redis + backend）

---

## 版本对照

| Tag | 时间 | 阶段 | 用例 | 说明 |
|---|---|---|---:|---|
| `v1.0.0` | 2026-10-21 | W1~W5 + P2 收尾 | **412** | 功能冻结，交付归档 |
