# AgentFlow Platform 生产部署指南（W5-Part2）

> 适用版本：`0.5.2-w5p2`（W5-Part1 MCP-HTTP + 分层记忆，W5-Part2 容器化 + OTel + CI）
> 配套文档：`README-W5.md`、`W5-Part2-自测核对清单.md`

---

## 1. 交付物与运行时拓扑

```
                         ┌──────────────────────────────┐
   浏览器 / 网关  ──────▶ │  Nginx / Traefik / K8s Ingress│
                         └──────────────┬───────────────┘
                                        │  轮询 / 最少连接
                 ┌──────────────────────┴───────────────────────┐
                 ▼                                              ▼
      ┌────────────────────┐                        ┌────────────────────┐
      │ backend  ×N        │  /api/v1  /health /ready│ mcp-http  ×N       │
      │ app.bootstrap:app  │  /metrics /observability│ app.mcp.transport_http
      │ :8000              │                        │ :9100  (/mcp/v1)   │
      └─────────┬──────────┘                        └─────────┬──────────┘
                │                                             │
                ├──────────────┬──────────────┬───────────────┤
                ▼              ▼              ▼               ▼
          ┌──────────┐   ┌──────────┐   ┌──────────────┐  ┌──────────────┐
          │  MySQL   │   │  Redis   │   │ OTel Collect │  │ 4× MCP stdio │
          │  8.0     │   │  7       │   │ :4318/:4317  │  │ (可选，同代码)│
          └──────────┘   └──────────┘   └──────────────┘  └──────────────┘
             业务数据      · SSE 会话表        trace/metrics
                          · 事件流(Stream)
                          · 取消标记 / 短期记忆
```

两个关键约束：

1. **backend 与 mcp-http 是同一个镜像、同一份 MCP 业务代码**，只是启动命令不同。
   这样避免「两套工具实现」，也让 `stdio` 传输继续可用（`python -m app.tools.mcp_servers.sql_query`）。
2. **SSE 会话表必须外置到 Redis**（`SESSION_REGISTRY_BACKEND=redis`）才能多副本。
   内存模式只在单实例 / 本地开发下使用。

---

## 2. 前置条件

| 项 | 要求 | 说明 |
|---|---|---|
| Docker | ≥ 24，compose v2 | `docker compose version` 可用 |
| 内存 | ≥ 4 GB | MySQL 1 G + Redis 256 M + 2×backend 各 512 M |
| 端口 | 8000 / 9100（容器内） | 生产建议不直接暴露宿主端口，交给网关 |
| 外部依赖 | 项目一知识库 / LLM API | `MOCK_EXTERNAL_RAG=false` 时必须可达 |

### 2.1 必填环境变量

```bash
cp .env.example .env
```

以下变量在 `docker-compose.prod.yml` 里用 `${VAR:?}` 强制要求，**缺一个就起不来**：

```dotenv
SECRET_KEY=<32 字节以上随机串>          # JWT 签名
MYSQL_ROOT_PASSWORD=<强口令>
MYSQL_PASSWORD=<强口令>
MCP_HTTP_API_KEY=<强口令>               # /mcp/v1 的 X-MCP-API-Key
```

> `SECRET_KEY` 生成：`python -c "import secrets;print(secrets.token_urlsafe(48))"`

### 2.2 生产建议值（与开发默认不同）

```dotenv
APP_ENV=prod
DEBUG=false
LOG_JSON=true
BCRYPT_ROUNDS=12
SESSION_REGISTRY_BACKEND=redis
ENABLE_OTEL=true
OTEL_SAMPLE_RATIO=0.2          # 全采会放大存储成本；排障时临时调 1.0
OTEL_EXPORTER=otlp
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
SSE_GUARD_ENABLED=true
ENABLE_LAYERED_MEMORY=true
```

---

## 3. 构建与启动

### 3.1 生产编排（推荐）

```bash
# 1) 构建 + 启动（首次会跑迁移，migrate 是一次性服务）
docker compose -f docker-compose.prod.yml up -d --build

# 2) 看状态（backend 是 2 副本，端口为随机映射）
docker compose -f docker-compose.prod.yml ps

# 3) 健康检查
curl -s localhost:<映射端口>/health   | jq
curl -s localhost:<映射端口>/ready    | jq
```

