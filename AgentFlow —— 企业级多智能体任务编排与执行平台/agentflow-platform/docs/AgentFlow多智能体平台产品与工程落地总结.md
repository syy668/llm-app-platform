# AgentFlow 多智能体平台产品与工程落地总结

> 面试速记手册 / 简历底稿。所有结论均可翻源码验证，括号内为代码文件路径（后端根目录 `agentflow-platform/`，前端 `agentflow-web/`）。
> 工程底座：Python 3.12 + FastAPI + LangGraph（带环 StateGraph）+ MySQL + Redis + MCP 协议；**715 个测试全绿基线**（pytest + junitxml 权威统计，2026-10-27）。

---

## 一、真实应用背景与解决的问题

### 1. 为什么不做一个单体大模型 + Function Calling？

**痛点现象**：企业级的"电商大促复盘分析"这种任务，不是一次函数调用能完成的——要拆数、查库、画图、交叉验证、写结论、再自查。单模型循环里跑，三个问题必现：

1. **黑盒不可控**：单循环里 LLM 想到哪步算哪步，没有"当前处于什么阶段、还剩几步"的显式状态，跑偏了无法拦截、无法打回重来。
2. **无法验收**：模型自己说自己做完了，谁来判"做没做对"？没有独立的评审角色，产出质量全凭运气。
3. **崩了就是崩了**：跑到第 6 步连接断开，前 5 步的查询结果全部作废，只能从头再来，token 白烧。

**方案**：把"想、做、审、修"拆成**职责单一、可独立治理的智能体角色**，用带环状态机编排：

- **Planner** 把目标拆成 DAG 子任务（结构化 JSON，坏了自动修复重试）→ **Executor** 逐个调工具执行 → **Reviewer** 按五维打分（format/evidence/consistency/completeness/faithfulness，通过线 3.5 分、拒绝线 2.5 分、置信度下限 0.55）→ 不过线走 **Replan** 带着修改建议回去重做（上限 2 轮，防死循环）→ 过线走 Summarizer 汇总 → Notifier 通知收尾。（`app/agents/graph.py` 装配全部节点与边，`app/agents/planner.py`、`app/agents/executor.py`、`app/agents/reviewer.py`、`app/agents/replan.py`、`app/core/w2_config.py` 里的 `REVIEW_PASS_THRESHOLD/REPLAN_MAX_COUNT`）

**为什么要"多角色"而不是"多提示词"**：因为每个角色需要**不同的治理策略**——Planner 要结构化修复、Executor 要超时/重试/熔断/幂等、Reviewer 要降级路径（LLM 连续 2 次输出不合规自动切本地规则打分，本地权重 0.6 兜底）。单体循环里你只能一个 prompt 塞所有逻辑，治理面根本拆不开。（`app/agents/structured.py`、`app/core/w2_config.py` 的 `REVIEW_SKIP_AFTER_FAILURES=2`）

### 2. 解决了企业落地大模型的哪些致命痛点？

| 痛点 | AgentFlow 的回答 |
|---|---|
| 长链路任务跑一半崩了 | 每步快照进 Redis checkpointer，中断可续跑；终态自动清理不占资源（`app/services/checkpoint_service.py`、`app/services/task_cleanup.py`） |
| AI 陷入死循环互相踢皮球 | 状态机显式建模 + 重规划硬上限 2 轮 + 评审降级本地规则，环永远有出口（`app/agents/graph.py` 的 `route_after_review`、`app/core/w2_config.py`） |
| API 额度被无限重试刷爆 | 三层防线：单节点 token 预算（planner 6000/executor 8000/reviewer 4000/summarizer 5000，超限抛 `TOKEN_BUDGET_EXCEEDED` 直接中断执行图）→ 用户级每日 token 熔断 → 工具级熔断器（`app/core/w2_config.py` 的 `token_budgets()`、`app/core/daily_quota.py`、`app/core/circuit_breaker.py`） |
| 工具重试导致重复副作用 | (task, tool, args) 三元组幂等缓存，重试命中缓存直接返回旧结果（`app/tools/tool_governance.py` 的 `IdempotencyStore`、`app/tools/mcp_client.py` 的 `call_tool` 幂等分支） |
| LLM 输出不合规整体报废 | JSON 提取容忍 markdown 围栏/前后废话/残缺括号自动补全，逐级修复候选重试（`app/agents/structured.py`） |
| 高危动作无人把关 | human_gate 唯一 interrupt 挂起点，审批通过才继续（见第四节） |

