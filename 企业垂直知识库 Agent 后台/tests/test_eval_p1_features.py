"""P1 功能回归测试（离线，沿用 conftest 夹具风格）。

覆盖：
  B1-5  POST /api/v1/eval/runs/compare        契约 / 参数校验(422) / 登录校验 /
                                              同数据集校验(400) / 对齐与差值计算（sqlite 直连）
  B2-4  stratum：导入落库 / GET stratum_metrics 分层聚合（sqlite 直连）
  多服务商 system_config 热更新：新配置键读写 / 枚举校验 / 服务商预设 / 密钥脱敏
"""
import pytest

import app.services.eval_service as eval_service
from app.core import runtime_config
from app.core.exceptions import BizError
from app.schemas.eval import ItemInput, ItemsImport
from conftest import assert_error, assert_ok

API = "/api/v1/eval"


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """system_config 用例使用独立配置文件，避免污染与相互影响。"""
    monkeypatch.setenv("SYSTEM_CONFIG_FILE", str(tmp_path / "system_config.json"))
    runtime_config.reset_cache()
    yield
    runtime_config.reset_cache()


@pytest.fixture
def eval_db():
    """独立 sqlite 内存库：建 eval 相关表，用例结束关闭。

    sqlite 方言下把 BigInteger 编译为 INTEGER（恢复 INTEGER PRIMARY KEY rowid
    别名机制，自增可用；仅影响本测试进程，生产 MySQL 不受影响）。
    """
    from sqlalchemy import BigInteger, create_engine
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    @compiles(BigInteger, "sqlite")
    def _render_bigint_as_integer(type_, compiler, **kw):
        return "INTEGER"

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    eval_service.EvalRun.__table__.create(engine)
    eval_service.EvalRunScore.__table__.create(engine)
    eval_service.EvalDataset.__table__.create(engine)
    eval_service.EvalDatasetItem.__table__.create(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


def _add_run(db, run_id: int, dataset_id: int, metrics=None) -> None:
    db.add(eval_service.EvalRun(
        id=run_id, dataset_id=dataset_id, kb_id=1, model="m",
        status="done", total_items=0, success_items=0, created_by=1,
        metrics=metrics,
    ))


def _add_score(db, score_id: int, run_id: int, question: str, item_id: int | None = None, **kw) -> None:
    defaults = dict(faithfulness=None, answer_relevancy=None,
                    context_precision=None, context_recall=None, latency_ms=1)
    defaults.update(kw)
    db.add(eval_service.EvalRunScore(id=score_id, run_id=run_id, question=question,
                                     item_id=item_id if item_id is not None else score_id,
                                     answer="ans", **defaults))


# ---------------------------------------------------------------------------
# B1-5 A/B 对比
# ---------------------------------------------------------------------------

class TestCompareRuns:
    def test_align_and_diff(self, eval_db):
        """同 dataset 两个 run：question 对齐、Δ 计算、is_diff 标记、汇总兜底现算。"""
        _add_run(eval_db, 1, 100)
        _add_run(eval_db, 2, 100)
        # 对齐样本：A faith=0.2 / B faith=0.9 → Δ=0.7（差异样本）
        _add_score(eval_db, 101, 1, "q1 ", faithfulness=0.2, context_recall=0.5)
        _add_score(eval_db, 201, 2, "q1", faithfulness=0.9, context_recall=0.55)
        # 仅 A 侧出现（B 缺失）
        _add_score(eval_db, 102, 1, "q2-only-a", faithfulness=0.8)
        # 仅 B 侧出现
        _add_score(eval_db, 202, 2, "q3-only-b", faithfulness=0.1)
        eval_db.commit()

        data = eval_service.compare_runs(eval_db, 1, 2)
        assert data["diff_threshold"] == 0.1
        assert len(data["rows"]) == 3

        row_q1 = next(r for r in data["rows"] if r["question"].strip() == "q1")
        assert row_q1["diffs"]["faithfulness"] == 0.7
        assert row_q1["is_diff"] is True
        assert row_q1["a"]["id"] == 101 and row_q1["b"]["id"] == 201

        # 仅单侧出现的样本：另一侧为 null，Δ 为 null
        row_a = next(r for r in data["rows"] if r["question"] == "q2-only-a")
        assert row_a["b"] is None and row_a["diffs"]["faithfulness"] is None
        row_b = next(r for r in data["rows"] if r["question"] == "q3-only-b")
        assert row_b["a"] is None

        # run.metrics 未落库 → 汇总由得分现算（仅非空值均值，口径与 _mean_or_none 一致）
        # A 侧 faithfulness: 0.2 + 0.8 → 0.5；B 侧: 0.9 + 0.1 → 0.5 → diff = 0.0
        summary = {s["metric"]: s for s in data["summary"]}
        assert summary["faithfulness"]["avg_a"] == pytest.approx(0.5)
        assert summary["faithfulness"]["avg_b"] == pytest.approx(0.5)
        assert summary["faithfulness"]["diff"] == pytest.approx(0.0)
        # context_recall 仅 A 侧 q1（0.5）与 B 侧 q1（0.55）有值 → avg_b=0.55, diff=0.05
        assert summary["context_recall"]["avg_a"] == pytest.approx(0.5)
        assert summary["context_recall"]["avg_b"] == pytest.approx(0.55)
        assert summary["context_recall"]["diff"] == pytest.approx(0.05)

    def test_summary_prefers_run_metrics(self, eval_db):
        """run.metrics 已落库时优先使用（与评测落库口径一致）。"""
        _add_run(eval_db, 1, 100, metrics={"faithfulness": 0.5})
        _add_run(eval_db, 2, 100, metrics={"faithfulness": 0.7})
        data = eval_service.compare_runs(eval_db, 1, 2)
        summary = {s["metric"]: s for s in data["summary"]}
        assert summary["faithfulness"]["avg_a"] == 0.5
        assert summary["faithfulness"]["diff"] == pytest.approx(0.2)

    def test_dataset_mismatch_rejected(self, eval_db):
        """不同数据集的两个 run → 400 业务错误。"""
        _add_run(eval_db, 1, 100)
        _add_run(eval_db, 2, 200)
        with pytest.raises(BizError) as exc:
            eval_service.compare_runs(eval_db, 1, 2)
        assert "同一数据集" in str(exc.value.message)

    def test_run_not_found(self, eval_db):
        _add_run(eval_db, 1, 100)
        with pytest.raises(BizError):
            eval_service.compare_runs(eval_db, 1, 999)

    def test_endpoint_contract(self, admin_client, monkeypatch):
        """接口契约：入参透传 service；登录可读；统一响应体。"""
        captured = {}

        def fake_compare(db, run_id_a, run_id_b):
            captured.update(a=run_id_a, b=run_id_b)
            return {"run_a": {}, "run_b": {}, "summary": [], "rows": [], "diff_threshold": 0.1}

        monkeypatch.setattr(eval_service, "compare_runs", fake_compare)
        data = assert_ok(admin_client.post(f"{API}/runs/compare", json={"run_id_a": 3, "run_id_b": 5}))
        assert data["run_a"] == {}
        assert captured == {"a": 3, "b": 5}

    def test_endpoint_validation_422(self, admin_client):
        """run_id 缺失 / 非正整数 → 422。"""
        assert_error(admin_client.post(f"{API}/runs/compare", json={}), 422, 422)
        assert_error(
            admin_client.post(f"{API}/runs/compare", json={"run_id_a": 0, "run_id_b": 1}),
            422, 422,
        )

    def test_endpoint_requires_login(self, client):
        assert_error(client.post(f"{API}/runs/compare", json={"run_id_a": 1, "run_id_b": 2}), 401, 401)


# ---------------------------------------------------------------------------
# B2-4 stratum 分层
# ---------------------------------------------------------------------------

class TestStratum:
    def test_add_items_persists_stratum(self, eval_db):
        """导入样本时 stratum 随 ItemInput 持久化；不传为 NULL（向后兼容）。"""
        eval_db.add(eval_service.EvalDataset(
            id=1, kb_id=1, name="ds", item_count=0, created_by=1,
        ))
        eval_db.commit()
        payload = ItemsImport(items=[
            ItemInput(question="q1", reference_answer="a1", stratum="hard"),
            ItemInput(question="q2", reference_answer="a2"),
        ])
        data = eval_service.add_items(eval_db, 1, payload)
        assert data["imported"] == 2
        rows = eval_db.query(eval_service.EvalDatasetItem).order_by(
            eval_service.EvalDatasetItem.id
        ).all()
        # 显式给 id 的两行（sqlite）；按插入顺序取
        rows = sorted(rows, key=lambda r: r.question)
        assert rows[0].stratum == "hard"
        assert rows[1].stratum is None

    def test_item_input_stratum_max_length_422(self, admin_client, monkeypatch):
        """stratum 超过 64 字符 → 422。"""
        monkeypatch.setattr(eval_service, "add_items",
                            lambda db, did, data: {"imported": 1, "item_count": 1})
        resp = admin_client.post(f"{API}/datasets/1/items", json={
            "items": [{"question": "q", "reference_answer": "a", "stratum": "x" * 65}],
        })
        assert_error(resp, 422, 422)

    def test_stratum_metrics_grouping(self, eval_db):
        """分层聚合：按 stratum 分组计数 + AVG 仅统计非 NULL；未分层归入 stratum=null。"""
        _add_run(eval_db, 1, 100)
        eval_db.add(eval_service.EvalDataset(id=1, kb_id=1, name="ds", item_count=0, created_by=1))
        items = [
            eval_service.EvalDatasetItem(id=11, dataset_id=1, question="q1",
                                         reference_answer="a", stratum="easy"),
            eval_service.EvalDatasetItem(id=12, dataset_id=1, question="q2",
                                         reference_answer="a", stratum="hard"),
            eval_service.EvalDatasetItem(id=13, dataset_id=1, question="q3",
                                         reference_answer="a", stratum=None),
        ]
        eval_db.add_all(items)
        # easy: 0.8 + 0.6 → 均值 0.7
        _add_score(eval_db, 101, 1, "q1", item_id=11, faithfulness=0.8)
        _add_score(eval_db, 102, 1, "q1b", item_id=11, faithfulness=0.6)
        # hard: 0.2
        _add_score(eval_db, 103, 1, "q2", item_id=12, faithfulness=0.2)
        # 未分层: 0.9
        _add_score(eval_db, 104, 1, "q3", item_id=13, faithfulness=0.9)
        # 全 NULL 指标的未分层样本：不影响均值，只计数
        _add_score(eval_db, 105, 1, "q3-null", item_id=13)
        eval_db.commit()

        data = eval_service.get_stratum_metrics(eval_db, 1)
        assert data["total"] == 3
        by_stratum = {it["stratum"]: it for it in data["items"]}
        assert by_stratum["easy"]["sample_count"] == 2
        assert by_stratum["easy"]["metrics"]["faithfulness"] == pytest.approx(0.7)
        assert by_stratum["hard"]["sample_count"] == 1
        assert by_stratum["hard"]["metrics"]["faithfulness"] == pytest.approx(0.2)
        # stratum=None 组：2 条样本（104/105），均值只算非空
        assert by_stratum[None]["sample_count"] == 2
        assert by_stratum[None]["metrics"]["faithfulness"] == pytest.approx(0.9)

    def test_stratum_metrics_requires_run(self, eval_db):
        with pytest.raises(BizError):
            eval_service.get_stratum_metrics(eval_db, 999)

    def test_stratum_metrics_endpoint(self, admin_client, monkeypatch):
        monkeypatch.setattr(
            eval_service, "get_stratum_metrics",
            lambda db, rid: {"run_id": rid, "total": 1, "items": []},
        )
        data = assert_ok(admin_client.get(f"{API}/runs/5/stratum_metrics"))
        assert data["run_id"] == 5

    def test_stratum_metrics_requires_login(self, client):
        assert_error(client.get(f"{API}/runs/5/stratum_metrics"), 401, 401)


# ---------------------------------------------------------------------------
# 多服务商 system_config 热更新
# ---------------------------------------------------------------------------

class TestProviderConfig:
    def test_update_llm_provider_enum(self, admin_client):
        """llm_provider 合法值可保存；非法值被 schema(422) 与 runtime_config(ValueError) 双层拦截。"""
        data = assert_ok(admin_client.put("/api/v1/system/config", json={"llm_provider": "openai"}))
        assert data["llm_provider"] == "openai"
        assert runtime_config.get_value("llm_provider") == "openai"

        resp = admin_client.put("/api/v1/system/config", json={"llm_provider": "bogus"})
        assert_error(resp, 422, 422)
        with pytest.raises(ValueError):
            runtime_config.update_config({"llm_provider": "bogus"})

    def test_update_embedding_config_hot(self, admin_client):
        """Embedding 提供方/模型/地址/密钥保存后热生效；密钥只回掩码。"""
        data = assert_ok(admin_client.put("/api/v1/system/config", json={
            "embedding_provider": "openai",
            "embedding_model": "BAAI/bge-m3",
            "embedding_api_base": "https://api.siliconflow.cn/v1",
            "embedding_api_key": "sk-emb-123456",
        }))
        assert data["embedding_provider"] == "openai"
        assert data["embedding_model"] == "BAAI/bge-m3"
        assert data["embedding_api_key"] == "******"
        assert runtime_config.get_secret("embedding_api_key") == "sk-emb-123456"
        assert runtime_config.get_value("embedding_provider") == "openai"

    def test_embedding_provider_invalid_rejected(self, admin_client):
        resp = admin_client.put("/api/v1/system/config", json={"embedding_provider": "azure"})
        assert_error(resp, 422, 422)

    def test_provider_presets_delivered(self, admin_client):
        """四家服务商预设随 GET 配置下发（后台页面渲染切换下拉框）。"""
        data = assert_ok(admin_client.get("/api/v1/system/config"))
        presets = {p["id"]: p for p in data["provider_presets"]}
        assert set(presets) == {"deepseek", "siliconflow", "dashscope", "modelscope"}
        assert presets["deepseek"]["llm_api_base"] == "https://api.deepseek.com/v1"
        assert presets["siliconflow"]["llm_api_base"] == "https://api.siliconflow.cn/v1"
        assert presets["dashscope"]["llm_api_base"] == (
            "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        assert presets["modelscope"]["llm_api_base"] == "https://api-inference.modelscope.cn/v1"
        # 硅基流动提供 Embedding 预设模型；DeepSeek/魔搭官方无 Embedding 接口
        assert presets["siliconflow"]["embedding_models"]
        assert presets["deepseek"]["embedding_models"] == []

    def test_provider_presets_not_writable(self, admin_client):
        """provider_presets 是只读元数据：不在可写 schema 中（被忽略 → 空修改体 400）。"""
        resp = admin_client.put("/api/v1/system/config", json={"provider_presets": []})
        assert_error(resp, 400, 400)
