# AgentFlow W5-Part1 交付说明：MCP HTTP 传输 + FR-014 分层记忆

> 本阶段把 PRD 里挂了两轮的 **P2 首两项**真正落地：`MCP HTTP` 第二套传输、`FR-014 分层记忆`。
> **stdio 传输完整保留**，两条传输并存；分层记忆是**外层增强模块**，默认关闭，关闭时对 W1~W4 零影响。

---

## 0. 完成边界（先看这段）

| 项 | 状态 |
|---|---|
| W5-Part1：MCP HTTP 传输（服务端 + 客户端 + SSE 长会话 + 鉴权/连接池/超时/熔断） | ✅ 完成 |
| W5-Part1：FR-014 三层记忆（short/mid/long + 注入 + 管理 API + 总开关） | ✅ 完成 |
| W5-Part1：新增单元测试 57 条（HTTP 26 + 记忆 31），全量 207 passed | ✅ 完成 |
| **W5-Part2：容器化**（docker-compose 增加 MCP HTTP 服务、多副本、健康探针） | ⏳ 待做 |
| **W5-Part2：OTel 可观测**（trace/span/metric 导出，替代当前结构化日志） | ⏳ 待做 |
| **W5-Part2：GitHub CI**（矩阵构建、缓存、覆盖率门禁、镜像发布） | ⏳ 待做 |

---

## 1. 基线约束遵守情况（逐条对应任务书 1~9）

| # | 约束 | 实际做法 | 证据 |
|---|---|---|---|
| 1 | W1 鉴权 / ORM / 中间件 / docker-compose / 12 条鉴权用例不动 | 未触碰任何 W1 文件；`tests/test_auth.py` 12 条原样通过 | `pytest tests/test_auth.py` 12 passed |
| 2 | W2 LangGraph 内核、4 个 stdio MCP Server、SQL 安全校验、context_service 压缩、原有测试只读 | `app/tools/mcp_stdio.py`、`app/tools/mcp_client.py`、`app/agents/**`、`app/services/context_service.py` **零改动**；新代码只**调用**它们 | `git status` 无这些文件；W2 用例全绿 |
| 3 | W3 SSE / 断点续跑 / Vue 前端 / 种子脚本 / 150 条基线用例不动 | 未触碰 W3 文件；150 条基线用例全绿 | 全量 207 passed（含基线 150） |
| 4 | W4 评测脚本 / A-B / kb_adapter / demo 只读 | 仅 **import 复用** `app.tools.kb_adapter.CircuitBreaker`（只读调用，不改其代码） | 无 diff |
| 5 | stdio 必须保留，只新增 HTTP 作为第二套传输 | `MCPServerApp` 未改；`HttpTransport` 实现既有 `MCPTransport` 抽象 → `MCPClient` 可插拔换传输 | `test_same_instance_serves_both_transports`、`test_stdio_transport_still_works` |
| 6 | 分层记忆不能侵入 `plan_context`，需全局开关，关闭则不生效 | 记忆只往**既有插槽** `TaskState.memory_hits` 写值；提示词模板里「长期记忆（参考，可裁剪）」是 W2 就有的片段；`ENABLE_LAYERED_MEMORY=false`（默认）时**连 Redis 都不碰** | `test_augment_state_switch_off_returns_same_object`、`test_short_term_switch_off_touches_nothing` |
| 7 | 挂起判断继续用 `graph.pending_interrupts()`，禁止 `__interrupt__` | `MemoryAwareGraph.__getattr__` 透传内层图，`pending_interrupts` 原样可用；新代码 0 处 `__interrupt__` | `test_memory_aware_graph_injects_and_finalizes` 断言透传 |
| 8 | 夹具沿用 fakeredis + 内存 SQLite + InMemoryTransport | 新用例直接复用 `tests/conftest.py` 既有夹具，未新增容器依赖 | 全部离线可跑 |
| 9 | 只新增文件、新增单元测试 | 新增 10 个文件；**存量文件仅 2 处最小改动**（见 §10） | 见 §10 |

---

## 2. 新增文件清单

| 文件 | 行数 | 职责 |
|---|---|---|
| `app/core/w5_config.py` | 147 | W5 专项配置（与 `w2_config.py` 同构，不动 W1 `config.py`） |
| `app/mcp/__init__.py` | 9 | 包声明 |
| `app/mcp/transport_http.py` | 744 | **任务 1**：HTTP 服务端路由 + SSE 长会话 + HTTP 客户端传输 + 连接池 + 熔断 + 独立进程入口 |
| `app/mcp/http_entry.py` | 54 | 单进程「双传输」入口（主应用 + `/mcp/v1` 共存） |
| `app/memory/__init__.py` | 27 | 包导出 |
| `app/memory/layered_memory.py` | 1001 | **任务 2**：三层记忆 + 相似度召回 + 向量库抽象/兜底 + 图入口包装 |
| `app/schemas/w5.py` | 33 | 记忆管理 API 入参 |
| `app/routers/memory_router.py` | 189 | **任务 2.5**：记忆管理 API（鉴权保护） |
| `tests/test_w5_mcp_http.py` | 435 | **新增用例 26 条**（HTTP 传输） |
| `tests/test_w5_layered_memory.py` | 602 | **新增用例 31 条**（分层记忆） |

