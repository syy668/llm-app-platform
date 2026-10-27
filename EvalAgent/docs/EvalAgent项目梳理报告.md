# EvalAgent — LLM-RAG 自动化评测与 A/B 调优平台 · 项目梳理报告

> 梳理时间：2026-10-20 ｜ 证据来源：全量代码探查 + 集成测试报告（2026-10-19）+ 并发提速验证报告（run21/23）
>
> 架构定性：EvalAgent = **Vue3 独立前端（evalagent_vue）+ 项目一 FastAPI 后端内的 eval 增量模块**，不新增后端服务；评测/RAG/RAGAS 底座全部在「企业垂直知识库 Agent 后台」（E:/AI_Project_Workspace/企业垂直知识库 Agent 后台）。

---

## 一、系统架构总览

```
evalagent_vue (5174, Vue3+Vite)
   │ HTTP Bearer Token（axios 拦截器）
   ▼
企业垂直知识库 Agent 后台 (8000, FastAPI)
   ├─ /api/v1/auth/*      登录/会话（bcrypt + Redis Session Token）
   ├─ /api/v1/kbs         知识库
   ├─ /api/v1/eval/*      评测数据集/任务/得分/导出 ← EvalAgent 核心依赖
   ├─ /api/v1/system/*    配置热更新（LLM 模型/密钥）
   ├─ RAG 链路: Chroma + BM25/向量混合(RRF) + Rerank
   └─ RAGAS 评测执行器（DeepSeek judge + 并发池）
   ▼
MySQL 8.0（业务+评测数据） / Redis 7（会话+限流） / Chroma 持久化（向量）
```

---

## 二、技术栈全景

### 2.1 后端（项目一 FastAPI）

| 层 | 选型 | 证据/版本 |
|---|---|---|
| Web 框架 | FastAPI | v0.143.3（requirements ≥0.110） |
| ORM | SQLAlchemy 2.0（Mapped 新式声明） | `app/core/database.py` |
| 数据库 | MySQL 8.0（PyMySQL 驱动，utf8mb4） | `docker-compose.yml`、`scripts/init_db.sql` |
| 鉴权 | bcrypt 密码哈希 + Redis Session Token（`随机串.HMAC-SHA256签名`，Bearer 头，滑动过期 + refresh token） | `app/core/security.py` |
| 缓存/限流 | Redis 7（支持 `FAKE_REDIS=true` 内存模式） | `app/core/redis.py` |
| Agent 编排 | LangGraph ≥0.2 | `requirements.txt` |
| LLM 接入 | langchain-openai（ChatOpenAI，DeepSeek 兼容协议，timeout=60s/retries=2） | `app/agent/llm.py` |
| 配置热更新 | runtime_config + `data/system_config.json` 原子写入 | `app/core/runtime_config.py` |
| 迁移 | ❌ 无 Alembic —— `create_all` 自动建表 + 手写 ALTER 补列兜底 | `eval_service.py:54-69` |
| 任务队列 | ❌ 无 Celery/APScheduler —— 评测用 daemon 线程 + 线程内 asyncio 事件循环 | `eval_service.py:194` |

### 2.2 前端（evalagent_vue）

| 层 | 选型 | 说明 |
|---|---|---|
| 框架 | Vue 3.5 + `<script setup lang="ts">` | |
| 构建 | Vite 6 + TypeScript 5.7（vue-tsc 2.2 类型检查 0 errors） | |
| UI | Element Plus 2.9（全量注册 + zh-cn）+ @element-plus/icons | |
| 图表 | ECharts 5.6（目前仅 A/B 分组柱状图） | |
| HTTP | Axios 1.7（Bearer 拦截器、统一拆包、401 跳登录/403 提示） | |
| 状态 | 轻量模块级单例 store（reactive，localStorage 持久化）—— 有意不引 Pinia | `src/store/auth.ts` |
| 单测 | ❌ 无 vitest/jest，仅 vue-tsc 类型检查 | `package.json` scripts 无 test |

### 2.3 评测框架

