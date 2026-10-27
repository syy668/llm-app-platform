# AgentFlow 开发开工前确认单

| 项 | 内容 |
|---|---|
| 文档目的 | 后端开发开工前自检与范围裁剪确认（不含任何业务实现代码） |
| 依据 | 《AgentFlow需求设计文档（PRD）》v1.0（2026-10-20）＋ 本地环境全量核查（2026-10-21） |
| 核查方式 | 对项目一/项目二实际代码与运行环境逐条取证（接口路由、鉴权源码、.env 配置、本机服务状态） |
| 总体结论 | **无阻断性障碍，可以开工**。A-04 / A-05 已具备；A-01 / A-02 / A-06 部分具备，按 PRD 原降级策略执行；A-03 按本次约束在仓内复现。W1 存在 2 项环境前置任务（安装 Docker Desktop、部署真实 Redis 实例） |

---

## 1. 假设清单逐条现状评估（A-01 ~ A-06）

> 现状取证说明：项目二 EvalAgent 无独立后端（经《EvalAgent项目梳理报告》与代码核查确认：EvalAgent = evalagent_vue 前端 + 项目一 FastAPI 内的 `/api/v1/eval/*` 模块，8000 端口统一出口）。下表"项目二接口"均指该模块。

| 假设编号 | 假设原文（摘要） | 我当前现状 | 启用降级方案（严格复用 PRD 降级策略） |
|---|---|---|---|
| A-01 | 项目一已暴露 `POST /api/v1/kb/search`（BM25+向量 RRF+Rerank），支持 `tenant_id/query/top_k`，返回 chunks 及得分 | **部分具备**。检索底座完整存在：`app/rag/retriever.py::hybrid_retrieve`（BM25+向量双路召回、RRF 融合 _RRF_K=60、rerank_threshold 重排）。但假设的独立检索端点**不存在**，现有 `POST /api/v1/kbs/{kb_id}/query` 是"检索+LLM 问答"复合端点（多一次 LLM 调用，返回 `answer + sources`）；且项目一**无 tenant 概念**，隔离单位是 kb_id + 用户权限 | 按 PRD 调整方式第①条：**在项目一增加薄适配层**——新增只读 search-only 端点（内部直调 `hybrid_retrieve`，返回 chunks+score，跳过 LLM 生成），改动量约一个路由函数；"tenant=agentflow"落地为项目一内**新建专属知识库 + 专用服务账号**。第②条（kb-search 直连项目一 services 层同仓部署）因本次约束 1（独立仓库、仅 HTTP）**排除**。MVP 开发期 `MOCK_EXTERNAL_RAG=true` 走内置 mock chunks（含 W41 周报断货伏笔样本），核心链路不依赖项目一在线 |
| A-02 | 项目二 EvalAgent 提供 `POST /api/v1/eval/runs`（创建评测）、`GET /api/v1/eval/runs/{id}`（取结果），支持自定义数据集与异步轮询 | **部分具备**。端点全部存在且语义匹配：`app/api/v1/eval.py` 含 `POST /datasets`、`POST /datasets/{id}/items`、`POST /runs`、`GET /runs/{id}`、`/badcases`、`/runs/compare`、`/stratum_metrics`，daemon 线程异步执行 + 轮询取结果。但缺两块：① 无"回调被测系统"机制（PRD 10.3 第 2 步的 `POST /tasks?sync=true` 回调不存在，评测对象仅限项目一 RAG QA）；② 评测输入为 RAG QA 样本格式，不能直接对"报告产物"打分 | 按 PRD 调整方式：**AgentFlow 内嵌轻量评测 runner**——30 条评测集由本地脚本逐条同步调 AgentFlow 任务接口，按 golden_plan/golden_tools 本地断言计算全部指标（完成率/规划 F1/工具准确率/延迟/Token），**项目二仅作结果展示端**（dataset 登记与结果登记，看板数据源改为本地 `eval_runs` 表）。Reviewer 的 faithfulness 默认走 FR-013 验收标准 2 自带的降级（本地规则校验：引用存在性 + 数字与工具结果一致），时间富余再验证"构造 (goal, 报告, chunks) 样本提交项目二 RAGAS"的适配 |
| A-03 | 项目二双 Token 轮换 + Redis 黑名单鉴权可抽为共享库 `auth-kit`（本地 wheel 安装），各系统独立 users 表 | **暂不具备（交付物）/ 逻辑已具备**。无 auth-kit wheel 包；但该鉴权逻辑已在项目一 `app/core/security.py` 完整实现并经双 Token 改造：access 会话 token（随机串 + HMAC-SHA256 签名，滑动过期）、refresh 一次性轮换 `rotate_refresh_token`、`REFRESH_BLACKLIST_PREFIX` Redis 黑名单、bcrypt 密码哈希 | 按 PRD 调整方式（复制集成、对外接口签名不变）＋ 本次约束 3 收窄：**不引 wheel、不复制源码文件，在 AgentFlow 仓内复现同款逻辑**——接口签名对齐（`POST /auth/login`、`POST /auth/refresh`、`POST /auth/logout`、Bearer 头、统一响应包 `{code,message,data}`），token 结构 / 一次性轮换 / 黑名单语义逐条对齐，TTL 按 PRD NFR-S4（access 2h / refresh 7d）在 settings.py 外置；以 W1 的 12 条鉴权用例（登录/刷新轮换/注销拉黑/越权 403）保证行为等价 |
| A-04 | 主 LLM DeepSeek-V3（32K 上下文），备用 SiliconFlow Qwen2.5-72B，均支持 Function Calling 与 JSON 输出 | **已具备**。项目一 `.env` 已配置 `LLM_API_KEY`（deepseek-chat @ api.deepseek.com，OpenAI 兼容协议）与 SiliconFlow key（现用于 embedding，同账号可开通 chat 模型）；两平台均支持 FC 与 JSON 输出。注意项目一已有"并发上限 1~10 防 429"的实战配置，评测集并发跑批时沿用并发 ≤2~3 限制 | 按 PRD 调整方式：deepseek-chat 实际窗口大于 32K，**第 9 章 Token 预算表仍按 32K 保守执行不放宽**（真实压缩压力 + 成本可控 + 为 A/B 实验留干净对照）；仅当评测集任务因窗口不足失败时，按实际模型窗口等比例放大 Executor / Summarizer 预算并记录变更 |
| A-05 | Demo 数据截止 2026-10-18；"上周"固定解释为 2026-10-12 ~ 2026-10-18（W42） | **已具备（时点有效）**。今日 2026-10-21，"上周"恰为 W42，与 PRD 一致；数据生成器尚未开发（AgentFlow 目录现有文件仅 PRD 一份），时间窗天然可参数化，无存量数据迁移成本 | 按 PRD 调整方式：生成器把**数据截止日 / 埋因周 / 噪声窗口 / seed=42 全部做成参数**（CLI 参数 + settings.py 默认值，满足 NFR-M3 零硬编码）；若 demo 时点后移，整体平移时间窗重跑生成器并平移评测集标注即可，`verify_seed.py` 断言随参数联动，不改代码 |
| A-06 | 单机 16G 内存；MySQL / Redis / Chroma 与项目一、二共用实例但独立 database | **部分具备**。① 内存实测 16G ✅；② MySQL：本机 Windows 服务 MySQL80 **Running**（项目一 `kb_agent` 库在其上，127.0.0.1:3306），新建 `agentflow` 独立库可行 ✅；③ Redis：6379 端口**未监听**——项目一当前以 `FAKE_REDIS=true` 进程内内存模式运行，本机无真实 Redis 实例 ❌；④ Docker：**未安装**（docker 命令不可用），Compose 一键起（NFR-M4）前置缺失 ❌；⑤ Chroma：项目一仓内本地目录 `chroma_db`，受独立仓约束 AgentFlow **不能直连文件**，只能经项目一 HTTP 间接访问 | 按 PRD 调整方式（Compose 内独立容器）＋ 2 项环境补齐前置任务：**W1 任务①安装 Docker Desktop**（承载 mysql / redis / backend / 4 个 MCP Server 容器）；安装失败则降级为本机 MySQL80 + WSL/Windows Redis + 本地进程启动，Compose 文件照常交付（仅演示环境形态不同）。**Redis 必须是真实实例**（Stream 事件重放 / 黑名单 / checkpointer 均依赖跨请求持久化，不复用项目一 FAKE_REDIS 进程内实例）。Chroma 不新建不直连：kb 检索经 A-01 薄适配层 HTTP 访问；长期记忆向量库（FR-014，本就 P1）W4 再评估经项目一灌入的可行性，不可行则降级 MySQL 关键词检索 |

