# AgentFlow Platform — W1 启动说明

> 企业级多智能体任务编排与执行平台 · 第一周交付物（工程骨架）
> 版本：`0.1.0-w1` ｜ 技术栈：Python 3.11+ / FastAPI / SQLAlchemy 2.0 / Pydantic-Settings / MySQL 8 / Redis 7 / pytest / Docker Compose

---

## 1. 这一周交付了什么

| 交付项 | 状态 | 对应 PRD |
|---|---|---|
| 分层目录骨架（routers / services / agents / tools / schemas / core / db / utils / scripts / frontend） | ✅ | 5.1 |
| 7 张 MySQL 表 ORM 模型 + Alembic 迁移体系 | ✅ | 第 6 章 |
| 双 Token 鉴权（access 2h / refresh 7d + Redis 黑名单 + 轮换） | ✅ | NFR-S4 |
| `require_role` 角色校验依赖（admin / approver / viewer） | ✅ | 2.1 / NFR-S2 |
| TraceID 中间件 + JSON 结构化日志 | ✅ | NFR-M2 |
| 全局异常处理器 + 统一响应包 `{code, message, data, trace_id}` | ✅ | 7.1 |
| 健康检查（liveness / readiness / 运行态摘要） | ✅ | NFR-M4 |
| docker-compose：mysql + redis + backend | ✅ | NFR-M4 |
| pytest 骨架：鉴权 12 条用例 | ✅ | W1 验收 |
| `scripts/verify_seed.py` CLI 骨架（断言清单 + 退出码约定） | ✅ 占位 | 11.2 |
| **LangGraph 状态机 / Planner / Executor / Reviewer** | ❌ W2 | 5.2 / 第 8 章 |
| **4 个 MCP Server 与 MCP Client** | ❌ W2 | FR-005 ~ 008 |
| **SSE 事件总线 / 审批 / 轨迹回放 / 记忆 / 评测看板** | ❌ W3~W4 | FR-009 ~ 016 |
| **前端工程** | ❌ W3 | 5.1 |

---

## 2. 目录结构

```
agentflow-platform/
├── app/
│   ├── main.py                  # FastAPI 入口：中间件 / 异常 / 路由 / OpenAPI
│   ├── core/                    # 全局横切能力
│   │   ├── config.py            # Pydantic-Settings 全量配置（零硬编码）
│   │   ├── context.py           # trace_id / span_id ContextVar
│   │   ├── logging.py           # JSON 结构化日志（自动带 trace_id）
│   │   ├── trace.py             # TraceIDMiddleware
│   │   ├── security.py          # bcrypt + JWT 双 Token 签发/校验
│   │   ├── redis_client.py      # Redis 异步连接池
│   │   ├── exceptions.py        # ErrorCode + AppException + 4 类处理器
│   │   └── deps.py              # get_current_user / require_role / get_auth_service
│   ├── db/
│   │   ├── base.py              # DeclarativeBase + 命名约定 + TimestampMixin
│   │   ├── session.py           # async engine / AsyncSessionLocal / get_db
│   │   ├── types.py             # db_enum()：MySQL 原生 ENUM
│   │   └── checkpointer.py      # LangGraph RedisSaver 导入预留（不建图）
│   ├── schemas/
│   │   ├── orm_models.py        # 7 张表 ORM（users/tasks/subtasks/task_executions/approvals/memories/eval_runs）
│   │   ├── enums.py             # 领域枚举（取值对齐 PRD 第六章）
│   │   ├── auth.py              # LoginRequest / TokenPair / CurrentUser / UserOut
│   │   ├── common.py            # ApiResponse / PageData
│   │   └── graph_state.py       # TaskState / SubtaskPlan / PlanOutput ...（仅结构）
│   ├── services/
│   │   ├── auth_service.py      # ✅ 登录 / 刷新轮换 / 注销
│   │   ├── token_store.py       # ✅ Redis：refresh 登记 + 黑名单
│   │   └── (其余 9 个为 W2~W4 占位)
│   ├── routers/
│   │   ├── auth.py              # ✅ /auth/login /refresh /logout /me
│   │   ├── health.py            # ✅ /healthz /readyz /api/v1/health
│   │   └── api_router.py        # 路由聚合 + W2~W4 注册位（注释）
│   ├── agents/                  # 空占位：planner / executor / reviewer / graph
│   ├── tools/                   # 空占位：mcp_client + mcp_servers/{sql_query,chart_render,kb_search,notify}
│   └── utils/                   # ids / time / masking
├── alembic/                     # 迁移环境（async engine，DSN 由 Settings 注入）
├── scripts/verify_seed.py       # 模拟数据校验脚本（占位）
├── tests/                       # conftest + test_auth.py（12 条）
├── frontend/                    # 空目录（W3）
├── docker-compose.yml           # mysql + redis + backend
├── Dockerfile
├── requirements.txt / requirements-dev.txt
├── pytest.ini
├── .env.example
└── README-W1.md
```