| 项 | 选型/实现 | 说明 |
|---|---|---|
| 打分框架 | RAGAS 0.4.3（约束 ≥0.4.0,<0.5） | `app/eval/metrics.py` |
| 指标 | faithfulness / answer_relevancy / context_precision / context_recall 四项全 | |
| LLM Judge | ChatOpenAI → DeepSeek（deepseek-chat），模型/密钥/base_url 支持 /system/config 热更新 | ⚠️ judge 链路未配 timeout/max_retries |
| Embedding | 复用 RAG 侧 embed_texts 包装为 LangChain Embeddings；`hash` provider 时告警分数失真 | `metrics.py:50-57` |
| 并发机制 | 后台线程内 asyncio 事件循环 + `ThreadPoolExecutor(max_workers=EVAL_MAX_CONCURRENCY)` + as_completed；1~10 可配（默认 2） | 实测并行度 ≈ 配置值（run21=1.99, run23=3.66） |
| 容错 | 单样本重试 EVAL_RETRY_ATTEMPTS（默认 2，上限 5，线性退避）；失败摘要写 run.error_msg | |
| 落库 | 得分内存缓冲，满 EVAL_SCORE_BATCH_SIZE（默认 10）批量 flush | |
| 兼容 shim | 手写 vertexai 占位模块解决 ragas 0.4.x 导入兼容 | `metrics.py:20-33` |
| ⚠️ 已知缺陷 | 在线 `evaluate()` 未传 RunConfig → 默认 timeout=180s，慢样本指标被置 None（实测 10/30 题撞线） | 提速报告已给方案 A/B/C |

### 2.4 中间件与存储

| 组件 | 选型 | 说明 |
|---|---|---|
| 关系库 | MySQL 8.0 | eval_datasets / eval_dataset_items / eval_runs / eval_run_scores |
| 缓存 | Redis 7（会话、限流） | |
| 向量库 | Chroma 本地持久化（PersistentClient，HNSW cosine，每库一个 collection，全局 RLock 防竞态） | `app/rag/vector_store.py` |
| 检索策略 | `vector_only` / `bm25_vector_hybrid`（双路召回 + RRF k=60 融合），**请求级可切** | `app/rag/retriever.py` |
| BM25 | 纯手写内存实现（k1=1.5,b=0.75，中英文分词，按库缓存失效） | `app/rag/bm25.py` |
| Rerank | rerank_threshold>0 时对融合候选（top_k×3）按余弦重排 + 阈值过滤 | `retriever.py:110-132` |
| Chunk 切片 | 字符滑窗 500/overlap 50（token 估算中英分治） | ⚠️ 评测请求级不生效，仅记录展示 |
| Embedding 服务 | `hash`（384 维测试用）/ `openai` 兼容接口（默认 text-embedding-3-small） | |

### 2.5 部署

| 项 | 状态 | 说明 |
|---|---|---|
| Dockerfile | ✅ | python:3.11-slim，CMD uvicorn 8000 |
| docker-compose | ✅ | mysql:8.0 + redis:7-alpine + app 三服务，healthcheck + 卷挂载（chroma_db/uploads/data） |
| 初始化脚本 | ✅ | `scripts/seed.py`（管理员）、`scripts/init_db.sql` |
| 冒烟/运维脚本 | ✅ | `scripts/smoke_*.ps1`、`run_baseline_eval.py`（离线评测）、`monitor_ab_runs.py`（A/B 离线聚合） |
| 前端部署 | ⚠️ | dist 已构建，但无 nginx 配置/容器编排 |
| CI/CD | ❌ | 无 |

---

## 三、功能清单

### 【A. 已完成】（✅ 可逐条核对）

**A1. 评测数据集管理（后端 + 前端）**
- [x] 数据集 CRUD：创建/分页查询/删除（级联删样本、保留运行记录），仅管理员可写 — `eval.py:19-71`
- [x] 样本批量导入：单次上限 500 条，分页查询 — `schemas/eval.py:19-20`
- [x] 知识库归属：数据集绑定 kb_id，创建时选择 — `eval_datasets.kb_id`
- [x] 前端 CSV 上传解析校验：BOM 剥离/空文件拒绝/表头必须含 question+ground_truth/空行过滤/逐行非空校验/500 上限/RFC4180 引号转义 — `src/utils/csv.ts`
- [x] 前端样本分页预览（每页 10 条）+ 删除二次确认 — `DatasetView.vue`
- [x] 分层难例评测集构建脚本：30 题（10 同义改写 + 10 跨文档组合 + 10 库外），含 stratum 元数据，API 直导 — `stratified_dataset_build.py` + manifest.json

