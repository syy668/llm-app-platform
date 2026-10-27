# W5-Part1 自测核对清单

> 阶段：**MCP HTTP 传输 + FR-014 分层记忆**（P2 首批落地）
> 结论：**全部通过**。`pytest` 207 passed（150 基线 + 57 新增），基线零回归；
> 双传输并存验证通过；分层记忆开关关闭时零副作用。
> 完成边界：**W5-Part1 完成；Part2（容器化 / OTel 可观测 / GitHub CI）未开始**。

---

## A. 基线强制约束（9 条，逐条核对）

| 编号 | 约束 | 结果 | 证据 |
|---|---|---|---|
| A-1 | W1 鉴权 / ORM / 中间件 / docker-compose / 12 条鉴权用例不动 | ✅ | `pytest tests/test_auth.py` → 12 passed；`docker-compose.yml` 无 diff |
| A-2 | W2 图内核 / 4 个 stdio MCP Server / SQL 安全校验 / `context_service` 压缩 / 原有测试只读 | ✅ | `app/agents/**`、`app/tools/mcp_stdio.py`、`app/tools/mcp_client.py`、`app/services/context_service.py` 全部零 diff；HTTP 层只调用 `MCPServerApp.handle` |
| A-3 | W3 SSE / 审批断点续跑 / Vue 前端 / 种子脚本 / 150 条基线用例不动 | ✅ | 全量 207 passed，基线 150 条未改未跳过 |
| A-4 | W4 评测 / A-B / `kb_adapter` / demo 只读 | ✅ | 仅 `from app.tools.kb_adapter import CircuitBreaker`（只读复用） |
| A-5 | stdio 必须保留，HTTP 为第二套传输 | ✅ | `python -m app.tools.mcp_servers.kb_search` pipe 测试返回 `initialize` + `tools/list`；`test_same_instance_serves_both_transports` 断言两传输工具表与调用结果一致 |
| A-6 | 记忆为外层增强，不侵入 `plan_context`，全局开关关闭则不生效 | ✅ | `plan_context` 零 diff；开关关闭：`augment_state` 返回**同一对象**、Redis 无 key、DB 无写入（3 条用例） |
| A-7 | 挂起判断用 `pending_interrupts()`，禁止 `__interrupt__` | ✅ | 新代码 `grep __interrupt__` → 0 命中；`MemoryAwareGraph` 透传 `pending_interrupts`，有用例断言 |
| A-8 | 夹具沿用 fakeredis + 内存 SQLite + InMemoryTransport | ✅ | 新用例 0 处新建容器依赖，直接复用 `tests/conftest.py` 夹具，CI 可离线 |
| A-9 | 只新增文件 + 新增用例 | ⚠️→✅ | 新增 10 文件；存量仅 2 处最小接入（`api_router.py` +2 行 / `.env.example` 末尾追加），详见 §Z |

---

## B. 任务 1：MCP HTTP 传输

