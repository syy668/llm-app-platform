# 项目一 · 企业级 RAG 知识库产品与技术总结

> 定位：求职简历「项目经历」素材 + GitHub 核心展示项目（README 首页）。
>
> **数据口径（红线）**：本文所有技术点逐条对齐 **v1.17 封版源码**，全量测试基线 **524 passed / 0 failed**。  
> **本版重写已修正两处旧文档错误**：① 删除「跨页表格无表头」等已解决旧描述——该功能已在 `loader.py` v1.9 + `splitter.py` `_table_prefixes` 实打实解决；② 修正「Redis 分布式锁」——代码里不存在 Redis 分布式锁，真实并发控制为「进程内并发上限 + 原子 SQL 认领 + 僵死回收」。

---

## 模块 1：真实应用场景与业务价值

**一句话**：这是一个面向企业内部的知识库问答系统，把"企业文档"变成"能溯源、防幻觉、可量化评测"的问答能力，让任何员工用自然语言就能查到制度、故障处理、报表口径等散落在各处的信息。

**1. 三个真实业务场景（痛点 → 价值）**

| 场景       | 企业痛点                                          | 本系统价值                                        |
| -------- | --------------------------------------------- | -------------------------------------------- |
| 新员工查制度   | 员工手册/考勤/报销制度散落在多个 Word/PDF，新人问老员工、问 HR，口径还不一致 | 上传制度文档即问即答，答案带「《文档》第N块」出处可点击溯源，杜绝"听谁说"       |
| 运维查历史故障  | 服务器宕机处理经验在 wiki/工单里，故障时翻不全、找不到根因              | 多轮对话 + 意图继承，追问"去成都呢？"也能接着上一轮检索，给出带出处的处理步骤    |
| 财务/业务查报表 | 月度报表是 Excel，指标口径只存在于某个人脑子里                    | XLSX 逐行解析保留列序，向量+BM25 双路召回，问"Q1 销售额"直接定位到具体行 |

**2. 相比"套壳 RAG"的商业卖点**

- **不是"检索+拼 prompt"**：六节点 LangGraph 路由（classify/rag/db_query/report/parallel_compare/answer）按问题类型分流，能查库、能出报表、能做 A/B 对比，而不只是吐一段文本。
- **防幻觉是产品级能力**：后置引用校验（citation_guard）抽长数字/日期/块号做子串比对，疑似幻觉标 `suspect`，而不是"答得漂亮但数字全错"。
- **评测内建于系统**：RAGAS 四指标全自动评测 + 点踩转 Badcase 数据飞轮，回答质量可量化、可回归，而不是上线后"凭感觉好不好"。
- **企业级安全**：双 Token 鉴权（access 30min + refresh 7天黑名单/轮换）+ RBAC 知识库级权限，禁用账号即时掉线；全链路 Trace 让"AI 乱回答"可排查。

---

## 模块 2：核心功能实现清单（已落地，均对齐源码）

- **文档解析与切分（表格原位还原 + 跨页表头回填）**
  - PDF：`pdfplumber` 抽表转 Markdown；**跨页续表判定**（`_PAGE_TOP_MARGIN=100pt`、`_PAGE_BOTTOM_RATIO=0.78`、`_looks_like_data_row` 首行含数字）→ 上一页表被页尾截断 + 本页表从页首开始 + 列数一致时，自动回填上一页表头（已解决"跨页无表头"）。
  - 正文与表格按 bbox **纵向切条带交错输出**（`_page_blocks_with_tables`），解决旧版"表格单元格被重复拼进正文 + 表格全堆页尾与标题脱节"两个坑。
  - DOCX：按 `body` 子节点**真实顺序**遍历 `w:p`/`w:tbl`，表格留在原位（旧版表格漂到文末，实测距标题从 151 字符漂到 804 字符）。
  - XLSX：`openpyxl` 逐行 join，天然保留列序。
  - 分块（`splitter.py`）：含表格走**行对齐切分**（行不被拦腰截断），并为每个从表格中段开始的块自动前置「表头行 + 分隔线」（`_table_prefixes`），保证任何一条记录脱离原块仍自带列名；纯文本走字符滑窗。