---

## 二、系统全貌与核心功能（业务流转链路）

### 一次复杂任务的完整流转（大白话）

用户在工作台输入"分析这次大促的 GMV 和退款情况，出对比图和趋势图"——

1. **memory_recall**：先去长期记忆库做**语义检索**（本地 embedding + 余弦，阈值 0.55），把"以前跑过的大促任务结论"注入规划上下文，Planner 不做重复探索。（`app/agents/context_steps.py`、`app/memory/layered_memory.py`）
2. **Planner** 把目标拆成 DAG：取数(sql-query) → 交叉分析(kb-search) → 画图(chart-render) → 汇总。输出强校验 JSON，坏了自动修复。（`app/agents/planner.py`、`app/agents/structured.py`）
3. **context_compress**（可选开关）：消息历史压缩成 digest，防长任务上下文爆炸。（`app/agents/context_steps.py`）
4. **Executor** 逐个子任务调 MCP 工具：每个工具有**独立超时预算**（kb 20s / sql 10s / chart 15s）、重试矩阵、熔断计数、幂等缓存；失败信息**脱敏**后回填。（`app/agents/executor.py` 的 `_TOOL_BUDGETS`/`RETRY_MATRIX`/`sanitize_tool_error`）
5. **review_gate → Reviewer**：报告五维打分。过了 → summarizer；没过 → **replan** 带着 suggestions 回 executor 重做（这就是"带环"）。（`app/agents/review_gate.py`、`app/agents/reviewer.py`、`app/agents/replan.py`）
6. **human_gate**：检测到高危动作（如外发通知）→ 写 approvals 记录（PENDING，24h 过期）→ `interrupt()` 挂起整个图，任务态 `AWAITING_APPROVAL`，前端弹审批抽屉带高危动作 preview。（`app/agents/human_gate.py`）
7. 审批通过 → resume 从断点继续；超时未审 → sweeper 自动收口为 EXPIRED。终态后 **task_cleanup** 清 checkpointer 快照和取消信号，不留"长生不老"key。（`app/services/approval_service.py`、`app/services/task_cleanup.py`）

### 核心功能板块

- **带环状态机**：LangGraph StateGraph，`route_after_review` 按 PASS/REVISE/REJECT 三分流，REVISE/REJECT 回 replan 形成环，`replan_count` 上限兜底。（`app/agents/graph.py`）
- **断点持久化**：AsyncRedisSaver 存每步快照，后端重启可 resume；对三方库缺陷打了运行时补丁（见第三节 Bug 1）。（`app/services/redis_saver_patch.py`、`app/services/w3_runtime.py`）
- **工具治理**：MCP 协议（stdio/SSE）对接，工具热启停、版本路由（`ToolVersionRouter`）、强幂等（`IdempotencyStore`）、per-tool 超时、重试矩阵、哨兵重启。（`app/tools/mcp_client.py`、`app/tools/tool_governance.py`）
- **全链路 Trace**：请求进来自动注入 TraceID（中间件），执行期每个节点/工具调用落 TraceBuffer，异步批量削峰入库；同时桥接 OpenTelemetry 导出——**自研（业务语义全、零依赖）+ OTel（标准协议、接主流 APM）双轨可切换**。（`app/core/trace.py`、`app/services/trace_service.py`、`app/core/tracing_middleware.py`、`app/observability/otel_tracing.py`）
- **记忆系统**：任务执行完 reviewer 评分达标自动沉淀长/中期记忆，planner 前语义召回；带生命周期（30 天中期 → 永久长期）、去重（特征哈希 0.88 阈值）、召回降级自省。（`app/memory/layered_memory.py`、`app/memory/memory_lifecycle.py`、`app/memory/local_embedding.py`）

---

## 三、开发中遇到的真问题及解决（深水区排雷）

> 以下 4 个全部是真实生产/联调事故，按"痛点现象 → 根因排查 → 代码级方案"展开。

### Bug 1：三方库 checkpoint key 错位——每次保存都在覆盖上一次快照（最硬核的一个）

**痛点现象**：所有走到人工审批挂起（interrupt）的任务，最终都被误判成 FAILED；审批抽屉拿不到高危动作 preview；后端重启后 resume 全部失败。而且这个 bug **InMemorySaver 测试环境复现不了**，一换 Redis 就现形。