---

## 2. 功能落地范围最终确认

### ✅ 必须落地（MVP，必须全部完成，可演示核心闭环）

| 编号 | 功能名称 | 备注（范围裁剪点） |
|---|---|---|
| FR-001 | 任务接入 | 目标校验（空/超 500 字/注入黑名单）+ trace_id 生成 + task.created 事件 |
| FR-002 | Planner 任务规划 | PlanOutput 结构化校验 + 2 次修复重试 + 时间词归一化（W42） |
| FR-003 | Replan 重规划 | 仅重排受影响分支；replan_count 上限 2 |
| FR-004 | Executor 执行 | 工具选择 / 指数退避重试 3 次 / 降级矩阵 / 8 次循环上限 |
| FR-005 | MCP Server：sql-query | AST 白名单 + 只读账号 + 强制 LIMIT（安全红线，见第 3 节） |
| FR-006 | MCP Server：chart-render | ECharts 渲染 + 独立进程池 + Markdown 表格兜底 |
| FR-007 | MCP Server：kb-search | MVP 默认 Mock；真实联调走 A-01 薄适配层；降级标注"引用缺失" |
| FR-008 | MCP Server：notify（Mock） | 恒 Mock 不出网 + APPROVAL_MISSING 客户端侧闸门 |
| FR-009 | SSE 流式推送 | Redis Stream + Last-Event-ID 重放 + 15s 心跳 |
| FR-010 | 上下文工程 | 预算 / 压缩 / 瘦身 / 结构化校验（A/B 实验的开关与记账在此实现） |
| FR-011 | 人机协同（批准 / 驳回重规划） | **仅验收标准 1~3**；interrupt 挂起 + 驳回意见触发 Replan + 7 样本 100% 拦截。验收标准 4（人工改写）归 P1 |
| FR-013 | Reviewer 质量闸门 | 5 维评分 + faithfulness 本地规则降级（A-02 裁剪后默认路径）+ REVIEW_SKIPPED 放行 |

