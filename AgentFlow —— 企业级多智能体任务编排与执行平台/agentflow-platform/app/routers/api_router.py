"""/api/v1 路由聚合。

W1：鉴权 + 健康检查。
W2：新增 tasks（POST 创建 / GET 详情）。
W3：tasks_ext（SSE 进度流 / 审批断点 / resume / cancel / snapshot / trace）+ approvals（收件箱）。
W4：memories、eval、dashboards（占位见下）。
W5-Part1：memory_router（FR-014 分层记忆管理，P2 首个落地项）。
W6：tasks_w6（GET /tasks 分页列表）+ dashboards（summary/tools）+ eval（/eval/runs 组）——W5-Part2 欠账收口。

注意：W5 只**新增**一个 include 行，既有路由与顺序零改动
（W5 交付说明中登记为「唯一一处存量文件改动」）。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.config import settings
from app.routers import (
    approvals,
    auth,
    dashboards,
    eval,
    kb,
    logs,
    memory_router,
    task_archive,
    task_export,
    tasks,
    tasks_ext,
    tasks_w6,
    tools,
)

api_router = APIRouter(prefix=settings.API_V1_PREFIX)

api_router.include_router(auth.router)
api_router.include_router(tasks.router)  # W2：FR-001
api_router.include_router(tasks_ext.router)  # W3：FR-009 / FR-011 / FR-012 / FR-015
api_router.include_router(approvals.router)  # W3：审批收件箱
api_router.include_router(memory_router.router)  # W5-Part1：FR-014 记忆管理
api_router.include_router(tasks_w6.router)  # W6：GET /tasks 分页列表（缺口 G1）
api_router.include_router(dashboards.router)  # W6：GET /dashboards/summary + /tools + /trend（缺口 G2）
api_router.include_router(eval.router)  # W6：/eval/runs 评测接口组（缺口 G3，FR-016）
api_router.include_router(logs.router)  # v1.0：POST /logs/search 日志查询（缺口 G6）
api_router.include_router(kb.router)  # v1.0.6：GET /kb/status 知识库工具连通性自检（项目一真实检索契约）
api_router.include_router(task_export.router)  # v1.1：GET /tasks/{id}/export + /trace/export（缺口 G10）
api_router.include_router(task_archive.router)  # v1.3：POST /tasks/{id}/archive（归档，第一阶段）
api_router.include_router(tools.router)  # v1.1：GET/PATCH /tools 工具注册面板（元信息 + 启停）

# ---------------------------------------------------------------------------
# 后续阶段注册位（尚未实现，禁止提前落地业务逻辑）
# ---------------------------------------------------------------------------
#
# 约定：所有写接口必须挂 Depends(require_role(...))（PRD NFR-S2）。