**根因排查**：顺着 `aget_state()` 为什么 `interrupts` 恒为空往下挖，发现 `langgraph-checkpoint-redis 0.0.8` 的 `AsyncRedisSaver.aput` 拿 **config 里的 `checkpoint_id`（这是父快照的 id！）** 当作新快照的 Redis key 和顶层字段，而本次快照真实 id 在 `checkpoint["id"]`。后果是：每次保存都写到同一个 key 上**覆盖上一条快照**，pending write（含 `channel="__interrupt__"` 的挂起信息）找不到对应 checkpoint，组装 interrupts 时恒为空。

**代码级方案**：给三方库打**运行时猴子补丁**——调用原实现前把 config 里的 `checkpoint_id` 换成 `checkpoint["id"]`，进程内幂等安装，库升级到 >= 0.1 后可整体删除。（`app/services/redis_saver_patch.py`，安装点在 `app/services/w3_runtime.py`）

**面试点**：这体现了对依赖库**不信任但要读懂**的态度——三方库 bug 不改源码、用补丁模块隔离，升级路径留好；同时解释了为什么单测绿了生产还是炸（测试基建 InMemory 与生产 Redis 行为分叉）。

### Bug 2：5 秒一刀切超时把知识库检索"全灭"，熔断器又把整轮任务判死

**痛点现象**：复杂电商分析任务里所有 kb-search 子任务连续 3 次超时 → 熔断器打开 → 整个任务的检索类子任务全部失败。而单独测知识库，检索 8~11 秒是正常的。

**根因排查**：全局默认工具超时 5 秒是为 notify 这类轻工具定的，但知识库检索走 RAG（召回+重排）天然 8 秒起。一刀切超时 → 必然超时 → 重试矩阵重试 3 次（每次照样超时）→ 熔断计数打满 → 熔断打开。**超时、重试、熔断三个机制各自正确，组合起来却是自杀**。

**代码级方案**：per-tool 独立预算——`_TOOL_BUDGETS` 按工具名映射到 w2 配置（kb=20s/sql=10s/chart=15s/notify=5s），调用点 `asyncio.wait_for(..., timeout=tool_budget)`，超时报错文案带上"本工具预算 Xs"方便定位；同时明确**分工边界：哨兵只管传输层瞬断（MCP_PROTOCOL_ERROR 自动重启子进程重试，仅限只读工具），超时走重试矩阵+熔断——语义不动**。上线后复杂任务 8/8 子任务全 DONE，chart 首次生产成功。（`app/agents/executor.py` 的 `_TOOL_BUDGETS`/`_tool_budget`、`app/core/w2_config.py`、哨兵在 `app/tools/mcp_client.py` 的 `call_tool`）

**面试点**："可观测性驱动"的排障——先从 Trace 看到 kb-search 连续超时，再实测上游延迟分布，才定性是预算错配而非服务故障。

### Bug 3：asyncio 管道 64KB 行限制——趋势图数据一大整个 MCP 工具崩掉

**痛点现象**：大促复盘任务里"多日趋势图"子任务必失败，报 `Separator is found, but chunk is longer than limit`，且重试永远失败。小图表（对比图）却正常。

**根因排查**：MCP stdio 子进程用 `asyncio.create_subprocess_exec` 通信，**默认 stream limit 是 64KB**——一行 JSON-RPC 响应超过 64KB（大趋势图数据序列化后 200KB+），readline 直接抛 ValueError，进程异常。这解释了为什么同一工具时好时坏：纯粹取决于返回数据量。

**代码级方案**：`_STDIO_LINE_LIMIT = 4MB` 传入 `create_subprocess_exec(limit=...)`；同时把"行超限"的 ValueError **显式映射为 MCP_PROTOCOL_ERROR**——因为哨兵只重启传输层错误，这样即使未来数据量再涨破 4MB，也能被哨兵捕获重启降级，而不是无声挂死。测试用真实 200KB 子进程回归（Windows 命令行长度限制下把子脚本落临时文件执行）。（`app/tools/mcp_client.py` 的 `_STDIO_LINE_LIMIT` 与 read loop、`tests/test_task17_stdio_large_line.py`）

### Bug 4：MCP JSON 层把数值序列化成字符串——chart-render 生产 0 成功

**痛点现象**：chart-render 工具在生产环境成功率为 0，参数校验全部报"数值字段非法"，但本地单测全绿。

**根因排查**：抓了一次真实调用参数才发现——MCP 的 JSON 层把 `3888138.87` 序列化成了**字符串** `"3888138.87"`，而图表参数构造器严格 `isinstance(x, (int, float))` 校验，全被拒。单测里 mock 的返回是原生数字，所以永远测不出来。

