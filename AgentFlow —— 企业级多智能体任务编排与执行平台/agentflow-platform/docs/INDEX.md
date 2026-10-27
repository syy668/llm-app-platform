# 文档索引

> AgentFlow Platform `v1.0.0` · 全部 W1~W5 文档已归档至 `docs/`，根目录只保留 `README.md` 与 `CHANGELOG.md`。

---

## 1. 我该看哪一份？

| 你想知道 | 看这里 |
|---|---|
| 项目是什么、怎么跑起来、架构长什么样 | [`../README.md`](../README.md) |
| 每一轮改了什么、修了什么 Bug | [`../CHANGELOG.md`](../CHANGELOG.md) |
| 接口怎么调（审批 / 记忆 / 导出 / 知识库自检，含真实响应示例） | [`API参考-v1.1.md`](API参考-v1.1.md) |
| v1.2 新接口（工具注册表 / 限流 429 / 评审人工批注） | [`API参考-v1.2.md`](API参考-v1.2.md) |
| v1.3 新接口（任务归档 / 列表 archived 口径 / 看板含归档口径） | [`API参考-v1.3.md`](API参考-v1.3.md) |
| v1.3 审批超时收口（PRD L98/L245/L382：EXPIRED + FAILED + 断点清理 + 红点口径） | [`审批超时收口-v1.3.md`](审批超时收口-v1.3.md) |
| 怎么部署到生产（拓扑/变量/扩容/网关/监控） | [`deploy/production-deploy-guide.md`](deploy/production-deploy-guide.md) |
| W1 交付了什么（鉴权/ORM/脚手架） | [`delivery/README-W1.md`](delivery/README-W1.md) |
| W2 交付了什么（编排内核/MCP/上下文） | [`delivery/README-W2.md`](delivery/README-W2.md) |
| W3 交付了什么（SSE/审批断点续跑/前端） | [`delivery/README-W3.md`](delivery/README-W3.md) |
| W4 交付了什么（评测/校验/A-B/Demo） | [`delivery/README-W4.md`](delivery/README-W4.md) |
| W5-Part1 交付了什么（MCP HTTP/三层记忆） | [`delivery/README-W5-Part1.md`](delivery/README-W5-Part1.md) |
| W5-Part2 交付了什么（容器化/OTel/CI/加固） | [`delivery/README-W5.md`](delivery/README-W5.md) |
| W1~W4 整体盘点与 P2 缺口 | [`delivery/W1-W4项目总览与总结.md`](delivery/W1-W4项目总览与总结.md) |
| 某个阶段的验收证据逐条核对 | [`checklists/`](checklists/) |
| 评测/压缩 A/B 的实测数字与结论 | [`reports/`](reports/) |
| 主干保护、PR 与 hotfix 流程 | [`../CONTRIBUTING.md`](../CONTRIBUTING.md) |

---

## 2. 交付说明（`docs/delivery/`）

| 文档 | 阶段主题 | 对应用例 |
|---|---|---:|
| `README-W1.md` | 工程骨架、双 Token 鉴权、TraceID、健康探针、compose | 12 |
| `README-W2.md` | LangGraph 7 节点、4 个 stdio MCP Server、SQL 五道闸门、上下文压缩 | 120 |
| `README-W3.md` | SSE 四类事件、审批断点续跑、取消、轨迹回放、Vue3 MVP | 18 |
| `README-W4.md` | 30 条分层难例评测、种子硬断言、KB 适配层、压缩 A/B、Demo 三链路 | 0（脚本） |
| `README-W5-Part1.md` | MCP HTTP 第二套传输、FR-014 三层记忆、记忆管理 API | 57 |
| `README-W5.md` | 多阶段镜像、多副本 SSE 会话、OTel 五层埋点、CI 五段门禁、边界加固 | 100 |
| `W1-W4项目总览与总结.md` | W1~W4 目录总览 + 已完成功能 + P2 待办 | — |

## 3. 自测核对清单（`docs/checklists/`）

| 文档 | 核对段 |
|---|---|
| `W2-自测核对清单.md` | 编排内核 / MCP / 上下文 |
| `W3-自测核对清单.md` | SSE / 审批 / 取消 / 回放 / 前端 |
| `W4-自测核对清单.md` | A~J 九段（评测、种子、适配层、A/B、Demo、联调） |
| `W5-Part1-自测核对清单.md` | 双传输 / 三层记忆 |
| `W5-Part2-自测核对清单.md` | A~I 段（容器、会话、OTel、CI、加固） |
| `P2收尾-自测核对清单.md` | A~I 段（RAG 契约、记忆生命周期、开关零执行） |

## 4. 部署文档（`docs/deploy/`）