**A2. 评测任务执行（后端核心）**
- [x] 异步评测任务：创建即返回，daemon 线程后台执行 — `eval.py:74-84`
- [x] 任务状态机 pending→running→done/failed，success_items 实时递增，前端 5s 轮询进度条 — `eval_service.py:381-389`
- [x] RAGAS 四指标打分（faithfulness/answer_relevancy/context_precision/context_recall），均值汇总入 `eval_runs.metrics`
- [x] 逐样本得分明细：answer/retrieved_contexts/四分/latency_ms 全落库 `eval_run_scores`
- [x] rag_config 实验参数持久化（chunk_size/search_mode/top_k），列表/详情均返回，旧数据自动 ALTER 补列 — `eval_service.py:54-69,176-186`
- [x] 检索策略请求级生效：search_mode（vector_only/bm25_vector_hybrid）+ top_k 透传到检索链路 — `eval_service.py:233-244`
- [x] 并发评测：EVAL_MAX_CONCURRENCY 1~10（默认 2），实测并行度≈配置值；并发 4 时 30 题 13.2 分钟（较串行快 42%）— run21/run23 验证
- [x] 失败重试 + 线性退避 + error_msg 摘要；批量 flush 落库（10 条/批）
- [x] 任务删除级联删得分；仅管理员
- [x] CSV 导出（UTF-8 BOM 防 Excel 乱码）— `eval_export.py`
- [x] 任务详情抽屉含 rag_config 展示（BUG-2 已修复）— `TaskView.vue:393`

**A3. A/B 对照实验（前端 100% 实现）**
- [x] 双任务选择 + 同数据集联动过滤（选定一侧另一侧只列同 dataset_id）+ 双 watch 自动清空失效选择 — `AbCompareView.vue:191-230`
- [x] 后端二次校验同 dataset_id，不符弹窗拦截
- [x] 样本 question 对齐算法（Map 对齐 + usedB 去重 + 仅 B 兜底追加）— `abCompare.ts:45-65`
- [x] 逐指标 Δ 计算 + 差异行标黄（|Δ|>0.1）+ 差异样本按最大 |Δ| 降序
- [x] 汇总指标对比（优先后端 run.metrics，缺省前端求均值兜底）
- [x] ECharts 四指标 A/B 分组柱状图（dispose/resize 生命周期处理完备）
- [x] Markdown A/B 报告导出（实验配置对照表/汇总指标结论列/差异样本列表/前 50 条明细）— `markdown.ts`
- [x] 实测有效性验证：run9(top_k=3) vs run7(top_k=8) 报告产出，faithfulness Δ+0.32 判定「B 显著更优」— `ab_report_run9_vs_run7.md`

**A4. RAG 检索底座（项目一，被评测对象）**
- [x] 双检索策略：vector_only / bm25_vector_hybrid（RRF k=60）
- [x] Rerank 阈值重排；chunk 滑窗切片；Chroma 持久化 + 并发锁
- [x] Agent 编排（LangGraph）+ 多 LLM provider（mock/openai/anthropic）
- [x] LLM 配置热更新：`GET/PUT /system/config`（admin，实时生效），前端设置页已接入 — `system.py:66-85`

**A5. 鉴权与权限**
- [x] 登录 + Bearer Token（bcrypt + Redis Session）+ refresh token
- [x] 权限模型：eval 查询接口登录可读，写/删 admin only（前端按 isAdmin 显隐 + 后端 require_admin 兜底）
- [x] axios 拦截器：401 清凭据跳项目一登录页、403 全局提示、断连提示
- [x] 前端内嵌登录页 + 手动粘贴 token 备用通道 + token 与项目一前端 localStorage 隔离

**A6. 质量保障与验证**
- [x] 集成测试 35 用例（6 大模块）：32 PASS / 2 FAIL / 1 BLOCK，通过率 91.4% — `EvalAgent集成测试报告_20261019.md`
- [x] 三阶段测试脚本（接口预置/轮询级联/MySQL 直查复核）— `phase1/2/3*.py`
- [x] 并发机制实测验证（并行度反推 + 逐条 latency 证据链）— 提速报告
- [x] 慢题根因闭环（RAGAS RunConfig 180s 超时 + DeepSeek 尾部延迟，日志+数据双实锤）
- [x] 手工测试用例文档 — `docs/EvalAgent-test-cases.md`
- [x] vue-tsc 类型检查 0 errors

---

### 【B. 待开发缺失功能】（□ 可逐条核对补齐）

