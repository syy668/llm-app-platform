"""评测任务与单样本得分（独立于项目一的 eval_runs / eval_run_scores）。

与项目一的差异（为什么不能直接复用）：
  - 项目一 `eval_runs.kb_id` 必填、只服务单一 KB；项目二还要评测 AgentFlow 编排，
    故改为 `target` + `target_ref` 三元组（评测集已声明 target_type）；
  - `rag_config` 保留（请求级 RAG 实验参数，A/B 调优的核心自变量）；
  - 新增 `ext_run_ref`：承载项目三的 callback_run_no，双向可寻址；
  - 新增 `trace_id` / `cost_usd` / `total_tokens`：跨项目中台必须能算钱、能串联链路；
  - 明细表新增 `agent_role` / `agent_trace` / `suspect`：多智能体归因与引用可疑标记。
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# 运行状态
RUN_PENDING = "pending"
RUN_RUNNING = "running"
RUN_DONE = "done"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"

TERMINAL_STATUSES = (RUN_DONE, RUN_FAILED, RUN_CANCELLED)

# 明细状态
SCORE_OK = "ok"
SCORE_FAILED = "failed"
SCORE_SKIPPED = "skipped"

# 四项 RAGAS 核心指标（键名与前端 RagRadarChart / 项目一完全一致，避免前端改口径）
CORE_METRICS = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")


class EvalRun(Base):
    """评测任务主表。"""

    __tablename__ = "eval_runs"
    __table_args__ = (
        UniqueConstraint("run_no", name="uk_eval_runs_run_no"),
        {"comment": "评测任务主表：一次评测运行的状态、配置与汇总指标"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_no: Mapped[str] = mapped_column(String(32), nullable=False, comment="业务编号，如 EV20261026-0001")
    dataset_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="评测集ID")
    # 冗余快照：评测集后续被改名/改版本，历史 run 仍能复现口径
    dataset_version: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="评测集版本快照")

    # ---- 评测对象与实验变量 ----
    target: Mapped[str] = mapped_column(
        String(32), nullable=False, default="kb", server_default="kb",
        comment="评测对象：kb / agentflow / multi_agent",
    )
    target_ref: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="上游对象引用快照")
    variant: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="A/B 变体：baseline / treatment")
    rag_config: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, comment="请求级 RAG 实验参数：top_k / search_mode / chunk_size",
    )
    evaluator_config: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, comment="裁判配置快照：judge_model / metrics / thresholds / embedding",
    )
    agent_config: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, comment="多智能体配置：参与角色 / 并发度 / 编排标识",
    )

    # ---- 状态与进度（processed 才是进度条口径：成功+失败+跳过）----
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RUN_PENDING, server_default=RUN_PENDING, index=True,
        comment="pending / running / done / failed / cancelled",
    )
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    processed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0", comment="已处理（成功+失败+跳过）")
    success_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    failed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    skipped_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # ---- 结果 ----
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="四项指标均值")
    metric_detail: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, comment="聚合明细：按 stratum / agent_role 分层聚合",
    )

    # ---- 成本与链路 ----
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False, default=0, server_default="0", comment="本次评测花费（美元）")
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="跨项目 trace 串联ID")
    ext_run_ref: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True, comment="外部（项目三）callback_run_no，双向寻址",
    )

    error_msg: Mapped[str | None] = mapped_column(String(1024), nullable=True, comment="失败原因（带可读文案）")
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now(),
    )


class EvalScore(Base):
    """单条样本得分明细。"""

    __tablename__ = "eval_scores"
    __table_args__ = (
        UniqueConstraint("run_id", "item_id", name="uk_eval_scores_run_item"),
        {"comment": "单样本得分明细：一条 run 内每个样本一行"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="所属 run")
    item_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="所属样本")
    item_seq: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="样本序号快照（1-based）")
    stratum: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="分层标签快照")

    # ---- 被测系统产出 ----
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True, comment="被测系统回答")
    reference_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    retrieved_contexts: Mapped[list | None] = mapped_column(JSON, nullable=True, comment="实际召回片段（context_recall 输入）")

    # ---- 四项核心指标（NULL 表示未算出，禁止写 0 冒充）----
    faithfulness: Mapped[float | None] = mapped_column(Float, nullable=True, comment="忠实度")
    answer_relevancy: Mapped[float | None] = mapped_column(Float, nullable=True, comment="答案相关性")
    context_precision: Mapped[float | None] = mapped_column(Float, nullable=True, comment="上下文精确率")
    context_recall: Mapped[float | None] = mapped_column(Float, nullable=True, comment="上下文召回率")
    # 全量原始分（未来加指标不必改表）
    metric_scores: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="RAGAS 全量原始分")

    # ---- 多智能体归因 ----
    agent_role: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="产出该答案的 agent 角色")
    agent_trace: Mapped[list | None] = mapped_column(JSON, nullable=True, comment="agent / 工具调用链快照")

    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # 引用/数字校验可疑（承接项目一 citation_guard 思路：只标不拦）
    suspect: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0", comment="引用或数字校验可疑")

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SCORE_OK, server_default=SCORE_OK,
        comment="ok / failed / skipped",
    )
    error_msg: Mapped[str | None] = mapped_column(String(512), nullable=True)
    scored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="打分完成时间")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
