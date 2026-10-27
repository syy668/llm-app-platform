"""pytest 共享夹具与离线环境准备。

本测试骨架仅覆盖「新增模块」：trace、eval、stream(SSE) 三个接口，
不改动任何旧模块业务代码，支持本地离线运行（不依赖 MySQL / Redis / DeepSeek）。

离线运行的三个关键点：
1. 在 `import app` 之前注入环境变量，避免 `app.config` 因缺少
   DATABASE_URL / REDIS_URL / SECRET_KEY 而在导入期抛错；
   同时 `FAKE_REDIS=true` 让会话走内存版 Redis（fakeredis），全程不连外部服务。
2. 通过 `app.dependency_overrides` 注入假用户 / 假会话，绕过真实鉴权与数据库访问。
3. 被测服务函数（trace_service / eval_service / stream_service）在服务层 mock，
   测试聚焦于「路由契约 + {code, message, data} 统一响应体」。

运行方式（本地离线）：
    pip install pytest          # httpx 已随 fastapi 安装，无需额外安装
    pytest -q

参考：httpx 0.28.x 下 TestClient 正常工作；Python 3.12 已验证。
"""
import os
import sys
from pathlib import Path

# ---- 1) 离线环境变量：必须发生在 import app 之前 ----
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "pytest-secret-key")
os.environ.setdefault("FAKE_REDIS", "true")
os.environ.setdefault("LLM_PROVIDER", "mock")

# ---- 3) sqlite 测试兼容方案（固化，供全部测试自动复用）----
# 背景：模型主键普遍用 sqlalchemy BigInteger。MySQL 下 BIGINT AUTO_INCREMENT 正常自增，
# 但 sqlite 把 "BIGINT PRIMARY KEY" 视为普通整数列（不等于 "INTEGER PRIMARY KEY"），
# 失去 rowid 别名机制 => 主键不再自增，多线程（TestClient 在子线程发请求）插入报
# NOT NULL constraint failed。
# 方案（三件套，全部在 conftest 固化，业务代码零改动）：
#   a) 方言级 monkeypatch：sqlite 下把 BigInteger 编译为 INTEGER（恢复 rowid 自增）；
#   b) 文件型 sqlite + StaticPool + check_same_thread=False：跨线程共享同一连接，
#      且数据落盘便于排查（内存库在多连接场景下会出现"各连各的库"）；
#   c) 所有基于 sqlite 的测试统一走下方 sqlite_engine / eval_db 夹具。
# 注意：仅影响测试进程的 sqlite 方言渲染，生产 MySQL 不受任何影响。
from sqlalchemy import BigInteger  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402


@compiles(BigInteger, "sqlite")
def _render_bigint_as_integer(type_, compiler, **kw):
    """sqlite 方言下 BIGINT -> INTEGER，恢复 INTEGER PRIMARY KEY rowid 自增。"""
    return "INTEGER"


# ---- 4) 安全守卫：禁止对「真实库」执行 drop_all（2026-10-27 事故后加）----
# 事故经过：测试套件里 6 个模块在 fixture 收尾调 `Base.metadata.drop_all(bind=engine)`，
# 而这里的 engine 是 `app.core.database.engine` = 真实 MySQL 的 kb_agent。
# 2026-10-27 10:37 一次全量 pytest 把真实库 15 张 ORM 表逐个 DROP 后重建，
# 用户/知识库/文档/切片/会话全部清空，评测任务随即全部 401 失败。
# 守卫规则（三层放行，其余一律拒绝）：
#   1. sqlite（内存/文件）—— 单测默认走这里，随便 drop；
#   2. 库名以 `_test` 结尾 —— 例如 kb_agent_test，专用测试库；
#   3. 显式设置 KB_AGENT_ALLOW_DESTRUCTIVE_TESTS=1 —— 明知故犯时的逃生门。
# 拒绝时抛 RuntimeError（而不是静默跳过）：宁可让用例红在收尾，也不能再静默毁库。
import os  # noqa: E402

from sqlalchemy import MetaData  # noqa: E402


def _database_name(bind) -> str | None:  # noqa: ANN001
    url = None
    try:
        target = getattr(bind, "engine", bind)  # Connection -> Engine
        url = getattr(target, "url", None)
    except Exception:  # noqa: BLE001 - 拿不到 URL 时按"未知"处理
        return None
    if url is None:
        return None
    if url.drivername.startswith("sqlite"):
        return "sqlite"
    return url.database


def _install_drop_all_guard() -> None:
    if getattr(MetaData, "_kb_agent_drop_guard", False):
        return
    original = MetaData.drop_all

    def guarded_drop_all(self, bind=None, **kw):  # noqa: ANN001
        target = bind if bind is not None else getattr(self, "bind", None)
        db = _database_name(target)
        if db == "sqlite" or db is None:
            return original(self, bind=bind, **kw)
        if str(db).endswith("_test") or os.getenv("KB_AGENT_ALLOW_DESTRUCTIVE_TESTS") == "1":
            return original(self, bind=bind, **kw)
        raise RuntimeError(
            f"已阻止对真实数据库 `{db}` 执行 DROP TABLE（测试收尾清理）。\n"
            f"  危害：这会清空演示/生产数据（2026-10-27 已发生一次，评测全线 401）。\n"
            f"  正确做法：把 DATABASE_URL 指向 `{db}_test`，或在明确要重建演示库时\n"
            f"  设置环境变量 KB_AGENT_ALLOW_DESTRUCTIVE_TESTS=1 后再跑。"
        )

    MetaData.drop_all = guarded_drop_all
    MetaData._kb_agent_drop_guard = True


_install_drop_all_guard()