> ORM 模型按 W1 约定放在 `schemas/orm_models.py`（与 PRD 分层保持一致），
> 若后续拆分 `app/models/`，只需移动文件并同步 alembic 的 import。

---

## 3. 启动步骤

### 3.1 方式 A：Docker Compose（推荐，一条命令起全栈）

```bash
cp .env.example .env          # 按需修改 SECRET_KEY / 密码
docker compose up -d --build  # mysql + redis + backend
docker compose ps             # 三个服务均 healthy
```

- backend 启动命令内置了 `alembic upgrade head`，首次启动自动建表。
- 健康检查：<http://localhost:8000/healthz>（存活）、<http://localhost:8000/readyz>（依赖就绪）、<http://localhost:8000/docs>（Swagger）。

### 3.2 方式 B：本地开发（Python 直跑）

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env
# .env 中把 MYSQL_HOST / REDIS_HOST 指到本机（或 compose 起的基础设施）

alembic upgrade head                     # 建表
uvicorn app.main:app --reload --port 8000
```

只跑基础设施、应用本地跑：

```bash
docker compose up -d mysql redis
```

### 3.3 冒烟验证

```bash
curl http://localhost:8000/healthz
curl http://localhost:8000/api/v1/health        # 含 mock 开关状态
curl -X POST http://localhost:8000/api/v1/auth/login \
     -H "Content-Type: application/json" \
     -d '{"username":"admin_user","password":"Passw0rd!"}'
