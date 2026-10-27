# AgentFlow Platform —— W5 交付说明（Part1 + Part2）

> 版本：`0.5.2-w5p2`
> 阶段定位：把 W1~W4 的「能跑」升级为「能上生产」——两套传输、三层记忆、容器化多副本、全链路可观测、CI 门禁。

| 阶段 | 主题 | 交付 | 用例 |
|---|---|---|---|
| W1 | 工程骨架 | 7 张表 ORM、双 Token 鉴权、TraceID 中间件、全局异常、健康检查 | 12 |
| W2 | 编排内核 | LangGraph 7 节点、Planner-Executor-Reviewer、4 个 stdio MCP Server、SQL 安全校验、上下文压缩 | 120 |
| W3 | 交互闭环 | SSE 流式、审批断点续跑、取消/快照/轨迹回放、Vue 前端 | 18 |
| W4 | 评测体系 | 30 条分层难例评测、种子硬断言、压缩 A/B、Demo 三链路 | 0（脚本） |
| **W5-Part1** | **P2 功能** | **MCP HTTP 传输（第二套）+ FR-014 三层记忆 + 记忆管理 API** | **+57** |
| **W5-Part2** | **工程化** | **多阶段镜像 + 多副本 SSE 会话 + OTel 全链路 + GitHub CI + 边界加固** | **+100** |
| **合计** | — | — | **307 passed** |

---

## 1. W5-Part1 回顾（已完成，本阶段只读）

| 能力 | 关键点 |
|---|---|
| MCP HTTP 传输 | `HttpTransport` 实现 W2 的 `MCPTransport` 抽象，因此 `MCPClient` / `MCPClientPool` 一行不改就能跑 HTTP；服务端只调 `MCPServerApp.handle`，**4 个工具实现零改动**。stdio 与 HTTP 可同时服务同一个实例。 |
| FR-014 三层记忆 | 短期（Redis，24h）/ 中期（压缩摘要落 MySQL `memories(scope='mid')`，30 天）/ 长期（Reviewer ≥3 分归档，向量化或本地关键词兜底）。注入点是 W2 早就预留的 `TaskState.memory_hits` + 提示词「长期记忆（参考，可裁剪）」插槽。 |
| 开关 | `MCP_HTTP_ENABLED` / `ENABLE_LAYERED_MEMORY` **默认 false** —— 这是 207 条基线零回归的前提。 |

---

## 2. W5-Part2 交付内容

### 2.1 任务 1：容器化 + 多副本 SSE 会话

**问题**：W5-Part1 的会话表在进程内 `dict` 里。多副本下 `GET /sse` 落到 Pod-A、`POST /messages` 落到 Pod-B，Pod-B 查不到会话 → 恒 404，SSE 直接断；Pod 重启时所有会话蒸发。

**解法**：只换「会话存储」，不动事件流生成器。

```
GET  /mcp/v1/sse       → registry.create()   → Redis hash + zset index
POST /mcp/v1/messages  → registry.get()      → Redis HGETALL（跨 Pod 可见）
                         session.queue.put() → LPUSH mcp:sess:<id>:q
sse_event_stream()     → session.queue.get() → RPOP（生成器一行未改）
```

`RedisSessionQueue` 用鸭子类型满足 `session.queue` 的 `get()/qsize()`，因此
`sse_event_stream()` **逐字节未动**——这是「只替换存储实现」的硬证据。

| 文件 | 作用 |
|---|---|
| `app/mcp/session_backend.py` | `RedisSessionQueue` / `RedisSessionRegistry` / `build_session_registry` |
| `Dockerfile` | 多阶段构建（builder venv → runtime），非 root，内置 HEALTHCHECK |
| `docker-compose.prod.yml` | mysql / redis / migrate / backend×N / mcp-http×N / 可选 otel-collector |
| `docker-compose.dev.yml` | 源码挂载 + `--reload` + 独立 mcp-http |
| `deploy/otel-collector-config.yaml` | OTLP 接入 + spanmetrics + Prometheus 暴露 |
| `deploy/Dockerfile.w1-single-stage` | 旧单阶段镜像备份（回滚参考） |

关键取舍：**元数据走同步 Redis 客户端（50ms 有界超时），消息队列走异步客户端**。
原因见 `production-deploy-guide.md` §4.2；Redis 抖动时自动降级为进程内会话表，请求不会 500。

### 2.2 任务 2：OpenTelemetry 全链路埋点

| 层 | 实现方式 | span |
|---|---|---|
| HTTP | 新增中间件 `OtelTracingMiddleware` | `GET /api/v1/tasks/{id}` |
| 图执行 | 回调 `GraphNodeSpanHandler` + 方法包装 | `graph.run` → `graph.node planner/executor/...` |
| MCP | 包装 `MCPClient.call_tool`（stdio/HTTP 汇合点） | `mcp.tools/call sql-query` |
| LLM | 包装 `LLMClient.complete`（主备门面） | `gen_ai.chat` |
| SQL | SQLAlchemy 引擎事件 | `db.select` / `db.update` |

