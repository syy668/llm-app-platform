# AgentFlow Platform —— W1 脚手架代码清单

| 项 | 内容 |
|---|---|
| 仓库目录 | `agentflow-platform/`（独立仓库，未拷贝任何外部项目源码） |
| 版本 | `0.1.0-w1` |
| 文件数 | 73（不含 `.venv` / 缓存） |
| 代码行数 | 3841 |
| 校验状态 | `pytest` 12/12 通过；`compileall` 全绿；7 张表 MySQL DDL 编译通过 |
| 明确未实现 | Planner / Executor / Reviewer、LangGraph 业务节点、4 个 MCP Server、SSE / 审批 / 回放 / 记忆 / 看板、前端 |

---

## 一、完整目录树

```text
agentflow-platform/
├── alembic/
│   ├── versions/
│   │   └── .gitkeep
│   ├── env.py
│   └── script.py.mako
├── app/
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── executor.py
│   │   ├── graph.py
│   │   ├── planner.py
│   │   └── reviewer.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py
│   │   ├── context.py
│   │   ├── deps.py
│   │   ├── exceptions.py
│   │   ├── logging.py
│   │   ├── redis_client.py
│   │   ├── security.py
│   │   └── trace.py
│   ├── db/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── checkpointer.py
│   │   ├── session.py
│   │   └── types.py
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── api_router.py
│   │   ├── auth.py
│   │   └── health.py
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── auth.py
│   │   ├── common.py
│   │   ├── enums.py
│   │   ├── graph_state.py
│   │   └── orm_models.py
│   ├── services/
│   │   ├── __init__.py
│   │   ├── approval_service.py
│   │   ├── auth_service.py
│   │   ├── checkpoint_store.py
│   │   ├── context_service.py
│   │   ├── eval_gateway.py
│   │   ├── event_bus.py
│   │   ├── memory_service.py
│   │   ├── plan_service.py
│   │   ├── rag_gateway.py
│   │   ├── task_service.py
│   │   └── token_store.py
│   ├── tools/
│   │   ├── mcp_servers/
│   │   │   ├── __init__.py
│   │   │   ├── chart_render.py
│   │   │   ├── kb_search.py
│   │   │   ├── notify.py
│   │   │   └── sql_query.py
│   │   ├── __init__.py
│   │   └── mcp_client.py
│   ├── utils/
│   │   ├── __init__.py
│   │   ├── ids.py
│   │   ├── masking.py
│   │   └── time.py
│   ├── __init__.py
│   └── main.py
├── frontend/
│   ├── .gitkeep
│   └── README.md
├── scripts/
│   ├── README.md
│   └── verify_seed.py
├── tests/
│   ├── conftest.py
│   └── test_auth.py
├── .dockerignore
├── .env.example
├── .gitignore
├── alembic.ini
├── docker-compose.yml
├── Dockerfile
├── pytest.ini
├── README-W1.md
├── requirements-dev.txt
└── requirements.txt
```

---

## 二、关键文件逐份代码

### `.dockerignore`

> 21 行 · `text`

```text
.git
.gitignore
.venv
venv
__pycache__
*.py[cod]
.pytest_cache
.ruff_cache
.mypy_cache
htmlcov
.coverage
tests
frontend
logs
data
*.md
!README-W1.md
.env
.env.*
!.env.example
```

---

### `.env.example`

> 78 行 · `dotenv`

```dotenv
# ============================================================================
# AgentFlow Platform — W1 环境变量示例
# 用法：cp .env.example .env 后按需修改（.env 不入库）
# ============================================================================

# ---------- 应用基础 ----------
APP_NAME=AgentFlow Platform
# local / test / staging / prod
APP_ENV=local
DEBUG=true
API_V1_PREFIX=/api/v1
LOG_LEVEL=INFO
# true：结构化 JSON 日志（含 trace_id）；本地调试可置 false 走可读格式
LOG_JSON=true
# 前端联调用，逗号分隔；W1 前端尚未实现，仅预留
CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173

# ---------- MySQL ----------
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=agentflow
MYSQL_PASSWORD=agentflow_pwd
MYSQL_DB=agentflow
MYSQL_POOL_SIZE=10
MYSQL_MAX_OVERFLOW=20
MYSQL_POOL_RECYCLE=1800
# 打印 SQL（仅本地排查用）
MYSQL_ECHO=false

# ---------- Redis ----------
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=
REDIS_MAX_CONNECTIONS=50
# 所有 key 统一前缀，避免与项目一 / 项目二共用实例时冲突
REDIS_KEY_PREFIX=agentflow

# ---------- 鉴权（双 Token + Redis 黑名单）----------
# 生产必须替换：openssl rand -hex 32
SECRET_KEY=dev-only-secret-change-me-0123456789abcdef
JWT_ALGORITHM=HS256
JWT_ISSUER=agentflow-platform
# access 2h / refresh 7d（PRD NFR-S4）
ACCESS_TOKEN_EXPIRE_MINUTES=120
REFRESH_TOKEN_EXPIRE_DAYS=7
# bcrypt cost（测试环境可降到 4 提速）
BCRYPT_ROUNDS=12
# 单用户最大会话数（refresh 轮换时超限淘汰最旧会话，0=不限制）
AUTH_MAX_SESSIONS_PER_USER=5

# ---------- LLM（W1 仅配置预留，未发起任何调用）----------
LLM_PROVIDER=deepseek
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=
LLM_MODEL=deepseek-chat
# 备用模型（SiliconFlow Qwen2.5-72B，PRD A-04）
LLM_FALLBACK_MODEL=Qwen/Qwen2.5-72B-Instruct
LLM_TEMPERATURE=0.2
LLM_MAX_TOKENS=8192
LLM_TIMEOUT_SECONDS=60
LLM_MAX_RETRY=2

# ---------- 外部依赖 Mock 开关 ----------
# true：不访问项目一 RAG 接口，返回结构化 Mock 结果（W1 默认 true）
MOCK_EXTERNAL_RAG=true
# true：不访问项目二 EvalAgent 接口，Reviewer 走本地规则降级（W1 默认 true）
MOCK_EXTERNAL_EVAL=true
EXTERNAL_RAG_BASE_URL=http://localhost:8000
EXTERNAL_RAG_TOKEN=
EXTERNAL_RAG_TIMEOUT_SECONDS=8
EXTERNAL_EVAL_BASE_URL=http://localhost:8100
EXTERNAL_EVAL_TIMEOUT_SECONDS=10

# ---------- LangGraph（W1 仅导入预留，不构建业务 graph）----------
# checkpointer 快照 TTL：72h（PRD 5.2）
LANGGRAPH_CHECKPOINT_TTL_SECONDS=259200
```

---

### `.gitignore`

> 42 行 · `text`

```text
# 环境与密钥
.env
.env.local
.env.*.local

# Python
__pycache__/
*.py[cod]
*.egg-info/
.eggs/
build/
dist/
.venv/
venv/
.python-version

# 测试与覆盖率
.pytest_cache/
.coverage
.coverage.*
htmlcov/
coverage.xml
.mypy_cache/
.ruff_cache/

# 运行产物
logs/
*.log
static/generated/
data/seed/
tmp/

# IDE / OS
.idea/
.vscode/
.DS_Store
Thumbs.db

# 前端（后续迭代）
frontend/node_modules/
frontend/dist/
```

---

### `alembic.ini`

> 47 行 · `ini`