- **多路混合检索（向量 + BM25 + RRF 融合）**
  - 向量（语义 Embedding，可配 bge-m3 等）+ BM25（本地内存索引，不依赖外部服务）**双路召回**，RRF（倒数排名融合，`k=60`）合并。`rerank_threshold>0` 时按查询-块余弦相似度重排 + 阈值过滤。
  - **embedding 不可用时降级纯 BM25**（v1.14）：索引是本地内存，充值/换 key 后自动恢复双路，无需改配置、无需重建索引。`search_mode` 支持 `vector_only` 单路。
- **多轮对话与溯源（意图继承 + 可点击引用）**
  - **意图继承**（`intent_inherit.py` v1.12，规则短路）：上一轮 ∈ {rag, db_query} + 本轮 ≤12 字 + 出现指代（呢/那/这个…）或极短疑问（≤8 字含谁/多少/哪…）→ 跳过 LLM 分类直接继承路由。确定性、省一次 LLM 调用、日志可定位原因。
  - 引用卡片可点击：`sources` 落 `messages.sources`，前端按《文档》第N块渲染；无快照时灰色虚线静态 chip（不可点），历史消息可溯源。
- **幻觉治理（冲突检测 + 后置引用校验）**
  - `citation_guard.py`（v1.9）：抽答案里**长数字/日期** → 归一化（去千分位、年月日统一、去前导零）→ 子串比对检索上下文，未命中标 `suspect=1`（**只标不拦**）。
  - v1.11 补"无上下文却声称出处"（direct 路由凭记忆编来源）硬判；v1.13 补**块号校验**（`check_chunk_refs`）：答案声称的《文档》第N块，与上下文真实下发的块集合比对，抄错块号也能抓。
  - 已知局限（不造假）：子串比对只证"这个数在上下文出现过"，不证"用在正确位置"。
- **自动化评测与数据飞轮（RAGAS + A/B + 点踩转 Badcase）**
  - RAGAS 四指标：faithfulness / answer_relevancy / context_precision / context_recall。
  - **A/B 对比**：勾选两个 run → 后端 `POST /eval/runs/compare` 出对比报告（同屏看实验参数与指标差异）。
  - **点踩转 Badcase**（`POST /chat/badcase`）：被点踩回答一键沉淀到按知识库聚合的「用户反馈Badcase」评测集（幂等、不重复入库）；低分样本按指标阈值**后端筛选**（`GET /eval/runs/{id}/badcases`）。空集直接判 `failed`，杜绝 0/0 伪成功。

---

## 模块 3：硬核工程化展现（面试装逼专用，全部对应最新代码）

### 1. 高可用与防呆

- **Schema 启动自检**（`schema_check.py` v1.7）：`SCHEMA_CHECK_MODE=strict`（默认）启动时比对 ORM `metadata` 与真实库——缺表/缺列直接生成 `ALTER TABLE ... ADD COLUMN` DDL 并 **fail-fast 抛 `SchemaDriftError` 中止启动**；`warn` 仅告警；`off` 跳过。只比"存在性"不比类型（避跨库误报）；`NOT NULL` 无默认值先按可空加、再给"回填后收紧" DDL；`import_all_models()` 保证 `eval_*`/`trace_*` 全注册，避免漏检。
- **参数热更新与缓存签名**（`runtime_config` + LLM `factory`）：12 个运行参数（限流/工具超时/llm_provider/temperature/model/api_base/api_key/eval_llm\_*）页面改完**免重启**；runtime_config 现读、LLM 工厂按配置签名重建 client，旧配置缓存自然失效。
- **级联安全删除**：删除走 service 层统一清理**派生状态**（向量、BM25 缓存失效、Redis 会话/令牌桶），外键 `ON DELETE CASCADE` 保一致；评测运行记录**刻意保留**（审计留痕）只删样本/得分；用户采用**软禁用不硬删**，避免级联炸库。

### 2. 性能与并发控制（如实表述，不写不存在的锁）