| 编号 | 检查项 | 结果 | 证据 |
|---|---|---|---|
| B-1 | 遵循 JSON-RPC 2.0：`initialize` / `tools/list` / `tools/call` | ✅ | `test_http_initialize_negotiates_protocol`、`test_http_tools_list_returns_input_schema` |
| B-2 | 复用 4 个 Server 业务逻辑，工具内部零改动 | ✅ | `build_mcp_http_router` 只依赖 `MCPServerApp.handle`；`test_load_server_app_returns_real_business_tools` |
| B-3 | 服务端 `/mcp/v1` 端点可用 | ✅ | OpenAPI 实测 `/mcp/v1 [post]` |
| B-4 | SSE 长会话（endpoint → message → close） | ✅ | `test_sse_handshake_returns_endpoint_and_recycles_session`、`test_sse_session_roundtrip` |
| B-5 | HTTP 客户端可连远端并**动态发现**工具 | ✅ | `test_http_client_discovers_tools_and_calls_sql_query`、`test_discover_tools_http_helper` |
| B-6 | 调用 `sql-query` 返回结构化结果 | ✅ | 同上用例断言 `row_count=3`、`columns` 与 stdio 一致 |
| B-7 | 异常错误码：`-32601` 方法不存在 | ✅ | `test_http_method_not_found_returns_32601` |
| B-8 | 异常错误码：`-32602` 参数非法（`arguments` 非对象） | ✅ | `test_http_arguments_must_be_object` |
| B-9 | 异常错误码：`-32700` JSON 解析失败 | ✅ | `test_http_parse_error_returns_32700` |
| B-10 | 业务错误走 `result.isError` + 业务码（`SQL_SYNTAX` / `TOOL_NOT_FOUND`） | ✅ | `test_http_sql_dml_rejected_with_business_code`、`test_http_unknown_tool_returns_is_error` |
| B-11 | 通知（无 `id`）返回 202 无包体 | ✅ | `test_http_notification_returns_202` |
| B-12 | 批量请求 | ✅ | `test_http_batch_request` |
| B-13 | 鉴权（Header / Bearer / 错误 key / 未配置则免鉴权） | ✅ | `test_http_requires_api_key`、`test_http_accepts_bearer_header`、`test_http_rejects_wrong_key`、`test_is_authorized_and_key_extraction` |
| B-14 | 连接池 + 多 Server 路由 | ✅ | `test_http_pool_discovers_and_routes`、`test_unknown_server_query_is_rejected`、`test_router_rejects_empty_server_apps` |
| B-15 | 熔断保护 | ✅ | `test_http_transport_circuit_breaker_opens`（2 次失败 → open → `MCP_CIRCUIT_OPEN`） |
| B-16 | 未 start 即调用给出明确码 | ✅ | `test_transport_not_started_raises` → `MCP_HTTP_NOT_READY` |
| B-17 | SSE 会话 TTL / 容量淘汰 | ✅ | `test_session_registry_ttl_and_capacity` |
| B-18 | 会话不存在 → 404 + `MCP_HTTP_SESSION_MISSING` | ✅ | `test_sse_unknown_session_returns_404` |
| B-19 | 兼容模式：同一实例 stdio + HTTP | ✅ | `test_same_instance_serves_both_transports` |
| B-20 | 单进程双传输入口（不改 `main.py`） | ✅ | `python -m app.mcp.http_entry`；OpenAPI 同时含 `/mcp/v1` 与 `/api/v1/memories` |

---

## C. 任务 2：FR-014 分层记忆

| 编号 | 检查项 | 结果 | 证据 |
|---|---|---|---|
| C-1 | 短期记忆：Redis 原文、TTL 24h、任务内完整可用 | ✅ | `test_short_term_memory_roundtrip_and_ttl`（TTL 落在 86000~86400s、3000 字原文逐字保留） |
| C-2 | 短期记忆轮次上限（ltrim） | ✅ | `test_short_term_trims_to_max_messages` |
| C-3 | 中期记忆：复用 W2 压缩摘要 | ✅ | `test_summarize_reuses_context_compression`（摘要长度 < 原文、关键实体保留） |
| C-4 | 中期记忆落 `memories(scope='mid')`，TTL 30 天，不入向量库 | ✅ | `test_mid_memory_written_with_ttl`（`expires_at` 29~30 天、`chroma_doc_id is None`） |
| C-5 | 长期记忆：Reviewer ≥ 3 分才归档 | ✅ | `test_long_memory_requires_review_score`（2.9 → 跳过；4.2 → 归档且 importance=4） |
| C-6 | 长期记忆 importance 下限 3 | ✅ | `test_long_memory_importance_floor_is_three` |
| C-7 | 长期记忆过向量化（mock 时本地兜底） | ✅ | `chroma_doc_id == "agentflow-mem-{id}"`；`test_build_store_follows_mock_switch` |
| C-8 | 相似度召回：排序正确 | ✅ | `test_keyword_score_prefers_topical_overlap`、`test_rank_candidates_sorted_and_limited` |
| C-9 | 相似度召回：端到端取到最相关记忆 | ✅ | `test_recall_returns_most_similar_first`（首条含 `SKU-10086`，score > 0.15） |
| C-10 | 召回按 `user_id` 隔离 | ✅ | `test_recall_isolated_by_user` |
| C-11 | 召回命中累加 `hit_count` | ✅ | `test_memory_api_crud_and_recall` 断言 `hit_count >= 1` |
| C-12 | 注入片段：≤3 条、单条截断 | ✅ | `test_injection_limited_and_truncated` |
| C-13 | 注入作为**独立片段**进提示（不改提示词模板） | ✅ | `test_planner_prompt_renders_memory_block`（「长期记忆（参考，可裁剪）」+ 记忆正文） |
| C-14 | 真图（7 节点）注入生效并抬高 Planner 输入 token | ✅ | `test_real_graph_receives_memory_hits`（注入组 planner input > 对照组） |
| C-15 | 图包装器透传 `aget_state` / `aupdate_state` / `pending_interrupts` | ✅ | `test_memory_aware_graph_injects_and_finalizes` |
| C-16 | 终态才归档（非 COMPLETED 不写） | ✅ | `test_memory_aware_graph_ignores_non_completed` |
| C-17 | 向量库全挂时降级本地（入库 + 检索双降级） | ✅ | `test_recall_degrades_to_local_when_vector_store_fails` |
| C-18 | 本地索引为空（进程重启）仍可仅靠候选集排序 | ✅ | `test_local_store_search_ranks_without_index` |
| C-19 | 管理 API：鉴权 | ✅ | `test_memory_api_requires_auth` → 401 `AUTH_TOKEN_MISSING` |
| C-20 | 管理 API：分页查询 / 新增 / 详情 / 删除 / 召回 | ✅ | `test_memory_api_crud_and_recall` |
| C-21 | 管理 API：写操作仅 ADMIN | ✅ | `test_memory_api_viewer_cannot_write` → 403 `FORBIDDEN` |
| C-22 | 管理 API：开关关闭时写 503、读空列表 `enabled=false` | ✅ | `test_memory_api_create_rejected_when_disabled` |
| C-23 | 管理 API：`scope=short` 拒绝手工新增 | ✅ | `test_memory_api_rejects_short_scope` → 422 |
| C-24 | 分层配置自检可见 | ✅ | `test_memory_api_meta_reports_layers` |
| C-25 | 服务层关开关后写操作显式报错（不静默成功） | ✅ | `test_service_describe_and_disabled_write` → `MemoryDisabledError` |

