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
    Boolean,
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
        # W6：GET /tasks 纯时间范围扫描（docs/W6接口设计文档.md §1.4）
        Index("ix_tasks_created_at", "created_at"),
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
    # ---- G8：任务级高级开关（默认关，不破坏默认链路） ----
    compress: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="上下文压缩开关")
    use_memory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="长期记忆检索开关")
    eval_tag: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True, comment="评测标签（绑定 eval_run 筛选）")
    result_artifact_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="最终报告产物")
    # 终态报告快照（summary/review/artifacts/token_ledger 等运行期产物）。
    # 为什么要在 MySQL 里再存一份：加固任务 4 会在任务终态后删除 Redis checkpointer 快照，
    # 而详情页的"结论摘要/五维评审/图表产物"面板是从快照里读的 → 清理后必然全空。
    # 这里在 commit 前落一份 JSON，详情接口在快照缺失时回退到它，展示能力不再依赖 Redis。
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="终态报告快照（快照清理后的详情页回退源）"
    )
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, comment="{code,message,span_id}")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="终态时间")
    # 归档（第一阶段：只做可逆归档，不做删除）。
    # 语义：从"全部任务"默认列表隐藏，数据与审计链完整保留，可随时取消归档。
    # 与后续的 deleted_at（软删除）区别在于：归档是**用户可见可逆**的整理动作，删除是合规动作。
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True, comment="归档时间；NULL=未归档"
    )

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
    # G8：评测标签（由任务创建时 eval_tag 透传，支持评测筛选）
    eval_tag: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True, comment="评测标签，用于筛选")
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