# ---- 5) 项目根目录加入 sys.path，保证任意目录下 `import app` 可用 ----
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.api.deps import (  # noqa: E402
    CurrentUser,
    get_current_user,
    get_own_conversation,
    require_admin,
)
from app.models import Conversation  # noqa: E402


def make_user(role: str = "admin") -> CurrentUser:
    """构造假登录用户；admin 用 id=1，普通用户用 id=2。"""
    return CurrentUser(
        id=1 if role == "admin" else 2,
        username="admin" if role == "admin" else "user",
        display_name="测试用户",
        role=role,
        status="active",
    )


def make_conversation(conv_id: int = 1, user_id: int = 1) -> Conversation:
    """构造假会话对象（仅用于依赖注入，不落库）。"""
    return Conversation(id=conv_id, user_id=user_id, kb_id=None)


@pytest.fixture(autouse=True)
def _clear_overrides():
    """每个用例结束后清空依赖覆盖，避免用例间相互污染。"""
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _isolate_runtime_config(tmp_path, monkeypatch):
    """把运行时配置存储指向临时文件并清缓存（测试不得读写 data/system_config.json）。

    【v1.9 封版修复】此前用例只隔离了 FastAPI 依赖，没有隔离 runtime_config：
    它的 `_overrides` 是进程级缓存，默认会去读项目根目录的 `data/system_config.json`。
    后果是管理员只要在配置页点过一次保存，`llm_provider` / `tool_max_attempts` 等
    覆盖值就会漏进测试——表现为「代码没动，测试却红了」（实测 2 个用例变红）。
    这里统一把存储路径指到 tmp_path 并 reset_cache，用例内部如需验证读写，
    各自再 monkeypatch SYSTEM_CONFIG_FILE 即可（与 test_system_config 的做法一致）。
    """
    from app.core import runtime_config

    monkeypatch.setenv("SYSTEM_CONFIG_FILE", str(tmp_path / "system_config.json"))
    runtime_config.reset_cache()
    yield
    runtime_config.reset_cache()


@pytest.fixture(autouse=True)
def _disable_eval_preflight(monkeypatch):
    """【v1.17】单测默认关闭「评测判分模型探活」。

    探活会向真实上游发一次请求，单测不得依赖网络与外部额度。
    需要验证探活路径的用例自行 `monkeypatch.setattr(settings, "eval_preflight_enable", True)`
    并把 `RagasEvaluator` 换成 fake（见 tests/test_eval_scoring_guardrails.py）。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "eval_preflight_enable", False)


@pytest.fixture
def client():
    """裸 TestClient：不带任何鉴权覆盖（用于 401 场景）。"""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_client(client):
    """管理员客户端：get_current_user -> admin（require_admin 会间接命中）。"""
    app.dependency_overrides[get_current_user] = lambda: make_user("admin")
    yield client


@pytest.fixture
def user_client(client):
    """普通用户客户端：get_current_user -> user（用于 403 越权场景）。"""
    app.dependency_overrides[get_current_user] = lambda: make_user("user")
    yield client


@pytest.fixture
def stream_client(client):
    """SSE 流式接口专用：同时覆盖会话归属与当前用户，绕过真实 DB 查询。"""
    app.dependency_overrides[get_current_user] = lambda: make_user("admin")
    app.dependency_overrides[get_own_conversation] = (
        lambda: make_conversation(conv_id=1, user_id=1)
    )
    yield client


# 便捷断言：统一响应体结构
def assert_ok(resp, status: int = 200):
    """断言 HTTP 状态与统一响应体 code=0，返回 data。"""
    assert resp.status_code == status
    body = resp.json()
    assert set(body.keys()) == {"code", "message", "data"}
    assert body["code"] == 0
    return body["data"]


def assert_error(resp, status: int, code: int):
    """断言 HTTP 状态与统一错误体 {code, message, data}。"""
    assert resp.status_code == status
    body = resp.json()
    assert set(body.keys()) == {"code", "message", "data"}
    assert body["code"] == code
    assert body["data"] is None
    return body


# ---------------------------------------------------------------------------
# sqlite 测试共享夹具（文件型 sqlite + StaticPool，见文件头部方案说明）
# ---------------------------------------------------------------------------
@pytest.fixture
def sqlite_engine(tmp_path):
    """独立文件型 sqlite 引擎：跨线程安全、主键自增，用例结束自动清理。

    - 文件落在 pytest 临时目录（tmp_path），用例结束由 pytest 统一回收；
    - StaticPool：全局仅一条连接，TestClient 子线程与用例主线程共享同一库；
    - check_same_thread=False：允许连接跨线程使用（配合 StaticPool 安全）。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    yield engine
    engine.dispose()


@pytest.fixture
def eval_db(sqlite_engine):
    """eval 模块专用会话：建 eval 四张表 + 审计表，返回 Session（用例结束关闭）。

    依赖 conftest 级 sqlite 兼容方案（BigInteger->INTEGER + StaticPool），
    主键无需显式指定 id 即可自增。仅新建缺失表，不影响 MySQL 生产库。

    【v1.13 新增】操作日志表也一并建好：样本修正会写 operation_logs 审计，
    缺表时 `_audit` 走静默降级（生产可接受），但那样就没法断言审计真的落库。
    """
    from sqlalchemy.orm import sessionmaker

    from app.models.eval_dataset import EvalDataset, EvalDatasetItem
    from app.models.eval_run import EvalRun, EvalRunScore
    from app.models.operation_log import OperationLog

    EvalRun.__table__.create(sqlite_engine)
    EvalRunScore.__table__.create(sqlite_engine)
    EvalDataset.__table__.create(sqlite_engine)
    EvalDatasetItem.__table__.create(sqlite_engine)
    OperationLog.__table__.create(sqlite_engine)
    db = sessionmaker(bind=sqlite_engine)()
    yield db
    db.close()