**代码级方案**：`_as_number()` 三级矫正——bool 显式排除（True 不是数字）→ 数字直过 → 字符串 strip 并去千分位后转 float，非法才拒。同样思路修了 SQL 修复循环的列名丢失问题：**运行时 `inspect` 白名单表拉真实列名（TTL 1h 缓存）注入修复 Prompt，并做标识符形状校验杜绝 DDL 注入拼接面**——LLM 修 SQL 时不再瞎编列名，也不再把用户输入拼进 DDL。（`app/agents/executor.py` 的 `_as_number`/`_build_chart_arguments`、`app/tools/sql_schema.py`）

---

## 四、硬核工程化落地（资深架构师素养）

### 1. 状态机与高可用

- **Redis 分布式锁管写路径**：审批 resume 的竞态窗口是"两个请求都读到 AWAITING_APPROVAL、都通过预检、各自驱动一次图执行"——checkpointer 直写 Redis 绕过 ORM，**数据库 version 乐观锁管不到它**，必须在服务层入口用 Redis 锁串行化。选型上不用 RedLock（单实例 Redis 用不上）、不用 `Redis.lock()`（释放走 Lua/evalsha，fakeredis 测试环境不支持）——**手写 `SET key token NX EX` + `WATCH` 事务比对后 DEL**，语义等价、真实 Redis 和 fakeredis 通吃，持锁崩溃靠 TTL 兜底不死锁。（`app/core/redis_lock.py` 模块头注释就是完整的选型论证）
- **终态清理**：任务终态后删 checkpointer 快照线（`adelete_thread`）+ 取消信号，checkpointer 虽有 72h TTL 但终态任务占着是纯浪费。（`app/services/task_cleanup.py`）
- **取消信号降级设计**：取消标记放 Redis，Redis 故障时 `_degrade` 记日志放行而不是把任务卡死——**辅助设施故障不能放大成主链路故障**。（`app/services/cancel_service.py`）

### 2. 成本与安全防线

- **三层成本防线**：①单节点 token 预算（每次 LLM 调用前预检，超限抛 `TOKEN_BUDGET_EXCEEDED` 强制中断执行图）②用户级每日 token/请求数熔断（Redis 按日计数，UTC 口径）③工具级熔断器（Redis 计数，连续失败开闸，冷却期后半开试探）。（`app/core/w2_config.py` 的 `token_budgets()`、`app/core/daily_quota.py`、`app/core/circuit_breaker.py`）
- **重试与幂等绑定**：`RETRY_MATRIX` 按工具定重试次数与退避序列，重试前先查幂等缓存——**重试永不放大副作用**；幂等键 = (task_id, tool, args) 哈希 + TTL。（`app/agents/executor.py`、`app/tools/tool_governance.py`）
- **脱敏**：工具错误信息 `sanitize_tool_error` 出 Agent 边界前脱敏（SQL/路径/凭证模式），Trace 落库前 payload 削峰截断 + PII 处理，OTel span 异常吞掉不影响主链路。（`app/agents/executor.py`、`app/services/trace_service.py` 的 `_safe_payload`、任务 12/13 系列测试 `tests/test_task12_otel_bridge.py`、`tests/test_task13_trace_pipeline.py`）
- **输入防御**：用户目标先过 `sanitize_user_input` + IntentGuardrail（提示注入/越权意图拦截）。（`app/core/guardrails.py`、`tests/test_task10_security.py`）

### 3. 人工干预（Human-in-the-loop）

- **挂起机制**：human_gate 是全图**唯一 interrupt 点**——高危动作（默认 notify 类）触发时写 approvals 记录（PENDING，24h 过期），`interrupt({approval_id, reason, preview})` 把任务停在 `AWAITING_APPROVAL`，前端审批抽屉展示动作 preview。（`app/agents/human_gate.py`、`app/routers/approvals.py`）
- **超时自动收口**：后台 sweeper 扫 `expires_at < now` 的 PENDING 审批自动置 EXPIRED 并回写任务终态；**兜底哲学**：即使 sweep 进程挂了，第一个撞上过期审批的 decide 请求也会触发同款纠正逻辑——**任何单点清理失效都有路径补偿**（`app/services/approval_service.py` 的 `decide` 内嵌过期检查）。
- **resume 互斥**：见第四节第 1 点的 Redis 锁。

---

## 五、项目局限性与演进

### 1. 已知局限（诚实清单）

