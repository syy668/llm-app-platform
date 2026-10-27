"""裁判配置的运行时读取与热更新。

设计要点（对齐项目一 runtime_config 的成熟做法，但落库不落文件）：

  1. **优先级**：runtime_configs 表 > .env > 代码默认值。
     库里没有的键视为「沿用 .env」，因此现有部署不需要做数据迁移就能升级。
  2. **热更新**：写库后调用 invalidate() 清本地缓存，下一次 judge._build() 会
     按新签名重建 LLM/Embedding 客户端 —— 全程不重启进程。
  3. **不落非法配置**：PUT 接口先在内存里用候选配置跑 preflight，通过才写库。
     配置错误（模型名写错 / key 失效）在这一刻就返回 422，绝不留到评测跑到一半。
  4. **温度锁死 0**：JUDGE_TEMPERATURE 不在可写键里，也不从库里读，
     恒为 0.0 —— 裁判要的是可复现性，温度 >0 会让同一份样本两次跑分不同。
"""

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.runtime_config import RuntimeConfig

logger = logging.getLogger("evalagent.runtime_config")

# 允许在界面上修改的键（白名单，不在表里的键即便被写库也不会生效）
EDITABLE_KEYS = (
    "JUDGE_MODEL",
    "JUDGE_API_BASE",
    "JUDGE_API_KEY",
    "JUDGE_EMBEDDING_MODEL",
    # 向量模型可以与裁判 LLM **不同源**（典型：LLM 走百炼、embedding 走硅基流动的 bge-m3）。
    # 留空 = 沿用裁判的 api_base / api_key（老行为，配置最省事）。
    "JUDGE_EMBEDDING_API_BASE",
    "JUDGE_EMBEDDING_API_KEY",
)
SECRET_KEYS = frozenset({"JUDGE_API_KEY", "JUDGE_EMBEDDING_API_KEY"})
# 只读展示项：不在库里、不可写
READONLY_KEYS = {
    "JUDGE_TEMPERATURE": 0.0,
    "JUDGE_TIMEOUT": settings.JUDGE_TIMEOUT,
    "JUDGE_MAX_CONCURRENCY": settings.JUDGE_MAX_CONCURRENCY,
    "JUDGE_ITEM_TIMEOUT": settings.JUDGE_ITEM_TIMEOUT,
    "JUDGE_METRIC_TIMEOUT": settings.JUDGE_METRIC_TIMEOUT,
    "JUDGE_MAX_RETRIES": settings.JUDGE_MAX_RETRIES,
    "JUDGE_DISABLE_THINKING": settings.JUDGE_DISABLE_THINKING,
}
KEY_DESCRIPTIONS = {
    "JUDGE_MODEL": "裁判模型名（OpenAI 兼容；百炼/DeepSeek 用短名，硅基流动/魔搭需带组织前缀）",
    "JUDGE_API_BASE": "接口地址（OpenAI 兼容 base_url，如 https://dashscope.aliyuncs.com/compatible-mode/v1）",
    "JUDGE_API_KEY": "API Key（读接口恒返掩码；提交掩码或留空表示不修改）",
    "JUDGE_EMBEDDING_MODEL": (
        "向量模型（仅 answer_relevancy 使用）。建议与项目一检索用的 embedding 保持一致"
        "（BAAI/bge-m3），否则同一指标名的分数跨平台不可比"
    ),
    "JUDGE_EMBEDDING_API_BASE": "向量模型接口地址（留空=与裁判 LLM 同源，如硅基流动 bge-m3 需单独填）",
    "JUDGE_EMBEDDING_API_KEY": "向量模型 API Key（留空=与裁判 LLM 同一个 Key；读接口恒返掩码）",
}

MASK = "******"
# 读缓存的存活时间：写操作会主动 invalidate，这个 TTL 只防多进程/外部改库时长期陈旧
CACHE_TTL_SECONDS = 30.0

_cache_lock = threading.Lock()
_cache: dict[str, str | None] = {}
_cache_at: float = 0.0


def _env_default(key: str) -> str | None:
    value = getattr(settings, key, None)
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _load_from_db(db: Session) -> dict[str, str | None]:
    """读库（只取白名单键）。失败不抛：读不到就当未配置，回落到 .env。"""
    try:
        rows = db.query(RuntimeConfig).filter(RuntimeConfig.config_key.in_(EDITABLE_KEYS)).all()
    except Exception as exc:  # noqa: BLE001 - 库不可用不该让裁判完全不可用
        logger.warning("读取 runtime_configs 失败，回落到 .env：%s", exc)
        return {}
    return {row.config_key: row.config_value for row in rows}


def invalidate() -> None:
    """清本地缓存（写库后必须调用，否则热更新不生效）。"""
    global _cache_at
    with _cache_lock:
        _cache.clear()
        _cache_at = 0.0


def effective_config(db: Session | None = None, *, force: bool = False) -> dict[str, str | None]:
    """返回生效的裁判配置（已合并库里覆盖值）。线程安全，带短 TTL 缓存。"""
    global _cache, _cache_at
    with _cache_lock:
        fresh = _cache and (time.time() - _cache_at) < CACHE_TTL_SECONDS
        if fresh and not force:
            return dict(_cache)

    if db is not None:
        db_overrides = _load_from_db(db)
        own_session = False
    else:
        from app.core.database import SessionLocal

        session = SessionLocal()
        own_session = True
        try:
            db_overrides = _load_from_db(session)
        finally:
            if own_session:
                session.close()

    merged: dict[str, str | None] = {}
    for key in EDITABLE_KEYS:
        value = db_overrides.get(key)
        merged[key] = value if value not in (None, "") else _env_default(key)

    with _cache_lock:
        _cache = dict(merged)
        _cache_at = time.time()
    return merged