---

## 3. 任务 1：MCP HTTP 传输

### 3.1 分层与关键设计

```
                 ┌──────────────────────────────────────────────┐
  stdio 子进程 ──▶│  MCPServerApp（W2 原实例，工具实现零改动）      │◀── HTTP /mcp/v1
  （保留，默认）   └──────────────────────────────────────────────┘   （W5 新增）
                              ▲                        ▲
                    serve_stdio()              build_mcp_http_router()
```

- **服务端复用**：`initialize` / `tools/list` / `tools/call` 全部走 `MCPServerApp.handle()`，
  4 个 Server 的业务逻辑、错误码、`isError` 语义与 stdio **完全一致**；
- **客户端可插拔**：`HttpTransport` 实现 W2 既有的 `MCPTransport` 抽象（`start/request/close`），
  因此 `MCPClient`、`MCPClientPool` 一行不改就能跑 HTTP；
- **兼容模式**：同一个 `MCPServerApp` 实例既能被 `serve_stdio()` 驱动，也能被 HTTP 路由驱动，
  两种启动方式共享同一份工具表（有专门用例断言两者 `tools/list` 与调用结果一致）。

### 3.2 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/mcp/v1` | 单发或批量 JSON-RPC；无 `id` 的通知返回 `202` 无包体 |
| GET | `/mcp/v1/sse` | 打开 SSE 长会话；首帧 `event: endpoint` 下发 `session_id` |
| POST | `/mcp/v1/messages?session_id=...` | 向 SSE 会话投递请求，`202` 受理，响应经 SSE `message` 帧回流 |

`?server=<name>` 用于多 Server 同进程路由；未知 name 明确回 `-32600`，不静默落到默认 Server。

### 3.3 鉴权 / 连接池 / 超时 / 熔断

| 能力 | 实现 |
|---|---|
| 鉴权 | `X-MCP-API-Key` → `Authorization: Bearer` → `?api_key=`（SSE 给 EventSource 兜底）；`hmac.compare_digest` 常量时间比较；`MCP_HTTP_API_KEY` 为空则跳过并在启动日志 `mcp_http_auth_disabled` 告警 |
| 连接池 | `httpx.AsyncClient` + `Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30s)`，进程内复用 |
| 超时 | 读超时与连接超时分离（`MCP_HTTP_TIMEOUT_SECONDS` / `MCP_HTTP_CONNECT_TIMEOUT_SECONDS`），单次调用可覆盖 |
| 重试 | 仅传输层故障重试（默认 1 次，退避 0.05s）；**业务错误码绝不重试** |
| 熔断 | 复用 W4 `CircuitBreaker`（3 次失败 / 30s 半开）；只对传输故障计数，JSON-RPC `error` 属正常应答不计入 |
| 会话表 | `HttpSessionRegistry`（TTL 600s、容量 128、LRU 淘汰）；多副本需外置到 Redis，属 Part2 |

### 3.4 三种启动方式

```bash
# ① 只跑 HTTP（单 Server 一个进程，推荐生产）
python -m app.mcp.transport_http --server sql-query --port 9101 --api-key "$MCP_HTTP_API_KEY"

# ② 只跑 HTTP（4 个 Server 同进程，用 ?server= 路由）
python -m app.mcp.transport_http --host 0.0.0.0 --port 9100

# ③ 主应用 + MCP HTTP 同进程（本地联调；http_entry 不改 W1 的 main.py）
MCP_HTTP_ENABLED=true python -m app.mcp.http_entry --port 8000

# stdio（原方式，未受影响）
python -m app.tools.mcp_servers.sql_query
```

### 3.5 客户端用法

```python
from app.mcp.transport_http import HttpTransport, create_http_pool, discover_tools_http
from app.tools.mcp_client import MCPClient

# 连远端，动态发现工具（与 stdio 完全同一套客户端代码）
client = MCPClient("sql-query", HttpTransport("http://10.0.0.8:9101", server_name="sql-query", api_key="***"))
tools = await client.list_tools()          # 来自远端 tools/list
rows  = await client.call_tool("sql-query", {"sql": "SELECT ... LIMIT 50"})

# 或多 Server 连接池
pool = create_http_pool(["sql-query", "kb-search"], base_url="http://mcp-gw:9100", api_key="***")
await pool.discover()
```

---