`/ready` 返回 503 表示有依赖没就绪，响应体里能直接看到是哪一项：

```json
{
  "status": "not_ready",
  "checks": {"mysql": true, "redis": false, "session_registry": false},
  "session_registry": {"backend": "redis", "degraded": true}
}
```

### 3.2 水平扩容

```bash
# backend 扩到 4 副本、mcp-http 扩到 3
docker compose -f docker-compose.prod.yml up -d --scale backend=4 --scale mcp-http=3

# 或改 .env 里的副本数后重新 up
BACKEND_REPLICAS=4 MCP_HTTP_REPLICAS=3 docker compose -f docker-compose.prod.yml up -d
```

> **为什么 ports 写成 `- "8000"` 而不是 `"8000:8000"`**：前者只声明容器端口，宿主端口由
> Docker 随机分配，因此可以安全扩容；后者会固定占用宿主端口，第二个副本直接绑不上。
> `scripts/verify_deploy_manifest.py` 的 **P6** 检查项会在 CI 里挡住这种写法。

### 3.3 本地开发

```bash
docker compose -f docker-compose.dev.yml up -d
# 源码挂载 + uvicorn --reload，改 app/ 立即生效
docker compose -f docker-compose.dev.yml logs -f backend
```

### 3.4 不起容器的最小验证（离线）

```bash
pip install -r requirements-dev.txt -r requirements-observability.txt
pytest -q                                  # 307 passed
python scripts/verify_seed.py              # SEED VERIFIED
python scripts/demo_three_flows.py         # 3/3 链路
python scripts/verify_deploy_manifest.py   # DEPLOY MANIFEST OK
```

---

## 4. 多副本与前置负载均衡

### 4.1 会话为什么能在 Pod 之间共享

`GET /mcp/v1/sse` 与 `POST /mcp/v1/messages` 可能落在不同副本。会话表放在 Redis：

| key | 类型 | 内容 | TTL |
|---|---|---|---|
| `mcp:sess:<session_id>` | hash | `server` / `created_at` / `last_seen` / `pid` | `MCP_HTTP_SESSION_TTL_SECONDS`（默认 600s） |
| `mcp:sess:<session_id>:q` | list | 待回流给 SSE 的 JSON-RPC 响应（LPUSH/RPOP） | 随会话过期 |
| `mcp:sess:index` | zset | `score = 创建时间`，用于过期回收（`reap()`） | 无 |

排查命令：

```bash
docker compose -f docker-compose.prod.yml exec redis redis-cli --scan --pattern 'mcp:sess:*'
docker compose -f docker-compose.prod.yml exec redis redis-cli zcard mcp:sess:index
docker compose -f docker-compose.prod.yml exec redis redis-cli hgetall mcp:sess:<id>
docker compose -f docker-compose.prod.yml exec redis redis-cli llen   mcp:sess:<id>:q
```

### 4.2 关键设计取舍（务必知晓）

1. **元数据读写走同步 Redis 客户端，`socket_timeout` 默认 50ms。**
   原因：`transport_http.py` 的路由是 async，但注册表接口（`create/get/drop`）是
   Part1 既定的**同步签名**，本阶段不允许改那个文件。元数据都是 O(1) 小对象，
   有界阻塞 50ms 可接受；一旦 Redis 抖动，走「降级 → 进程内会话表」，请求不会 500。
2. **消息队列走异步客户端，RPOP + 50ms 轮询**，不用 BLPOP：
   BLPOP 在协程被 `wait_for` 取消时存在「值已弹出但没消费」的丢消息窗口。
3. **`drop()` 会先向队列投一个 `null` 哨兵**，让在线的 SSE 消费者优雅收尾，再删元数据。
4. **不做本地会话缓存直读**：跨副本 `drop` 后，本地缓存会让已关闭会话「复活」，
   表现为消息投进黑洞。宁可每次 HGETALL。

### 4.3 网关配置要点

SSE 是长连接，网关必须满足：