@dataclass
class JudgeConfig:
    """裁判配置快照。JUDGE_TEMPERATURE 恒为 0.0，不可配置。"""

    model: str = ""
    api_base: str = ""
    api_key: str = ""
    embedding_model: str = ""
    # 向量模型可独立于裁判 LLM（不同服务商 / 不同 Key）；留空表示沿用裁判的同源配置
    embedding_api_base: str = ""
    embedding_api_key: str = ""

    # 为保证评测结果的可复现性，裁判温度强制锁定为 0（不接受外部传入）。
    temperature: float = 0.0

    @property
    def emb_api_base(self) -> str:
        """向量请求实际使用的 base_url：独立配置优先，否则回落到裁判的。"""
        return self.embedding_api_base or self.api_base

    @property
    def emb_api_key(self) -> str:
        """向量请求实际使用的 key：独立配置优先，否则回落到裁判的。"""
        return self.embedding_api_key or self.api_key

    def signature(self) -> tuple[str, str, str, str, str, str, float]:
        """配置指纹：任一字段变化都必须触发客户端重建。

        embedding 的 base/key 也要进指纹 —— 只改向量服务商却复用旧客户端，
        表现是「界面改了没生效」，比报错更难查。
        """
        return (
            self.model,
            self.api_base,
            self.api_key,
            self.embedding_model,
            self.emb_api_base,
            self.emb_api_key,
            self.temperature,
        )

    def missing(self) -> list[str]:
        need = []
        if not self.model:
            need.append("JUDGE_MODEL")
        if not self.api_key:
            need.append("JUDGE_API_KEY")
        if not self.embedding_model:
            need.append("JUDGE_EMBEDDING_MODEL")
        return need


def build_judge_config(override: dict[str, str | None] | None = None) -> JudgeConfig:
    """构造裁判配置；override 用于「保存前用候选配置探活」。"""
    base = effective_config()
    if override:
        base = {**base, **{k: v for k, v in override.items() if k in EDITABLE_KEYS}}
    return JudgeConfig(
        model=(base.get("JUDGE_MODEL") or "").strip(),
        api_base=(base.get("JUDGE_API_BASE") or "").strip(),
        api_key=(base.get("JUDGE_API_KEY") or "").strip(),
        embedding_model=(base.get("JUDGE_EMBEDDING_MODEL") or settings.JUDGE_EMBEDDING_MODEL or "").strip(),
        embedding_api_base=(base.get("JUDGE_EMBEDDING_API_BASE") or "").strip(),
        embedding_api_key=(base.get("JUDGE_EMBEDDING_API_KEY") or "").strip(),
    )


def mask(value: str | None) -> str:
    """密钥脱敏：已配置只回固定掩码，不暴露长度与前后缀（避免被暴力枚举）。"""
    return MASK if value else ""


def snapshot(db: Session) -> dict[str, Any]:
    """供 GET 接口使用的完整视图（含来源与更新时间）。"""
    cfg = build_judge_config()
    rows = {
        row.config_key: row
        for row in db.query(RuntimeConfig).filter(RuntimeConfig.config_key.in_(EDITABLE_KEYS)).all()
    }
    latest = max((r.updated_at for r in rows.values() if r.updated_at), default=None)
    updated_by = next((r.updated_by for r in rows.values() if r.updated_by), None)
    env_cfg = {key: _env_default(key) for key in EDITABLE_KEYS}
    return {
        "model": cfg.model,
        "api_base": cfg.api_base,
        "api_key": mask(cfg.api_key),
        "api_key_configured": bool(cfg.api_key),
        "embedding_model": cfg.embedding_model,
        # 向量模型的独立连接配置；留空时 emb_api_base/emb_api_key 会自动回落到裁判的
        "embedding_api_base": cfg.embedding_api_base,
        "embedding_api_key": mask(cfg.embedding_api_key),
        "embedding_api_key_configured": bool(cfg.embedding_api_key),
        "effective_embedding_api_base": cfg.emb_api_base,
        "embedding_shares_judge_endpoint": not cfg.embedding_api_base and not cfg.embedding_api_key,
        # 温度只读：前端展示但不可编辑
        "temperature": 0.0,
        "temperature_locked": True,
        "readonly": READONLY_KEYS,
        # 每个键的来源：db（界面改过）/ env（沿用 .env）
        "source": {key: ("db" if rows.get(key) and rows[key].config_value else "env") for key in EDITABLE_KEYS},
        # 与 .env 相比，界面上真正覆盖过的键
        "overridden": [
            key for key in EDITABLE_KEYS
            if rows.get(key) and (rows[key].config_value or None) not in (None, env_cfg.get(key))
        ],
        "env_default": {key: (mask(v) if key in SECRET_KEYS else v) for key, v in env_cfg.items()},
        "descriptions": KEY_DESCRIPTIONS,
        "updated_at": latest.isoformat() if isinstance(latest, datetime) else None,
        "updated_by": updated_by,
    }


def persist(db: Session, updates: dict[str, str], updated_by: str | None) -> int:
    """写库（upsert）。只写白名单键，值已由调用方校验/合并。返回写入键数。"""
    written = 0
    for key, value in updates.items():
        if key not in EDITABLE_KEYS:
            continue
        row = db.get(RuntimeConfig, key)
        if row is None:
            row = RuntimeConfig(
                config_key=key,
                is_secret=key in SECRET_KEYS,
                description=KEY_DESCRIPTIONS.get(key),
                updated_by=updated_by,
            )
            db.add(row)
        row.config_value = value
        row.updated_by = updated_by
        written += 1
    db.commit()
    invalidate()
    logger.info("裁判配置已更新：keys=%s by=%s", sorted(updates), updated_by)
    return written