## 4. 任务 2：FR-014 分层记忆

### 4.1 三层结构

| 层 | 存储 | 生命周期 | 内容 | 写入时机 |
|---|---|---|---|---|
| short | Redis List | TTL **24h** | 当前任务会话消息**原文**（不摘要） | 任务执行中逐条 append（`ltrim` 保留最近 400 条） |
| mid | MySQL `memories(scope='mid')` | **30 天**滚动 | **复用 W2 `ContextService.compress`** 生成的摘要 | 任务结束后 |
| long | MySQL `memories(scope='long')` + 向量库 | 永久（仅手工删） | Reviewer **≥ 3 分**的关键业务结论 | 任务结束后且评分达标 |

> 为什么 mid 复用 `ContextService.compress`：同一份对话不能出现两套摘要口径。摘要质量、数字保留策略、
> 轮次切分全部继承 W2 的实现，W5 只负责"把它落到 `memories` 表"。

### 4.2 注入链路（不侵入内核）

```
MemoryAwareGraph.ainvoke(state)
   │  ① memory.enabled? 否 → 原样透传（零副作用）
   ├─▶ ② recall(goal, user_id) → 长期记忆 top-3（向量库打分，失败降级本地关键词）
   ├─▶ ③ state["memory_hits"] = ["[记忆#12·重要度4] …", …]   ← 既有插槽
   ├─▶ ④ 内层 graph.ainvoke(...)  → planner 读 state.memory_hits
   │        └─ build_planner_messages 里「长期记忆（参考，可裁剪）」独立片段（W2 模板，未改）
   └─▶ ⑤ 终态 COMPLETED → finalize_task：写 mid 摘要 + （score≥3 时）归档 long
```

- `plan_context` / `build_planner_messages` **一行未改**；
- 包装器对 `aget_state` / `aupdate_state` / `pending_interrupts` 等全部 `__getattr__` 透传，
  所以 W3 的挂起判断、断点续跑、轨迹回放无需改动；
- 归档失败只打日志，不回滚任务（记忆是增强能力）。

### 4.3 召回算法与兜底

- 候选集**始终来自 MySQL**（保证 `user_id` 隔离与删除一致性），向量库只负责**打分**；
- `MOCK_EXTERNAL_RAG=true` → `LocalKeywordVectorStore`：`0.6×查询覆盖率 + 0.4×Jaccard + 0.1×数字命中`，
  中文 2-gram 切词 + 数字/英文词，零外部依赖、完全离线；
- `=false` → `HttpRagVectorStore` 调项目一 `/api/v1/kb/upsert`、`/api/v1/kb/search`；
- 向量库 5xx/超时/鉴权失败 → **降级本地排序**（不抛异常、不返回空），入库失败则 doc_id 落本地命名。

### 4.4 管理 API（`app/routers/memory_router.py`）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/v1/memories` | ADMIN/APPROVER/VIEWER | 分页 + `scope` / `task_id` 过滤（ADMIN 全量，其他角色仅本人） |
| GET | `/api/v1/memories/meta` | 同上 | 分层配置自检（开关 / 存储实现 / TTL / 阈值） |
| POST | `/api/v1/memories/recall` | 同上 | 相似度召回演示，返回 `hits` + 可直接注入的 `injection` |
| POST | `/api/v1/memories` | **ADMIN** | 手工新增 mid / long（long 同步向量化） |
| GET | `/api/v1/memories/{id}` | 同上（按归属过滤） | 详情 |
| DELETE | `/api/v1/memories/{id}` | **ADMIN** | 删除（long 随即失去召回资格） |

开关关闭时：查询返回 `enabled=false` + 空列表，写操作返回 `503 SERVICE_UNAVAILABLE`
（明确告知"未启用"，不静默成功）。

### 4.5 代码接法（接入现有 W3 运行时）

```python
from app.memory import install_layered_memory

runtime = await create_w3_runtime(session_factory=factory)          # W3 原样
graph = install_layered_memory(
    runtime.graph,
    session_factory=factory,
    redis=fake_or_real_redis,
    enabled=True,                                                   # 或读 ENABLE_LAYERED_MEMORY
)
final = await graph.ainvoke(initial_state, config)                   # 注入 + 归档都在包装层
assert await graph.pending_interrupts(config)                       # 透传，W3 逻辑不变
```

---

## 5. 配置项（已追加到 `.env.example` 末尾）