- **Redis 分布式令牌桶限流**（`rate_limit.py`）：Lua 原子脚本按 uid 令牌桶，AI 速率上限（默认 30/min），**真·分布式**跨 worker 共享；项目独占 Redis **db1**，与项目三 AgentFlow（db0）隔离，互不打扰。
- **评测并发闸**（`eval_service` + `config`）：样本级并发 `EVAL_MAX_CONCURRENCY`（默认 2）+ 活跃任务上限 `eval_max_active_runs`（默认 3）→ 最多 6 路打同一上游 key，**防 429**；单样本失败自动重试 `EVAL_RETRY_ATTEMPTS`（默认 2）；空数据集直接 `failed` 防 0/0 伪成功。
- **没有 Redis 分布式锁，这是刻意取舍**：评测是单进程后台 job worker，并发边界由「进程内并发上限 + **原子 SQL 认领**（`UPDATE ... WHERE status=pending`，消除"用户刚取消就被覆盖成 running"的竞态）+ 僵死回收」收口，避免了分布式锁的死锁/脑裂复杂度——面试说"用 Redis 锁防并发"会露怯。
- **流式输出防阻塞**：SSE 逐字流式（`chat_stream.py`/`stream_service.py`），事件 `route/delta/report/done/error`，与 contextvars Trace 天然兼容。

### 3. 成本与边界控制（Token 双护栏）

- **输出截断感知**（`truncation.py` v1.16）：强制下发 `max_tokens`（默认 `LLM_MAX_TOKENS=2048`，对成本封顶）；检测 `finish_reason="length"`（OpenAI 兼容）/`stop_reason="max_tokens"`（Anthropic）/`finishReason`（网关），命中在回答末尾追加「[注意：回答已达到最大长度限制…]」提示，并置 `trace_logs.is_truncated` 供大盘算截断率。三处统一收口（answer_node / tracing / stream_service）。
- **输入历史滑动窗口**（`load_history_window`，`agent_service`）：SQL 层先按**状态过滤**（user 全留、assistant 只认 `done`，过滤 `failed`/`running` 的错误正文）+ 取尾部 `max_turns*2+1`（默认 **5 轮**），内存再按**轮数 + 字符双预算**（`max_chars` 默认 **2000**）裁剪，超预算丢最早消息。改造前 `.all()` 无限拉取 → 长会话 prompt 线性膨胀撞上下文窗口、且把 failed 错误文案当回答喂回。

### 4. 数据一致性与可观测性

- **评测得分抢救落库**（`eval_service`）：计数器/缓冲区提到 `try` 外，异常路径也能抢救"已算完未落库"得分；按 `EVAL_SCORE_BATCH_SIZE`（**默认 1 = 每样本即时落库**，v1.13.1 由 10 改 1，消除"最多 9 条丢失窗口"，run 33 实测 9/30 成功 0 得分）flush；进程被 kill 的僵死 run 由 `backfill_terminal_metrics` + `cleanup_stale_running_runs` 回收，用已落库得分回填 metrics，**不丢已完成结果**。
- **全链路 Trace**（`tracing.py`，基于 `contextvars`）：`trace_id` 跨 LLM 调用/工具/检索/SSE 全程绑定，落 `trace_logs`（route / 检索块 / suspect / citation_misses / is_truncated / 耗时），对接自有大盘。排查"AI 乱回答"三步法：① 看 `route` 是否走对分支；② 看 `citation_misses`/`suspect` 是否命中引用校验；③ 看 `is_truncated` 是否截断导致答非所问。

---

## 模块 4：架构演进与工程取舍（坦诚局 · 最重要）

### 已解决的旧问题（明确归档，不再算"边界"）

- **跨页表格无表头** → 已在 `loader.py` v1.9 通过跨页续表判定 + 表头回填解决。
- **分块丢失表头/表格漂页尾** → 已在 `splitter.py` 通过 `_table_prefixes`（块首前置表头+分隔线）+ bbox 纵向切条带解决。
- **长会话 prompt 膨胀 / failed 错误文案被当回答** → 已由 `load_history_window` 状态过滤 + 双预算解决。
- **截断静默无提示** → 已由 `truncation.py` 截断感知解决。

### 当前真实遗留边界（不避短）

