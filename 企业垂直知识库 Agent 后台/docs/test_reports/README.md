# 测试报告（可直接提交到仓库作为质量证据）

本目录由 pytest 生成，**内容是真实执行结果，未做任何人工修改**。

| 文件 | 内容 |
|---|---|
| `report.html` | pytest-html 单文件报告：全部 **560 个用例（560 passed / 0 skipped / 0 failed）** 的用例名、耗时、状态、失败堆栈 |
| `coverage/index.html` | coverage.py HTML 报告：`app/` 下每个文件的语句数、未覆盖行、覆盖率 |
| `last_run.log` | 最近一次运行的完整终端输出（含覆盖率逐文件明细） |
| `archive/` | 历史报告归档（含 2026-10-27 真实 MySQL 环境跑出的 21 例失败，见下文「环境口径」） |

## 当前报告对应的运行环境

| 组件 | 口径 | 说明 |
|---|---|---|
| 数据库 | **SQLite 文件库（每个用例独立临时库）** | 由 `tests/conftest.py` 注入，用例级隔离 |
| Redis | **真实 Redis 127.0.0.1:6379/db1（Docker redis-stack）** | 7 个令牌桶用例（Lua EVAL）必须在真实 Redis 上跑 |
| LLM / Embedding | Mock（`LLM_PROVIDER=mock`） | 不消耗额度、不依赖外网 |

总耗时 **78.33 秒**（560 例）。

### 为什么数据库不用真实 MySQL

项目一的 560 个用例是**单元 / 接口契约测试**：全程 `TestClient` + `dependency_overrides` + 服务层 mock，
按测试金字塔本就应跑在独立临时库上。强行切到真实 MySQL 会同时触发两类问题：

1. **数据可见性前提被破坏**：多个用例依赖「同一连接内未提交数据对其他会话可见」（SQLite `StaticPool`
   单连接特性）。MySQL 是多连接 + 独立事务隔离，后台线程读不到主会话未提交的 run 记录，
   表现为 `评测任务未认领（不存在或已取消）run_id=1`、`status == 'pending'`。
2. **⚠️ 致命：会删生产表**。以下 6 个文件的夹具在 `finally` 中执行 `Base.metadata.drop_all(bind=engine)`，
   而 `engine` 是**全局引擎**（受 `DATABASE_URL` 控制）：

   ```
   tests/test_eval_concurrency.py:105
   tests/test_eval_rag_config.py:58
   tests/test_eval_run_durability.py:69
   tests/test_eval_scoring_guardrails.py:169
   tests/test_retrieval_embedding_fallback.py:87
   tests/test_retriever_search_mode.py:79
   ```

   SQLite 下无害；一旦 `DATABASE_URL` 指向 `kb_agent`，每执行一个用例就 `DROP` 一次真实业务表。
   **在加装护栏前，禁止把 `DATABASE_URL` 指向任何真实库跑全量测试。**

真实 MySQL 的集成验证应单独做（独立 `kb_agent_test` 库 + 独立用例），不要混入这 560 个单元用例。

## 如何重新生成

```bash
# 1) 安装测试依赖（只影响测试，不改运行时依赖）
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt

# 2) 全量测试 + 两份报告
#    FAKE_REDIS=false + REDIS_URL 指向真实 Redis db1；不覆盖 DATABASE_URL（保持 conftest 的 sqlite 隔离）
#    --basetemp 每次换新目录，避免 safe-delete 守卫误报
FAKE_REDIS=false REDIS_URL="redis://127.0.0.1:6379/1" SECRET_KEY="pytest-secret-key" LLM_PROVIDER=mock \
.venv/Scripts/python.exe -m pytest tests/ \
  --basetemp=".pytest_tmp/run_$(date +%H%M%S)" \
  --html=docs/test_reports/report.html --self-contained-html \
  --cov=app --cov-report=term --cov-report=html:docs/test_reports/coverage
```

只看覆盖率、不重跑测试：

```bash
.venv/Scripts/python.exe -m coverage report --sort=cover
```

## 关于 Redis 与 skipped

`tests/test_rate_limit_enforcement.py` 有 7 例需要**真实 Redis**（令牌桶是 Lua + Redis hash，
fakeredis 不支持 EVAL）：

| 环境 | 结果 |
|---|---|
| Redis 在线（本仓库当前报告 / CI） | **560 passed / 0 skipped** |
| Redis 未启动（本地未开 Docker） | 553 passed / 7 skipped（`pytest.skip` 写明原因，不算失败） |

## 关于覆盖率

当前 `TOTAL 82%`（4819 语句 / 860 未覆盖）。未覆盖主要集中在依赖外部服务或文件 I/O 的薄封装层：

| 模块 | 覆盖率 | 原因 |
|---|---|---|
| `services/document_service.py` | 16% | 解析真实 PDF/DOCX 并写向量库，单测需构造真实文件 |
| `rag/embeddings.py` | 22% | 需真实 embedding 服务（充值前上游 402，已做 BM25 降级） |
| `services/conversation_service.py` | 25% | 纯 CRUD 封装 |
| `services/trace_service.py` | 27% | 纯查询封装 |
| `rag/vector_store.py` | 34% | Chroma 持久化 |

反之，风险最集中的部分覆盖很高：`rag/citation_guard.py` **100%**、`rag/bm25.py` 97%、
`rag/retriever.py` **95%**、`rag/splitter.py` 94%、`services/eval_service.py` 86%、
`agent/graph.py` 69%（分支多、含路由回退）。

## 归档说明

| 归档 | 说明 |
|---|---|
| `archive/report_20261025.html` | 2026-10-25 版（551 passed，82%） |
| `archive/report_mysql_20261027.html` | 2026-10-27 **真实 MySQL 未隔离**跑出的结果：14 failed / 535 passed / 7 errors。用于证明上述两类问题真实存在，**不可作为质量达标证据** |