`production-deploy-guide.md` —— 拓扑图、必填环境变量、多副本扩容、Redis key 表、nginx 网关配置、
Collector 部署、PromQL 速查、探针语义、已知限制、安全基线。

## 5. 实验报告（`docs/reports/`）

| 文档 | 内容 |
|---|---|
| `eval_report_full.md` | 30 条分层难例评测报告（成功率 / GMV / token / P95 / faithfulness） |
| `AB实验报告_full.md` | 上下文压缩 A/B 报告（16 项指标，压缩率 73.2%） |
| `Demo三条链路_offline.md` | 三条演示链路逐步骤结果（21/21） |
| `AB实验报告模板.md` | 通用 A/B 实验报告模板（供后续复跑复用） |

> 原始明细（`eval_output/*.json`、`*.csv`）为运行期产物，可由
> `python scripts/eval_runner.py --tag full` / `python scripts/ab_compression.py --tag full` 重建，不入库。

---

## 6. 完整项目目录树

> 已排除：`.venv/`、`__pycache__/`、`.pytest_cache/`、`.ruff_cache/`、覆盖率缓存、容器镜像缓存、
> `eval_output/` 下的原始明细（仅保留 4 份人读报告于 `docs/reports/`）。

```
agentflow-platform/
├── alembic/
│   ├── versions/
│   │   └── .gitkeep
│   ├── env.py
│   └── script.py.mako
├── app/
│   ├── agents/                          # W2 · LangGraph 编排内核
│   │   ├── __init__.py
│   │   ├── common.py                    # State 容错读取 / 记账 / 风险判定
│   │   ├── executor.py                  # 按 DAG 执行 + 重试矩阵 + 结果瘦身
│   │   ├── graph.py                     # 7 节点装配 + 5 个纯函数路由
│   │   ├── human_gate.py                # 审批断点（建单幂等 → interrupt → 读决策）
│   │   ├── llm.py                       # OpenAI 兼容 + Mock + 主备降级门面
│   │   ├── notifier.py                  # 审批闸门（APPROVAL_MISSING）
│   │   ├── planner.py                   # 拆解 → 结构化 → DAG 校验 → 兜底
│   │   ├── prompts.py                   # 版本化 system prompt
│   │   ├── replan.py                    # 重规划（≤2 次）
│   │   ├── reviewer.py                  # 逐条 acceptance 验收
│   │   ├── structured.py                # JSON 提取与修复重试
│   │   └── summarizer.py                # 带证据引用的报告汇总
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py                    # W1 · Settings（MYSQL_* 派生 database_url）
│   │   ├── context.py                   # 请求上下文（trace_id / user）
│   │   ├── deps.py                      # 依赖注入
│   │   ├── exceptions.py                # 统一异常与错误响应
│   │   ├── logging.py                   # 结构化日志
│   │   ├── redis_client.py              # Redis 连接（可降级）
│   │   ├── resilience_guard.py          # ★ W5-P2 · SSE/任务边界加固
│   │   ├── security.py                  # JWT + bcrypt + RBAC
│   │   ├── trace.py                     # 跨层 trace 工具
│   │   ├── tracing_middleware.py        # ★ W5-P2 · HTTP span 中间件
│   │   ├── w2_config.py                 # W2 配置
│   │   ├── w3_config.py                 # W3 配置
│   │   ├── w3_errors.py                 # W3 错误码
│   │   ├── w5_config.py                 # W5-P1 配置
│   │   └── w6_config.py                 # ★ W5-P2 配置（会话后端/OTel/SSE）
│   ├── db/
│   │   ├── __init__.py
│   │   ├── base.py                      # Declarative Base
│   │   ├── checkpointer.py              # checkpointer 工厂（redis / memory）
│   │   ├── session.py                   # async engine / sessionmaker
│   │   └── types.py                     # 自定义列类型
│   ├── mcp/                             # W5-P1 / W5-P2
│   │   ├── __init__.py
│   │   ├── http_entry.py                # 双传输单进程入口
│   │   ├── session_backend.py           # ★ W5-P2 · Redis 会话注册表/队列
│   │   └── transport_http.py            # W5-P1 · HTTP 服务端 + 客户端传输
│   ├── memory/
│   │   ├── __init__.py
│   │   ├── layered_memory.py            # W5-P1 · FR-014 三层记忆
│   │   ├── memory_lifecycle.py          # ★ P2 · 去重 + 衰减 + TTL
│   │   └── rag_schema.py                # ★ P2 · RAG 六字段契约
│   ├── observability/
│   │   ├── __init__.py
│   │   └── otel_tracing.py              # ★ W5-P2 · 埋点与指标
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── api_router.py                # 汇总挂载
│   │   ├── approvals.py                 # W3 · 审批收件箱
│   │   ├── auth.py                      # W1 · 登录/刷新/登出/me
│   │   ├── health.py                    # W1 · /healthz /readyz /api/v1/health
│   │   ├── memory_router.py             # W5-P1 · 记忆管理 API
│   │   ├── tasks.py                     # W2 · 建任务 / 查详情
│   │   └── tasks_ext.py                 # W3 · SSE / 审批 / 取消 / 快照 / 轨迹
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── agent_io.py                  # Agent 入出参契约
│   │   ├── auth.py
│   │   ├── common.py                    # ApiResponse 信封
│   │   ├── context.py                   # 压缩/瘦身契约
│   │   ├── enums.py                     # TaskStatus / MemoryScope …
│   │   ├── graph_state.py               # TaskState + reducer
│   │   ├── mcp.py                       # JSON-RPC + 4 工具入出参
│   │   ├── orm_models.py                # W1 · 8 张表
│   │   ├── orm_models_ext.py            # W2 · mock_messages
│   │   ├── w3.py                        # SSE 事件契约
│   │   └── w5.py                        # 记忆管理入参
│   ├── services/
│   │   ├── __init__.py
│   │   ├── approval_service.py           # 审批单 CRUD（幂等建单）
│   │   ├── auth_service.py               # 登录/刷新/登出
│   │   ├── cancel_service.py             # 取消信号（Redis 标记）
│   │   ├── checkpoint_service.py         # checkpoint 配置与清理
│   │   ├── checkpoint_store.py           # 断点存取封装
│   │   ├── context_service.py            # 上下文构造 + 压缩 + 瘦身
│   │   ├── eval_gateway.py               # 项目二评测门面
│   │   ├── event_bus.py                  # Redis Stream 事件总线
│   │   ├── interrupt_service.py          # 挂起判定 + 收件箱 + 过期
│   │   ├── memory_service.py             # 短期记忆
│   │   ├── plan_service.py               # 计划落库与读取
│   │   ├── rag_gateway.py                # 项目一 RAG 门面
│   │   ├── snapshot_service.py           # 快照 + 轨迹回放
│   │   ├── stream_context.py             # StreamContext + MCP 事件钩子
│   │   ├── stream_service.py             # astream_events 驱动 + SSE 帧
│   │   ├── task_service.py               # 建任务 + 驱动图
│   │   ├── token_store.py                # token 计量
│   │   ├── trace_service.py              # 轨迹落库与查询
│   │   └── w3_runtime.py                 # 运行时装配
│   ├── tools/
│   │   ├── mcp_servers/
│   │   │   ├── __init__.py
│   │   │   ├── chart_render.py            # 图表渲染（process pool + 超时）
│   │   │   ├── kb_search.py               # 知识库检索
│   │   │   ├── notify.py                  # 消息发送（审批闸门）
│   │   │   └── sql_query.py               # SQL 五道安全闸门
│   │   ├── __init__.py
│   │   ├── kb_adapter.py                  # W4 · 熔断 + mall 只读兜底
│   │   ├── mcp_client.py                  # MCP 客户端池
│   │   └── mcp_stdio.py                   # stdio 传输与子进程管理
│   ├── utils/
│   │   ├── __init__.py
│   │   ├── dag.py                         # 拓扑排序 / 环检测
│   │   ├── ids.py                         # task_no / trace_id
│   │   ├── masking.py                     # 敏感信息脱敏
│   │   ├── time.py                        # 时间窗解析
│   │   └── tokens.py                      # token 估算
│   ├── __init__.py
│   ├── bootstrap.py                       # ★ W5-P2 · 生产装配层入口
│   └── main.py                            # FastAPI 应用
├── deploy/
│   ├── legacy/
│   │   └── Dockerfile.w1-single-stage     # W1 单阶段镜像（回滚参考）
│   └── otel-collector-config.yaml         # OTel Collector 配置
├── docs/                                  # ★ v1.0.0 文档归档
│   ├── checklists/
│   │   ├── P2收尾-自测核对清单.md
│   │   ├── W2-自测核对清单.md
│   │   ├── W3-自测核对清单.md
│   │   ├── W4-自测核对清单.md
│   │   ├── W5-Part1-自测核对清单.md
│   │   └── W5-Part2-自测核对清单.md
│   ├── delivery/
│   │   ├── README-W1.md
│   │   ├── README-W2.md
│   │   ├── README-W3.md
│   │   ├── README-W4.md
│   │   ├── README-W5-Part1.md
│   │   ├── README-W5.md
│   │   └── W1-W4项目总览与总结.md
│   ├── deploy/
│   │   └── production-deploy-guide.md
│   ├── reports/
│   │   ├── AB实验报告_full.md
│   │   ├── AB实验报告模板.md
│   │   ├── Demo三条链路_offline.md
│   │   └── eval_report_full.md
│   └── INDEX.md                           # 本文件
├── eval_datasets/
│   └── 分层难例评测集-manifest.json        # W4 · 30 条 L1~L6
├── eval_output/                           # 运行期产物（可重建，不入库）
│   ├── ab_detail_full.json
│   ├── ab_metrics_full.csv
│   ├── ab_samples_full.csv
│   ├── demo_detail_offline.json
│   ├── eval_detail_full.json
│   ├── eval_metrics_full.csv
│   └── eval_summary_full.csv
├── frontend/                              # W3 · Vue3 静态 MVP（无构建）
│   ├── src/
│   │   ├── components/
│   │   │   ├── approval-modal.js
│   │   │   ├── dag.js
│   │   │   └── event-console.js
│   │   ├── pages/
│   │   │   ├── detail.js
│   │   │   ├── inbox.js
│   │   │   └── submit.js
│   │   ├── api.js
│   │   ├── main.js
│   │   ├── sse.js
│   │   ├── store.js
│   │   ├── styles.css
│   │   └── util.js
│   ├── .gitkeep
│   ├── README.md
│   └── index.html
├── scripts/
│   ├── README.md
│   ├── ab_compression.py                  # W4 · 压缩 A/B
│   ├── demo_three_flows.py                # W4 · 三条演示链路
│   ├── eval_runner.py                     # W4 · 评测跑批
│   ├── seed_w3_demo_users.py              # W3 · 演示账号种子
│   ├── verify_deploy_manifest.py          # ★ W5-P2 · 26 项部署静态校验
│   ├── verify_seed.py                     # W4 · 种子硬断言 + 断点抽样
│   └── w4_common.py                       # W4 · 嵌入式 harness
├── storage/                               # 运行期本地存储（图产物已 gitignore）
│   ├── charts/
│   │   └── .gitkeep
│   └── verify_seed.json
├── tests/                                 # 19 个文件 · 412 用例
│   ├── conftest.py
│   ├── test_auth.py                       # W1 · 12
│   ├── test_context_service.py            # W2 · 17
│   ├── test_executor.py                   # W2 · 12
│   ├── test_graph_routing.py              # W2 · 17
│   ├── test_mcp_client.py                 # W2 · 15
│   ├── test_planner.py                    # W2 · 12
│   ├── test_replan.py                     # W2 · 8
│   ├── test_sql_query_security.py         # W2 · 35
│   ├── test_w2_e2e.py                     # W2 · 4
│   ├── test_w3_e2e.py                     # W3 · 18
│   ├── test_w5_layered_memory.py          # W5-P1 · 31
│   ├── test_w5_mcp_http.py                # W5-P1 · 26
│   ├── test_w6_bootstrap.py               # W5-P2 · 12
│   ├── test_w6_otel.py                    # W5-P2 · 13
│   ├── test_w6_platform_extra.py          # W5-P2 · 40
│   ├── test_w6_resilience.py              # W5-P2 · 21
│   ├── test_w6_session_backend.py         # W5-P2 · 14
│   ├── test_p2_memory_lifecycle.py        # P2 · 46
│   └── test_p2_rag_schema.py              # P2 · 59
├── .github/
│   └── workflows/
│       └── ci-test.yml                    # ★ W5-P2 · 5 job 门禁
├── .dockerignore
├── .env.example
├── .gitignore
├── CHANGELOG.md                           # ★ v1.0.0 逐轮变更记录
├── CONTRIBUTING.md                        # ★ v1.0.0 主干保护与 PR 流程
├── Dockerfile                             # 多阶段构建（非 root）
├── README.md                              # ★ v1.0.0 根文档
├── alembic.ini
├── docker-compose.yml                     # 单机快速栈
├── docker-compose.dev.yml                 # 开发热重载
├── docker-compose.prod.yml                # 生产多副本
├── pyproject.toml                         # ruff + coverage
├── pytest.ini
├── requirements-dev.txt
├── requirements-observability.txt
└── requirements.txt
```

---

## 7. 规模统计

| 类别 | 数量 |
|---|---:|
| 后端 Python 模块（`app/`） | 102 |
| 前端文件（`frontend/`） | 15 |
| 运维/评测脚本（`scripts/`） | 7 |
| 测试文件（`tests/`） | 20（含 `conftest.py`） |
| 测试用例 | **412** |
| 文档（`docs/` 19 + 根 3 + 组件说明 3） | 25 |
| 容器 / 编排 / CI 配置 | 6 份 |
