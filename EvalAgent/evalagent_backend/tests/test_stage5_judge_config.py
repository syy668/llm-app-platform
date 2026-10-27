"""第五步：裁判配置管理（runtime_configs + 热更新 + 保存即探活）。

这里锁死四条契约，任何一条破了都会造成「看着能改，其实改坏了」：
1. GET 永不返回明文 Key（只回掩码）；
2. 非 admin 写必须 403（身份从上游 /auth/me 取，不落本地用户表）；
3. **探活失败必须 422 且不落库** —— 配置写错却不拦，代价是整场评测静默空分；
4. 提交掩码 / 空字符串 = 未修改（前端占位符回传的就是掩码）；
5. 温度不可写：请求体里带 temperature 也不生效，恒为 0.0。
"""

from __future__ import annotations

import pytest

from app.core.auth import CurrentUser, get_current_user, require_admin
from app.services import runtime_config as rc
from app.services.judge import JudgeUnavailable


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def as_admin(client):
    from app.main import app

    app.dependency_overrides[require_admin] = lambda: CurrentUser(
        id=1, username="admin_user", role="admin"
    )
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=1, username="admin_user", role="admin"
    )
    yield client
    app.dependency_overrides.clear()


@pytest.fixture()
def stored(monkeypatch):
    """把写库重定向到内存字典，避免污染真实 runtime_configs 表。"""
    saved: dict[str, str] = {}

    def fake_persist(db, updates, updated_by):
        saved.update(updates)
        rc.invalidate()
        return len(updates)

    monkeypatch.setattr(rc, "persist", fake_persist)
    monkeypatch.setattr("app.api.v1.system.rc.persist", fake_persist)
    # 读路径也用同一份内存态，保证「写完再读」看到新值
    monkeypatch.setattr(
        rc,
        "build_judge_config",
        lambda override=None: _fake_cfg(saved, override),
    )
    monkeypatch.setattr("app.api.v1.system.rc.build_judge_config", rc.build_judge_config)
    return saved


def _fake_cfg(saved: dict[str, str], override: dict | None = None):
    from app.core.config import settings

    base = {
        "JUDGE_MODEL": saved.get("JUDGE_MODEL", settings.JUDGE_MODEL),
        "JUDGE_API_BASE": saved.get("JUDGE_API_BASE", settings.JUDGE_API_BASE),
        "JUDGE_API_KEY": saved.get("JUDGE_API_KEY", settings.JUDGE_API_KEY),
        "JUDGE_EMBEDDING_MODEL": saved.get(
            "JUDGE_EMBEDDING_MODEL", settings.JUDGE_EMBEDDING_MODEL
        ),
    }
    if override:
        base.update({k: v for k, v in override.items() if k in rc.EDITABLE_KEYS})
    return rc.JudgeConfig(
        model=(base["JUDGE_MODEL"] or "").strip(),
        api_base=(base["JUDGE_API_BASE"] or "").strip(),
        api_key=(base["JUDGE_API_KEY"] or "").strip(),
        embedding_model=(base["JUDGE_EMBEDDING_MODEL"] or "").strip(),
    )


# ---------------------------------------------------------------- 读
def test_get_masks_api_key(as_admin):
    res = as_admin.get("/api/v1/system/judge-config")
    assert res.status_code == 200
    body = res.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["api_key"] in ("", rc.MASK)
    assert rc.MASK in (data["api_key"], "") or data["api_key"] == ""
    # 明文绝不能出现在响应体里
    from app.core.config import settings

    if settings.JUDGE_API_KEY:
        assert settings.JUDGE_API_KEY not in res.text


def test_temperature_locked_readonly(as_admin):
    data = as_admin.get("/api/v1/system/judge-config").json()["data"]
    assert data["temperature"] == 0.0
    assert data["temperature_locked"] is True


# ---------------------------------------------------------------- 权限
def test_put_requires_admin(client):
    """未带 token → 401（不是 500，也不能静默放行）。"""
    res = client.put("/api/v1/system/judge-config", json={"model": "x"})
    assert res.status_code == 401
    assert res.json()["code"] == 401


def test_put_rejects_non_admin(as_admin):
    from app.main import app

    app.dependency_overrides[require_admin] = lambda: (_ for _ in ()).throw(
        __import__("fastapi").HTTPException(status_code=403, detail="仅管理员可修改评测中台配置")
    )
    res = as_admin.put("/api/v1/system/judge-config", json={"model": "x"})
    assert res.status_code == 403