**埋点即旁路**：4 处包装全是「进入开 span / 返回关 span」，不改参数、不改返回值、不改异常。
`ENABLE_OTEL=false` 时**一个 patch 都不打**。

导出：优先 OTLP/HTTP；启动时 TCP 探测 + `_FallbackExporter`（连续 3 次失败切控制台），
保证「没有采集器链路也不空」。

指标（`GET /metrics`，Prometheus 文本）：`agentflow_mcp_calls_total`、
`agentflow_mcp_call_duration_seconds`（含 P50/P95/P99）、`agentflow_llm_tokens_total`、
`agentflow_graph_node_duration_seconds`、`agentflow_http_requests_total`、`agentflow_db_calls_total`。

| 文件 | 作用 |
|---|---|
| `app/observability/otel_tracing.py` | TracerProvider 装配、指标注册表、四类自动埋点、`/observability/status` 数据源 |
| `app/core/tracing_middleware.py` | HTTP span 中间件 + `install_tracing(app)` |
| `app/bootstrap.py` | 生产装配层：探针 + 指标 + 可选 OTel + 可选 MCP HTTP（不改 `app/main.py`） |

### 2.3 任务 3：GitHub Actions CI

```
quality（ruff + 格式 + 部署清单静态校验）
   ├── test × 4 分片（matrix: py3.11，并行）
   ├── coverage（新增模块 ≥85% 门禁 + 全仓基线留档）
   └── scripts（verify_seed.py + demo_three_flows.py）
          └── docker（main/tag 触发，GHCR + 容器 /health 冒烟）
```

| 文件 | 作用 |
|---|---|
| `.github/workflows/ci-test.yml` | 5 个 job，含 4 分片测试矩阵与覆盖率门禁 |
| `pyproject.toml` | ruff 配置（含冻结基线豁免清单）+ coverage 配置 |
| `requirements-observability.txt` | OTel 依赖独立清单（不污染 W1 的 `requirements.txt`） |
| `scripts/verify_deploy_manifest.py` | 26 项部署不变量静态校验（无 Docker 也能跑） |

### 2.4 任务 4：边界加固

| 文件 | 能力 |
|---|---|
| `app/core/resilience_guard.py` | `sse_guard()`（心跳 / 空闲超时 / 连接上限 / 重连提示 / 优雅回收）、`@async_guard` / `@guard`、`release_task_resources()`、`check_task_consistency()` |

`sse_guard` 用**泵任务 + 队列**解耦，而不是 `wait_for(anext(inner))`——
后者会在超时时把内层异步生成器取消掉（表现为「超时一次，流就死了」）。

`release_task_resources` 只清 `cancel:{id}` / `events:{id}` / `memory:short:{id}`，
**保留 `ckpt:{id}`**：断点是审计与回放资产，不该被取消动作抹掉。

---

## 3. 工程化部署章节

完整教程见 **`production-deploy-guide.md`**，这里只放最短路径。

### 3.1 生产（多副本）

```bash
cp .env.example .env      # 必填：SECRET_KEY / MYSQL_PASSWORD / MYSQL_ROOT_PASSWORD / MCP_HTTP_API_KEY
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps

curl -s localhost:<port>/health   # liveness
curl -s localhost:<port>/ready    # readiness（MySQL + Redis + 会话注册表）
```

默认 `backend` / `mcp-http` 各 2 副本，会话表在 Redis：

```bash
docker compose -f docker-compose.prod.yml up -d --scale backend=4 --scale mcp-http=3
```

> 端口写成 `- "8000"`（只声明容器端口）才能安全扩容；写 `"8000:8000"` 第二个副本直接绑不上。
> `verify_deploy_manifest.py` 的 **P6** 会在 CI 里挡住这种写法。

### 3.2 开发（热重载）

```bash
docker compose -f docker-compose.dev.yml up -d
docker compose -f docker-compose.dev.yml logs -f backend
```

### 3.3 可观测性

```bash
# 起采集器（profile，默认不启动）
docker compose -f docker-compose.prod.yml --profile observability up -d otel-collector
curl -s localhost:8889/metrics | head

# 应用侧开关
ENABLE_OTEL=true OTEL_EXPORTER=otlp OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
```

链路验证：

```bash
curl -si localhost:8000/api/v1/health | grep -i x-trace-id   # 拿 trace id
curl -s  localhost:8000/observability/status | jq '.otel'
docker compose logs --tail=50 otel-collector | grep graph.node
```

### 3.4 本地离线验证（不需要任何容器）