---

## D. 开关与回归

| 编号 | 检查项 | 结果 | 证据 |
|---|---|---|---|
| D-1 | `ENABLE_LAYERED_MEMORY=false`（默认）→ 不检索、不注入、不落库 | ✅ | `test_augment_state_switch_off_returns_same_object`、`test_recall_switch_off_returns_empty`、`test_short_term_switch_off_touches_nothing`、`test_memory_aware_graph_skips_when_disabled` |
| D-2 | 开关开启后行为完整 | ✅ | 上述 C 段全部用例显式 `enabled=True` 或 monkeypatch 开启 |
| D-3 | `MCP_HTTP_ENABLED=false`（默认）→ 不挂载 HTTP 端点 | ✅ | `create_app_with_mcp_http(force=False)` 不出现 `/mcp/v1`；stdio 不受影响 |
| D-4 | 基线 150 条零回归 | ✅ | 全量 207 passed，退出码 0 |
| D-5 | 新代码 0 处 `__interrupt__` | ✅ | `grep -rn "__interrupt__" app/mcp app/memory app/routers/memory_router.py app/core/w5_config.py` → 0 |

---

## E. 新增单元测试用例清单（57 条，全部为本阶段新增）

> 基线 `tests/test_auth.py` / `test_context_service.py` / `test_executor.py` / `test_graph_routing.py` /
> `test_mcp_client.py` / `test_planner.py` / `test_replan.py` / `test_sql_query_security.py` /
> `test_w2_e2e.py` / `test_w3_e2e.py` **一条未改、一条未删、一条未跳过**。

### E-1 `tests/test_w5_mcp_http.py`（26 条）