> 基础设施说明：LangGraph checkpointer（RedisSaver 快照）与 interrupt 恢复机制是 FR-011 的必要基础设施，**属 MVP 必做**；FR-012 的完整验收项（进程崩溃后 resume、24h 审批过期、Token 恢复不重复计算）按 PRD 优先级归 P1，但因其被 W3 里程碑与 demo 剧本点名，实际按"P1 最高优先"执行（见下）。

### ⚠️ P1 选做（时间充裕再实现，完不成就作为后续迭代写进 README，不阻塞 Demo）

| 优先级 | 编号 | 功能名称 | 处置 |
|---|---|---|---|
| **优先做（排期保住）** | FR-012 | 断点恢复 | W3 里程碑验收硬性点名（kill 进程后 resume、子任务零重跑），demo 剧本含"断点恢复"桥段，不可放 |
| **优先做（排期保住）** | FR-015 | 轨迹回放 | W3 里程碑列明；面试演示与 Debug 价值最高的 P1 项，后端 trace 接口 + 前端回放组件 |
| 次优先 | FR-016 | 评测看板 | 数据源按 A-02 降级改为本地 eval_runs 表 + 内嵌 runner 结果（趋势图 / A/B 对比柱状图）；项目二真实对接作为增强项 |
| 次优先 | FR-011 验收 4 | 人工改写子任务参数 | 后端记录 diff（event=HUMAN_EDIT）+ 按新参数续跑；若前端时间不足，只做后端接口 |
| **可放弃 → README 后续迭代** | FR-014 | 分层记忆 | 跨仓 Chroma 依赖（经项目一 HTTP 灌入联调成本高）+ O-1 阈值为开放问题，单人收益/工期比最低；README 写明"设计已完成（PRD 9.4 / 6.6），列为迭代一" |
| **可放弃 → README 后续迭代** | — | P1 子任务编辑接口 | `POST /tasks/{id}/subtasks`、`PATCH/DELETE /subtasks/{sid}`（低代码画布依赖项，砍掉后画布维持只读） |

### ❌ P2 & 非目标：完全不编码实现，仅文档占位

复制 PRD 第 14 章核心要点：

| # | 不做 | 说明 |
|---|---|---|
| 1 | 多租户 SaaS 化、计费、组织架构管理 | 单租户演示系统；记忆与知识库仅以 tenant 字段预埋扩展位 |
| 2 | 通用低代码画布编辑器 | 画布仅 DAG 可视化展示 + P1 有限的手动增删改子任务 |
| 3 | 对接真实飞书 / 邮件 / 支付 | notify 一律 Mock（落库 + 日志，不出网），UI 明示"Mock 渠道" |
| 4 | 重写鉴权体系 | 复用（仓内复现）项目二同款双 Token 轮换 + Redis 黑名单逻辑，不另起炉灶 |
| 5 | 移动端与国际化 | 仅桌面 Web 中文 |

P2 占位（仅列不设计）：多租户隔离、Agent 技能市场、任务模板库、Webhook 外部触发、自然语言改写 DAG 的对话式编辑、MCP HTTP 传输。

---

## 3. 开发重要约束备忘录

