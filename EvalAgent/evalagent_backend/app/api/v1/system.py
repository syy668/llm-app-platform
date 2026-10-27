"""系统设置接口：裁判（考官）配置管理。

与「模型服务配置」卡片的区别（两者不要混）：
  - 项目一 PUT /system/config  → 改的是**被评测的问答模型**（考生），热更新
  - 本项目 PUT /system/judge-config → 改的是**RAGAS 评分用的裁判**（考官），热更新

保存流程（顺序不能反）：
  1. 合并出「候选配置」（未提交的字段沿用现值，密钥空/掩码视为不改）
  2. **先用候选配置探活**（judge.preflight(candidate)）——失败直接 422，不落库
  3. 探活通过才写 runtime_configs 表，然后 invalidate 缓存 + judge.reload()
  4. 下一个评测任务开跑时自动用新配置，全程不重启进程
"""

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import CurrentUser, require_admin
from app.core.database import get_db
from app.core.response import ok
from app.schemas.system import JudgeConfigUpdate, JudgePreflightResult
from app.services import runtime_config as rc
from app.services.judge import JudgeUnavailable, judge

logger = logging.getLogger("evalagent.system")

router = APIRouter(prefix="/system", tags=["系统设置"])


@router.get("/judge-config", summary="读取裁判配置（密钥掩码）")
def read_judge_config(db: Session = Depends(get_db)):
    """任何登录用户可读（不含明文密钥），便于排查「这次评测到底用哪个模型打的分」。"""
    return ok(rc.snapshot(db))


@router.put("/judge-config", summary="更新裁判配置（仅管理员，保存前探活）")
async def update_judge_config(
    payload: JudgeConfigUpdate,
    admin: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    current = rc.build_judge_config()

    # ---- 1) 合并候选配置 ----
    updates: dict[str, str] = {}
    if payload.model is not None:
        model = payload.model.strip()
        if not model:
            raise HTTPException(status_code=422, detail="模型名不能为空")
        if model != current.model:
            updates["JUDGE_MODEL"] = model

    if payload.api_base is not None:
        api_base = payload.api_base.strip()
        if api_base and not api_base.startswith(("http://", "https://")):
            raise HTTPException(status_code=422, detail="接口地址必须以 http:// 或 https:// 开头")
        if api_base != current.api_base:
            updates["JUDGE_API_BASE"] = api_base

    if payload.embedding_model is not None:
        embedding = payload.embedding_model.strip()
        if embedding and embedding != current.embedding_model:
            updates["JUDGE_EMBEDDING_MODEL"] = embedding

    # 向量模型独立连接：留空字符串 = 显式回落「与裁判同源」（要能清空，所以不能像 key 那样"空=不改"）
    if payload.embedding_api_base is not None:
        emb_base = payload.embedding_api_base.strip()
        if emb_base and not emb_base.startswith(("http://", "https://")):
            raise HTTPException(status_code=422, detail="向量模型接口地址必须以 http:// 或 https:// 开头")
        if emb_base != current.embedding_api_base:
            updates["JUDGE_EMBEDDING_API_BASE"] = emb_base

    if payload.embedding_api_key is not None:
        emb_key = payload.embedding_api_key.strip()
        if emb_key and emb_key != rc.MASK and not set(emb_key) <= {"*"}:
            if emb_key != current.embedding_api_key:
                updates["JUDGE_EMBEDDING_API_KEY"] = emb_key

    if payload.api_key is not None:
        key = payload.api_key.strip()
        # 掩码 / 空 = 未修改（前端占位符回传的就是掩码）
        if key and key != rc.MASK and not set(key) <= {"*"}:
            if key != current.api_key:
                updates["JUDGE_API_KEY"] = key

    if not updates:
        return ok({"changed": False, "config": rc.snapshot(db)}, message="配置未变化")

    # ---- 2) 用候选配置探活（关键：失败不落库）----
    candidate = rc.build_judge_config(updates)
    missing = candidate.missing()
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"裁判配置不完整：{' / '.join(missing)} 为空（首次配置必须同时提供模型名与 API Key）",
        )

    started = time.time()
    try:
        await asyncio.to_thread(judge.preflight, candidate)
    except JudgeUnavailable as exc:
        logger.warning("裁判配置探活失败，拒绝落库：%s", exc)
        raise HTTPException(
            status_code=422,
            detail=f"探活失败，配置未保存：{exc}",
        ) from exc
    latency_ms = int((time.time() - started) * 1000)

    # ---- 3) 通过才落库，并让裁判按新签名重建 ----
    rc.persist(db, updates, updated_by=admin.username)
    judge.reload()

    logger.info("裁判配置热更新成功：keys=%s by=%s latency=%sms", sorted(updates), admin.username, latency_ms)
    return ok(
        {
            "changed": True,
            "applied_keys": sorted(updates),
            "preflight": {"ok": True, "latency_ms": latency_ms},
            "config": rc.snapshot(db),
        },
        message="配置已保存并实时生效（探活通过，无需重启服务）",
    )


@router.post("/judge-config/preflight", summary="结构探活当前裁判配置（仅管理员）")
async def preflight_judge_config(
    _: CurrentUser = Depends(require_admin),
):
    """探活 = 连通性 + **能否按 RAGAS 要的 JSON 结构输出**。

    只要模型「有内容但解析不出 JSON」，这里就必须返回 ok=False，
    否则管理员会在"探活通过"的假象下保存一个永远出不了分的裁判模型。
    """
    started = time.time()
    try:
        # preflight 的返回值带「向量模型 + 维度」信息，直接回显比一句"探活通过"更有用：
        # 换 embedding 服务商时，1024 维 vs 其它维度一眼就能对上
        detail = await asyncio.to_thread(judge.preflight)
    except JudgeUnavailable as exc:
        return ok(JudgePreflightResult(ok=False, message=str(exc)).model_dump())
    latency_ms = int((time.time() - started) * 1000)
    return ok(
        JudgePreflightResult(
            ok=True,
            message=detail or "结构探活通过（JSON 可解析）",
            model=rc.build_judge_config().model,
            latency_ms=latency_ms,
        ).model_dump()
    )