| # | 用例 | 断言重点 |
|---|---|---|
| 1 | `test_http_initialize_negotiates_protocol` | `protocolVersion` / `serverInfo.name` |
| 2 | `test_http_tools_list_returns_input_schema` | 工具名 + JSON Schema 必填项 |
| 3 | `test_http_client_discovers_tools_and_calls_sql_query` | 客户端发现 + 调用 sql-query |
| 4 | `test_http_notification_returns_202` | 通知无包体 |
| 5 | `test_http_method_not_found_returns_32601` | `-32601` |
| 6 | `test_http_unknown_tool_returns_is_error` | `isError` + `TOOL_NOT_FOUND` |
| 7 | `test_http_arguments_must_be_object` | `-32602` |
| 8 | `test_http_sql_dml_rejected_with_business_code` | `SQL_SYNTAX`（DML 拦截） |
| 9 | `test_http_parse_error_returns_32700` | `-32700` |
| 10 | `test_http_batch_request` | 批量响应条数与 id |
| 11 | `test_http_requires_api_key` | 401 `-32001` |
| 12 | `test_http_accepts_bearer_header` | Bearer 等价 |
| 13 | `test_http_rejects_wrong_key` | 错误 key 401 |
| 14 | `test_is_authorized_and_key_extraction` | 常量时间比较 + 三级取值优先级 |
| 15 | `test_sse_handshake_returns_endpoint_and_recycles_session` | endpoint 帧 + 会话回收 |
| 16 | `test_sse_session_roundtrip` | 投递 → `message` 帧回流 |
| 17 | `test_sse_unknown_session_returns_404` | `MCP_HTTP_SESSION_MISSING` |
| 18 | `test_session_registry_ttl_and_capacity` | TTL + LRU 淘汰 |
| 19 | `test_http_pool_discovers_and_routes` | 池发现 + 路由 + 调用 |
| 20 | `test_http_transport_circuit_breaker_opens` | 熔断 open + `MCP_CIRCUIT_OPEN` |
| 21 | `test_transport_not_started_raises` | `MCP_HTTP_NOT_READY` |
| 22 | `test_same_instance_serves_both_transports` | stdio/HTTP 结果一致 |
| 23 | `test_load_server_app_returns_real_business_tools` | 复用 `build_app` |
| 24 | `test_unknown_server_query_is_rejected` | `-32600` |
| 25 | `test_router_rejects_empty_server_apps` | 构造期校验 |
| 26 | `test_discover_tools_http_helper` | 一次性发现后关闭连接 |

### E-2 `tests/test_w5_layered_memory.py`（31 条）

| # | 用例 | 断言重点 |
|---|---|---|
| 1 | `test_short_term_memory_roundtrip_and_ttl` | 原文保留 + TTL 24h |
| 2 | `test_short_term_switch_off_touches_nothing` | 开关关闭零 IO |
| 3 | `test_short_term_trims_to_max_messages` | ltrim 上限 |
| 4 | `test_summarize_reuses_context_compression` | 摘要变短且保留实体 |
| 5 | `test_mid_memory_written_with_ttl` | mid 行 + 30 天 + 无向量 |
| 6 | `test_long_memory_requires_review_score` | 2.9 跳过 / 4.2 归档 |
| 7 | `test_long_memory_importance_floor_is_three` | importance 下限 |
| 8 | `test_keyword_score_prefers_topical_overlap` | 相关 > 不相关 |
| 9 | `test_rank_candidates_sorted_and_limited` | 排序 + top_k |
| 10 | `test_recall_returns_most_similar_first` | 首条命中 |
| 11 | `test_recall_isolated_by_user` | 用户隔离 |
| 12 | `test_recall_switch_off_returns_empty` | 关闭即空 |
| 13 | `test_build_store_follows_mock_switch` | mock → 本地关键词 |
| 14 | `test_local_store_search_ranks_without_index` | 冷启动可排序 |
| 15 | `test_recall_degrades_to_local_when_vector_store_fails` | 双降级 |
| 16 | `test_injection_limited_and_truncated` | ≤3 条 + 截断 |
| 17 | `test_augment_state_does_not_mutate_input` | 不改入参 |
| 18 | `test_augment_state_switch_off_returns_same_object` | 零副作用 |
| 19 | `test_memory_aware_graph_injects_and_finalizes` | 注入 + 归档 + 透传 |
| 20 | `test_memory_aware_graph_skips_when_disabled` | 关闭不注入不写 |
| 21 | `test_memory_aware_graph_ignores_non_completed` | 仅终态归档 |
| 22 | `test_review_score_of_variants` | total/score/scores 三种取分 |
| 23 | `test_planner_prompt_renders_memory_block` | 独立片段渲染 |
| 24 | `test_real_graph_receives_memory_hits` | 真图 token 对比 |
| 25 | `test_memory_api_requires_auth` | 401 |
| 26 | `test_memory_api_meta_reports_layers` | 三层配置自检 |
| 27 | `test_memory_api_crud_and_recall` | 增查召删全链路 |
| 28 | `test_memory_api_viewer_cannot_write` | 403 |
| 29 | `test_memory_api_create_rejected_when_disabled` | 503 + 空列表 |
| 30 | `test_memory_api_rejects_short_scope` | 422 |
| 31 | `test_service_describe_and_disabled_write` | `MemoryDisabledError` |

---

## F. 实测记录