1. **Mock 开关说明**
   - `MOCK_EXTERNAL_RAG=true`：kb-search MCP Server 不发 HTTP 调项目一，直接返回内置 mock chunks（预置 W41 周报"SKU-10086 断货风险"伏笔样本，保证归因 demo 可复现）；`false` 时经项目一薄适配层真实检索（A-01）。
   - `MOCK_EXTERNAL_EVAL=true`：Reviewer 的 faithfulness 与评测看板不调 `/api/v1/eval/*`，使用本地规则评分与本地评测 runner；`false` 时对接项目二 dataset / runs（A-02）。
   - 何时开 Mock：日常开发、CI 回归、评测集跑批（可复现、不依赖外部服务在线、不烧项目一 LLM 配额）——**默认 true，clone 即跑**。
   - 何时真实联调：W1 末（项目一周报灌入 + 检索适配层联调）、W4（项目二 dataset/runs 登记与结果回写联调）；联调窗口显式置 false 并记录。
   - 两开关均走 `.env` → settings.py（NFR-M3 配置外置），适配逻辑收敛在 kb-search 与 eval_gateway 单一适配层（对应 R-7：接口变更只改适配层）。
2. **MCP 实现约束**：MVP 只实现 **stdio 传输**，仅 `initialize` / `tools/list` / `tools/call` 三个方法（JSON-RPC 2.0，对应 R-2 的最小集）；HTTP 传输放 P2 不做。工具 Schema 经 `tools/list` 动态发现注册为 Function Calling，零硬编码。
3. **鉴权约束**：不引入外部 wheel 共享库；在 AgentFlow 仓内复现双 Token 轮换鉴权（access 2h / refresh 7d、refresh 一次性轮换、Redis 黑名单、bcrypt），逻辑对齐项目二同款实现（项目一 `app/core/security.py`），但**不复制源码文件**；对外接口签名保持一致，12 条鉴权用例保证行为等价。
4. **SQL 安全红线**：sql-query 工具必须同时满足——① 数据库连接使用**只读账号**（仅 `SELECT` 权限 on mall 库）；② sqlglot **AST 白名单校验**，语句类型仅 SELECT，禁止 DML / DDL / 多语句 / 注释；③ 表名白名单（第 11 章三张业务表 + 维表）；④ 无 LIMIT 自动追加、max_rows ≤1000；⑤ 预估扫描 >100 万行返回 SQL_TOO_LARGE。验收：50 条恶意语句样本 100% 拒绝且零写入（NFR-S1）。
5. **LangGraph 约束**：interrupt **唯一挂起点为 human_gate 节点**（`interrupt({approval_id, reason, preview})`，恢复值为审批决策）；executor 并行子图与 replan 循环一律不挂起；checkpointer 采用 **RedisSaver 快照**（每次节点转移后写入，key=`ckpt:{task_id}`，TTL 72h），供审批恢复与断点恢复（FR-012）共用。

---

## 4. W1 第一周交付物核对清单（对应 PRD 第 12 章里程碑 W1）

| # | 交付物 | 【完成后验证方式】 |
|---|---|---|
| 1 | 工程骨架：分层目录（routers / services / agents / tools / schemas / core）+ FastAPI 入口 + settings.py 配置外置 | `uvicorn` 启动无报错，`GET /health` 返回 200；ruff 检查无硬编码魔数（NFR-M3）；目录结构与 PRD 5.1 分层一致 |
| 2 | 鉴权模块：仓内复现双 Token 轮换 + Redis 黑名单 + `require_role` 角色拦截 | **12 条鉴权 pytest 用例通过**（登录、刷新轮换旧 token 失效、注销后黑名单拦截、viewer 越权 403 并记安全日志等，对应 NFR-S2/S4）——W1 验收标准原文 |
| 3 | 全局异常处理中间件 + TraceID 中间件（trace_id / span_id，JSON 日志） | 构造异常请求，响应为统一 `{code, message}` 结构且日志含 trace_id；任意一条日志可按 trace_id 聚合（NFR-M2） |
| 4 | MySQL 7 表迁移：users / tasks / subtasks / task_executions / approvals / memories / eval_runs | 迁移脚本幂等重跑通过；逐表字段与 PRD 第 6 章定义比对一致（含索引与 ENUM）；`agentflow` 库独立于项目一 `kb_agent` 库（A-06） |
| 5 | 模拟数据生成器：mall 库 5 表约 52 万行 + W42 双因素埋因 + seed=42 + 时间窗参数化（A-05） | `verify_seed.py` 自动执行 3 条断言 + 20 个抽样断点，**输出 `SEED VERIFIED`**——W1 验收标准原文；用只读账号二次确认生成过程零写权限需求外的变更 |
| 6 | Docker Compose：mysql / redis / backend 三服务 + 健康检查 + 只读账号初始化 SQL | **Compose 冷启动健康检查全绿**（W1 验收标准原文，目标 ≤3min 为 NFR-M4）；前置：本机安装 Docker Desktop（A-06 补齐任务①）；Redis 为真实实例而非 FAKE_REDIS |
| 7 | 项目一周报灌入：20 份周报（W23~W42，W41 含断货伏笔）经上传接口灌入项目一专属知识库 + 服务账号 | 灌入脚本对项目一 `POST /kbs/{kb_id}/documents` 上传成功 20/20；项目一 `query` 接口抽查"库存预警"命中 W41 周报；**依赖项目一在线 + 服务账号**，若适配窗口冲突则顺延至 W4 前补做并临时依赖 Mock（不阻塞 W2） |
| 8 | `.env.example` + Mock 开关默认值（MOCK_EXTERNAL_RAG / MOCK_EXTERNAL_EVAL = true） | settings 加载单测通过；在项目一/二均未启动的机器上，backend 可独立启动且健康检查绿（约束 1"核心链路独立运行"的直接验证） |