```dotenv
# ---- MCP HTTP ----
MCP_HTTP_ENABLED=false            # 双传输开关：false 仅 stdio（默认）
MCP_HTTP_LISTEN_HOST=127.0.0.1
MCP_HTTP_LISTEN_PORT=9100
MCP_HTTP_API_KEY=                 # 生产必须配置
MCP_HTTP_BASE_URL=http://127.0.0.1:9100
MCP_HTTP_PATH=/mcp/v1
MCP_HTTP_TIMEOUT_SECONDS=15
MCP_HTTP_CONNECT_TIMEOUT_SECONDS=3
MCP_HTTP_MAX_CONNECTIONS=20
MCP_HTTP_RETRY_TIMES=1
MCP_HTTP_BREAKER_THRESHOLD=3
MCP_HTTP_BREAKER_RECOVERY_SECONDS=30
MCP_HTTP_SSE_MAX_SECONDS=300
MCP_HTTP_SESSION_TTL_SECONDS=600

# ---- 分层记忆 ----
ENABLE_LAYERED_MEMORY=false       # 总开关：false 完全不生效（默认，保证基线零回归）
MEMORY_SHORT_TTL_SECONDS=86400
MEMORY_MID_TTL_DAYS=30
MEMORY_LONG_MIN_REVIEW_SCORE=3
MEMORY_LONG_TOP_K=3
MEMORY_RECALL_MIN_SCORE=0.15
MEMORY_INJECT_MAX_CHARS=400
MEMORY_LOCAL_RECALL_ENABLED=true
```

---

## 6. 测试结果

```
$ .venv/Scripts/python.exe -m pytest -q
207 passed          # = 150 条基线（W1 12 / W2 120 / W3 18）+ 57 条 W5 新增
```

| 文件 | 用例数 | 说明 |
|---|---|---|
| `tests/test_w5_mcp_http.py` | **26（新增）** | 协议三方法、错误码、鉴权、SSE、连接池、熔断、双传输共存 |
| `tests/test_w5_layered_memory.py` | **31（新增）** | 三层写入、相似度召回、开关关闭、mock 兜底、图包装、管理 API |

基线 150 条一条未改、一条未跳过。

---

## 7. 快速自检

```bash
# 1) 全量回归
.venv/Scripts/python.exe -m pytest -q

# 2) 只看新增用例
.venv/Scripts/python.exe -m pytest tests/test_w5_mcp_http.py tests/test_w5_layered_memory.py -v

# 3) 双传输入口自检（应看到 /mcp/v1、/mcp/v1/sse、/api/v1/memories）
MCP_HTTP_ENABLED=true ENABLE_LAYERED_MEMORY=true python -c "
from app.mcp.http_entry import create_app_with_mcp_http
spec = create_app_with_mcp_http(force=True).openapi()
print([p for p in spec['paths'] if 'mcp' in p or 'memor' in p])"

# 4) stdio 未被破坏
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | python -m app.tools.mcp_servers.kb_search
```

---

## 8. 遗留与 Part2 待办

| 编号 | 事项 | 说明 |
|---|---|---|
| P2-1 | **容器化** | docker-compose 增加 `mcp-http` 服务（4 个 Server 各一实例或网关模式）、健康探针、镜像分层 |
| P2-2 | **OTel 可观测** | 现有 `TraceIDMiddleware` + 结构化日志升级为 OTel span/metric，MCP HTTP 客户端埋 `client/server` 双 span |
| P2-3 | **GitHub CI** | 矩阵（3.11/3.12/3.13）× 离线 pytest + ruff + 覆盖率门禁 + 镜像发布 |
| P2-4 | SSE 会话外置 | 当前 `HttpSessionRegistry` 是进程内存，多副本需迁 Redis（Part2 与容器化一起做） |
| P2-5 | 真实 RAG 联调 | `MEMORY_RAG_UPSERT_PATH` / `SEARCH_PATH` 字段需与项目一最终接口对齐后再压测 |
| P2-6 | 记忆治理 | long 记忆去重、时效衰减、importance 自动回收策略 |

---

## 9. 与既有阶段的关系

- **W2**：MCP 工具层与图内核完全复用；HTTP 只是"换了一条线"，没有第二套工具实现；
- **W3**：SSE / 审批断点 / 轨迹回放全部照旧；记忆包装器对图 API 全透传；
- **W4**：`kb_adapter.CircuitBreaker` 被复用（只读 import），评测脚本可直接对 `--llm real` + 记忆开关做 A/B，
  无需改 `scripts/ab_compression.py`。

---

## 10. 存量文件改动（仅 2 处，均为功能接入必需）

| 文件 | 改动 | 为什么必须 |
|---|---|---|
| `app/routers/api_router.py` | +2 行（`import memory_router`、`include_router(...)`），删除 1 行 W4 遗留的注释占位 | 不注册则记忆 API 不可达；这是 W2/W3 沿用的同一处注册位 |
| `.env.example` | 文件末尾**追加** 22 行 W5 配置（未改动任何既有内容） | 任务书「配置新增」明确要求在 `.env.example` 追加 |

其余所有存量文件（含 W1/W2/W3/W4 业务代码与 150 条用例）**零改动**。