```

> 演示账号需自行插入 users 表（W2 提供 `scripts/seed_users.py`）。W1 可手动生成哈希：
> `python -c "from app.core.security import hash_password; print(hash_password('Passw0rd!'))"`

---

## 4. 数据库迁移说明

- 迁移环境：`alembic/env.py` 使用 **async engine**，DSN 从 `settings.alembic_url` 注入，`.env` 不落明文到 `alembic.ini`。
- 建表来源：`app/schemas/orm_models.py` 中 7 个模型，`target_metadata = Base.metadata`。

```bash
alembic revision --autogenerate -m "init schema"   # 生成初始迁移（首次执行）
alembic upgrade head                               # 应用
alembic current                                    # 当前版本
alembic downgrade -1                               # 回滚一步
alembic upgrade head --sql > init.sql              # 离线导出 SQL（DBA 评审）
```

注意事项：

1. **首次需要手动生成初始迁移**：`alembic/versions/` 为空（W1 交付骨架，迁移文件随表结构一起评审后再入库）。
2. MySQL 原生 ENUM 由 `app/db/types.py::db_enum` 统一生成，新增枚举值需 `ALTER TABLE ... MODIFY COLUMN`（autogenerate 不识别枚举值变更，需手写迁移）。
3. 所有表统一 `utf8mb4`，主键 `BIGINT UNSIGNED AUTO_INCREMENT`，`created_at / updated_at` 由 `TimestampMixin` 提供。
4. 测试用 SQLite 内存库跑同一套 ORM（`tests/conftest.py`），字段类型差异由 `pk_column()` 的 variant 抹平。

---

## 5. Mock 开关说明

外部依赖全部可开关，**W1 默认全开 Mock，不产生任何出网请求**。

| 环境变量 | 默认 | 作用 | 生效位置 |
|---|---|---|---|
| `MOCK_EXTERNAL_RAG` | `true` | `true`：kb-search 不走项目一 `POST /api/v1/kb/search`，返回固定 Mock chunks（`source="mock"`, `degraded=false`）<br>`false`：走真实 HTTP，8s 超时、失败重试 2 次后退化为 `degraded=true, chunks=[]` | `services/rag_gateway.py`（W2 实现） |
| `MOCK_EXTERNAL_EVAL` | `true` | `true`：Reviewer 不调项目二 `POST /api/v1/eval/runs`，走本地规则评分<br>`false`：调项目二 RAGAS faithfulness，失败降级为本地规则 | `services/eval_gateway.py`（W4 实现） |
| `LLM_API_KEY` | 空 | 空值时任何 LLM 调用都会显式失败（W1 不调用 LLM） | `core/config.py` |

运行态可自查：`GET /api/v1/health` 返回 `mock.external_rag` / `mock.external_eval` 当前取值，
避免"以为在 Mock、其实在打真实接口"的排查成本。

> 关闭 Mock 前请确认 `EXTERNAL_RAG_BASE_URL` / `EXTERNAL_EVAL_BASE_URL` 与 `EXTERNAL_RAG_TOKEN` 已配置。

---

## 6. 鉴权设计（双 Token + Redis 黑名单）

> 按 PRD A-03 的**行为语义**在仓内独立复现，**未复制项目二 auth-kit 任何源码**；
> 若后续改用 auth-kit 官方 wheel，只需替换 `core/security.py` + `services/token_store.py` 的实现，接口签名不变。

| 维度 | 取值 | 说明 |
|---|---|---|
| access_token | 2h（`ACCESS_TOKEN_EXPIRE_MINUTES=120`） | 无状态 JWT，载荷 `sub/username/role/typ/jti/iat/exp/iss` |
| refresh_token | 7d（`REFRESH_TOKEN_EXPIRE_DAYS=7`） | 一次性消费，刷新即轮换（旧 refresh 立即失效） |
| 算法 | HS256（`SECRET_KEY`，长度 ≥32） | 生产必须替换默认密钥，`APP_ENV=prod` 时启动即校验 |
| 黑名单 | `agentflow:auth:blacklist:{jti}` | TTL = 令牌剩余有效期，注销后旧 access 立即失效 |
| refresh 登记 | `agentflow:auth:refresh:{jti}` → user_id | 刷新时 `GET` + `DEL`（原子消费），检测重放 |
| 会话上限 | `AUTH_MAX_SESSIONS_PER_USER=5` | 超出按过期时间淘汰最旧会话 |

接口与错误码：

| 接口 | 说明 | 主要错误码 |
|---|---|---|
| `POST /api/v1/auth/login` | 签发双 Token，更新 last_login_at | `AUTH_INVALID_CREDENTIALS`(401)、`AUTH_USER_DISABLED`(403) |
| `POST /api/v1/auth/refresh` | 轮换双 Token | `AUTH_REFRESH_REVOKED`(401)、`AUTH_TOKEN_EXPIRED`(401) |
| `POST /api/v1/auth/logout` | access jti 入黑名单 | `AUTH_TOKEN_REVOKED`(401) |
| `GET /api/v1/auth/me` | 当前用户 | `AUTH_TOKEN_MISSING` / `AUTH_TOKEN_INVALID` / `AUTH_TOKEN_TYPE_INVALID` |

角色校验统一入口（所有写接口必须挂载，PRD NFR-S2）：

```python
from app.core.deps import require_role
from app.schemas.enums import UserRole