```nginx
# nginx 片段
location /api/v1/ {
    proxy_pass http://backend_pool;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;          # 必须：否则 SSE 被缓冲，前端「一卡一卡」
    proxy_read_timeout 1800s;     # 必须 > SSE_CONNECTION_MAX_SECONDS
    proxy_send_timeout 1800s;
}

location /mcp/v1/ {
    proxy_pass http://mcp_pool;
    proxy_http_version 1.1;
    proxy_buffering off;          # SSE 会话同样需要
    proxy_read_timeout 900s;
}
```

K8s Ingress 需要 `nginx.ingress.kubernetes.io/proxy-buffering: "off"`；
若使用 Istio/Envoy，注意关闭 `response_flags` 之外的缓冲并放宽 `idleTimeout`。

---

## 5. OpenTelemetry 采集器部署

### 5.1 起 Collector（profile 方式，默认不启动）

```bash
docker compose -f docker-compose.prod.yml --profile observability up -d otel-collector

# 验证
curl -s localhost:8889/metrics | grep -E "calls|latency" | head
docker compose -f docker-compose.prod.yml logs -f otel-collector
```

`deploy/otel-collector-config.yaml` 已配置：

- 接收 `otlp/grpc:4317`、`otlp/http:4318`；
- `spanmetrics` 连接器把 trace 聚合成指标（含 p50/p95/p99），Prometheus exporter 在 `:8889`；
- `debug` exporter 把 span 摘要打进容器日志（本地排障用）；
- 需要接 Jaeger/Tempo 时，取消文件里 `otlp/jaeger` 的注释并在 traces pipeline 加上它。

### 5.2 应用侧开关

```dotenv
ENABLE_OTEL=true
OTEL_EXPORTER=otlp
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
OTEL_AUTO_FALLBACK_CONSOLE=true     # 采集器不可达时自动降级为控制台，链路不丢
```

**降级逻辑**：启动时对 OTLP 端点做一次 TCP 探测（0.4s 超时）。

- 可达 → 用 OTLP/HTTP 导出（外面还包了一层 `_FallbackExporter`，连续 3 次导出失败自动切控制台）；
- 不可达 → 直接 `ConsoleSpanExporter`，启动日志出现
  `otel_otlp_unreachable_fallback_console`。

### 5.3 埋点范围与语义

| 层 | span 名 | 关键属性 |
|---|---|---|
| HTTP | `GET /api/v1/tasks/{id}` | `http.request.method`、`url.path`、`http.route`、`http.response.status_code`、`trace.id` |
| 图执行 | `graph.run` | `task.id`、`graph.goal`、`graph.mode`(ainvoke/astream/astream_events) |
| 图节点 | `graph.node planner` / `graph.node executor` … | `langgraph.node`、`langgraph.step`、`langgraph.node.duration_ms` |
| MCP | `mcp.tools/call sql-query` | `rpc.system=mcp`、`mcp.server`、`mcp.tool.name`、`mcp.transport`、`mcp.error.code`、`mcp.retry.limit` |
| LLM | `gen_ai.chat` | `gen_ai.request.model`、`gen_ai.usage.input_tokens/output_tokens`、`gen_ai.duration_ms` |
| SQL | `db.select` / `db.update` / `db.insert` | `db.system`、`db.operation`、`db.statement`（默认不含参数） |

排除路径（不建 span）：`/health` `/ready` `/healthz` `/readyz` `/metrics` `/docs` `/redoc` `/openapi.json`。

### 5.4 指标

`GET /metrics`（Prometheus 文本格式，无需额外依赖）：

| 指标 | 类型 | 用途 |
|---|---|---|
| `agentflow_mcp_calls_total{server,tool,status}` | counter | MCP 调用成功率 = ok / 总数 |
| `agentflow_mcp_call_duration_seconds{...}` | summary | MCP P50/P95/P99 时延 |
| `agentflow_llm_tokens_total{model,kind}` | counter | token 消耗（input/output） |
| `agentflow_llm_calls_total{model,status}` | counter | LLM 调用次数 / 失败率 |
| `agentflow_graph_node_duration_seconds{node}` | summary | 节点耗时分布 |
| `agentflow_http_requests_total{method,route,status}` | counter | 接口 QPS / 5xx 比例 |
| `agentflow_db_calls_total{operation}` | counter | 数据库语句量 |