```bash
pip install -r requirements-dev.txt -r requirements-observability.txt
pytest -q                                    # 307 passed
python scripts/verify_seed.py                # 26/26 · SEED VERIFIED
python scripts/demo_three_flows.py           # 3/3 链路 · 21/21 步骤
python scripts/verify_deploy_manifest.py     # 26/26 · DEPLOY MANIFEST OK
```

---

## 4. 目录结构增量（W5）

```
agentflow-platform/
├── app/
│   ├── bootstrap.py                     ★ W5-P2 生产装配层（探针 + 指标 + OTel + MCP HTTP）
│   ├── core/
│   │   ├── w5_config.py                 W5-P1 配置（只读）
│   │   ├── w6_config.py                 ★ W5-P2 配置（会话后端 / OTel / SSE 守护）
│   │   ├── tracing_middleware.py        ★ W5-P2 HTTP span 中间件
│   │   └── resilience_guard.py          ★ W5-P2 边界加固
│   ├── mcp/
│   │   ├── transport_http.py            W5-P1 HTTP 传输（只读）
│   │   ├── http_entry.py                W5-P1 双传输单进程入口（只读）
│   │   └── session_backend.py           ★ W5-P2 Redis 会话注册表 / 队列
│   ├── memory/layered_memory.py         W5-P1 三层记忆（只读）
│   ├── observability/
│   │   ├── __init__.py                  ★ W5-P2
│   │   └── otel_tracing.py              ★ W5-P2 埋点与指标
│   ├── routers/memory_router.py         W5-P1 记忆管理 API（只读）
│   └── schemas/w5.py                    W5-P1 Schema（只读）
├── tests/
│   ├── test_w5_mcp_http.py              W5-P1（26 条，只读）
│   ├── test_w5_layered_memory.py        W5-P1（31 条，只读）
│   ├── test_w6_session_backend.py       ★ 14 条
│   ├── test_w6_otel.py                  ★ 13 条
│   ├── test_w6_resilience.py            ★ 21 条
│   ├── test_w6_bootstrap.py             ★ 12 条
│   └── test_w6_platform_extra.py        ★ 40 条
├── deploy/
│   ├── Dockerfile.w1-single-stage       ★ 旧镜像备份（回滚）
│   ├── docker-compose.w1.yml            ★ 旧编排备份
│   └── otel-collector-config.yaml       ★ 采集器配置
├── .github/workflows/ci-test.yml        ★ CI 流水线
├── Dockerfile                           ← 多阶段重写（旧版已备份）
├── docker-compose.prod.yml              ★ 生产编排
├── docker-compose.dev.yml               ★ 开发编排
├── pyproject.toml                       ★ ruff + coverage 配置
├── requirements-observability.txt       ★ OTel 依赖
├── scripts/verify_deploy_manifest.py    ★ 部署不变量校验（26 项）
├── production-deploy-guide.md           ★ 部署教程
├── W5-Part2-自测核对清单.md              ★ A~I 段自测
└── README-W5.md                         ★ 本文件
```

---

## 5. 开关速查

| 变量 | 默认 | 打开后 |
|---|---|---|
| `MCP_HTTP_ENABLED` | `false` | 挂载 `/mcp/v1`（stdio 仍可用，双传输并存） |
| `ENABLE_LAYERED_MEMORY` | `false` | 三层记忆检索与注入生效 |
| `SESSION_REGISTRY_BACKEND` | `memory` | `redis` → 多副本共享 SSE 会话 |
| `ENABLE_OTEL` | `false` | 全链路 span + `/metrics` 指标 |
| `SSE_GUARD_ENABLED` | `false` | 心跳 / 空闲超时 / 连接上限 / 重连提示 |
| `TASK_CANCEL_CLEANUP_ENABLED` | `true` | 取消后清理任务级 Redis 临时键 |

**默认值全部选择「零影响」**：不显式开启时，W1~W5-Part1 的行为逐字节不变。

---

## 6. 本阶段边界

**已完成**：容器化（多阶段镜像 + prod/dev 编排 + 多副本 + Redis 会话）、OTel 五层埋点 + 指标、
CI 五段流水线 + 覆盖率门禁、SSE 边界加固 + 一致性校验 + 取消回收。

**未做（后续）**：

1. K8s 原生清单（Deployment / HPA / PDB）——当前只到 compose；
2. Collector 侧多实例指标去重（`spanmetrics` 会各自累加）；
3. SSE 会话的 Redis Cluster 分片（当前单实例 Redis，主从可，Cluster 需改 key 设计）；
4. 全仓覆盖率从 62.10% 提到 85%（需为 W1~W4 的 P2 占位模块补测试，属独立任务）；
5. P2 剩余：项目一 RAG 向量接口字段对齐、long 记忆去重与时效衰减。