**B1. 后端 — 高优先级（直接影响指标口径与闭环）**

- [ ] **B1-1 RAGAS RunConfig 超时修复**：`metrics.py:89` 的 `evaluate()` 未传 `RunConfig(timeout=600, max_workers=...)`，默认 180s 导致慢样本指标置 None（实测 10/30 题丢 context_precision）。**最小改动一行**，优先级最高
- [ ] **B1-2 RAGAS judge 链路补 timeout/max_retries**：`metrics.py:68-72` 的 ChatOpenAI 未配置（agent 链路 60s/retries=2，judge 链路裸配），高峰期重试放大耗时
- [ ] **B1-3 LLM judge 切换 SiliconFlow**：提速报告方案 A——judge 链路切 `deepseek-ai/DeepSeek-V3`（SiliconFlow），实测 3.9s vs 官方 ~50s，纯配置可落地（agent 端走热更新、RAGAS 端改 .env 需重启）
- [ ] **B1-4 BadCase 服务端接口**：全仓无 badcase 端点，现靠前端阈值过滤 + CSV 离线分析。建议 `GET /eval/runs/{id}/badcases?threshold=`（支持按指标/阈值筛选 + 分页）
- [ ] **B1-5 A/B 后端聚合接口**：无 `/eval/runs/compare`；A/B 全靠前端拉全量 scores 计算。样本量大时（>2000 条）前端 `listAllScores` 兜底上限会截断。建议后端按 run 对齐 question 聚合返回
- [ ] **B1-6 已知 BUG 修复**（集成测试遗留）：
  - [ ] BUG-1（中）：空数据集创建任务终态为 done(0/0)，应为 failed
  - [ ] BUG-5（中）：direct 路由样本被静默跳过不写分（得分缺失无提示）
  - [ ] BUG-4（低）：JSON null 与 SQL NULL 两种空形态并存，查询口径不一致

**B2. 后端 — 中优先级（工程健壮性）**

- [ ] **B2-1 任务队列化**：评测跑在 daemon 线程，进程重启丢失 running 任务且无恢复机制；不建议直接上 Celery，但至少要启动时把僵死 running 置 failed
- [ ] **B2-2 Alembic 迁移体系**：现靠 create_all + 手写 ALTER 兜底，schema 演进不可追溯
- [ ] **B2-3 chunk_size 请求级生效或明确降级**：当前仅记录展示，做切片维度 A/B 必须重建知识库；要么支持「评测时按临时 chunk_size 重建临时集合」，要么在 UI/文档更醒目地声明约束
- [ ] **B2-4 分层（stratum）维度评测**：分层难例集有 stratum 元数据（改写/跨文档/库外），但数据集表无此字段、结果无法按层聚合（改写题看 recall、库外题看拒答率）。需：eval_dataset_items 加 stratum 列 + 汇总按层分组
- [ ] **B2-5 库外题（OOC）拒答检测指标**：现有四指标不覆盖「应该拒答却硬答」的场景，分层集第三层（10 库外题）无法量化

**B3. 前端 — 中优先级**

- [ ] **B3-1 权限路由守卫**：router 无 `requiresAdmin` meta，纯 UI 显隐 + 后端 403 兜底；管理员页面普通用户仍可进入（只读无害但体验差）
- [ ] **B3-2 可视化增强**：目前仅 1 张分组柱状图。建议补：① 雷达图（四指标全景）；② 样本得分散点/箱线图（分布与离群点）；③ A/B 逐样本 Δ 条形图；④ 多组实验（>2 run）对比支持——当前只支持两两对比
- [ ] **B3-3 BadCase 溯源深挖**：详情抽屉只有低分筛选，建议加：低分样本点击查看 retrieved_contexts 命中情况（哪个 chunk 被检回/漏检），定位是检索问题还是生成问题
- [ ] **B3-4 单元测试**：无 vitest/jest；`csv.ts`（解析/校验）、`abCompare.ts`（对齐/Δ）是纯函数，最值得补测
- [ ] **B3-5 错误边界**：无 ErrorBoundary/onErrorCaptured，列表加载失败部分静默吞掉
- [ ] **B3-6 token 刷新链路**：后端有 refresh token，前端未接，过期即跳登录页
- [ ] **B3-7 数据集上传支持 stratum 列**：CSV 目前仅 question/ground_truth 两列，配合 B2-4 扩展表头
- [ ] **B3-8 实验记录时间线**：同一数据集多轮 run 的配置-指标演进视图（调优历史）

