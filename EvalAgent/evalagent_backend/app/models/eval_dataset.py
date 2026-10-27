"""评测集与样本（独立于项目一的 eval_datasets / eval_dataset_items）。

多智能体评测需求体现在：
  - `target_type` / `target_ref`：评测对象可以是单个知识库、AgentFlow 编排任务，
    或一组 agent 角色，项目二不感知上游实现细节，只按 type + ref 路由；
  - `agent_scope`：声明参与评测的 agent 角色及其权重（多智能体算分的加权依据）；
  - `eval_items.item_type` / `expected_tools` / `expected_agents`：样本粒度不限于
    「一问一答」，可声明期望的工具链与期望参与的 agent，支撑流程级评测。
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

# 评测对象类型
TARGET_KB = "kb"                 # 项目一知识库问答
TARGET_AGENTFLOW = "agentflow"   # 项目三编排任务
TARGET_MULTI_AGENT = "multi_agent"  # 多智能体协同

# 样本类型
ITEM_RAG_QA = "rag_qa"           # 检索问答（RAGAS 主战场）
ITEM_TOOL_CALL = "tool_call"     # 工具调用正确性
ITEM_MULTI_TURN = "multi_turn"   # 多轮会话
ITEM_AGENT_TASK = "agent_task"   # 整条编排任务


class EvalDataset(Base):
    """评测集。"""

    __tablename__ = "eval_datasets"
    __table_args__ = (
        UniqueConstraint("code", name="uk_eval_datasets_code"),
        {"comment": "评测集：一组样本的逻辑集合，可版本化用于回归对比"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="业务编码，唯一，便于跨系统引用")
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="评测集名称")
    description: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="描述")

    # ---- 评测对象（多智能体路由的关键）----
    target_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TARGET_KB, server_default=TARGET_KB,
        comment="评测对象类型：kb / agentflow / multi_agent",
    )
    target_ref: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="上游对象引用：kb_id / workflow_code / 编排标识",
    )
    kb_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="绑定的项目一知识库ID（可空：纯 AgentFlow 评测不绑 KB）",
    )
    agent_scope: Mapped[list | None] = mapped_column(
        JSON, nullable=True, comment="参与评测的 agent 角色清单 [{role, agent_id, weight}]",
    )
    scenario: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="业务场景标签")
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True, comment="自由标签数组")

    # ---- 版本与统计 ----
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1", server_default="v1", comment="评测集版本")
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0", comment="样本数（冗余计数）")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1", comment="是否启用（软删/归档）")

    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="创建人（项目二自有口径，可空）")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now(),
    )

    items: Mapped[list["EvalItem"]] = relationship(
        back_populates="dataset",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="EvalItem.seq",
    )


class EvalItem(Base):
    """评测样本。"""

    __tablename__ = "eval_items"
    __table_args__ = (
        UniqueConstraint("dataset_id", "seq", name="uk_eval_items_dataset_seq"),
        {"comment": "评测样本：单条测试用例（问题 + 参考答案 + 期望行为）"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("eval_datasets.id", ondelete="CASCADE"), nullable=False, index=True,
        comment="所属评测集",
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False, comment="数据集内序号（1-based，对外展示用）")

    question: Mapped[str] = mapped_column(Text, nullable=False, comment="评测问题")
    reference_answer: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="参考答案（AgentFlow/工具类任务可为空）",
    )
    reference_contexts: Mapped[list | None] = mapped_column(
        JSON, nullable=True, comment="参考上下文数组（context_recall 计算的基准）",
    )

    # ---- 分层与难度（难例分层评测）----
    stratum: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="分层标签：easy/hard/业务自定义")
    difficulty: Mapped[int | None] = mapped_column(SmallInteger, nullable=True, comment="难度 1-5")

    # ---- 多智能体：期望行为断言 ----
    item_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ITEM_RAG_QA, server_default=ITEM_RAG_QA,
        comment="样本类型：rag_qa / tool_call / multi_turn / agent_task",
    )
    expected_tools: Mapped[list | None] = mapped_column(JSON, nullable=True, comment="期望调用的工具链")
    expected_agents: Mapped[list | None] = mapped_column(JSON, nullable=True, comment="期望参与的 agent 角色")
    max_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="SLA 上限，超时判劣化")

    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="扩展元数据")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    dataset: Mapped["EvalDataset"] = relationship(back_populates="items")
