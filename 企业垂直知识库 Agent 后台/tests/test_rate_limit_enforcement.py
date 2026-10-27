"""批次3：真实 Redis 下的限流生效验证 + 会话持久性验证。

分两组：
A. 离线组（任何环境都跑）：证明 fake 模式下限流被显式跳过、状态可被 /health 看见，
   以及 FAKE_REDIS 运行期切换时客户端会被重建（防止"配置改了句柄没换"导致限流失效）。
B. 真实 Redis 组（本地无 Redis 时自动 skip）：直接打到真实 Redis 的令牌桶，验证
   「第 N+1 次返回 429」、键 TTL 正确、以及 Session 在进程重启后依然有效（不掉线）。

真实 Redis 组统一使用 db=15 作为测试专用库，并与生产共享的 db=0 隔离；
若 REDIS_URL 指向的是远程/带密码实例，同样按 URL 派生 db 15。
"""
import time
from urllib.parse import urlsplit, urlunsplit

import pytest

from app.api import rate_limit as rl
from app.config import settings
from app.core import security
from app.core.redis import get_redis, is_fake_redis, redis_backend, reset_client

TEST_DB = 15
# 固定测试用户 ID 段，避免与真实用户（1..N）共用令牌桶
TEST_UID = 987_654


def _redis_url_with_db(index: int) -> str:
    """把 REDIS_URL 的 db 段替换为指定索引（用于隔离测试数据）。"""
    parts = urlsplit(settings.redis_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{index}", parts.query, parts.fragment))


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_redis_client():
    """每个用例前后丢弃缓存句柄，避免后端类型在用例之间互相污染。"""
    reset_client()
    yield
    reset_client()


@pytest.fixture
def real_redis():
    """真实 Redis 客户端（db 15）；不可用时跳过整条用例。"""
    import redis as redis_lib

    try:
        client = redis_lib.Redis.from_url(
            _redis_url_with_db(TEST_DB),
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=2,
        )
        client.ping()
    except Exception as exc:  # noqa: BLE001 - 无 Redis 环境是合法的 skip 条件
        pytest.skip(f"真实 Redis 不可用，跳过限流集成测试: {exc}")
    client.flushdb()  # 测试库独立，直接清空
    yield client
    try:
        client.flushdb()
    finally:
        client.close()


@pytest.fixture
def bucket(real_redis, monkeypatch):
    """把限流模块指向真实 Redis + 指定阈值，返回 (设置阈值的函数, 客户端)。"""
    monkeypatch.setenv("FAKE_REDIS", "false")
    monkeypatch.setattr(rl, "get_redis", lambda: real_redis)

    def _set_limit(n: int) -> None:
        monkeypatch.setattr(rl, "_rate_limit_per_minute", lambda: n)

    return _set_limit, real_redis


# ---------------------------------------------------------------------------
# A. 离线组：后端判定与状态可见性
# ---------------------------------------------------------------------------
def test_fake_mode_reports_disabled_and_skips(monkeypatch):
    """fake 模式下：限流状态必须是 disabled，且依赖直接放行（不报错）。"""
    from app.api.deps import CurrentUser

    monkeypatch.setenv("FAKE_REDIS", "true")
    assert is_fake_redis() is True
    assert redis_backend() == "fake"
    assert rl.rate_limit_status() == "disabled"

    user = CurrentUser(id=TEST_UID, username="u", display_name="u", role="user", status="active")
    # 连续 100 次都不应抛 429（fake 模式显式跳过）
    for _ in range(100):
        rl.rate_limit_ai(user)


def test_client_rebuilds_when_backend_toggles(monkeypatch):
    """FAKE_REDIS 运行期切换后，get_redis() 必须返回新后端，而不是沿用旧句柄。"""
    monkeypatch.setenv("FAKE_REDIS", "true")
    fake_client = get_redis()
    assert type(fake_client).__name__ == "FakeRedis"

    monkeypatch.setenv("FAKE_REDIS", "false")
    real_client = get_redis()
    assert type(real_client).__name__ == "Redis"
    assert real_client is not fake_client


def test_health_exposes_backend_and_rate_limit(client, monkeypatch):
    """健康检查必须区分 fake/real 后端，并直接暴露限流是否真的生效。"""
    monkeypatch.setenv("FAKE_REDIS", "true")
    body = client.get("/api/v1/health").json()
    data = body["data"]
    assert body["code"] == 0
    # 旧字段保持兼容（运维脚本/文档依赖）
    assert set(("status", "db", "redis")).issubset(data.keys())
    assert data["redis_backend"] == "fake"
    assert data["rate_limit"] == "disabled"


# ---------------------------------------------------------------------------
# B. 真实 Redis 组：令牌桶行为
# ---------------------------------------------------------------------------
def test_bucket_allows_capacity_then_rejects(bucket):
    """容量 5：前 5 次放行，第 6 次拒绝 —— 这是 429 的最小充分证明。"""
    set_limit, client = bucket
    set_limit(5)

    results = [rl._allow_request(TEST_UID) for _ in range(5)]
    assert results == [True] * 5, "容量内的请求必须全部放行"
    assert rl._allow_request(TEST_UID) is False, "超出容量必须拒绝"

    # 键存在且带 TTL（避免永久占用 Redis 内存）
    key = f"rate_limit:ai:{TEST_UID}"
    assert client.exists(key) == 1
    assert client.ttl(key) > 0


def test_bucket_refills_gradually(bucket):
    """匀速补充：60/min = 1 token/s，耗尽后等待约 1 秒可再放行一次。"""
    set_limit, _ = bucket
    set_limit(60)

    for _ in range(60):
        assert rl._allow_request(TEST_UID) is True
    assert rl._allow_request(TEST_UID) is False

    time.sleep(1.2)
    assert rl._allow_request(TEST_UID) is True, "等待一个补充周期后应恢复可用"


def test_limits_are_isolated_per_user(bucket):
    """按用户隔离：A 耗尽不影响 B（避免全员被一个人拖累）。"""
    set_limit, _ = bucket
    set_limit(1)

    assert rl._allow_request(TEST_UID) is True
    assert rl._allow_request(TEST_UID) is False
    assert rl._allow_request(TEST_UID + 1) is True


def test_dependency_raises_429_with_biz_code(bucket):
    """依赖层超限抛 BizError(429)，与统一响应体错误码对齐。"""
    from app.api.deps import CurrentUser
    from app.core.exceptions import BizError

    set_limit, _ = bucket
    set_limit(2)
    user = CurrentUser(id=TEST_UID, username="u", display_name="u", role="user", status="active")

    rl.rate_limit_ai(user)
    rl.rate_limit_ai(user)
    with pytest.raises(BizError) as exc_info:
        rl.rate_limit_ai(user)
    assert exc_info.value.status_code == 429
    assert exc_info.value.code == 429
    assert "频繁" in exc_info.value.message


# ---------------------------------------------------------------------------
# B2. 真实 Redis 组：HTTP 端到端（/kbs/{id}/query 高频请求 → 429）
# ---------------------------------------------------------------------------
@pytest.fixture
def kb_query_client(client, monkeypatch):
    """构造可直接打 /kbs/{id}/query 的客户端：绕过 DB 鉴权与真实 RAG 调用。"""
    from app.api.deps import CurrentUser, get_accessible_kb, get_current_user
    from app.main import app
    from app.models import KnowledgeBase
    from app.services import rag_service

    user = CurrentUser(id=TEST_UID, username="u", display_name="u", role="user", status="active")
    fake_kb = KnowledgeBase(id=1, name="限流测试库", owner_id=TEST_UID)

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_accessible_kb] = lambda: fake_kb
    # 避免真实 embedding / LLM 调用
    monkeypatch.setattr(
        rag_service,
        "query_kb",
        lambda db, kb, question, top_k=None: {"answer": "ok", "sources": []},
    )
    return client