| 命令 | 结果 |
|---|---|
| `.venv/Scripts/python.exe -m pytest -q` | **207 passed**（150 基线 + 57 新增），exit 0 |
| `pytest tests/test_w5_mcp_http.py -q` | 26 passed |
| `pytest tests/test_w5_layered_memory.py -q` | 31 passed |
| `printf ... \| python -m app.tools.mcp_servers.kb_search` | 正常返回 `initialize` / `tools/list`（stdio 未破坏） |
| `create_app_with_mcp_http(force=True).openapi()` | 路径含 `/mcp/v1`、`/mcp/v1/sse`、`/mcp/v1/messages`、`/api/v1/memories[/meta,/recall,/{id}]` |
| `HttpTransport("http://127.0.0.1:9")` × 2 次失败 | `breaker.state == "open"`，第 3 次直接 `MCP_CIRCUIT_OPEN` |

---

## G. 踩坑与修复（真实发生，非编造）

| # | 现象 | 根因 | 修复 |
|---|---|---|---|
| I-1 | 注入的会话表被替换成新实例，`POST /messages` 恒 404 | `registry = sessions or HttpSessionRegistry()` —— 空注册表 `__len__ == 0` 为假值 | 改为 `sessions if sessions is not None else ...`，并留注释 |
| I-2 | SSE 测试拿不到 202 | `ASGITransport` 会把响应体**整体缓冲**后才返回，无法"边读边发"；此时流已跑完、会话已回收 | 抽出 `sse_event_stream()` 生成器，HTTP 侧只测握手，会话往返直接消费同一生成器 |
| I-3 | 异步生成器 `RuntimeError: ignored GeneratorExit` | 在 `finally` 里 `yield` 关闭帧 | `finally` 只回收会话，`close` 帧移到 `try/finally` 之后 |
| I-4 | `row_count` 断言失败（期望 1 实得 3） | `FakeSqlExecutor` 固定返回 3 行，与 SQL 的 `LIMIT` 无关 | 断言改为 3，并注释说明 |
| I-5 | 连接池用例 401 | `create_http_pool` 未传 key 时读 `w5.MCP_HTTP_API_KEY`（默认空）→ 服务端要求 key | 用例显式传 `api_key` |
| I-6 | `chroma_doc_id` 唯一约束与"先入库后取名"冲突 | long 归档需要先有 `id` 才能命名 doc | `session.flush()` 拿自增 id → 向量化 → 回写 `chroma_doc_id` |
| I-7 | 若默认开关为 true，基线用例行为会漂移 | 记忆检索会带来额外 Redis/DB 访问与 state 变化 | 默认 **false**，仅在显式开启时生效（D-1） |
| I-8 | `.env.example` 直接读取受权限限制 | 敏感文件读取需人工授权 | 仅在文件**末尾追加**，不读取、不改写既有内容 |

---

## Z. 完成边界

**本阶段（W5-Part1）已完成：**

1. MCP HTTP 传输：服务端 `/mcp/v1` + SSE 长会话、HTTP 客户端（动态发现）、鉴权 / 连接池 / 超时 / 熔断、
   与 stdio 并存的兼容模式、独立进程与同进程两种入口；
2. FR-014 分层记忆：short（Redis 24h）/ mid（压缩摘要 30 天）/ long（≥3 分归档 + 向量化）、
   相似度召回与注入、mock 本地兜底、管理 API（6 个端点）、全局开关；
3. 新增 57 条单元测试，全量 207 passed，基线零回归。

**待做（W5-Part2）：**

| 项 | 具体内容 |
|---|---|
| 容器化 | `docker-compose` 增加 `mcp-http` 服务与健康探针；SSE 会话表外置 Redis 以支持多副本；镜像分层与只读根文件系统 |
| OTel 可观测 | 接入 `opentelemetry-sdk`：HTTP 客户端/服务端双 span、工具调用 metric（qps/错误码/时延直方图）、TraceID 与 OTel trace 打通 |
| GitHub CI | 矩阵（3.11/3.12/3.13）离线 `pytest` + `ruff` + 覆盖率门禁 + 镜像构建发布；缓存 pip/venv |

**存量文件改动（仅 2 处，已登记）：**

| 文件 | 改动 |
|---|---|
| `app/routers/api_router.py` | +2 行注册 `memory_router`，移除 W4 遗留的注释占位 1 行 |
| `.env.example` | 末尾追加 22 行 W5 配置，既有内容零改动 |