---

## 5. 风险重点提醒（针对单人开发，提取自 PRD 第 13 章）

| # | 风险 | 对单人开发的重点警示与应对 |
|---|---|---|
| **R-4（最高优先）** | 4 周工期单人并行度过载 | 严格 MVP / P1 切分：W4 只做对接与打磨，不新开功能；P2 全部占位不承诺；**每周预留 0.5 天缓冲**；FR-014 等低性价比 P1 果断进 README 而非硬撑 |
| R-2 | 自研 MCP 协议子集工作量超预期（W2 延期） | 只实现 initialize / tools/list / tools/call 三方法 + stdio；若 W2 中段仍未打通，kb-search 临时降级直连 HTTP（接口签名不变，验收不受影响）；Mock 开关保证不阻塞其余链路 |
| R-1 | LLM 规划输出不稳定（JSON 不合规、粒度漂移） | Pydantic 校验 + 带错误回喂的修复重试 2 次 + few-shot 2 例 + temperature 0.2 + 备用模型（SiliconFlow）自动切换；评测集 G-1/G-2 不达标时优先调 prompt 而非加代码 |
| R-3 | SSE 经代理/浏览器空闲断连、事件丢失 | 15s 心跳 + Last-Event-ID + Redis Stream 重放（保留 24h / MAXLEN≈10000）+ 前端 id 去重；**本地开发用 EventSource 直连，勿经热重载代理**，避免演示期诡异断连 |
| R-5 | 上下文压缩丢失关键数字 → 归因结论错误 | 摘要模板字段白名单保数值 + 压缩前后 Token/字段核对 + Reviewer consistency <3 分触发护栏回调（保留轮数 3→4 重跑） |
| R-6 | LLM 对埋因数据"过度归因"（编造未注入原因） | 评测集 expect 断言根因命中双因素；Reviewer 仅依据工具证据评分；prompt 显式"禁止引入无证据归因" |
| R-7 | 与项目一/二联调接口变更（A-01/A-02 降级已触发） | 本次核查已确认两处接口与 PRD 假设不一致，**所有外部调用必须收敛在 kb-search / eval_gateway 单一适配层**，联调变更只改适配层 + Mock 数据同步更新 |
| O-1 | 记忆晋级阈值（importance≥3）与注入相似度（0.75）为经验值 | 参数进 settings.py；FR-014 已裁剪为可放弃项后，此项自动降级为 README 记录的开放问题，不占 W4 时间 |
| **E-1（本次核查新增）** | 本机未安装 Docker Desktop（A-06） | W1 第一个工作日安装验证；失败则走降级路径（本机 MySQL80 + WSL Redis + 本地进程），Compose 文件仍交付，保证 README 环境说明两种模式都写清 |
| **E-2（本次核查新增）** | 本机无真实 Redis 实例（项目一用 FAKE_REDIS） | AgentFlow 的 Stream / 黑名单 / checkpointer 依赖持久化 Redis，**W1 必须落实独立实例**（Compose 内 redis:7 或 WSL）；禁止为省事复用 FAKE_REDIS 语义开发，否则断线重放与断点恢复的验收在真实环境下会失效 |

---

**确认单结束**。A-01/A-02/A-06 降级方案与 P1 裁剪（FR-014 进 README、FR-012/FR-015 保排期）确认后，即可进入 W1 开发排期。本文档不含任何业务实现代码。