- **无全局事务/补偿**：工具副作用（如已发出的 notify）在 replan 或任务失败后不会自动回滚，目前靠幂等缓存挡"重复执行"，不做"逆向补偿"——严格说缺一层 Saga。
- **单实例假设**：Redis 锁按单实例设计（非 RedLock 多主），水平扩展到多节点需要重估锁与限流的拓扑。
- **记忆召回年轻**：语义召回（本地 embedding，text-embedding-v3，1024 维）上线时库内仅 46 条样本，阈值 0.55 是按 6 档难度探针校准的，规模到几百条后需再校准；且 embedding 通道故障时自动降级关键词匹配（降级状态对用户可见）。（`app/memory/local_embedding.py`、`app/memory/layered_memory.py`）
- **长任务上下文**：context_compress 是任务级可选开关（默认关），不开的长任务早期约束可能被消息窗口截断。
- **评测成本口径**：token_cost 有意不做估算（宁 None 不编数），所以 A/B 对比的 Token 节省率在无真实计量数据时显示空——这是数据诚实性优先的设计取舍。（`app/services/eval_project1_adapter.py`）
- **跨项目依赖**：评测转发依赖独立评测中台，其不可用时任务评测环节 502（有意暴露而非静默）。

### 2. v2.0 演进方向

1. **Saga 式副作用补偿**：给工具声明补偿函数，replan/失败路径自动逆向执行。
2. **多实例水平扩展**：锁/限流/熔断已全部落在 Redis，剩余工作是部署拓扑与 checkpointer 的多实例验证。
3. **记忆规模化**：向量库从进程内缓存升级为独立索引服务，阈值随规模自动校准。
4. **评测 A/B 自动化**：分组批量跑（N≥3 控制裁判方差 ±0.05），置信区间代替单次对比。
5. **跨项目向量互通**：记忆与知识库统一向量空间（配置三行切换 embedding 供应商，代码零改动——模型名+内容哈希做缓存键，切换自动重嵌）。

---

## 六、面试绝杀话术

> **问：在多智能体系统里，你是如何用传统后端架构思维（锁、状态机、熔断）来"降服"大模型的？为什么不用 LangSmith？**

"我的核心观点是：**大模型本身不可靠，所以工程上要把它当'会犯错的下游服务'来治理，而不是当'聪明的黑盒'来祈祷。** 我做了三件事：

第一，**用状态机收编自由发挥**。LLM 只在节点内部有自由度，节点之间的流转完全由显式路由函数控制——评审不过就走重规划环，但环上有硬上限，任何一条路径都终态可达。这样'AI 会不会死循环'这个问题在图结构层面就不存在了，而不是靠 prompt 求它别循环。

第二，**把每个 LLM/工具调用都套上传统可靠性外壳**。每个调用有独立超时预算、重试矩阵、Redis 计数熔断；重试和幂等缓存绑定，永不放大副作用；每用户每日 token 硬熔断，重试风暴最多烧到预算线就被强制中断。这里有个真事：我们曾因 5 秒一刀切超时把 8~11 秒的知识库检索'全灭'，超时、重试、熔断三个机制各自都对，组合起来却自杀——最后是按工具拆预算解决的。**这让我深刻理解到：治理手段之间需要全局预算视角，局部正确不等于全局正确。**

第三，**持久化和并发用最笨但最可靠的方式做**。断点快照进 Redis checkpointer，中途挂了可续跑；审批 resume 的竞态用分布式锁串行化——而且我不用现成的 RedLock，因为它面向多主集群，我们是单实例，手写 SET NX + WATCH 释放，还顺手兼容了测试环境的 fakeredis。最狠的一次是三方库 checkpoint key 错位 bug，每次保存覆盖上一次快照，interrupt 信息全丢、任务被误判 FAILED，而且 InMemory 测试环境完全复现不了——最后是读懂库源码打了运行时补丁隔离掉。

至于为什么不用 LangSmith：一是**企业内网数据不出域**是硬约束，SaaS 观测平台过不了安全评审；二是 LangSmith 给的是'LLM 视角'的 trace，而我们的故障大多不在 prompt 上，在管道行限制、三方库 key 错位、超时预算错配这些**工程层**——所以我做了自研+OTel 双轨：自研这条有完整业务语义、异步批量削峰落库零外部依赖，OTel 这条保留标准协议随时接公司 APM。**观测工具要贴着故障的真实分布建，而不是贴着框架的营销叙事建。**"

---

*文档生成于 2026-10-27，基于 715 全绿测试基线；所有文件路径均已在源码中核实。*