**B4. 部署与流程 — 低优先级**

- [ ] **B4-1 前端部署编排**：dist 有产物但无 nginx 配置 / docker-compose 前端服务
- [ ] **B4-2 CI/CD**：无流水线（至少 typecheck + 后端冒烟脚本自动化）
- [ ] **B4-3 EVAL_MAX_CONCURRENCY 前端可视化配置**：现仅 .env 改后需重启，可挂到 /system/config
- [ ] **B4-4 i18n / 暗色主题**（锦上添花）

---

## 四、逐条核对速查表（开发进度 Checklist）

| # | 模块 | 项 | 状态 |
|---|---|---|---|
| 1 | 数据集 | CRUD + 级联删除 | ✅ |
| 2 | 数据集 | 批量导入（≤500） | ✅ |
| 3 | 数据集 | CSV 前端校验解析 | ✅ |
| 4 | 数据集 | 分层 stratum 字段与按层聚合 | □ B2-4 |
| 5 | 任务 | 异步执行 + 状态轮询 | ✅ |
| 6 | 任务 | RAGAS 四指标打分 | ✅ |
| 7 | 任务 | rag_config 持久化 + search_mode/top_k 请求级生效 | ✅ |
| 8 | 任务 | chunk_size 请求级生效 | □ B2-3 |
| 9 | 任务 | 并发评测（1~10 可配） | ✅ |
| 10 | 任务 | RAGAS RunConfig 超时修复 | □ B1-1（最高优先） |
| 11 | 任务 | judge 链路 timeout/retries | □ B1-2 |
| 12 | 任务 | judge 切 SiliconFlow | □ B1-3 |
| 13 | 任务 | 任务队列化/僵死恢复 | □ B2-1 |
| 14 | 结果 | 得分明细 + CSV 导出 | ✅ |
| 15 | 结果 | BadCase 前端筛选 | ✅ |
| 16 | 结果 | BadCase 服务端接口 | □ B1-4 |
| 17 | 结果 | 检索命中溯源可视化 | □ B3-3 |
| 18 | A/B | 同数据集校验 + 联动过滤 | ✅ |
| 19 | A/B | question 对齐 + Δ 计算 + 标黄 | ✅ |
| 20 | A/B | ECharts 柱状图 | ✅ |
| 21 | A/B | Markdown 报告导出 | ✅ |
| 22 | A/B | 后端聚合接口（大样本） | □ B1-5 |
| 23 | A/B | 雷达图/散点图/多组对比 | □ B3-2 |
| 24 | 鉴权 | 登录/Bearer/角色权限 | ✅ |
| 25 | 鉴权 | admin 路由守卫 | □ B3-1 |
| 26 | 鉴权 | refresh token 前端接入 | □ B3-6 |
| 27 | 配置 | LLM /system/config 热更新 | ✅ |
| 28 | 配置 | 并发参数可视化配置 | □ B4-3 |
| 29 | 质量 | 集成测试 35 例 | ✅（2 FAIL 待修：B1-6） |
| 30 | 质量 | 前端单测 | □ B3-4 |
| 31 | 质量 | Alembic 迁移 | □ B2-2 |
| 32 | 部署 | Dockerfile + compose（后端） | ✅ |
| 33 | 部署 | 前端部署编排 | □ B4-1 |
| 34 | 部署 | CI/CD | □ B4-4 |

---

## 五、风险与备注

1. **鉴权口径澄清**：后端实际是 **Redis Session Token（HMAC 签名）**而非标准 JWT，前端 README 称「JWT Bearer」只是传输形态（Bearer 头）。功能无影响，但文档口径建议统一。
2. **无 TODO/FIXME 残留**：`app/` 下 grep 零命中，代码无显式未完成标记——缺失项都是「设计上就没做」，需按本清单补齐。
3. **已知系统约束**（非缺陷，但做实验时要记住）：chunk_size 仅展示；`hash` embedding 会致 RAGAS 分数失真（有日志告警）；单次导入 ≤500 条。
4. **并发提速结论**（已实测）：并发 4 时 30 题 13.2 min，瓶颈在 DeepSeek 官方 API 尾部延迟而非并发代码；切 SiliconFlow（B1-3）是性价比最高的下一步。