def test_kb_query_endpoint_returns_429_under_high_frequency(kb_query_client, bucket):
    """核心验收：真实 Redis 下高频请求 /kbs/{id}/query，第 N+1 次返回 429。"""
    set_limit, _ = bucket
    limit = 3
    set_limit(limit)

    codes = []
    for _ in range(limit):
        resp = kb_query_client.post("/api/v1/kbs/1/query", json={"question": "打印机卡纸怎么办"})
        codes.append(resp.status_code)
        assert resp.status_code == 200, resp.text

    resp = kb_query_client.post("/api/v1/kbs/1/query", json={"question": "再来一次"})
    assert resp.status_code == 429, f"超限应返回 429，实际 {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["code"] == 429
    assert "频繁" in body["message"]

    # 连续超限仍然是 429（不会因为请求失败而重置计数器）
    assert kb_query_client.post(
        "/api/v1/kbs/1/query", json={"question": "继续"}
    ).status_code == 429


# ---------------------------------------------------------------------------
# B3. 真实 Redis 组：登录态跨进程/重启存续（"重启掉线"的正面验证）
# ---------------------------------------------------------------------------
def test_session_survives_process_restart(real_redis, monkeypatch):
    """真实 Redis 下会话写在共享存储里：进程重启（丢掉本地句柄）后 token 仍然有效。"""
    monkeypatch.setenv("FAKE_REDIS", "false")
    monkeypatch.setattr(security, "get_redis", lambda: real_redis)

    token = security.create_session(
        {"user_id": TEST_UID, "username": "u", "display_name": "u", "role": "user", "status": "active"}
    )
    assert security.get_session(token)["user_id"] == TEST_UID

    # 模拟后端重启：客户端句柄全部丢弃并重建
    reset_client()
    monkeypatch.setattr(security, "get_redis", lambda: real_redis)
    assert security.get_session(token) is not None, "真实 Redis 下重启不应导致用户掉线"


def test_revoked_session_raises_401_before_db(real_redis, monkeypatch):
    """Redis 数据丢失/会话被删后：鉴权立即 401（且不查库），前端据此跳登录页。

    对齐前端两条 401 处理路径：axios 拦截器（清凭据 + 跳 /login）与
    Chat.vue 的 SSE 分支（refresh 失败后同样清凭据 + 跳 /login）。
    """
    from fastapi.security import HTTPAuthorizationCredentials
    from starlette.requests import Request

    from app.api.deps import get_current_user
    from app.core.exceptions import UnauthorizedError

    monkeypatch.setenv("FAKE_REDIS", "false")
    monkeypatch.setattr(security, "get_redis", lambda: real_redis)

    token = security.create_session({"user_id": TEST_UID, "username": "u", "role": "user", "status": "active"})
    security.delete_session(token)  # 等价于 Redis 重启丢数据 / 会话被踢

    request = Request({"type": "http", "method": "GET", "path": "/api/v1/auth/me", "headers": []})
    with pytest.raises(UnauthorizedError) as exc_info:
        get_current_user(
            request,
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
            None,  # 无效 token 必须在查库之前就拒绝
        )
    assert exc_info.value.status_code == 401
    assert exc_info.value.code == 401