# ---------------------------------------------------------------- 写 + 探活
def test_preflight_failure_blocks_persist(as_admin, stored, monkeypatch):
    """探活失败 → 422 且一个键都没写进去（核心防呆）。"""

    def boom(cfg=None):
        raise JudgeUnavailable("裁判模型探活失败：NotFoundError: model not found")

    monkeypatch.setattr("app.api.v1.system.judge.preflight", boom)
    res = as_admin.put("/api/v1/system/judge-config", json={"model": "no-such-model"})
    assert res.status_code == 422
    assert "探活失败" in res.json()["message"]
    assert stored == {}, f"探活失败却写了库：{stored}"


def test_save_success_persists_and_reloads(as_admin, stored, monkeypatch):
    calls = {"preflight": 0, "reload": 0}

    def fake_preflight(cfg=None):
        calls["preflight"] += 1
        return "就绪"

    monkeypatch.setattr("app.api.v1.system.judge.preflight", fake_preflight)
    monkeypatch.setattr("app.api.v1.system.judge.reload", lambda: calls.__setitem__("reload", calls["reload"] + 1))

    res = as_admin.put(
        "/api/v1/system/judge-config",
        json={"model": "qwen3.7-flash", "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["data"]["changed"] is True
    assert stored["JUDGE_MODEL"] == "qwen3.7-flash"
    assert calls == {"preflight": 1, "reload": 1}


def test_mask_or_empty_key_keeps_existing(as_admin, stored, monkeypatch):
    monkeypatch.setattr("app.api.v1.system.judge.preflight", lambda cfg=None: "就绪")
    monkeypatch.setattr("app.api.v1.system.judge.reload", lambda: None)

    res = as_admin.put("/api/v1/system/judge-config", json={"model": "m1", "api_key": rc.MASK})
    assert res.status_code == 200
    assert "JUDGE_API_KEY" not in stored, "掩码被当成新密钥写入"

    stored.clear()
    res = as_admin.put("/api/v1/system/judge-config", json={"model": "m2", "api_key": ""})
    assert res.status_code == 200
    assert "JUDGE_API_KEY" not in stored, "空字符串被当成新密钥写入"


def test_temperature_in_payload_is_ignored(as_admin, stored, monkeypatch):
    """请求体带 temperature 也不生效：可复现性优先（pydantic extra=ignore + 无该字段）。"""
    monkeypatch.setattr("app.api.v1.system.judge.preflight", lambda cfg=None: "就绪")
    monkeypatch.setattr("app.api.v1.system.judge.reload", lambda: None)

    res = as_admin.put(
        "/api/v1/system/judge-config",
        json={"model": "m3", "temperature": 0.9},
    )
    assert res.status_code == 200
    assert "JUDGE_TEMPERATURE" not in stored
    assert rc.build_judge_config().temperature == 0.0


def test_invalid_api_base_rejected_as_admin_before_preflight(as_admin, stored, monkeypatch):
    monkeypatch.setattr(
        "app.api.v1.system.judge.preflight",
        lambda cfg=None: pytest.fail("地址非法时不该走到探活"),
    )
    res = as_admin.put("/api/v1/system/judge-config", json={"api_base": "dashscope.aliyuncs.com"})
    assert res.status_code == 422
    assert "http://" in res.json()["message"]
    assert stored == {}


# ---------------------------------------------------------------- 只读项
def test_readonly_values_reported(as_admin):
    data = as_admin.get("/api/v1/system/judge-config").json()["data"]
    ro = data["readonly"]
    assert ro["JUDGE_TEMPERATURE"] == 0.0
    # 重试收紧是踩过坑的硬约束，不能被界面放开
    assert ro["JUDGE_MAX_RETRIES"] <= 3
    # 单指标超时上限：faithfulness 的 NLI 一次要判几十条陈述（长中文答案可拆 59 条），
    # 单次 LLM 调用实测 40~110s。旧上限 90s 会把长答案的 faithfulness 硬超时吞成 NaN。
    # 现抬到 240s，仍低于 JUDGE_ITEM_TIMEOUT(420s)，不会让整条样本失控。
    assert ro["JUDGE_METRIC_TIMEOUT"] <= 240


# ---------------------------------------------------------------- 热更新
def test_signature_change_rebuilds_clients(monkeypatch):
    """配置签名变化必须触发客户端重建（否则「保存了但还是在用旧 key」）。"""
    from app.services.judge import judge as judge_singleton

    builds = {"n": 0}
    original = judge_singleton._make_clients

    def counting_make(cfg, counter):
        builds["n"] += 1
        return original(cfg, counter)

    monkeypatch.setattr(judge_singleton, "_make_clients", counting_make)
    judge_singleton.reload()

    cfg_a = rc.JudgeConfig(model="a", api_base="", api_key="k", embedding_model="e")
    cfg_b = rc.JudgeConfig(model="b", api_base="", api_key="k", embedding_model="e")
    judge_singleton._make_clients(cfg_a, judge_singleton._counter)
    judge_singleton._make_clients(cfg_b, judge_singleton._counter)
    assert builds["n"] == 2
    judge_singleton.reload()


# ---------------------------------------------------------------- 熔断
def test_fatal_error_classification():
    """额度耗尽 / key 失效 / 模型不存在 → 视为致命，必须熔断而不是逐条重试。"""
    from app.services.runner import _is_fatal_judge_error

    fatal_samples = [
        "打分失败：403 AllocationQuota.FreeTierOnly",
        "打分失败：Error code: 401 - invalid_api_key",
        "打分失败：NotFoundError: The model `x` does not exist",
        "打分异常：PermissionDeniedError",
    ]
    for text in fatal_samples:
        assert _is_fatal_judge_error(text), f"应判为致命：{text}"

    # 单条样本难 / 超时不属于配置错误，必须继续跑其余样本
    for text in ("打分失败：TimeoutError", "打分超时（>300s）", "取数失败：ConnectError", ""):
        assert not _is_fatal_judge_error(text), f"不应判为致命：{text}"


# ---------------------------------------------------------------- 主循环
def test_execute_run_processes_every_item(monkeypatch):
    """主循环必须处理**全部**样本。

    历史上这里踩过两次坑：`for` 循环体缩进错位（只处理第一条就跳出），
    以及致命错误不熔断（每条各耗 300s）。用 3 条样本把「processed == total」钉死。
    """
    import asyncio

    from app.core.database import SessionLocal
    from app.models import EvalDataset, EvalItem, EvalRun
    from app.services import runner
    from app.services.judge import JudgeOutcome
    from app.services.rag_client import RagAnswer

    db = SessionLocal()
    ds = EvalDataset(name="主循环测试集", code=f"LOOP-{int(__import__('time').time())}", target_type="kb", kb_id=6)
    db.add(ds)
    db.flush()
    for seq, q in enumerate(["问题一？", "问题二？", "问题三？"], start=1):
        db.add(EvalItem(dataset_id=ds.id, seq=seq, question=q, reference_answer="答案"))
    ds.item_count = 3
    run = EvalRun(run_no=f"EV-LOOP-{ds.id}", dataset_id=ds.id, status="pending", total_items=3)
    db.add(run)
    db.commit()
    run_id, ds_id = run.id, ds.id
    db.close()

    async def fake_query(kb_id, question, top_k=None):
        return RagAnswer(question=question, answer="原始答案", contexts=["上下文"], latency_ms=1)

    def fake_score(question, answer, contexts, reference, reference_contexts=None):
        return JudgeOutcome(
            scores={"faithfulness": 0.5, "answer_relevancy": 0.5,
                    "context_precision": 0.5, "context_recall": 0.5},
            raw={}, tokens=10,
        )

    monkeypatch.setattr(runner.rag_client, "query", fake_query)
    monkeypatch.setattr(runner.judge, "preflight", lambda: "就绪")
    monkeypatch.setattr(runner.judge, "score", fake_score)

    asyncio.run(runner.execute_run(run_id))

    db = SessionLocal()
    try:
        done = db.get(EvalRun, run_id)
        assert done.processed_items == 3, f"只处理了 {done.processed_items}/3 条"
        assert done.success_items == 3
        assert done.status == "done"
        assert done.metrics and done.metrics.get("faithfulness") == 0.5
    finally:
        db.query(EvalRun).filter(EvalRun.id == run_id).delete()
        db.query(EvalItem).filter(EvalItem.dataset_id == ds_id).delete()
        db.query(EvalDataset).filter(EvalDataset.id == ds_id).delete()
        db.commit()
        db.close()
