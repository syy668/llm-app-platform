"""系统与健康检查接口。"""
from fastapi import APIRouter, Depends
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from ...core.exceptions import BizError, NotFoundError
from ...core.response import ok
from ...core.runtime_config import delete_override, get_config, update_config
from ...core.redis import probe as probe_redis
from ...core.database import engine, get_db
from ...models import Document, KnowledgeBase, Message, User
from ...models.trace_log import TraceLog
from ...schemas.system import SystemConfigUpdate
from ..deps import CurrentUser, get_current_admin, get_current_user

router = APIRouter(tags=["系统"])


def _check_db() -> str:
    """探测数据库连通性，异常返回 error。"""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return "ok"
    except Exception:
        return "error"


@router.get("/health")
def health():
    """健康检查：返回数据库 / Redis 状态与限流真实开关。

    【批次3 新增】`redis_backend` 与 `rate_limit` 两个字段：
    旧版在 fakeredis 下 redis 恒为 ok，真实 Redis 挂了也看不出来；现在显式区分
    fake/real 后端，并直接告诉运维"限流当前是否真的生效"。
    """
    from ..rate_limit import rate_limit_status

    db_status = _check_db()
    redis_info = probe_redis()
    redis_status = "ok" if redis_info["connected"] else "error"
    overall = "ok" if (db_status == "ok" and redis_status == "ok") else "degraded"
    return ok({
        "status": overall,
        "db": db_status,
        "redis": redis_status,
        "redis_backend": redis_info["backend"],
        "rate_limit": rate_limit_status(),
    })


@router.get("/stats/overview")
def stats_overview(
    _: CurrentUser = Depends(get_current_admin),  # 【RBAC】统计为管理侧接口
    db: Session = Depends(get_db),
):
    """全局统计（仅管理员）：用户/知识库/文档/问答量/平均耗时/token 汇总。"""
    user_count = db.query(func.count(User.id)).scalar() or 0
    kb_count = db.query(func.count(KnowledgeBase.id)).scalar() or 0
    doc_count = db.query(func.count(Document.id)).scalar() or 0
    qa_count = (
        db.query(func.count(Message.id)).filter(Message.role == "assistant").scalar() or 0
    )
    avg_latency_row = db.query(
        func.coalesce(func.avg(TraceLog.total_latency_ms), 0),
        func.coalesce(func.sum(TraceLog.total_tokens), 0),
    ).first()
    avg_latency = round(float(avg_latency_row[0]), 2) if avg_latency_row else 0
    token_sum = int(avg_latency_row[1]) if avg_latency_row else 0
    return ok({
        "user_count": user_count,
        "kb_count": kb_count,
        "doc_count": doc_count,
        "qa_count": qa_count,
        "avg_latency": avg_latency,
        "token_sum": token_sum,
    })


@router.get("/system/config")
def read_system_config(user: CurrentUser = Depends(get_current_user)):
    """读取系统配置：登录用户均可查看。"""
    return ok(get_config())


@router.put("/system/config")
def write_system_config(
    payload: SystemConfigUpdate,
    admin: CurrentUser = Depends(get_current_admin),  # 【RBAC】配置修改（含服务商切换）仅管理员
):
    """修改系统配置：仅管理员。修改实时生效，无需重启服务。"""
    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise BizError(400, "未提供任何需要修改的配置项")
    try:
        data = update_config(changes)
    except ValueError as exc:
        raise BizError(400, str(exc))
    return ok(data)


@router.delete("/system/config/{key}")
def delete_system_config_key(
    key: str,
    admin: CurrentUser = Depends(get_current_admin),  # 【RBAC】配置变更仅管理员
):
    """删除单个配置项的覆盖值（即「恢复默认」）：仅管理员。

    【v1.6 新增】语义：
    - 删除后该键**立即回退**到 .env / 代码默认值，无需重启服务；
    - 幂等：键本来没有覆盖值时同样返回 200（结果都是「现在用的是默认值」）；
    - 未知键名返回 404（避免把拼写错误当成"恢复成功"静默吞掉）；
    - 返回值与 GET / PUT 保持同一形状，前端可直接刷新表单；`overridden_keys`
      中不再包含该键即可确认已恢复。
    """
    try:
        delete_override(key)
    except KeyError as exc:
        raise NotFoundError(str(exc.args[0]) if exc.args else f"未知的系统配置项: {key}")
    return ok(get_config())