@router.post("/tasks", dependencies=[Depends(require_role(UserRole.ADMIN))])
async def create_task(...): ...
```

---

## 7. 测试

```bash
pytest                     # 12 条鉴权用例
pytest -m auth -v          # 只跑鉴权
pytest --cov=app --cov-report=term-missing
```

- 不需要 MySQL / Redis：SQLite 内存库 + fakeredis 通过 `dependency_overrides` 替换（`tests/conftest.py`）。
- 用例覆盖：登录成功 / 密码错误 / 账号禁用 / 有效 access / 缺少 token / 非法 token / 过期 token / refresh 冒充 access / 刷新轮换与重放拒绝 / 注销拉黑 / viewer 越权 403 / admin 放行 200。

---

## 8. 当前脚手架的边界（务必按此理解）

**已实现（可直接验收）**

1. 分层目录与依赖方向：routers → services → db / core，agent / tool 层不反向依赖 routers。
2. 7 张表 ORM 与 PRD 第六章字段、索引、枚举逐项对齐。
3. 双 Token 鉴权完整闭环（登录 / 轮换 / 注销 / 黑名单 / 角色校验）。
4. TraceID 贯穿日志与响应头（`X-Trace-ID`），全局异常统一成 `{code, message, data, trace_id}`。
5. Compose 一键起 mysql + redis + backend，`/healthz`、`/readyz` 可做编排探针。

**未实现（W2~W4，禁止在本阶段假装可用）**

| 模块 | 现状 | 交付周 |
|---|---|---|
| LangGraph StateGraph（7 节点 + 条件路由 + interrupt） | `agents/graph.py` 仅占位；`db/checkpointer.py` 只做 `RedisSaver` 导入探测，**不建图、不写快照** | W2/W3 |
| Planner / Executor / Reviewer | 空占位，调用即 `NotImplementedError` | W2 |
| MCP Client 与 4 个 MCP Server | 空占位，compose 不启动 | W2 |
| 上下文工程（预算/压缩/瘦身） | 空占位 | W2 |
| SSE 事件总线 | 空占位 | W3 |
| 审批 / 断点恢复 / 轨迹回放 | 空占位 | W3 |
| 分层记忆 / 评测看板 / 项目二对接 | 空占位 | W4 |
| 前端任务台 / DAG 画布 | `frontend/` 空目录 | W3 |
| mall 模拟数据生成与 `verify_seed` 实校验 | `scripts/verify_seed.py` 仅 CLI + 断言清单，退出码 2 表示"未实现" | W2 |

---

## 9. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `readyz` 返回 503 | MySQL 或 Redis 未就绪；`docker compose ps` 看 healthcheck，首次启动 MySQL 初始化约 30~40s |
| 启动报 `SECRET_KEY 长度必须 >= 32 位` | `.env` 里的密钥太短，`openssl rand -hex 32` 生成 |
| 启动报 `生产环境必须通过环境变量注入独立 SECRET_KEY` | `APP_ENV=prod` 却用了示例密钥 |
| `alembic upgrade head` 无变化 | `versions/` 还是空的，先 `alembic revision --autogenerate -m "init schema"` |
| 刷新令牌第二次失败（`AUTH_REFRESH_REVOKED`） | 设计如此：refresh 一次性消费，重放即拒绝，前端需用最新 refresh 覆盖本地存储 |
| 注销后 access 仍能用 | 检查 Redis 是否被 flush 或 key 前缀不一致（`REDIS_KEY_PREFIX`） |

---

## 10. 下一步（W2 入口清单）

1. `alembic revision --autogenerate -m "init schema"` 并把迁移文件纳入评审。
2. `scripts/seed_mall_data.py` + `verify_seed.py` 实现（seed=42，3 条埋因断言 + 20 抽样点）。
3. `agents/graph.py` 装配 7 节点；`schemas/graph_state.py` 补 reducer。
4. `tools/mcp_client.py` + 4 个 MCP Server（先 stdio，FR-005 ~ 008）。
5. `services/context_service.py`（预算 / 压缩 / 瘦身）。