```ini
[alembic]
# 迁移脚本目录
script_location = alembic
# 使 `alembic` 命令可 import app.*
prepend_sys_path = .
# 版本文件名：20261021_0001_init_schema.py
file_template = %%(year)d%%(month).2d%%(day).2d_%%(rev)s_%%(slug)s
# 迁移脚本时区（统一 UTC，与 utcnow() 一致）
timezone = UTC

# sqlalchemy.url 故意留空：由 alembic/env.py 从 app.core.config.Settings 注入，
# 避免密码等敏感信息进入版本库。

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARNING
handlers = console
qualname =

[logger_sqlalchemy]
level = WARNING
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

---

### `alembic/env.py`

> 82 行 · `python`

```python
"""Alembic 迁移环境（async engine + Settings 注入 DSN）。

常用命令（在项目根目录、已激活虚拟环境下）：
    alembic revision --autogenerate -m "init schema"   # 生成迁移
    alembic upgrade head                                # 应用迁移
    alembic downgrade -1                                # 回滚一步
    alembic current / history                           # 查看状态
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings
from app.db.base import Base

# 必须导入 7 张表的 ORM 定义，否则 autogenerate 检测不到表
from app.schemas import orm_models  # noqa: F401  (User/Task/Subtask/TaskExecution/Approval/Memory/EvalRun)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 由 Settings 注入，避免明文 DSN 入库；% 需转义以兼容 ConfigParser 插值
config.set_main_option("sqlalchemy.url", settings.alembic_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：只输出 SQL（用于 DBA 评审）。"""
    context.configure(
        url=settings.alembic_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

---

### `alembic/script.py.mako`

> 29 行 · `mako`

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

---

### `alembic/versions/.gitkeep`

> 1 行 · `text`

```text

```

---

### `app/__init__.py`

> 4 行 · `python`

```python
"""AgentFlow Platform — 企业级多智能体任务编排与执行平台（W1 工程骨架）。"""

__version__ = "0.1.0-w1"
```

---

### `app/agents/__init__.py`

> 6 行 · `python`

```python
"""agents 层：LangGraph 编排与 Planner / Executor / Reviewer。

W1 边界：本目录只提供**空占位**，不含任何提示词、节点函数、图构建逻辑。
W2 交付内容：7 节点状态机、条件路由、interrupt、checkpointer 接入（PRD 5.2 / 第 8 章）。
"""
```

---

### `app/agents/executor.py`

> 14 行 · `python`

```python
"""Executor（占位）：子任务 -> 工具选择 -> MCP 调用 -> 结果瘦身。

W1 边界：**禁止实现**工具选择、重试/降级矩阵、8 次循环上限。
交付计划：W2（FR-004），依赖 tools/mcp_client.py（同样为占位）。
"""

from __future__ import annotations

AGENT_NAME = "executor"


async def execute_subtask(*args: object, **kwargs: object) -> None:
    raise NotImplementedError("W1 仅占位：Executor 在 W2 实现（PRD 里程碑 W2）")
```

---

### `app/agents/graph.py`

> 19 行 · `python`

```python
"""StateGraph 组装入口（占位）。

W1 边界：**不构建任何 graph**——不 add_node、不 add_edge、不设 conditional_edges、
不使用 interrupt / checkpointer。仅保留文件与约定，提醒 W2 在此完成 7 节点装配：

    START -> planner -> (human_gate | executor) -> reviewer
          -> (summarizer | replan) -> ... -> notifier -> END

交付计划：W2（PRD 5.2 节点清单与边表）。
"""

from __future__ import annotations

from typing import Any


def build_graph() -> Any:
    raise NotImplementedError("W1 不构建 StateGraph：W2 交付（PRD 里程碑 W2）")
```

---

### `app/agents/planner.py`

> 18 行 · `python`

```python
"""Planner（占位）：目标 -> 子任务 DAG、时间归一化、Replan、风险评估。

W1 边界：**禁止实现**任何规划逻辑 / 提示词 / 输出修复重试。
交付计划：W2（FR-002 / FR-003），输出结构见 schemas/graph_state.py::PlanOutput。
"""

from __future__ import annotations

AGENT_NAME = "planner"


async def plan(*args: object, **kwargs: object) -> None:
    raise NotImplementedError("W1 仅占位：Planner 在 W2 实现（PRD 里程碑 W2）")


async def replan(*args: object, **kwargs: object) -> None:
    raise NotImplementedError("W1 仅占位：Replan 在 W2 实现（PRD 里程碑 W2）")
```

---

### `app/agents/reviewer.py`

> 14 行 · `python`

```python
"""Reviewer（占位）：交付物 5 维评分 + 项目二 faithfulness 调用。

W1 边界：**禁止实现**任何评分逻辑与评审提示词。
交付计划：W2 本地规则、W4 接项目二评测（FR-013），输出结构见 schemas/graph_state.py::ReviewOutput。
"""

from __future__ import annotations

AGENT_NAME = "reviewer"


async def review(*args: object, **kwargs: object) -> None:
    raise NotImplementedError("W1 仅占位：Reviewer 在 W2/W4 实现（PRD 里程碑）")
```

---

### `app/core/__init__.py`

> 2 行 · `python`

```python
"""core 层：配置、日志、trace、鉴权、异常、依赖注入。"""
```

---

### `app/core/config.py`

> 148 行 · `python`

```python
"""全局配置：Pydantic-Settings 统一收口，零硬编码魔数（PRD NFR-M3）。"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import quote_plus

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_SECRET = "dev-only-secret-change-me-0123456789abcdef"


class Settings(BaseSettings):
    """全部运行期配置。环境变量优先于 .env 文件，字段名即环境变量名。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ---------------- 应用 ----------------
    APP_NAME: str = "AgentFlow Platform"
    APP_ENV: Literal["local", "test", "staging", "prod"] = "local"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ---------------- MySQL ----------------
    MYSQL_HOST: str = "127.0.0.1"
    MYSQL_PORT: int = 3306
    MYSQL_USER: str = "agentflow"
    MYSQL_PASSWORD: str = "agentflow_pwd"
    MYSQL_DB: str = "agentflow"
    MYSQL_POOL_SIZE: int = 10
    MYSQL_MAX_OVERFLOW: int = 20
    MYSQL_POOL_RECYCLE: int = 1800
    MYSQL_ECHO: bool = False

    # ---------------- Redis ----------------
    REDIS_HOST: str = "127.0.0.1"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str | None = None
    REDIS_MAX_CONNECTIONS: int = 50
    REDIS_KEY_PREFIX: str = "agentflow"

    # ---------------- 鉴权（双 Token + 黑名单） ----------------
    SECRET_KEY: str = _DEFAULT_SECRET
    JWT_ALGORITHM: str = "HS256"
    JWT_ISSUER: str = "agentflow-platform"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 120
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    BCRYPT_ROUNDS: int = 12
    AUTH_MAX_SESSIONS_PER_USER: int = 5

    # ---------------- LLM（W1 仅配置预留） ----------------
    LLM_PROVIDER: str = "deepseek"
    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "deepseek-chat"
    LLM_FALLBACK_MODEL: str = "Qwen/Qwen2.5-72B-Instruct"
    LLM_TEMPERATURE: float = Field(default=0.2, ge=0.0, le=2.0)
    LLM_MAX_TOKENS: int = 8192
    LLM_TIMEOUT_SECONDS: int = 60
    LLM_MAX_RETRY: int = 2

    # ---------------- 外部依赖 Mock 开关 ----------------
    MOCK_EXTERNAL_RAG: bool = True
    MOCK_EXTERNAL_EVAL: bool = True
    EXTERNAL_RAG_BASE_URL: str = "http://localhost:8000"
    EXTERNAL_RAG_TOKEN: str = ""
    EXTERNAL_RAG_TIMEOUT_SECONDS: int = 8
    EXTERNAL_EVAL_BASE_URL: str = "http://localhost:8100"
    EXTERNAL_EVAL_TIMEOUT_SECONDS: int = 10

    # ---------------- LangGraph（W1 仅预留） ----------------
    LANGGRAPH_CHECKPOINT_TTL_SECONDS: int = 72 * 3600

    # ---------------- 校验 ----------------
    @field_validator("SECRET_KEY")
    @classmethod
    def _secret_key_min_length(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("SECRET_KEY 长度必须 >= 32 位（openssl rand -hex 32）")
        return v

    @field_validator("LOG_LEVEL")
    @classmethod
    def _log_level_upper(cls, v: str) -> str:
        level = v.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"非法 LOG_LEVEL: {v}")
        return level

    def model_post_init(self, __context: object) -> None:  # noqa: D401
        if self.APP_ENV == "prod" and self.SECRET_KEY == _DEFAULT_SECRET:
            raise ValueError("生产环境必须通过环境变量注入独立 SECRET_KEY")

    # ---------------- 派生配置 ----------------
    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """SQLAlchemy 2.0 异步 DSN（aiomysql）。"""
        pwd = quote_plus(self.MYSQL_PASSWORD)
        return (
            f"mysql+aiomysql://{self.MYSQL_USER}:{pwd}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DB}?charset=utf8mb4"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def alembic_url(self) -> str:
        """Alembic 迁移使用同一异步 DSN（alembic/env.py 走 async engine）。"""
        return self.database_url

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redis_url(self) -> str:
        auth = f":{quote_plus(self.REDIS_PASSWORD)}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.CORS_ORIGINS.split(",") if item.strip()]

    @property
    def is_test(self) -> bool:
        return self.APP_ENV == "test"

    def key(self, *parts: str) -> str:
        """统一 Redis key 前缀拼装：settings.key("auth", "blacklist", jti)。"""
        return ":".join((self.REDIS_KEY_PREFIX, *parts))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例；测试通过 cache_clear() 或环境变量覆盖。"""
    return Settings()


settings = get_settings()
```

---

### `app/core/context.py`

> 21 行 · `python`

```python
"""请求级上下文变量（ContextVar）。

单独成模块，避免 logging <-> trace 之间的循环导入。
"""

from __future__ import annotations

from contextvars import ContextVar

trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
span_id_var: ContextVar[str] = ContextVar("span_id", default="")
user_id_var: ContextVar[int | None] = ContextVar("user_id", default=None)


def get_trace_id() -> str:
    return trace_id_var.get()


def get_span_id() -> str:
    return span_id_var.get()
```

---

### `app/core/deps.py`

> 107 行 · `python`

```python
"""FastAPI 依赖：数据库、Redis、鉴权服务、当前用户、角色校验。

require_role 是 PRD 2.1 / NFR-S2 的统一越权拦截入口，所有写接口必须挂载。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException, ErrorCode
from app.core.logging import get_logger
from app.core.redis_client import get_redis
from app.core.security import TokenType, decode_token
from app.db.session import get_db
from app.schemas.auth import CurrentUser
from app.schemas.enums import UserRole
from app.schemas.orm_models import User
from app.services.auth_service import AuthService
from app.services.token_store import TokenStore

logger = get_logger(__name__)

# auto_error=False：缺失 Authorization 时由本层抛统一业务异常（而非 403 detail="Not authenticated"）
bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAuth",
    description="登录后签发的 access_token，格式：Bearer <token>",
)


async def get_token_store(redis: Redis = Depends(get_redis)) -> TokenStore:
    """Token 存储依赖（测试可 override get_redis 指向 fakeredis）。"""
    return TokenStore(redis)


async def get_auth_service(
    db: AsyncSession = Depends(get_db),
    store: TokenStore = Depends(get_token_store),
) -> AuthService:
    return AuthService(db=db, store=store)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
    store: TokenStore = Depends(get_token_store),
) -> CurrentUser:
    """解析并校验 access_token：签名 -> 类型 -> 黑名单 -> 用户有效性。"""
    if credentials is None or not credentials.credentials:
        raise AppException(ErrorCode.AUTH_TOKEN_MISSING, "缺少 Authorization: Bearer <access_token>")

    claims = decode_token(credentials.credentials, TokenType.ACCESS)

    if await store.is_blacklisted(claims.jti):
        raise AppException(ErrorCode.AUTH_TOKEN_REVOKED, "令牌已注销，请重新登录")

    user = await db.get(User, claims.user_id)
    if user is None:
        raise AppException(ErrorCode.AUTH_TOKEN_INVALID, "令牌对应的用户不存在")
    if int(user.status) != 1:
        raise AppException(ErrorCode.AUTH_USER_DISABLED, "账号已被禁用")

    return CurrentUser(
        id=user.id,
        username=user.username,
        role=user.role if isinstance(user.role, UserRole) else UserRole(str(user.role)),
        status=int(user.status),
        jti=claims.jti,
        token_type=claims.typ,
        expires_at=claims.expires_at,
    )


def require_role(*roles: UserRole) -> Callable[..., Awaitable[CurrentUser]]:
    """角色校验依赖工厂。

    用法：
        @router.post("/tasks", dependencies=[Depends(require_role(UserRole.ADMIN))])
        # 或直接取用户：user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER))
    """
    allowed = tuple(roles)

    async def _dependency(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not current.has_role(*allowed):
            logger.warning(
                "rbac_denied",
                extra={
                    "user_id": current.id,
                    "role": current.role.value,
                    "required": [r.value for r in allowed],
                },
            )
            raise AppException(
                ErrorCode.FORBIDDEN,
                "当前角色无权执行该操作",
                detail={"required_roles": [r.value for r in allowed], "actual_role": current.role.value},
            )
        return current

    _dependency.__name__ = "require_role_" + "_".join(r.value for r in allowed)
    return _dependency
```

---

### `app/core/exceptions.py`

> 149 行 · `python`

```python
"""业务异常与全局异常处理器（统一响应包 {code, message, data, trace_id}）。"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import get_trace_id

logger = logging.getLogger(__name__)


class ErrorCode(str, Enum):
    """错误码：与 HTTP 状态码解耦，前端按 code 分支处理。"""

    OK = "OK"

    # ---- 鉴权 / 权限（W1 已实现） ----
    AUTH_INVALID_CREDENTIALS = "AUTH_INVALID_CREDENTIALS"
    AUTH_USER_DISABLED = "AUTH_USER_DISABLED"
    AUTH_TOKEN_MISSING = "AUTH_TOKEN_MISSING"
    AUTH_TOKEN_INVALID = "AUTH_TOKEN_INVALID"
    AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"
    AUTH_TOKEN_TYPE_INVALID = "AUTH_TOKEN_TYPE_INVALID"
    AUTH_TOKEN_REVOKED = "AUTH_TOKEN_REVOKED"
    AUTH_REFRESH_REVOKED = "AUTH_REFRESH_REVOKED"
    FORBIDDEN = "FORBIDDEN"

    # ---- 通用 ----
    NOT_FOUND = "NOT_FOUND"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"

    # ---- W2+ 预留（业务层启用，W1 不产生） ----
    TASK_INVALID = "TASK_INVALID"          # FR-001
    PLAN_INVALID = "PLAN_INVALID"          # FR-002
    REPLAN_EXHAUSTED = "REPLAN_EXHAUSTED"  # FR-003
    APPROVAL_MISSING = "APPROVAL_MISSING"  # FR-008


# 错误码 -> 默认 HTTP 状态码
_STATUS_MAP: dict[ErrorCode, int] = {
    ErrorCode.AUTH_INVALID_CREDENTIALS: 401,
    ErrorCode.AUTH_USER_DISABLED: 403,
    ErrorCode.AUTH_TOKEN_MISSING: 401,
    ErrorCode.AUTH_TOKEN_INVALID: 401,
    ErrorCode.AUTH_TOKEN_EXPIRED: 401,
    ErrorCode.AUTH_TOKEN_TYPE_INVALID: 401,
    ErrorCode.AUTH_TOKEN_REVOKED: 401,
    ErrorCode.AUTH_REFRESH_REVOKED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
    ErrorCode.TASK_INVALID: 400,
    ErrorCode.PLAN_INVALID: 422,
    ErrorCode.REPLAN_EXHAUSTED: 409,
    ErrorCode.APPROVAL_MISSING: 403,
}


class AppException(Exception):
    """业务异常基类：服务层抛出，由全局处理器转为标准响应。"""

    def __init__(
        self,
        code: ErrorCode | str,
        message: str | None = None,
        *,
        status_code: int | None = None,
        detail: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.code = code.value if isinstance(code, ErrorCode) else str(code)
        self.message = message or self.code
        if status_code is not None:
            self.status_code = status_code
        elif isinstance(code, ErrorCode):
            self.status_code = _STATUS_MAP.get(code, 400)
        else:
            self.status_code = 400
        self.detail = detail or {}
        self.headers = headers or {}
        super().__init__(self.message)


def _envelope(code: str, message: str, status_code: int, data: Any = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "data": data,
            "trace_id": get_trace_id() or None,
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    """注册 4 类处理器：业务异常、参数校验、HTTP 异常、兜底 500。"""

    @app.exception_handler(AppException)
    async def _app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
        log = logger.warning if exc.status_code < 500 else logger.error
        log(
            "business_exception",
            extra={
                "path": str(request.url.path),
                "method": request.method,
                "code": exc.code,
                "status": exc.status_code,
                "detail": exc.detail or None,
            },
        )
        return _envelope(exc.code, exc.message, exc.status_code, {"detail": exc.detail} if exc.detail else None)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": ".".join(str(i) for i in err.get("loc", [])), "msg": err.get("msg", ""), "type": err.get("type", "")}
            for err in exc.errors()
        ]
        logger.warning("validation_error", extra={"path": str(request.url.path), "errors": errors})
        return _envelope(ErrorCode.VALIDATION_ERROR.value, "请求参数校验失败", 422, {"errors": errors})

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = ErrorCode.NOT_FOUND.value if exc.status_code == 404 else f"HTTP_{exc.status_code}"
        if exc.status_code == 401:
            code = ErrorCode.AUTH_TOKEN_MISSING.value
        elif exc.status_code == 403:
            code = ErrorCode.FORBIDDEN.value
        return _envelope(code, str(exc.detail), exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception", extra={"path": str(request.url.path), "method": request.method})
        return _envelope(ErrorCode.INTERNAL_ERROR.value, "服务内部错误", 500)
```

---

### `app/core/logging.py`

> 96 行 · `python`

```python
"""结构化日志（JSON），每条日志自动携带 trace_id（PRD NFR-M2）。"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.core.context import get_span_id, get_trace_id

_CONFIGURED = False

_RESERVED = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """单行 JSON；异常信息序列化到 exc 字段。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": getattr(record, "trace_id", None) or get_trace_id() or None,
            "span_id": getattr(record, "span_id", None) or get_span_id() or None,
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class PlainFormatter(logging.Formatter):
    """本地可读格式，保留 trace_id 便于排查。"""

    def format(self, record: logging.LogRecord) -> str:
        trace_id = get_trace_id() or "-"
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} [{trace_id[:8]}] {record.name}: {record.getMessage()}"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def setup_logging(level: str | None = None) -> None:
    """幂等初始化根 logger（Uvicorn 默认 handler 会被接管）。"""
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if settings.LOG_JSON else PlainFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel((level or settings.LOG_LEVEL).upper())

    # uvicorn 自带 access log 与 TraceID 中间件的 http_request 重复，这里关闭前者
    logging.getLogger("uvicorn.access").propagate = False

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
```

---

### `app/core/redis_client.py`

> 55 行 · `python`

```python
"""Redis 异步客户端（连接池复用，进程内单例）。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from redis.asyncio import Redis

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_client: Redis | None = None


def _build_client() -> Redis:
    return Redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        max_connections=settings.REDIS_MAX_CONNECTIONS,
        socket_connect_timeout=3,
        socket_timeout=5,
        health_check_interval=30,
    )


def get_shared_redis() -> Redis:
    """惰性单例；避免 import 期建立连接。"""
    global _client
    if _client is None:
        _client = _build_client()
    return _client


async def get_redis() -> AsyncIterator[Redis]:
    """FastAPI 依赖：测试可整体 override。"""
    yield get_shared_redis()


async def ping_redis() -> bool:
    try:
        return bool(await get_shared_redis().ping())
    except Exception:  # noqa: BLE001
        logger.warning("redis_ping_failed")
        return False


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
```

---

### `app/core/security.py`

> 158 行 · `python`

```python
"""密码哈希与 JWT 双 Token 签发/校验。

说明：
- 本项目按 PRD A-03 的**接口语义**内部复现双 Token 轮换 + Redis 黑名单逻辑，
  不引入、不复制项目二 auth-kit 的任何源码；对外函数签名与语义保持一致，
  后续若替换为 auth-kit 官方 wheel，只需替换本模块实现。
- 轮换策略：refresh 一次性消费（GETDEL），旧 refresh 即刻失效；access 无状态，
  注销时以 jti 进黑名单（TTL = 剩余有效期）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

import bcrypt
import jwt
from pydantic import BaseModel, ConfigDict

from app.core.config import settings
from app.core.exceptions import AppException, ErrorCode


class TokenType(str, Enum):
    ACCESS = "access"
    REFRESH = "refresh"


@dataclass(frozen=True)
class IssuedToken:
    token: str
    jti: str
    expires_at: datetime


class TokenClaims(BaseModel):
    """解码后的载荷（字段名保持 JWT 标准短名）。"""

    model_config = ConfigDict(extra="ignore")

    sub: str
    username: str
    role: str
    typ: TokenType
    jti: str
    iat: int
    exp: int
    iss: str | None = None

    @property
    def user_id(self) -> int:
        return int(self.sub)

    @property
    def expires_at(self) -> datetime:
        return datetime.fromtimestamp(self.exp, tz=timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------
# 密码
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """bcrypt 哈希，输出 60 字符（对齐 users.password_hash CHAR(60)）。"""
    if not password:
        raise ValueError("password 不能为空")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# 用户不存在时也执行一次校验，抹平响应时间差（防用户名枚举）
_DUMMY_HASH = bcrypt.hashpw(b"agentflow-dummy-password", bcrypt.gensalt(rounds=4)).decode("utf-8")


def dummy_verify(password: str) -> None:
    verify_password(password, _DUMMY_HASH)


# --------------------------------------------------------------------------
# JWT
# --------------------------------------------------------------------------
def _expire_delta(token_type: TokenType) -> timedelta:
    if token_type is TokenType.ACCESS:
        return timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)


def create_token(
    *,
    user_id: int,
    username: str,
    role: str,
    token_type: TokenType,
    expires_delta: timedelta | None = None,
    jti: str | None = None,
) -> IssuedToken:
    now = datetime.now(timezone.utc)
    delta = expires_delta if expires_delta is not None else _expire_delta(token_type)
    expire_at = now + delta
    token_jti = jti or uuid.uuid4().hex
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "typ": token_type.value,
        "jti": token_jti,
        "iat": int(now.timestamp()),
        "exp": int(expire_at.timestamp()),
        "iss": settings.JWT_ISSUER,
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return IssuedToken(
        token=token,
        jti=token_jti,
        expires_at=expire_at.replace(tzinfo=None),
    )


def decode_token(token: str, expected_type: TokenType) -> TokenClaims:
    """解码并校验：签名 / 过期 / 签发者 / 类型。失败一律抛 AppException。"""
    if not token:
        raise AppException(ErrorCode.AUTH_TOKEN_MISSING, "缺少访问令牌")
    try:
        raw = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            issuer=settings.JWT_ISSUER,
            options={"require": ["exp", "iat", "sub", "jti", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AppException(ErrorCode.AUTH_TOKEN_EXPIRED, "令牌已过期") from exc
    except jwt.InvalidIssuerError as exc:
        raise AppException(ErrorCode.AUTH_TOKEN_INVALID, "令牌签发者不匹配") from exc
    except jwt.InvalidTokenError as exc:
        raise AppException(ErrorCode.AUTH_TOKEN_INVALID, "令牌非法") from exc

    try:
        claims = TokenClaims.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - 结构异常统一收敛
        raise AppException(ErrorCode.AUTH_TOKEN_INVALID, "令牌结构非法") from exc

    if claims.typ is not expected_type:
        raise AppException(
            ErrorCode.AUTH_TOKEN_TYPE_INVALID,
            f"需要 {expected_type.value} token，收到 {claims.typ.value}",
        )
    return claims
```

---

### `app/core/trace.py`

> 47 行 · `python`

```python
"""TraceID 中间件：入口生成 / 透传 trace_id，回写响应头，输出访问日志（NFR-M2）。"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.context import trace_id_var
from app.core.logging import get_logger
from app.utils.ids import new_trace_id

logger = get_logger("agentflow.access")

TRACE_HEADER = "X-Trace-ID"


class TraceIDMiddleware(BaseHTTPMiddleware):
    """在请求生命周期内绑定 trace_id（ContextVar），贯穿日志 / 异常 / 响应头。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        trace_id = request.headers.get(TRACE_HEADER) or new_trace_id()
        token = trace_id_var.set(trace_id)
        request.state.trace_id = trace_id
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[TRACE_HEADER] = trace_id
            return response
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.info(
                "http_request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": status_code,
                    "latency_ms": elapsed_ms,
                    "client": request.client.host if request.client else None,
                },
            )
            trace_id_var.reset(token)
```

---

### `app/db/__init__.py`

> 6 行 · `python`

```python
"""db 层：会话、元数据、迁移、checkpoint 预留。"""

from app.db.base import Base, TimestampMixin, pk_column

__all__ = ["Base", "TimestampMixin", "pk_column"]
```

---

### `app/db/base.py`

> 61 行 · `python`

```python
"""SQLAlchemy 2.0 声明式基类、命名约定、公共列。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, MetaData, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func

# 统一约束命名，保证 alembic autogenerate 产出稳定可回滚的迁移脚本
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """全局声明式基类（PRD 第六章全部表继承它）。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def to_dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


def pk_column() -> Mapped[int]:
    """主键：MySQL BIGINT UNSIGNED AUTO_INCREMENT；SQLite 下退化为 INTEGER 以支持自增（测试用）。"""
    return mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )


class TimestampMixin:
    """所有表统一 created_at / updated_at（PRD 第六章通用约定）。

    说明：DDL 只生成 `DEFAULT CURRENT_TIMESTAMP`，`ON UPDATE` 由 SQLAlchemy 在
    UPDATE 语句中自动写入（onupdate），这样 MySQL / SQLite 行为一致、迁移可移植；
    若需要数据库层兜底，可在业务迁移中追加
    `ALTER TABLE ... MODIFY updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP`。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        onupdate=func.now(),
        comment="更新时间",
    )
```

---

### `app/db/checkpointer.py`

> 39 行 · `python`

```python
"""LangGraph RedisSaver —— **仅导入预留**。

W1 边界（PRD 里程碑）：本阶段不构建任何 StateGraph，不注册节点 / 条件边，
因此这里只做依赖可用性探测与构造入口占位；W2 接入 planner / executor /
reviewer 节点后，再在此实现「每次节点转移后写快照、key=ckpt:{task_id}、
TTL 72h」的逻辑（PRD 5.2）。
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

try:  # pragma: no cover - 依赖缺失时降级，不阻断 W1 启动
    from langgraph.checkpoint.redis import RedisSaver  # type: ignore[import-not-found]

    REDIS_SAVER_AVAILABLE = True
except Exception:  # noqa: BLE001
    RedisSaver = None  # type: ignore[assignment]
    REDIS_SAVER_AVAILABLE = False


def checkpointer_status() -> dict[str, Any]:
    """健康检查用：暴露预留能力状态，不做任何连接。"""
    return {
        "redis_saver_available": REDIS_SAVER_AVAILABLE,
        "checkpoint_ttl_seconds": settings.LANGGRAPH_CHECKPOINT_TTL_SECONDS,
        "enabled": False,  # W1 未启用，W2 接入
    }


def build_checkpointer() -> Any:
    """W2 实现：返回 RedisSaver 实例（含 TTL 与序列化配置）。"""
    raise NotImplementedError("W1 仅做导入预留：LangGraph checkpointer 在 W2 交付（PRD 里程碑 W2）")
```

---

### `app/db/session.py`

> 82 行 · `python`

```python
"""数据库会话（SQLAlchemy 2.0 Async + aiomysql）。

engine / sessionmaker 均**惰性创建**：import 期不加载 DBAPI 驱动，
便于无 MySQL 驱动的环境（如纯 SQLite 单测、文档生成）直接导入模块。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """进程内单例 Engine（首次使用时创建）。"""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.MYSQL_ECHO,
            pool_pre_ping=True,
            pool_recycle=settings.MYSQL_POOL_RECYCLE,
            pool_size=settings.MYSQL_POOL_SIZE,
            max_overflow=settings.MYSQL_MAX_OVERFLOW,
            future=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每请求一个 Session，异常自动回滚。

    注意：本层不做隐式 commit，事务边界由 service 层显式控制。
    """
    async with get_session_factory()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def check_db() -> bool:
    """就绪探针：SELECT 1。"""
    try:
        from sqlalchemy import text

        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        logger.warning("mysql_ping_failed")
        return False


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
```

---

### `app/db/types.py`

> 23 行 · `python`

```python
"""数据库列类型助手（MySQL 原生 ENUM，SQLite 下自动降级为带 CHECK 的 VARCHAR）。"""

from __future__ import annotations

import enum

from sqlalchemy import Enum as SAEnum


def db_enum(enum_cls: type[enum.Enum], name: str) -> SAEnum:
    """统一枚举列定义。

    - native_enum=True：MySQL 生成原生 ENUM（严格对齐 PRD 字段类型）
    - values_callable：落库值取 Enum.value（如 'admin' / 'PLANNING'），而非成员名
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda e: [str(member.value) for member in e],
        validate_strings=True,
    )
```

---

### `app/main.py`

> 108 行 · `python`

```python
"""FastAPI 入口：中间件、异常处理、路由注册、OpenAPI 文档。

W1 只做工程骨架：鉴权 + 健康检查可用；Planner / Executor / Reviewer、
4 个 MCP Server、LangGraph 业务节点均不在本阶段（见 README-W1.md 边界说明）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import get_logger, setup_logging
from app.core.redis_client import close_redis, get_shared_redis
from app.db.checkpointer import checkpointer_status
from app.db.session import dispose_engine
from app.routers import health
from app.routers.api_router import api_router

logger = get_logger(__name__)

OPENAPI_TAGS = [
    {"name": "auth", "description": "登录 / 刷新 / 注销（双 Token + Redis 黑名单）"},
    {"name": "health", "description": "健康检查与运行态摘要"},
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    logger.info(
        "app_starting",
        extra={
            "env": settings.APP_ENV,
            "version": __version__,
            "mock_rag": settings.MOCK_EXTERNAL_RAG,
            "mock_eval": settings.MOCK_EXTERNAL_EVAL,
            "checkpointer": checkpointer_status(),
        },
    )
    # 启动期只做连通性探测，失败不阻断进程（便于本地先起服务后补依赖）
    try:
        await get_shared_redis().ping()
        logger.info("redis_connected")
    except Exception as exc:  # noqa: BLE001
        logger.warning("redis_unavailable_on_startup", extra={"error": str(exc)})

    yield

    await close_redis()
    await dispose_engine()
    logger.info("app_stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version=__version__,
        description=(
            "AgentFlow —— 企业级多智能体任务编排与执行平台（W1 工程骨架）。\n\n"
            "本阶段交付：分层目录、7 张表 ORM、双 Token 鉴权、TraceID 中间件、"
            "全局异常处理、健康检查。业务 Agent / MCP 工具尚未实现。"
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
    )

    # 中间件：trace 在内层、CORS 在外层（异常响应也能带上 CORS 头）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Trace-ID"],
    )
    from app.core.trace import TraceIDMiddleware

    app.add_middleware(TraceIDMiddleware)

    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(api_router)

    @app.get("/", tags=["health"], summary="服务信息")
    async def root() -> dict:
        return {
            "service": settings.APP_NAME,
            "version": __version__,
            "env": settings.APP_ENV,
            "docs": "/docs",
            "api_prefix": settings.API_V1_PREFIX,
        }

    return app


app = create_app()
```

---

### `app/routers/__init__.py`

> 2 行 · `python`

```python
"""routers 接口层：全部前缀 /api/v1（PRD 7.1）。"""
```

---

### `app/routers/api_router.py`

> 28 行 · `python`

```python
"""/api/v1 路由聚合。W1 仅挂载鉴权与健康检查，其余模块保留注册位（W2 / W3 / W4）。"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.config import settings
from app.routers import auth

api_router = APIRouter(prefix=settings.API_V1_PREFIX)

api_router.include_router(auth.router)

# ---------------------------------------------------------------------------
# 预留路由注册位（W1 不实现，禁止在此前提前落地业务逻辑）
# ---------------------------------------------------------------------------
# from app.routers import tasks, approvals, events, trace, eval, memories, dashboards
#
# api_router.include_router(tasks.router)        # W2：POST/GET /tasks、cancel、resume（FR-001/012）
# api_router.include_router(events.router)       # W3：GET /tasks/{id}/events（SSE，FR-009）
# api_router.include_router(approvals.router)    # W3：GET /approvals、POST /approvals/{id}/decide（FR-011）
# api_router.include_router(trace.router)        # W3：GET /tasks/{id}/trace（轨迹回放，FR-015）
# api_router.include_router(memories.router)     # W4：memories CRUD（FR-014）
# api_router.include_router(eval.router)         # W4：eval/runs（FR-016）
# api_router.include_router(dashboards.router)   # W4：GET /dashboards/summary（FR-016）
#
# 约定：所有写接口必须挂 Depends(require_role(...))（PRD NFR-S2）。
```

---

### `app/routers/auth.py`

> 72 行 · `python`

```python
"""鉴权接口：POST /auth/login、/auth/refresh、/auth/logout、GET /auth/me（PRD 7.1）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.core.deps import get_auth_service, get_current_user
from app.core.exceptions import AppException, ErrorCode
from app.schemas.auth import CurrentUser, LoginRequest, RefreshRequest, TokenPair, UserOut
from app.schemas.common import ApiResponse
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/login",
    response_model=ApiResponse[TokenPair],
    summary="登录（签发 access + refresh）",
    description="校验用户名密码，返回双 Token。access 默认 2h，refresh 默认 7d 且一次性消费。",
)
async def login(
    payload: LoginRequest,
    svc: AuthService = Depends(get_auth_service),
) -> ApiResponse[TokenPair]:
    _, pair = await svc.login(payload.username, payload.password)
    return ApiResponse.ok(pair, message="登录成功")


@router.post(
    "/refresh",
    response_model=ApiResponse[TokenPair],
    summary="刷新令牌（轮换）",
    description="使用 refresh_token 换取新 Token 对；旧 refresh 立即失效（一次性消费），重复使用返回 AUTH_REFRESH_REVOKED。",
)
async def refresh(
    payload: RefreshRequest,
    svc: AuthService = Depends(get_auth_service),
) -> ApiResponse[TokenPair]:
    pair = await svc.refresh(payload.refresh_token)
    return ApiResponse.ok(pair, message="刷新成功")


@router.post(
    "/logout",
    response_model=ApiResponse[None],
    status_code=status.HTTP_200_OK,
    summary="注销（access token 进黑名单）",
    description="将当前 access_token 的 jti 写入 Redis 黑名单，TTL 为令牌剩余有效期。",
)
async def logout(
    current: CurrentUser = Depends(get_current_user),
    svc: AuthService = Depends(get_auth_service),
) -> ApiResponse[None]:
    await svc.logout(current)
    return ApiResponse.ok(None, message="已注销")


@router.get(
    "/me",
    response_model=ApiResponse[UserOut],
    summary="当前登录用户",
)
async def me(
    current: CurrentUser = Depends(get_current_user),
    svc: AuthService = Depends(get_auth_service),
) -> ApiResponse[UserOut]:
    user = await svc.get_user_by_id(current.id)
    if user is None:  # 理论上 get_current_user 已校验，双保险
        raise AppException(ErrorCode.NOT_FOUND, "用户不存在")
    return ApiResponse.ok(UserOut.model_validate(user))
```

---

### `app/routers/health.py`

> 56 行 · `python`

```python
"""健康检查：liveness / readiness / 运行态摘要（含 Mock 开关与预留能力状态）。"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import __version__
from app.core.config import settings
from app.core.exceptions import ErrorCode
from app.core.redis_client import ping_redis
from app.db.checkpointer import checkpointer_status
from app.db.session import check_db

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="存活探针（不依赖任何外部组件）")
async def healthz() -> dict:
    return {"status": "ok", "service": settings.APP_NAME, "version": __version__}


@router.get("/readyz", summary="就绪探针（MySQL + Redis）")
async def readyz() -> JSONResponse:
    db_ok = await check_db()
    redis_ok = await ping_redis()
    ready = db_ok and redis_ok
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "checks": {"mysql": db_ok, "redis": redis_ok},
            "code": ErrorCode.OK.value if ready else ErrorCode.SERVICE_UNAVAILABLE.value,
        },
    )


@router.get("/api/v1/health", summary="运行态摘要（Mock 开关 / checkpointer 预留状态）")
async def health_summary() -> dict:
    return {
        "status": "ok",
        "service": settings.APP_NAME,
        "version": __version__,
        "env": settings.APP_ENV,
        "mock": {
            "external_rag": settings.MOCK_EXTERNAL_RAG,
            "external_eval": settings.MOCK_EXTERNAL_EVAL,
        },
        "auth": {
            "access_expire_minutes": settings.ACCESS_TOKEN_EXPIRE_MINUTES,
            "refresh_expire_days": settings.REFRESH_TOKEN_EXPIRE_DAYS,
            "blacklist_enabled": True,
        },
        "langgraph": checkpointer_status(),
    }
```

---

### `app/schemas/__init__.py`

> 6 行 · `python`

```python
"""schemas 层：ORM 映射、Pydantic 出入参、LangGraph 基础 State。"""

from app.schemas.common import ApiResponse, PageMeta

__all__ = ["ApiResponse", "PageMeta"]
```

---

### `app/schemas/auth.py`

> 52 行 · `python`

```python
"""鉴权相关 Pydantic 模型（请求 / 响应 / 当前用户上下文）。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.security import TokenType
from app.schemas.enums import UserRole


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64, description="登录名")
    password: str = Field(min_length=1, max_length=128, description="明文密码，仅用于本次校验")


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, description="登录时下发的 refresh_token")


class TokenPair(BaseModel):
    access_token: str = Field(description="访问令牌，默认 2h 有效")
    refresh_token: str = Field(description="刷新令牌，默认 7d 有效，一次性消费")
    token_type: str = Field(default="Bearer", description="令牌类型")
    expires_in: int = Field(description="access_token 剩余有效期（秒）")


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: UserRole
    status: int
    last_login_at: datetime | None = None


class CurrentUser(BaseModel):
    """请求级登录态：由 get_current_user 依赖注入。"""

    id: int
    username: str
    role: UserRole
    status: int = 1
    jti: str | None = Field(default=None, description="当前 access token 的 jti（注销拉黑使用）")
    token_type: TokenType = TokenType.ACCESS
    expires_at: datetime | None = Field(default=None, description="当前 access token 过期时间")

    def has_role(self, *roles: UserRole) -> bool:
        return self.role in roles
```

---

### `app/schemas/common.py`

> 35 行 · `python`

```python
"""通用响应包与分页模型（统一 {code, message, data, trace_id}，PRD 7.1）。"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

from app.core.context import get_trace_id
from app.core.exceptions import ErrorCode

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    code: str = Field(default=ErrorCode.OK.value, description="业务码，成功恒为 OK")
    message: str = Field(default="ok", description="提示信息")
    data: T | None = Field(default=None, description="业务数据")
    trace_id: str | None = Field(default=None, description="本次请求 trace_id，便于日志关联")

    @classmethod
    def ok(cls, data: T | None = None, message: str = "ok") -> "ApiResponse[T]":
        return cls(code=ErrorCode.OK.value, message=message, data=data, trace_id=get_trace_id() or None)


class PageMeta(BaseModel):
    page: int = Field(ge=1, default=1)
    size: int = Field(ge=1, le=100, default=20)
    total: int = Field(ge=0, default=0)


class PageData(BaseModel, Generic[T]):
    items: list[T] = Field(default_factory=list, description="当前页数据")
    meta: PageMeta = Field(default_factory=PageMeta)
```

---

### `app/schemas/enums.py`

> 103 行 · `python`

```python
"""领域枚举：ORM 与 Pydantic 共用，取值严格对齐 PRD 第六章。"""

from __future__ import annotations

import enum


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    APPROVER = "approver"
    VIEWER = "viewer"


class TaskStatus(str, enum.Enum):
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    RUNNING = "RUNNING"
    SUMMARIZING = "SUMMARIZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SubtaskToolHint(str, enum.Enum):
    SQL_QUERY = "sql_query"
    CHART_RENDER = "chart_render"
    KB_SEARCH = "kb_search"
    NOTIFY = "notify"
    NONE = "none"


class SubtaskRiskLevel(str, enum.Enum):
    NORMAL = "normal"
    HIGH = "high"


class SubtaskStatus(str, enum.Enum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class ExecutionNode(str, enum.Enum):
    PLANNER = "planner"
    EXECUTOR = "executor"
    REVIEWER = "reviewer"
    REPLAN = "replan"
    SUMMARIZER = "summarizer"
    NOTIFIER = "notifier"
    HUMAN_GATE = "human_gate"


class ExecutionEvent(str, enum.Enum):
    """task_executions.event 取值（PRD 6.4）；字段类型 VARCHAR(32)。"""

    ENTER = "enter"
    EXIT = "exit"
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    REPLAN = "replan"
    HUMAN_EDIT = "human_edit"
    ERROR = "error"


class ApprovalType(str, enum.Enum):
    HIGH_RISK_TOOL = "HIGH_RISK_TOOL"
    LARGE_SCAN = "LARGE_SCAN"


class ApprovalStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class MemoryScope(str, enum.Enum):
    SHORT = "short"
    MID = "mid"
    LONG = "long"


class EvalSource(str, enum.Enum):
    REVIEWER_GATE = "reviewer_gate"
    REGRESSION = "regression"
    AB_TEST = "ab_test"


class EvalVariant(str, enum.Enum):
    COMPRESS_ON = "compress_on"
    COMPRESS_OFF = "compress_off"


class EvalRunStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
```

---

### `app/schemas/graph_state.py`

> 132 行 · `python`

```python
"""LangGraph 基础 State / Agent IO 结构定义（PRD 5.2 与 8.3）。

W1 边界：本文件**只定义数据结构**（TypedDict / Pydantic），不含任何节点实现、
不含 StateGraph 构建、不含 reducer 业务逻辑；W2 接入 Planner / Executor /
Reviewer 时直接复用这些结构。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field

from app.schemas.enums import SubtaskRiskLevel, SubtaskToolHint

# ---------------------------------------------------------------------------
# Agent IO（PRD 8.3）
# ---------------------------------------------------------------------------
ToolHintLiteral = Literal["sql_query", "chart_render", "kb_search", "notify", "none"]


class TimeRange(BaseModel):
    start: date
    end: date


class SubtaskPlan(BaseModel):
    """Planner 输出的单个子任务。"""

    seq: int = Field(ge=1, le=15)
    name: str = Field(max_length=64)
    description: str = Field(max_length=300)
    tool_hint: SubtaskToolHint | ToolHintLiteral = SubtaskToolHint.NONE
    depends_on: list[int] = Field(default_factory=list)
    risk_level: SubtaskRiskLevel = SubtaskRiskLevel.NORMAL
    acceptance: str = Field(default="", max_length=200)


class PlanOutput(BaseModel):
    subtasks: list[SubtaskPlan] = Field(default_factory=list, min_length=0, max_length=15)
    assumptions: list[str] = Field(default_factory=list, max_length=5)
    replan_count: int = Field(default=0, ge=0, le=2)


class ArtifactRef(BaseModel):
    artifact_id: int | None = None
    type: Literal["chart", "report", "table"]
    url: str | None = None


class SubtaskResult(BaseModel):
    subtask_seq: int
    status: Literal["DONE", "FAILED", "SKIPPED"]
    data_digest: str = Field(default="", max_length=4000)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    error: dict[str, Any] | None = None


class ReviewScores(BaseModel):
    evidence: int = Field(ge=1, le=5)
    faithfulness: int = Field(ge=1, le=5)
    consistency: int = Field(ge=1, le=5)
    completeness: int = Field(ge=1, le=5)
    format: int = Field(ge=1, le=5)


class ReviewOutput(BaseModel):
    scores: ReviewScores
    total: float = Field(ge=1.0, le=5.0)
    pass_: bool = Field(default=True, alias="pass")
    suggestions: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# TaskState（PRD 5.2 字段级定义）
# ---------------------------------------------------------------------------
class ApprovalState(TypedDict, total=False):
    approval_id: int | None
    status: Literal["PENDING", "APPROVED", "REJECTED", "EXPIRED"]


class TaskError(TypedDict, total=False):
    code: str
    message: str
    span_id: str | None


class TokenLedgerEntry(TypedDict, total=False):
    input: int
    output: int


class TaskState(TypedDict, total=False):
    """LangGraph 全局状态。

    W2 接入时通过 Annotated[..., reducer] 为 messages / subtask_results /
    artifacts / token_ledger 声明合并策略；W1 仅保留结构定义。
    """

    task_id: int | str
    trace_id: str
    goal: str
    time_range: TimeRange | dict[str, Any]
    plan: list[SubtaskPlan]
    subtask_results: dict[str, SubtaskResult]
    replan_count: int
    approval_state: ApprovalState
    messages: list[dict[str, Any]]
    token_ledger: dict[str, TokenLedgerEntry]
    context_digest: str
    artifacts: list[ArtifactRef]
    eval_score: dict[str, Any]
    error: TaskError


__all__ = [
    "TimeRange",
    "SubtaskPlan",
    "PlanOutput",
    "ArtifactRef",
    "SubtaskResult",
    "ReviewScores",
    "ReviewOutput",
    "ApprovalState",
    "TaskError",
    "TokenLedgerEntry",
    "TaskState",
]
```

---

### `app/schemas/orm_models.py`

> 316 行 · `python`

```python
"""PRD 第六章 7 张 MySQL 表的 SQLAlchemy 2.0 ORM 映射。

表清单（严格对齐 PRD 6.1 ~ 6.7）：
1. users            用户
2. tasks            任务主表
3. subtasks         子任务
4. task_executions  执行轨迹 / span
5. approvals        审批
6. memories         分层记忆
7. eval_runs        评测运行

约定：
- 所有表继承 Base + TimestampMixin（created_at / updated_at）
- 主键 id BIGINT UNSIGNED AUTO_INCREMENT（SQLite 测试下由 pk_column 变体处理）
- 字段注释保留 PRD 语义，便于 alembic 迁移评审
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CHAR,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base import Base, TimestampMixin, pk_column
from app.db.types import db_enum
from app.schemas.enums import (
    ApprovalStatus,
    ApprovalType,
    EvalRunStatus,
    EvalSource,
    EvalVariant,
    ExecutionNode,
    MemoryScope,
    SubtaskRiskLevel,
    SubtaskStatus,
    SubtaskToolHint,
    TaskStatus,
    UserRole,
)


# ---------------------------------------------------------------------------
# 6.1 users
# ---------------------------------------------------------------------------
class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = pk_column()
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, comment="登录名")
    password_hash: Mapped[str] = mapped_column(CHAR(60), nullable=False, comment="bcrypt 哈希（60 字符）")
    role: Mapped[UserRole] = mapped_column(
        db_enum(UserRole, "user_role"),
        nullable=False,
        default=UserRole.VIEWER,
        index=True,
        comment="admin / approver / viewer",
    )
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1, comment="1 启用 / 0 禁用")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="最近登录时间")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} username={self.username!r} role={self.role.value}>"


# ---------------------------------------------------------------------------
# 6.2 tasks
# ---------------------------------------------------------------------------
class Task(Base, TimestampMixin):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_status_created_at", "status", "created_at"),
    )

    id: Mapped[int] = pk_column()
    task_no: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, comment="业务编号 AF20261020-0001")
    creator_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True, comment="创建人 FK users"
    )
    goal: Mapped[str] = mapped_column(Text, nullable=False, comment="自然语言目标原文")
    time_range: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict, comment='{"start","end"}')
    status: Mapped[TaskStatus] = mapped_column(
        db_enum(TaskStatus, "task_status"),
        nullable=False,
        default=TaskStatus.PLANNING,
        comment="状态机见 PRD 5.2",
    )
    replan_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, comment="重规划计数 0~2")
    token_usage: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="全任务 Token 合计")
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, comment="任务级追踪 ID")
    result_artifact_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="最终报告产物")
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, comment="{code,message,span_id}")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="终态时间")

    subtasks: Mapped[list["Subtask"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Subtask.seq",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Task id={self.id} no={self.task_no} status={self.status.value}>"


# ---------------------------------------------------------------------------
# 6.3 subtasks
# ---------------------------------------------------------------------------
class Subtask(Base, TimestampMixin):
    __tablename__ = "subtasks"
    __table_args__ = (
        UniqueConstraint("task_id", "seq", name="uq_subtasks_task_seq"),
        Index("ix_subtasks_task_id_seq", "task_id", "seq"),
    )

    id: Mapped[int] = pk_column()
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, comment="FK tasks"
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False, comment="子任务序号")
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="子任务名称")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="子任务描述")
    tool_hint: Mapped[SubtaskToolHint] = mapped_column(
        db_enum(SubtaskToolHint, "subtask_tool_hint"),
        nullable=False,
        default=SubtaskToolHint.NONE,
        comment="建议工具 sql_query/chart_render/kb_search/notify/none",
    )
    depends_on: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list, comment="前置子任务 seq 数组")
    risk_level: Mapped[SubtaskRiskLevel] = mapped_column(
        db_enum(SubtaskRiskLevel, "subtask_risk_level"),
        nullable=False,
        default=SubtaskRiskLevel.NORMAL,
        index=True,
        comment="high 触发审批闸门",
    )
    status: Mapped[SubtaskStatus] = mapped_column(
        db_enum(SubtaskStatus, "subtask_status"),
        nullable=False,
        default=SubtaskStatus.PENDING,
    )
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True, comment="结果摘要（瘦身版，进上下文）")
    artifact_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list, comment="产物引用")
    retry_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, comment="工具重试计数")

    task: Mapped[Task] = relationship(back_populates="subtasks")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Subtask id={self.id} task={self.task_id} seq={self.seq} status={self.status.value}>"


# ---------------------------------------------------------------------------
# 6.4 task_executions（执行轨迹 / span）
# ---------------------------------------------------------------------------
class TaskExecution(Base, TimestampMixin):
    __tablename__ = "task_executions"
    __table_args__ = (
        Index("ix_task_executions_trace_id_started_at", "trace_id", "started_at"),
    )

    id: Mapped[int] = pk_column()
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False, comment="任务级 trace_id")
    span_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, comment="节点级 span_id")
    parent_span_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True, comment="父 span，支持子图嵌套")
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subtask_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("subtasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    node: Mapped[ExecutionNode] = mapped_column(
        db_enum(ExecutionNode, "execution_node"), nullable=False, comment="LangGraph 节点"
    )
    event: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="enter/exit/tool.call/tool.result/replan/human_edit/error"
    )
    tool_name: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True, comment="工具名")
    tool_error: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="工具错误码")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, comment="脱敏后入参出参")
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), comment="span 开始时间（回放排序依据）"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<TaskExecution span={self.span_id} node={self.node.value} event={self.event}>"


# ---------------------------------------------------------------------------
# 6.5 approvals
# ---------------------------------------------------------------------------
class Approval(Base, TimestampMixin):
    __tablename__ = "approvals"
    __table_args__ = (
        Index("ix_approvals_status_created_at", "status", "created_at"),
    )

    id: Mapped[int] = pk_column()
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subtask_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("subtasks.id", ondelete="SET NULL"), nullable=True, comment="触发审批的子任务"
    )
    type: Mapped[ApprovalType] = mapped_column(
        db_enum(ApprovalType, "approval_type"), nullable=False, comment="HIGH_RISK_TOOL / LARGE_SCAN"
    )
    reason: Mapped[str] = mapped_column(String(255), nullable=False, comment="触发原因")
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, comment="产物/参数预览（脱敏）")
    status: Mapped[ApprovalStatus] = mapped_column(
        db_enum(ApprovalStatus, "approval_status"), nullable=False, default=ApprovalStatus.PENDING
    )
    requested_by: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, comment="发起人：系统发起固定 0"
    )
    decided_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="审核人")
    decide_comment: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="驳回意见 / 批准备注")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="挂起截止（24h）")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Approval id={self.id} task={self.task_id} status={self.status.value}>"


# ---------------------------------------------------------------------------
# 6.6 memories
# ---------------------------------------------------------------------------
class Memory(Base, TimestampMixin):
    __tablename__ = "memories"
    __table_args__ = (
        CheckConstraint("importance >= 1 AND importance <= 5", name="importance_range"),
    )

    id: Mapped[int] = pk_column()
    scope: Mapped[MemoryScope] = mapped_column(
        db_enum(MemoryScope, "memory_scope"),
        nullable=False,
        index=True,
        comment="short 存 Redis；本表落 mid / long",
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="记忆正文")
    chroma_doc_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, comment="Chroma 向量引用（复用项目一库）"
    )
    importance: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=3, comment="1~5，>=3 可晋级 long")
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="命中计数")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="mid 层 30 天滚动")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Memory id={self.id} scope={self.scope.value} importance={self.importance}>"


# ---------------------------------------------------------------------------
# 6.7 eval_runs
# ---------------------------------------------------------------------------
class EvalRun(Base, TimestampMixin):
    __tablename__ = "eval_runs"

    id: Mapped[int] = pk_column()
    run_no: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, comment="业务编号")
    source: Mapped[EvalSource] = mapped_column(
        db_enum(EvalSource, "eval_source"),
        nullable=False,
        index=True,
        comment="reviewer_gate / regression / ab_test",
    )
    dataset_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="项目二 dataset 标识 agentflow-30")
    variant: Mapped[EvalVariant | None] = mapped_column(
        db_enum(EvalVariant, "eval_variant"), nullable=True, comment="A/B 分组"
    )
    ext_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="项目二 eval run 回溯 id")
    metrics: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="{task_completion,plan_f1,tool_acc,token_cost,latency_p50/p95}"
    )
    status: Mapped[EvalRunStatus] = mapped_column(
        db_enum(EvalRunStatus, "eval_run_status"), nullable=False, default=EvalRunStatus.PENDING
    )
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, comment="逐样本结果摘要")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<EvalRun id={self.id} no={self.run_no} status={self.status.value}>"


__all__ = [
    "User",
    "Task",
    "Subtask",
    "TaskExecution",
    "Approval",
    "Memory",
    "EvalRun",
]
```

---

### `app/services/__init__.py`

> 2 行 · `python`

```python
"""services 层：业务服务（W1 仅 auth_service 为可运行实现，其余为占位）。"""
```

---

### `app/services/approval_service.py`

> 15 行 · `python`

```python
"""审批服务（占位）。

W1 边界：不创建 / 不裁决 approval，不接 human_gate interrupt。
交付计划：W3 交付（FR-011）。
"""

from __future__ import annotations


class ApprovalService:
    """W3 交付。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError("W1 仅骨架：ApprovalService 在 W3 实现（PRD 里程碑 W3）")
```

---

### `app/services/auth_service.py`

> 101 行 · `python`

```python
"""鉴权业务服务：登录 / 刷新轮换 / 注销拉黑。

对齐项目二 auth-kit 的**行为语义**（登录签发双 Token、refresh 一次性轮换、
注销将 access jti 写入 Redis 黑名单），本项目内部独立实现，未复制任何外部源码。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppException, ErrorCode
from app.core.logging import get_logger
from app.core.security import TokenType, create_token, decode_token, dummy_verify, verify_password
from app.schemas.auth import CurrentUser, TokenPair
from app.schemas.enums import UserRole
from app.schemas.orm_models import User
from app.services.token_store import TokenStore
from app.utils.time import utcnow

logger = get_logger(__name__)


class AuthService:
    def __init__(self, db: AsyncSession, store: TokenStore) -> None:
        self._db = db
        self._store = store

    # ---------------- 查询 ----------------
    async def get_user_by_username(self, username: str) -> User | None:
        stmt = select(User).where(User.username == username).limit(1)
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def get_user_by_id(self, user_id: int) -> User | None:
        return await self._db.get(User, user_id)

    # ---------------- 登录 ----------------
    async def authenticate(self, username: str, password: str) -> User:
        user = await self.get_user_by_username(username)
        if user is None:
            # 用户不存在也走一次 bcrypt，避免响应时间差暴露账号存在性
            dummy_verify(password)
            logger.info("login_failed", extra={"username": username, "reason": "user_not_found"})
            raise AppException(ErrorCode.AUTH_INVALID_CREDENTIALS, "用户名或密码错误")
        if not verify_password(password, user.password_hash):
            logger.info("login_failed", extra={"username": username, "reason": "bad_password"})
            raise AppException(ErrorCode.AUTH_INVALID_CREDENTIALS, "用户名或密码错误")
        if int(user.status) != 1:
            logger.warning("login_blocked", extra={"user_id": user.id, "reason": "disabled"})
            raise AppException(ErrorCode.AUTH_USER_DISABLED, "账号已被禁用，请联系管理员")
        return user

    async def login(self, username: str, password: str) -> tuple[User, TokenPair]:
        user = await self.authenticate(username, password)
        pair = await self._issue_pair(user)
        user.last_login_at = utcnow()
        await self._db.commit()
        await self._db.refresh(user)
        logger.info("login_success", extra={"user_id": user.id, "role": user.role.value})
        return user, pair

    # ---------------- 刷新（一次性轮换） ----------------
    async def refresh(self, refresh_token: str) -> TokenPair:
        claims = decode_token(refresh_token, TokenType.REFRESH)
        if await self._store.is_blacklisted(claims.jti):
            raise AppException(ErrorCode.AUTH_REFRESH_REVOKED, "刷新令牌已失效，请重新登录")
        if not await self._store.consume_refresh(claims.jti):
            # 已消费 / 过期 / 不存在：可能是令牌重放，直接拒绝
            logger.warning("refresh_replay_detected", extra={"user_id": claims.user_id, "jti": claims.jti})
            raise AppException(ErrorCode.AUTH_REFRESH_REVOKED, "刷新令牌已失效，请重新登录")

        user = await self.get_user_by_id(claims.user_id)
        if user is None:
            raise AppException(ErrorCode.AUTH_TOKEN_INVALID, "令牌对应的用户不存在")
        if int(user.status) != 1:
            raise AppException(ErrorCode.AUTH_USER_DISABLED, "账号已被禁用，请联系管理员")

        pair = await self._issue_pair(user)
        logger.info("token_refreshed", extra={"user_id": user.id})
        return pair

    # ---------------- 注销 ----------------
    async def logout(self, current: CurrentUser) -> None:
        if not current.jti:
            raise AppException(ErrorCode.AUTH_TOKEN_INVALID, "缺少可注销的令牌标识")
        await self._store.blacklist_access(jti=current.jti, expires_at=current.expires_at)
        logger.info("logout_success", extra={"user_id": current.id, "jti": current.jti})

    # ---------------- 内部 ----------------
    async def _issue_pair(self, user: User) -> TokenPair:
        role = user.role.value if isinstance(user.role, UserRole) else str(user.role)
        access = create_token(user_id=user.id, username=user.username, role=role, token_type=TokenType.ACCESS)
        refresh = create_token(user_id=user.id, username=user.username, role=role, token_type=TokenType.REFRESH)
        await self._store.register_refresh(jti=refresh.jti, user_id=user.id, expires_at=refresh.expires_at)
        return TokenPair(
            access_token=access.token,
            refresh_token=refresh.token,
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )
```

---

### `app/services/checkpoint_store.py`

> 22 行 · `python`

```python
"""断点快照服务（占位）：ckpt:{task_id}，TTL 72h。

W1 边界：不写快照、不做恢复；LangGraph RedisSaver 仅在 db/checkpointer.py 做导入预留。
交付计划：W3 交付（FR-012）。
"""

from __future__ import annotations

from app.core.config import settings


def checkpoint_key(task_id: int | str) -> str:
    """快照 key 约定（PRD 5.2）：agentflow:ckpt:{task_id}。"""
    return settings.key("ckpt", str(task_id))


class CheckpointStore:
    """W3 交付。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError("W1 仅骨架：CheckpointStore 在 W3 实现（PRD 里程碑 W3）")
```

---

### `app/services/context_service.py`

> 15 行 · `python`

```python
"""上下文工程服务（占位）：Token 预算 / 压缩 / 工具结果瘦身。

W1 边界：不实现任何压缩与瘦身规则。
交付计划：W2 交付（FR-010，规则见 PRD 9.2 / 9.3）。
"""

from __future__ import annotations


class ContextService:
    """W2 交付。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError("W1 仅骨架：ContextService 在 W2 实现（PRD 里程碑 W2）")
```

---

### `app/services/eval_gateway.py`

> 24 行 · `python`

```python
"""项目二 EvalAgent 网关（占位）：评测创建 / 结果轮询 / faithfulness 打分。

W1 边界：不发任何 HTTP 请求。Mock 开关 settings.MOCK_EXTERNAL_EVAL 已在配置层就绪，
W4 实现时按开关分流：
    True  -> 返回结构化 Mock 结果（固定分数 + degraded 标记），不发网络请求
    False -> httpx.AsyncClient 调用 EXTERNAL_EVAL_BASE_URL（POST /api/v1/eval/runs 等）
交付计划：W4 交付（FR-013 / FR-016，接口见 PRD 10.3）。
"""

from __future__ import annotations

from app.core.config import settings


def is_mock() -> bool:
    return settings.MOCK_EXTERNAL_EVAL


class EvalGateway:
    """W4 交付。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError("W1 仅骨架：EvalGateway 在 W4 实现（PRD 里程碑 W4）")
```

---

### `app/services/event_bus.py`

> 19 行 · `python`

```python
"""事件总线（占位）：SSE 事件经 Redis Stream 分发。

W1 边界：不创建 Stream、不实现发布订阅与断线重放。
交付计划：W3 交付（FR-009，协议见 PRD 7.2）。
"""

from __future__ import annotations

EVENT_STREAM_PREFIX = "events"  # W3 使用：settings.key("events", task_id)
EVENT_STREAM_MAXLEN = 10000
EVENT_STREAM_TTL_SECONDS = 24 * 3600


class EventBus:
    """W3 交付。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError("W1 仅骨架：EventBus 在 W3 实现（PRD 里程碑 W3）")
```

---

### `app/services/memory_service.py`

> 15 行 · `python`

```python
"""分层记忆服务（占位）。

W1 边界：不读写 memories 表、不接 Chroma。
交付计划：W4 交付（FR-014）。
"""

from __future__ import annotations


class MemoryService:
    """W4 交付。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError("W1 仅骨架：MemoryService 在 W4 实现（PRD 里程碑 W4）")
```

---

### `app/services/plan_service.py`

> 15 行 · `python`

```python
"""规划服务（占位）。

W1 边界：不实现规划 / 重规划 / DAG 拓扑校验。
交付计划：W2 与 agents/planner.py 一同实现（FR-002、FR-003）。
"""

from __future__ import annotations


class PlanService:
    """W2 交付。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError("W1 仅骨架：PlanService 在 W2 实现（PRD 里程碑 W2）")
```

---

### `app/services/rag_gateway.py`

> 25 行 · `python`

```python
"""项目一 RAG 检索网关（占位）：调用 POST {rag}/api/v1/kb/search。

W1 边界：不发任何 HTTP 请求。Mock 开关 settings.MOCK_EXTERNAL_RAG 已在配置层就绪，
W2 实现时按开关分流：
    True  -> 返回固定 chunks 的 Mock 结果（degraded=false, source="mock"）
    False -> httpx 调用项目一混合检索接口，超时 8s、失败重试 2 次后退化为
             degraded=true, chunks=[]（PRD FR-007 降级策略）
交付计划：W2 交付（FR-007）。
"""

from __future__ import annotations

from app.core.config import settings


def is_mock() -> bool:
    return settings.MOCK_EXTERNAL_RAG


class RagGateway:
    """W2 交付。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError("W1 仅骨架：RagGateway 在 W2 实现（PRD 里程碑 W2）")
```

---

### `app/services/task_service.py`

> 18 行 · `python`

```python
"""任务服务（占位）。

W1 边界：仅占位，不实现任何任务创建 / 状态机 / 取消 / 恢复逻辑。
交付计划：
- W2：create_task(goal, time_range) 落 tasks 表 + 生成 trace_id + 推 task.created 事件
- W3：cancel / resume（配合 checkpointer 断点恢复）
对应 FR：FR-001、FR-012
"""

from __future__ import annotations


class TaskService:
    """W2 交付。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError("W1 仅骨架：TaskService 在 W2 实现（PRD 里程碑 W2）")
```

---

### `app/services/token_store.py`

> 106 行 · `python`

```python
"""Token 存储（Redis）：refresh 轮换登记 + access 黑名单。

Key 设计（统一前缀 settings.REDIS_KEY_PREFIX = agentflow）：
    agentflow:auth:refresh:{jti}        -> user_id       TTL = refresh 剩余有效期
    agentflow:auth:refresh:user:{uid}   -> ZSET(jti, exp) 单用户会话索引（限会话数）
    agentflow:auth:blacklist:{jti}      -> "1"           TTL = access 剩余有效期
"""

from __future__ import annotations

from datetime import datetime

from redis.asyncio import Redis

from app.core.config import settings
from app.core.logging import get_logger
from app.utils.time import utcnow

logger = get_logger(__name__)


def _ttl_seconds(expires_at: datetime | None) -> int:
    if expires_at is None:
        return 1
    return max(1, int((expires_at - utcnow()).total_seconds()))


class TokenStore:
    """鉴权状态的 Redis 侧读写；不接触数据库。"""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    # ---------------- key ----------------
    @staticmethod
    def refresh_key(jti: str) -> str:
        return settings.key("auth", "refresh", jti)

    @staticmethod
    def user_sessions_key(user_id: int) -> str:
        return settings.key("auth", "refresh", "user", str(user_id))

    @staticmethod
    def blacklist_key(jti: str) -> str:
        return settings.key("auth", "blacklist", jti)

    # ---------------- refresh ----------------
    async def register_refresh(self, *, jti: str, user_id: int, expires_at: datetime) -> None:
        ttl = _ttl_seconds(expires_at)
        pipe = self._redis.pipeline()
        pipe.set(self.refresh_key(jti), str(user_id), ex=ttl)
        pipe.zadd(self.user_sessions_key(user_id), {jti: expires_at.timestamp()})
        # 会话索引多留 1 天，避免 refresh 刚过期时索引先消失导致统计偏差
        pipe.expire(self.user_sessions_key(user_id), ttl + 86400)
        await pipe.execute()
        await self._trim_user_sessions(user_id)

    async def consume_refresh(self, jti: str) -> bool:
        """一次性消费：删除成功表示本次轮换合法，否则视为重放。"""
        key = self.refresh_key(jti)
        pipe = self._redis.pipeline()
        pipe.get(key)
        pipe.delete(key)
        value, deleted = await pipe.execute()
        return bool(value) and bool(deleted)

    async def revoke_refresh(self, jti: str) -> None:
        await self._redis.delete(self.refresh_key(jti))

    async def revoke_user_sessions(self, user_id: int) -> int:
        """强制下线：清空该用户全部 refresh 会话。"""
        key = self.user_sessions_key(user_id)
        jtis = await self._redis.zrange(key, 0, -1)
        if jtis:
            await self._redis.delete(*[self.refresh_key(jti) for jti in jtis])
        await self._redis.delete(key)
        return len(jtis)

    async def _trim_user_sessions(self, user_id: int) -> None:
        limit = settings.AUTH_MAX_SESSIONS_PER_USER
        if limit <= 0:
            return
        key = self.user_sessions_key(user_id)
        # 清理已过期会话，再按过期时间淘汰最旧会话
        await self._redis.zremrangebyscore(key, "-inf", utcnow().timestamp())
        count = await self._redis.zcard(key)
        if count > limit:
            stale = await self._redis.zrange(key, 0, count - limit - 1)
            if stale:
                await self._redis.delete(*[self.refresh_key(jti) for jti in stale])
                await self._redis.zrem(key, *stale)

    # ---------------- blacklist ----------------
    async def blacklist_access(self, *, jti: str, expires_at: datetime | None) -> None:
        await self._redis.set(self.blacklist_key(jti), "1", ex=_ttl_seconds(expires_at))

    async def is_blacklisted(self, jti: str) -> bool:
        return bool(await self._redis.exists(self.blacklist_key(jti)))

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:  # noqa: BLE001
            logger.warning("token_store_ping_failed")
            return False
```

---

### `app/tools/__init__.py`

> 6 行 · `python`

```python
"""tools 层：MCP Client 与 4 个 MCP Server（均为目录占位）。

W1 边界：不实现 JSON-RPC 握手、不实现 tools/list / tools/call、不启动任何 server 进程。
W2 交付：initialize / tools/list / tools/call 三个方法（stdio 优先，PRD 5.3 / R-2）。
"""
```

---

### `app/tools/mcp_client.py`

> 26 行 · `python`

```python
"""MCP Client（占位）：JSON-RPC 2.0 over stdio。

W1 边界：**禁止实现**协议握手、工具发现、调用与超时重试。
交付计划：W2（PRD 5.3 时序 1~8 步）。
"""

from __future__ import annotations

from typing import Any


class MCPClient:
    """W2 交付。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError("W1 仅占位：MCPClient 在 W2 实现（PRD 里程碑 W2）")

    async def initialize(self) -> dict[str, Any]:
        raise NotImplementedError

    async def list_tools(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError
```

---

### `app/tools/mcp_servers/__init__.py`

> 6 行 · `python`

```python
"""4 个 MCP Server 的占位包（sql-query / chart-render / kb-search / notify）。

W1 边界：**不实现任何 server 业务逻辑**，docker-compose 也不启动这些服务。
W2 交付；每个 server 独立进程入口，stdio 传输优先（PRD FR-005 ~ FR-008）。
"""
```

---

### `app/tools/mcp_servers/chart_render.py`

> 21 行 · `python`

```python
"""MCP Server：chart-render（占位）。

W1 边界：**不实现** ECharts option 生成、图片渲染、字段白名单校验。
交付计划：W2（FR-006）。接口契约（提前固化）：
    输入  {chart_type: line|bar|pie, title, x_field, y_fields: [str], data: [obj] (<=500), size: s|m|l}
    输出  {chart_id, image_url, echarts_option, render_ms}
    错误码 CHART_DATA_INVALID / RENDER_TIMEOUT / RENDER_FAILED
"""

from __future__ import annotations

SERVER_NAME = "chart-render"


def main() -> None:
    raise NotImplementedError("W1 仅占位：chart-render MCP Server 在 W2 实现（PRD FR-006）")


if __name__ == "__main__":  # pragma: no cover
    main()
```

---

### `app/tools/mcp_servers/kb_search.py`

> 21 行 · `python`

```python
"""MCP Server：kb-search（占位，对外 HTTP 调用项目一 RAG）。

W1 边界：**不实现** HTTP 调用、重试、degraded 降级；不读取 MOCK_EXTERNAL_RAG 分支。
交付计划：W2（FR-007）。接口契约（提前固化）：
    输入  {query: str (<=200), top_k: int = 3 (<=5), tenant: str = "agentflow"}
    输出  {chunks: [{doc_id, title, content, score, source}], latency_ms, degraded: bool}
    错误码 KB_TIMEOUT / KB_UNAVAILABLE / KB_AUTH_FAILED
"""

from __future__ import annotations

SERVER_NAME = "kb-search"


def main() -> None:
    raise NotImplementedError("W1 仅占位：kb-search MCP Server 在 W2 实现（PRD FR-007）")


if __name__ == "__main__":  # pragma: no cover
    main()
```

---

### `app/tools/mcp_servers/notify.py`

> 21 行 · `python`

```python
"""MCP Server：notify（占位，恒 Mock，不出网）。

W1 边界：**不实现** APPROVED 审批前置校验、mock_messages 落库、结构化日志。
交付计划：W2（FR-008）。接口契约（提前固化）：
    输入  {channel: feishu|email, title (<=50), content (<=2000), receivers: [str] (<=10)}
    输出  {message_id, status: SENT|QUEUED|FAILED, mock: true, sent_at}
    错误码 APPROVAL_MISSING / NOTIFY_INVALID / NOTIFY_FAILED
"""

from __future__ import annotations

SERVER_NAME = "notify"


def main() -> None:
    raise NotImplementedError("W1 仅占位：notify MCP Server 在 W2 实现（PRD FR-008）")


if __name__ == "__main__":  # pragma: no cover
    main()
```

---

### `app/tools/mcp_servers/sql_query.py`

> 21 行 · `python`

```python
"""MCP Server：sql-query（占位）。

W1 边界：**不实现** SQL 解析白名单、只读执行、强制 LIMIT、截断。
交付计划：W2（FR-005）。接口契约（提前固化）：
    输入  {sql: str, max_rows: int = 200 (<=1000)}
    输出  {columns: [str], rows: [[any]], row_count: int, truncated: bool, exec_ms: int}
    错误码 SQL_SYNTAX / SQL_TIMEOUT / SQL_TOO_LARGE / DB_UNAVAILABLE
"""

from __future__ import annotations

SERVER_NAME = "sql-query"


def main() -> None:
    raise NotImplementedError("W1 仅占位：sql-query MCP Server 在 W2 实现（PRD FR-005）")


if __name__ == "__main__":  # pragma: no cover
    main()
```

---

### `app/utils/__init__.py`

> 2 行 · `python`

```python
"""通用工具：ID 生成、时间、脱敏。"""
```

---

### `app/utils/ids.py`

> 33 行 · `python`

```python
"""ID 生成（trace_id / span_id / 业务编号）。"""

from __future__ import annotations

import uuid
from datetime import datetime


def new_trace_id() -> str:
    """任务级追踪 ID：32 位 hex（对齐 PRD tasks.trace_id VARCHAR(32)）。"""
    return uuid.uuid4().hex


def new_span_id() -> str:
    """节点级 span ID：36 位 UUID 字符串（对齐 task_executions.span_id VARCHAR(36)）。"""
    return str(uuid.uuid4())


def new_uuid() -> str:
    return str(uuid.uuid4())


def new_task_no(seq: int, when: datetime | None = None) -> str:
    """业务编号：AF20261020-0001。"""
    day = (when or datetime.now()).strftime("%Y%m%d")
    return f"AF{day}-{seq:04d}"


def new_run_no(seq: int, when: datetime | None = None) -> str:
    """评测编号：AFRUN20261020-0001。"""
    day = (when or datetime.now()).strftime("%Y%m%d")
    return f"AFRUN{day}-{seq:04d}"
```

---

### `app/utils/masking.py`

> 42 行 · `python`

```python
"""日志 / 轨迹 payload 脱敏（PRD NFR-S3：手机号、邮箱、token 打码）。"""

from __future__ import annotations

import re
from typing import Any

_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]{1,3})[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_TOKEN_KEYS = {"token", "access_token", "refresh_token", "authorization", "password", "api_key", "secret"}
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")


def mask_phone(value: str) -> str:
    return _PHONE_RE.sub(r"\1****\2", value)


def mask_email(value: str) -> str:
    return _EMAIL_RE.sub(r"\1***\2", value)


def mask_text(value: str) -> str:
    value = _BEARER_RE.sub("Bearer ***", value)
    return mask_email(mask_phone(value))


def mask_mapping(data: dict[str, Any]) -> dict[str, Any]:
    """递归脱敏字典：敏感 key 全量打码，字符串值做正则替换。"""
    masked: dict[str, Any] = {}
    for key, value in data.items():
        if key.lower() in _TOKEN_KEYS:
            masked[key] = "***"
        elif isinstance(value, dict):
            masked[key] = mask_mapping(value)
        elif isinstance(value, list):
            masked[key] = [mask_mapping(v) if isinstance(v, dict) else (mask_text(v) if isinstance(v, str) else v) for v in value]
        elif isinstance(value, str):
            masked[key] = mask_text(value)
        else:
            masked[key] = value
    return masked
```

---

### `app/utils/time.py`

> 23 行 · `python`

```python
"""时间工具（统一 UTC 存储、带时区安全）。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


def utcnow() -> datetime:
    """统一以 UTC naive datetime 落库（MySQL DATETIME 无时区）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utcnow_tz() -> datetime:
    return datetime.now(timezone.utc)


def from_timestamp(ts: int | float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def days_ago(days: int) -> date:
    return (utcnow() - timedelta(days=days)).date()
```

---

### `docker-compose.yml`

> 104 行 · `yaml`

```yaml
# AgentFlow Platform —— W1 本地编排
# 仅 mysql + redis + backend；前端（W3）与 4 个 MCP 业务服务（W2）本阶段不启动。
# 使用：cp .env.example .env && docker compose up -d --build

name: agentflow-platform

services:
  mysql:
    image: mysql:8.0
    container_name: agentflow-mysql
    command:
      - --character-set-server=utf8mb4
      - --collation-server=utf8mb4_general_ci
      - --default-authentication-plugin=mysql_native_password
    environment:
      MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD:-root_pwd}
      MYSQL_DATABASE: ${MYSQL_DB:-agentflow}
      MYSQL_USER: ${MYSQL_USER:-agentflow}
      MYSQL_PASSWORD: ${MYSQL_PASSWORD:-agentflow_pwd}
      TZ: Asia/Shanghai
    ports:
      - "${MYSQL_PORT:-3306}:3306"
    volumes:
      - mysql_data:/var/lib/mysql
    healthcheck:
      test: ["CMD-SHELL", "mysqladmin ping -h 127.0.0.1 -uroot -p$$MYSQL_ROOT_PASSWORD --silent"]
      interval: 5s
      timeout: 5s
      retries: 30
      start_period: 40s
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    container_name: agentflow-redis
    command: ["redis-server", "--appendonly", "yes", "--maxmemory-policy", "noeviction"]
    ports:
      - "${REDIS_PORT:-6379}:6379"
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 20
      start_period: 5s
    restart: unless-stopped

  backend:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: agentflow-backend
    environment:
      APP_ENV: ${APP_ENV:-local}
      DEBUG: ${DEBUG:-true}
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
      LOG_JSON: ${LOG_JSON:-true}
      SECRET_KEY: ${SECRET_KEY:-dev-only-secret-change-me-0123456789abcdef}
      # 容器内通过服务名访问，覆盖 .env 里的 127.0.0.1
      MYSQL_HOST: mysql
      MYSQL_PORT: 3306
      MYSQL_USER: ${MYSQL_USER:-agentflow}
      MYSQL_PASSWORD: ${MYSQL_PASSWORD:-agentflow_pwd}
      MYSQL_DB: ${MYSQL_DB:-agentflow}
      REDIS_HOST: redis
      REDIS_PORT: 6379
      REDIS_DB: ${REDIS_DB:-0}
      REDIS_KEY_PREFIX: ${REDIS_KEY_PREFIX:-agentflow}
      ACCESS_TOKEN_EXPIRE_MINUTES: ${ACCESS_TOKEN_EXPIRE_MINUTES:-120}
      REFRESH_TOKEN_EXPIRE_DAYS: ${REFRESH_TOKEN_EXPIRE_DAYS:-7}
      MOCK_EXTERNAL_RAG: ${MOCK_EXTERNAL_RAG:-true}
      MOCK_EXTERNAL_EVAL: ${MOCK_EXTERNAL_EVAL:-true}
      LLM_PROVIDER: ${LLM_PROVIDER:-deepseek}
      LLM_BASE_URL: ${LLM_BASE_URL:-https://api.deepseek.com/v1}
      LLM_API_KEY: ${LLM_API_KEY:-}
      LLM_MODEL: ${LLM_MODEL:-deepseek-chat}
    ports:
      - "8000:8000"
    depends_on:
      mysql:
        condition: service_healthy
      redis:
        condition: service_healthy
    # 先跑迁移再起服务：冷启动幂等（无新迁移时 upgrade head 为空操作）
    command: >
      sh -c "alembic upgrade head &&
             uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers"
    healthcheck:
      test:
        - CMD-SHELL
        - python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"
      interval: 10s
      timeout: 5s
      retries: 12
      start_period: 30s
    restart: unless-stopped

volumes:
  mysql_data:
    name: agentflow_mysql_data
  redis_data:
    name: agentflow_redis_data
```

---

### `Dockerfile`

> 30 行 · `text`

```text
# ---------- 构建阶段 ----------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 仅安装运行期依赖（bcrypt / aiomysql 均有 wheel，无需编译工具链）
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# 拷贝应用代码（.dockerignore 已排除 .venv / tests / frontend 等）
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts

# 非 root 运行
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 默认命令由 docker-compose 覆盖（先 alembic upgrade head 再起服务）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

### `frontend/.gitkeep`

> 1 行 · `text`

```text

```

---

### `frontend/README.md`

> 12 行 · `markdown`

```markdown
# frontend/ — 前端目录（W1 空目录占位）

Vue3 + TypeScript + Element-Plus + ECharts，W3 / W4 迭代交付：

- 任务台（目标输入 + 时间范围）
- DAG 画布（只读展示 + P1 有限增删改）
- 审批收件箱
- 轨迹回放器
- 评测看板

W1 不创建任何前端工程文件，`docker-compose.yml` 也不启动前端服务。
```

---

### `pytest.ini`

> 23 行 · `ini`

```ini
[pytest]
minversion = 8.0
testpaths = tests
# 保证 `pytest` 在任意 cwd 下都能 import app.*
pythonpath = .
python_files = test_*.py
python_classes = Test*
python_functions = test_*
addopts =
    -ra
    -q
    --strict-markers
    --import-mode=importlib
asyncio_mode = auto
asyncio_default_fixture_loop_scope = function
markers =
    unit: 纯单元测试（无外部依赖）
    auth: 鉴权模块用例（PRD W1 验收项）
    integration: 依赖 MySQL / Redis 容器的集成用例（默认不跑）
filterwarnings =
    ignore::DeprecationWarning
    ignore::PendingDeprecationWarning
```

---

### `README-W1.md`

> 264 行 · `markdown`

```markdown
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
```

---

### `requirements-dev.txt`

> 15 行 · `text`

```text
-r requirements.txt

# ---------- 测试 ----------
pytest>=8.2.0,<9.0.0
pytest-asyncio>=0.23.0,<0.26.0
httpx>=0.27.0,<0.29.0
# 测试期用 SQLite 内存库替代 MySQL，无需启动容器
aiosqlite>=0.20.0,<0.22.0
# 测试期用 fakeredis 替代真实 Redis
fakeredis>=2.23.0,<3.0.0

# ---------- 质量 ----------
ruff>=0.6.0,<0.9.0
pytest-cov>=5.0.0,<7.0.0
```

---

### `requirements.txt`

> 33 行 · `text`

```text
# ---------- Web 框架 ----------
fastapi>=0.115.0,<0.120.0
uvicorn[standard]>=0.30.0,<0.36.0

# ---------- 配置与校验 ----------
pydantic>=2.8.0,<3.0.0
pydantic-settings>=2.4.0,<3.0.0

# ---------- 数据库 ----------
SQLAlchemy[asyncio]>=2.0.30,<2.1.0
aiomysql>=0.2.0,<0.3.0
alembic>=1.13.0,<1.16.0
# 迁移脚本与本地工具链使用的同步驱动（alembic offline / 数据脚本备用）
PyMySQL>=1.1.0,<2.0.0

# ---------- 缓存 / 会话 / 黑名单 ----------
redis>=5.0.0,<6.0.0

# ---------- 鉴权 ----------
PyJWT>=2.9.0,<3.0.0
bcrypt>=4.1.0,<5.0.0

# ---------- 外部调用（项目一 RAG / 项目二 EvalAgent）----------
httpx>=0.27.0,<0.29.0

# ---------- LangGraph（W1 仅导入预留，不写业务 graph）----------
langgraph>=0.2.0,<0.4.0
langgraph-checkpoint-redis>=0.0.3,<0.1.0

# ---------- 工具 ----------
python-dotenv>=1.0.0,<2.0.0
orjson>=3.10.0,<4.0.0
```

---

### `scripts/README.md`

> 20 行 · `markdown`

```markdown
# scripts/ — 数据脚本目录（W1 仅占位）

| 文件 | 状态 | 说明 | 交付周 |
|---|---|---|---|
| `verify_seed.py` | 占位（仅 CLI + 断言清单） | 校验 mall 模拟数据 3 条埋因断言 + 20 个抽样断点，通过输出 `SEED VERIFIED` | W2 |
| `seed_mall_data.py`（待建） | 未创建 | 生成 orders / traffic_records / ad_campaigns / skus / categories，seed=42 | W2 |
| `seed_users.py`（待建） | 未创建 | 初始化 admin / approver / viewer 演示账号 | W2 |
| `ingest_weekly_reports.py`（待建） | 未创建 | 20 份历史周报经项目一上传接口灌入 tenant=agentflow | W2 |

W1 只交付 `verify_seed.py` 的骨架：CLI 参数、断言清单、退出码约定；
不实现任何 SQL 校验与数据生成逻辑。

```bash
# 查看断言清单（不需要数据库）
python scripts/verify_seed.py --dry-run

# W2 之后的真实校验
python scripts/verify_seed.py --dsn "$MYSQL_DSN" --seed 42
```
```

---

### `scripts/verify_seed.py`

> 134 行 · `python`

```python
#!/usr/bin/env python
"""mall 模拟数据校验脚本（W1：占位骨架，未实现实际校验）。

背景（PRD 第 11 章）：
    模拟数据总量约 52 万行（orders ≈50 万 / traffic_records ≈14.4 万 / ad_campaigns ≈5,400），
    时间窗 2026-04-15 ~ 2026-10-18（187 天），随机种子固定 seed=42。
    埋因设定：W42（10-12 ~ 10-18）总 GMV 环比 W41 下跌 18.0%，由且仅由两个因素造成：
      主因：数码配件（category_id=3）转化率由基线 3.1% 降至 1.7%（SKU-10086 断货下架）
      次因：该品类广告日预算由 12,000 降至 7,200（-40%），付费流量 -24%

本脚本职责（W2 交付时实现）：
    数据生成完成后自动执行 3 条硬断言 + 20 个抽样断点，全部通过打印 `SEED VERIFIED`，
    否则以非 0 退出码失败，保证 demo 结论可复现、评测集断言可依赖。

W1 边界：
    只保留 CLI 骨架、断言清单与连接方式说明，**不实现任何 SQL 校验逻辑**，
    也不做数据生成（生成器同样属于 W2 交付物）。

用法（W2 生效）：
    python scripts/verify_seed.py --dsn mysql+aiomysql://... --seed 42 --sample-points 20
    python scripts/verify_seed.py --dry-run        # 打印断言清单，不连库
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class SeedAssertion:
    """验收断言定义（PRD 11.2 可验证断言）。"""

    code: str
    title: str
    expectation: str


# 3 条硬断言：任一失败即判定数据不可用
ASSERTIONS: tuple[SeedAssertion, ...] = (
    SeedAssertion(
        code="A1",
        title="主因：数码配件转化率断崖",
        expectation="W42 category_id=3 的加权 conv_rate ∈ [1.6%, 1.8%]；其余 7 个品类环比波动 ≤ ±8%",
    ),
    SeedAssertion(
        code="A2",
        title="次因：该品类投放预算削减",
        expectation="W42 category_id=3 的计划日预算合计 = 7,200 × 7 × 计划数；其余品类预算环比 ≤ ±5%",
    ),
    SeedAssertion(
        code="A3",
        title="噪声控制与总量口径",
        expectation="除 cat=3 外各品类 W42 GMV 环比 ∈ [-3%, +3%]；总 GMV 环比 = -18.0% ± 0.5pct",
    ),
)

# 20 个抽样断点：验证汇总层（订单明细 / 流量 / 投放）与报表层一致性
SAMPLING_CHECKS: tuple[str, ...] = (
    "S01 orders 总行数落在 [45万, 55万]",
    "S02 orders 日期覆盖 187 天且无未来日期",
    "S03 orders.order_id + sku_id 唯一无重复",
    "S04 pay_status 分布符合基线（成功占比 92%~96%）",
    "S05 W41 与 W42 的 GMV 汇总与明细逐日累加一致",
    "S06 traffic_records = 800 SKU × 187 天，无缺日",
    "S07 traffic.uv >= paid_uv 恒成立",
    "S08 conv_rate ∈ [0, 1] 且 cat=3 W42 显著低于基线",
    "S09 ad_campaigns = 30 计划 × 187 天，无缺日",
    "S10 cat=3 W42 日预算值为 7,200 的计划条数符合预期",
    "S11 cost <= budget 恒成立",
    "S12 roas 与 (GMV/cost) 口径一致（容差 1%）",
    "S13 维表 skus 800 行、categories 8 行且外键齐全",
    "S14 SKU-10086 自 2026-10-10 起无成功支付订单（断货语义）",
    "S15 各品类 GMV 占比合计 = 100% ± 0.1pct",
    "S16 W42 订单数环比跌幅与 GMV 跌幅方向一致",
    "S17 随机种子复现：同 seed 两次生成的抽样行完全一致",
    "S18 金额字段无负数、无 NULL",
    "S19 时区与日期边界：W42 = 2026-10-12 00:00:00 ~ 2026-10-18 23:59:59",
    "S20 只读账号可查询全部三张业务表（权限校验）",
)


def verify_gmv_attribution(*args: object, **kwargs: object) -> None:
    """断言 A1~A3（W2 实现：按品类 × 周聚合后逐条比对）。"""
    raise NotImplementedError("W1 仅占位：埋因断言校验在 W2 实现（PRD 11.2）")


def verify_sampling_points(*args: object, **kwargs: object) -> None:
    """20 个抽样断点（W2 实现：明细与汇总双向核对）。"""
    raise NotImplementedError("W1 仅占位：抽样断点校验在 W2 实现（PRD 11.2）")


def print_plan(sample_points: int) -> None:
    print("=" * 78)
    print("AgentFlow mall 模拟数据校验（W1 占位）")
    print(f"硬断言 {len(ASSERTIONS)} 条 / 抽样断点 {sample_points} 个 / 随机种子固定")
    print("=" * 78)
    for item in ASSERTIONS:
        print(f"[{item.code}] {item.title}\n     期望：{item.expectation}")
    print("-" * 78)
    for check in SAMPLING_CHECKS[:sample_points]:
        print(f"  - {check}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="校验 mall 模拟数据是否满足 PRD 第 11 章埋因设定")
    parser.add_argument("--dsn", default="", help="SQLAlchemy DSN，默认读取 .env 中的 MYSQL_* 配置")
    parser.add_argument("--seed", type=int, default=42, help="随机种子（默认 42，须与生成器一致）")
    parser.add_argument("--sample-points", type=int, default=20, help="抽样断点数量（默认 20）")
    parser.add_argument("--dry-run", action="store_true", help="只打印断言清单，不连接数据库")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print_plan(min(args.sample_points, len(SAMPLING_CHECKS)))

    if args.dry_run:
        print("\n[DRY-RUN] 已打印断言清单，未连接数据库。")
        return 0

    print(
        "\n[W1 PLACEHOLDER] 校验逻辑尚未实现（W2 交付）。\n"
        "  - 生成器完成后本脚本将依次执行 verify_gmv_attribution() / verify_sampling_points()\n"
        "  - 全部通过输出：SEED VERIFIED\n"
        "  - 任一失败输出具体断言编号与偏差值，并以退出码 1 结束\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
```

---

### `tests/conftest.py`

> 166 行 · `python`

```python
"""pytest 公共夹具。

设计目标：**不依赖 MySQL / Redis 容器**即可跑通鉴权用例。
- MySQL -> SQLite 内存库（StaticPool 保证同一连接，create_all 建 7 张表）
- Redis -> fakeredis（异步客户端，行为与 redis-py 一致）
- FastAPI 依赖通过 dependency_overrides 替换，业务代码零改动
"""

from __future__ import annotations

import os

# 必须在导入 app.* 之前设置：Settings 为 import 期单例（lru_cache）
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789")
os.environ.setdefault("BCRYPT_ROUNDS", "4")  # 测试提速：4 轮足够验证哈希逻辑
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("MOCK_EXTERNAL_RAG", "true")
os.environ.setdefault("MOCK_EXTERNAL_EVAL", "true")

from collections.abc import AsyncIterator  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from fastapi import Depends, FastAPI  # noqa: E402
from fakeredis.aioredis import FakeRedis  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.deps import require_role  # noqa: E402
from app.core.exceptions import register_exception_handlers  # noqa: E402
from app.core.redis_client import get_redis  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.schemas.auth import CurrentUser  # noqa: E402
from app.schemas.enums import UserRole  # noqa: E402
from app.schemas.orm_models import User  # noqa: E402
from app.routers.api_router import api_router  # noqa: E402

# 测试账号：与 README-W1.md 的演示账号保持一致（生产环境必须改密）
TEST_PASSWORD = "Passw0rd!"
TEST_USERS: tuple[tuple[str, UserRole, int], ...] = (
    ("admin_user", UserRole.ADMIN, 1),
    ("approver_user", UserRole.APPROVER, 1),
    ("viewer_user", UserRole.VIEWER, 1),
    ("disabled_user", UserRole.VIEWER, 0),
)


@pytest_asyncio.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """SQLite 内存库 + 全量建表（7 张表 DDL 与 MySQL 同源）。"""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        future=True,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def fake_redis() -> AsyncIterator[FakeRedis]:
    client = FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()


@pytest_asyncio.fixture
async def seeded_users(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    """预置 4 个账号（admin / approver / viewer / 已禁用）。返回 username -> id。"""
    ids: dict[str, int] = {}
    async with session_factory() as session:
        for username, role, status in TEST_USERS:
            user = User(
                username=username,
                password_hash=hash_password(TEST_PASSWORD),
                role=role,
                status=status,
            )
            session.add(user)
            await session.flush()
            ids[username] = user.id
        await session.commit()
    return ids


def _install_overrides(
    target: FastAPI,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: FakeRedis,
) -> None:
    async def _get_db_override() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    async def _get_redis_override() -> AsyncIterator[FakeRedis]:
        yield redis_client

    target.dependency_overrides[get_db] = _get_db_override
    target.dependency_overrides[get_redis] = _get_redis_override


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    fake_redis: FakeRedis,
    seeded_users: dict[str, int],
) -> AsyncIterator[AsyncClient]:
    """完整应用（含 TraceID 中间件、全局异常处理器、/api/v1 路由）。"""
    app = create_app()
    _install_overrides(app, session_factory, fake_redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as http_client:
        yield http_client


@pytest_asyncio.fixture
async def protected_client(
    session_factory: async_sessionmaker[AsyncSession],
    fake_redis: FakeRedis,
    seeded_users: dict[str, int],
) -> AsyncIterator[AsyncClient]:
    """最小 RBAC 探针应用：/admin-only 仅 admin 可访问，用于验证 require_role。"""
    app = FastAPI(title="rbac-probe")
    register_exception_handlers(app)
    _install_overrides(app, session_factory, fake_redis)
    # 复用真实鉴权路由（登录拿 token 后再打 /admin-only）
    app.include_router(api_router)

    @app.get("/admin-only")
    async def admin_only(user: CurrentUser = Depends(require_role(UserRole.ADMIN))) -> dict:
        return {"code": "OK", "data": {"user_id": user.id, "role": user.role.value}}

    @app.get("/any-login")
    async def any_login(user: CurrentUser = Depends(require_role(UserRole.ADMIN, UserRole.APPROVER, UserRole.VIEWER))) -> dict:
        return {"code": "OK", "data": {"user_id": user.id}}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as http_client:
        yield http_client


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
```

---

### `tests/test_auth.py`

> 161 行 · `python`

```python
"""鉴权模块基础用例（12 条）。

覆盖 PRD W1 验收标准：「登录 / 刷新 / 注销 12 条鉴权用例通过」。
每条用例只校验一个行为，断言错误码而非仅状态码，便于定位契约回归。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import AsyncClient

from app.core.security import TokenType, create_token

pytestmark = [pytest.mark.unit, pytest.mark.auth]

LOGIN_URL = "/api/v1/auth/login"
REFRESH_URL = "/api/v1/auth/refresh"
LOGOUT_URL = "/api/v1/auth/logout"
ME_URL = "/api/v1/auth/me"

# 与 tests/conftest.py::TEST_PASSWORD 保持一致
PASSWORD = "Passw0rd!"


def _token_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


async def _login(client: AsyncClient, username: str = "admin_user", password: str = PASSWORD) -> dict:
    resp = await client.post(LOGIN_URL, json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# ---------------------------------------------------------------------------
# 1~3 登录
# ---------------------------------------------------------------------------
async def test_login_success_returns_token_pair(client: AsyncClient) -> None:
    body = await _login(client, "admin_user")
    assert body["access_token"] and body["refresh_token"]
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 120 * 60


async def test_login_with_wrong_password_returns_401(client: AsyncClient) -> None:
    resp = await client.post(LOGIN_URL, json={"username": "admin_user", "password": "wrong-password"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_INVALID_CREDENTIALS"


async def test_login_with_disabled_account_returns_403(client: AsyncClient) -> None:
    resp = await client.post(LOGIN_URL, json={"username": "disabled_user", "password": PASSWORD})
    assert resp.status_code == 403
    assert resp.json()["code"] == "AUTH_USER_DISABLED"


# ---------------------------------------------------------------------------
# 4~8 access token 校验
# ---------------------------------------------------------------------------
async def test_me_with_valid_access_token_returns_profile(client: AsyncClient) -> None:
    body = await _login(client, "approver_user")
    resp = await client.get(ME_URL, headers=_token_headers(body["access_token"]))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["username"] == "approver_user"
    assert data["role"] == "approver"


async def test_me_without_token_returns_401(client: AsyncClient) -> None:
    resp = await client.get(ME_URL)
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_TOKEN_MISSING"


async def test_me_with_malformed_token_returns_401(client: AsyncClient) -> None:
    resp = await client.get(ME_URL, headers=_token_headers("this-is-not-a-jwt"))
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_TOKEN_INVALID"


async def test_me_with_expired_access_token_returns_401(
    client: AsyncClient, seeded_users: dict[str, int]
) -> None:
    expired = create_token(
        user_id=seeded_users["admin_user"],
        username="admin_user",
        role="admin",
        token_type=TokenType.ACCESS,
        expires_delta=timedelta(seconds=-10),
    )
    resp = await client.get(ME_URL, headers=_token_headers(expired.token))
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_TOKEN_EXPIRED"


async def test_me_rejects_refresh_token_as_access(client: AsyncClient) -> None:
    body = await _login(client)
    resp = await client.get(ME_URL, headers=_token_headers(body["refresh_token"]))
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_TOKEN_TYPE_INVALID"


# ---------------------------------------------------------------------------
# 9 刷新轮换
# ---------------------------------------------------------------------------
async def test_refresh_rotates_token_and_rejects_replay(client: AsyncClient) -> None:
    old_refresh = (await _login(client))["refresh_token"]

    first = await client.post(REFRESH_URL, json={"refresh_token": old_refresh})
    assert first.status_code == 200
    new_pair = first.json()["data"]
    assert new_pair["refresh_token"] != old_refresh

    # 新 access token 可用
    me = await client.get(ME_URL, headers=_token_headers(new_pair["access_token"]))
    assert me.status_code == 200

    # 旧 refresh 已一次性消费 -> 重放被拒
    replay = await client.post(REFRESH_URL, json={"refresh_token": old_refresh})
    assert replay.status_code == 401
    assert replay.json()["code"] == "AUTH_REFRESH_REVOKED"


# ---------------------------------------------------------------------------
# 10 注销拉黑
# ---------------------------------------------------------------------------
async def test_logout_blacklists_access_token(client: AsyncClient) -> None:
    body = await _login(client)
    headers = _token_headers(body["access_token"])

    assert (await client.get(ME_URL, headers=headers)).status_code == 200

    logout = await client.post(LOGOUT_URL, headers=headers)
    assert logout.status_code == 200

    after = await client.get(ME_URL, headers=headers)
    assert after.status_code == 401
    assert after.json()["code"] == "AUTH_TOKEN_REVOKED"


# ---------------------------------------------------------------------------
# 11~12 角色校验 require_role
# ---------------------------------------------------------------------------
async def test_viewer_forbidden_on_admin_only_route(protected_client: AsyncClient) -> None:
    body = await _login(protected_client, "viewer_user")
    resp = await protected_client.get("/admin-only", headers=_token_headers(body["access_token"]))
    assert resp.status_code == 403
    payload = resp.json()
    assert payload["code"] == "FORBIDDEN"
    assert payload["data"]["detail"]["required_roles"] == ["admin"]
    assert payload["data"]["detail"]["actual_role"] == "viewer"


async def test_admin_allowed_on_admin_only_route(protected_client: AsyncClient) -> None:
    body = await _login(protected_client, "admin_user")
    resp = await protected_client.get("/admin-only", headers=_token_headers(body["access_token"]))
    assert resp.status_code == 200
    assert resp.json()["data"]["role"] == "admin"
```
