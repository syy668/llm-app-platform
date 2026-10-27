"""【v1.19.x 回归】评测任务创建时必须快照「当前生效的生成模型」。

背景 bug：create_run 旧实现用 settings.llm_model（.env 启动快照）记录 run.model，
而实际执行链路 get_chat_model() 读 runtime_config（系统配置页热更新值）。
配置页首次保存后两者分叉 —— 界面模型列恒显 .env 默认值（deepseek-chat），
与 trace_logs 的真实调用记录（如 qwen3.5-flash）对不上。

回归口径（与修复后的取值链一致）：
    配置页覆盖值 > settings.llm_model > "deepseek-chat"
"""
import pytest

from app.core import runtime_config
from app.core.database import Base, SessionLocal, engine
from app.models.eval_dataset import EvalDataset, EvalDatasetItem
from app.models.knowledge_base import KnowledgeBase
from app.schemas.eval import RunCreate
from app.services import eval_service

KB_ID = 997


@pytest.fixture()
def snapshot_env():
    """内存库 + KB(997) + 1 条样本数据集；用例结束清库（sqlite 放行 drop_all 守卫）。"""
    eval_service._tables_ready = False
    Base.metadata.create_all(bind=engine, checkfirst=True)
    db = SessionLocal()
    try:
        db.merge(KnowledgeBase(
            id=KB_ID, name="模型快照KB", owner_id=1, embedding_model="mock",
            chunk_size=500, chunk_overlap=50, top_k=3, rerank_threshold=0.0,
            status="ready", doc_count=0,
        ))
        ds = EvalDataset(kb_id=KB_ID, name="模型快照集", item_count=1, created_by=1)
        db.add(ds)
        db.commit()
        db.refresh(ds)
        db.add(EvalDatasetItem(dataset_id=ds.id, question="问题", reference_answer="答案"))
        db.commit()
        yield db, ds.id
    finally:
        db.rollback()
        db.close()
        Base.metadata.drop_all(bind=engine)


def _admin():
    from app.api.deps import CurrentUser

    return CurrentUser(id=1, username="admin", display_name=None, role="admin", status="active")


def test_run_model_records_runtime_override(snapshot_env, monkeypatch):
    """配置页覆盖 llm_model 后，新任务必须记录覆盖值，而非 .env 启动快照。"""
    db, ds_id = snapshot_env
    monkeypatch.setattr(eval_service, "_run_eval_background", lambda run_id: None)
    real_get_value = runtime_config.get_value

    def fake_get_value(key):
        if key == "llm_model":
            return "qwen3.5-flash"
        return real_get_value(key)

    monkeypatch.setattr(runtime_config, "get_value", fake_get_value)

    run = eval_service.create_run(db, _admin(), RunCreate(dataset_id=ds_id))
    assert run["model"] == "qwen3.5-flash"


def test_run_model_falls_back_to_env_default(snapshot_env, monkeypatch):
    """配置页未覆盖时，回退 .env 默认值（settings.llm_model），行为与修复前一致。"""
    db, ds_id = snapshot_env
    monkeypatch.setattr(eval_service, "_run_eval_background", lambda run_id: None)

    from app.config import settings

    run = eval_service.create_run(db, _admin(), RunCreate(dataset_id=ds_id))
    assert run["model"] == settings.llm_model