1. **RAGAS 达标基准重跑未闭环**：v1.17 已加 `preflight` 探活 + 连续失败熔断（零成功且连续 N 败即中止，避免 403 空跑），但"faithfulness ≥ 0.7 达标"的量化结论仍依赖**人工用真实 LLM 跑黄金集重跑**，尚未自动化闭环（需真实上游额度）。
2. **Badcase 缺参考答案**：点踩自动入集时 `reference_answer` 刻意留空（填错答案会让 RAGAS 把幻觉当标准答案反向打分），导致 `context_recall` 无法自动计算，需**人工补录**（已提供 `PUT` 补录接口 + 审计，但仍是人工动作）。
3. **Token 绝对过期上限缺失**：access 30min + refresh 7天，refresh 采用**滑动续期**（每次访问重置过期），无绝对过期上限——`deps.py` 已自陈"对方持续访问即可无限期留在系统里"，长期活跃会话的安全边界待补（如 refresh 最大生命周期）。
4. **检索精排偏轻量**：RRF `k=60` 融合后，`rerank_threshold` 默认 0（正负例余弦区间重叠，阈值难定），**无 cross-encoder 重排**；BM25 召回粒度对中文同义召回有限。复杂语义排序仍有上限。

### 为什么用「滑动窗口」而不是「动态摘要 / 记忆检索」

- **确定性**：同输入同输出，评测可复现；摘要是另一次 LLM 调用，结果不可复现，会让 RAGAS 评测失去基准。
- **成本与透明**：摘要本身要再调一次 LLM，长会话额外开销；且真实历史可见，定位幻觉/溯源直接，摘要黑盒难调试。
- **简单可靠**：SQL 状态过滤 + 双预算裁剪，无额外服务依赖。
- **已知权衡**：>5 轮前的长程事实会丢失——对"制度/故障"强上下文场景是取舍，用意图继承 + 块号溯源缓解。

### 为什么放弃 LangSmith，选自研 Trace

- **零成本、数据不出域**：企业合规强诉求，不引入 SaaS 依赖、不把对话日志传第三方。
- **与流式天然兼容**：基于 `contextvars`，same-task 内全链路取到 `trace_id`，SSE 流式下也不丢。
- **字段贴合本系统**：`route`/`suspect`/`citation_misses`/`is_truncated` 直接映射业务，可对接自有大盘与告警。
- **已知权衡**：无 LangSmith 的可视化标注社区，看板需自建（本项目用 `trace_logs` + 前端大盘承载）。

---

## 模块 5：一句话简历总结（可直接复制）

- **主导设计并交付企业级 RAG 知识库（FastAPI + LangGraph 六节点路由 + Chroma 向量库）**，落地 PDF/DOCX/XLSX 混合解析与**跨页续表表头回填**、向量+BM25+RRF 双路召回，全量 **524 passed / 0 failed** 封版。
- 构建**四层防幻觉体系**（入库清洗→混合检索→生成约束→RAGAS 四指标离线评测），落地 citation_guard 后置引用校验（长数字/日期归一化 + 块号比对，只标不拦），显著降低线上幻觉留存率。
- 工程化高可用：**Schema 启动自检 fail-fast**、Redis 分布式令牌桶限流 + 评测并发闸（进程内信号量 + 活跃任务上限 + 僵死回收）防 429、**Token 双护栏**（max_tokens 截断感知 `finish_reason` + 历史滑动窗口 5 轮/2000 字）、contextvars 全链路 Trace 与评测得分抢救落库（Batch=1）。
- 建立**数据飞轮与可观测闭环**：点踩→Badcase 自动沉淀（按知识库聚合、幂等）、A/B 评测对比、基于 contextvars 的全链路 Trace 大盘，支撑"检索→生成→评测→回归"可持续优化。

---

> **面试防杠备注**：以上每一条均可对应到 `app/rag/loader.py`、`app/rag/splitter.py`、`app/rag/retriever.py`、`app/rag/citation_guard.py`、`app/agent/intent_inherit.py`、`app/llm/truncation.py`、`app/core/schema_check.py`、`app/core/tracing.py`、`app/services/eval_service.py` 的具体实现，被追问"锁怎么设计 / 表头怎么补 / 截断怎么感知"时可直接展开源码级讲解。