几条可直接用的 PromQL：

```promql
# MCP 调用成功率（5 分钟）
sum(rate(agentflow_mcp_calls_total{status="ok"}[5m]))
  / sum(rate(agentflow_mcp_calls_total[5m]))

# MCP P95 时延
histogram_quantile(0.95, sum(rate(agentflow_mcp_call_duration_seconds_bucket[5m])) by (le, tool))

# token 消耗速率
sum(rate(agentflow_llm_tokens_total[5m])) by (kind)
```

### 5.5 链路验证命令

```bash
# 1) 打一个请求，拿到 trace id
curl -si localhost:8000/api/v1/health | grep -i x-trace-id

# 2) 看应用内的 span 是否生成
curl -s localhost:8000/observability/status | jq '.otel'

# 3) 看采集器是否收到（debug exporter 会打印 span 名）
docker compose -f docker-compose.prod.yml logs --tail=50 otel-collector | grep -E "graph.node|mcp.tools"

# 4) 本地无采集器时：把 OTEL_EXPORTER 设为 console，直接看 stdout
OTEL_EXPORTER=console ENABLE_OTEL=true uvicorn app.bootstrap:app --port 8000
```

---

## 6. 健康探针与就绪

| 探针 | 路径 | 依赖 | 用途 |
|---|---|---|---|
| liveness | `/health` | 无（纯进程） | 容器 healthcheck / K8s livenessProbe |
| readiness | `/ready` | MySQL + Redis + 会话注册表 | 就绪门禁，未就绪返回 503 |
| 兼容 | `/healthz` `/readyz` | 同 W1 | 老编排文件继续可用 |

K8s 示例：

```yaml
livenessProbe:
  httpGet: {path: /health, port: 8000}
  initialDelaySeconds: 20
  periodSeconds: 15
readinessProbe:
  httpGet: {path: /ready, port: 8000}
  initialDelaySeconds: 10
  periodSeconds: 10
  failureThreshold: 3
```

---

## 7. 已知限制与运维处置

| 现象 | 根因 | 处置 |
|---|---|---|
| `POST /mcp/v1/messages` 返回 404 | 会话不在当前副本，且 `SESSION_REGISTRY_BACKEND=memory` | 切到 `redis` 并确认所有副本连同一个 Redis |
| `/ready` 报 `session_registry:false` 但服务能跑 | Redis 不可用已降级为内存会话 | 恢复 Redis；降级状态下多副本 SSE 不可用 |
| SSE 每隔一段时间断开 | 网关 `proxy_read_timeout` 小于心跳间隔 | 调大网关超时，或减小 `SSE_HEARTBEAT_SECONDS`（默认 15s） |
| span 里 SQL 节点没有父 span | SQLAlchemy 在连接池线程执行，contextvars 不跨线程 | 已知限制；用 `trace.id` 关联即可 |
| 采集器挂了但业务正常 | OTLP 导出失败但被吞掉、自动切 console | 预期行为，看日志 `otel_export_fallback_console` |
| 想临时关掉埋点 | — | `ENABLE_OTEL=false` 重启（零 patch，无性能开销） |

**回滚**：镜像按 `sha-` tag 保留，`IMAGE_TAG=<旧 sha> docker compose -f docker-compose.prod.yml up -d` 即可。
数据库迁移不回滚（`alembic downgrade` 需人工评估），因此迁移脚本必须保持向后兼容。

---

## 8. 安全基线

- 容器以 `appuser`（uid 10001）运行，非 root；
- `/mcp/v1` 必须配置 `MCP_HTTP_API_KEY`，否则启动日志会告警且接口裸奔；
- OTel 属性默认不采集 SQL 参数（`OTEL_CAPTURE_SQL_PARAMS=false`），工具入参经
  `mask_mapping` 脱敏后入 span；
- `.env` 不入镜像（`.dockerignore` 已排除），生产用编排的环境变量或 Secret 注入。
