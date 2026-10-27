"""MCP Server：notify（PRD FR-008，**恒 Mock、绝不出网**）。

安全前置（唯一硬闸门）：
1. 调用前必须存在 ``status=APPROVED`` 的 approval 记录（按 ``approval_id`` 或 ``task_id`` 匹配）；
2. 无审批 / 审批为 PENDING / REJECTED / EXPIRED → 直接返回 ``APPROVAL_MISSING``，
   **消息不落库、不打印正文全文**，从源头阻断"模型自行外发通知"的风险；
3. 落库目标为 W2 扩展表 ``mock_messages``，只写结构化日志，不发起任何网络请求。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.core.w2_config import w2
from app.schemas.enums import ApprovalStatus
from app.schemas.mcp import McpErrorCode, NotifyInput, NotifyOutput, NotifyStatus
from app.schemas.orm_models import Approval
from app.schemas.orm_models_ext import MockMessage
from app.tools.mcp_stdio import FunctionTool, MCPServerApp, ToolError, run_server
from app.utils.masking import mask_text

logger = get_logger(__name__)

SERVER_NAME = "notify"
TOOL_NAME = "notify"

SessionFactory = async_sessionmaker[AsyncSession]


def _default_session_factory() -> SessionFactory:
    """延迟导入，避免 MCP 子进程 import 期即建立数据库连接。"""
    from app.db.session import get_session_factory

    return get_session_factory()


class NotifyTool:
    name = TOOL_NAME
    description = (
        "向运营群发送经营分析通知（**Mock 渠道，不出网**）。"
        "调用前系统必须已存在 APPROVED 审批记录，否则返回 APPROVAL_MISSING。"
    )

    def __init__(
        self,
        session_factory: SessionFactory | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._now = now or datetime.now

    @property
    def session_factory(self) -> SessionFactory:
        if self._session_factory is None:
            self._session_factory = _default_session_factory()
        return self._session_factory

    async def __call__(self, args: NotifyInput) -> dict[str, Any]:
        if not args.receivers:
            raise ToolError(McpErrorCode.NOTIFY_INVALID, "receivers 不能为空")
        if len(args.receivers) > w2.NOTIFY_MAX_RECEIVERS:
            raise ToolError(McpErrorCode.NOTIFY_INVALID, f"receivers 最多 {w2.NOTIFY_MAX_RECEIVERS} 个")
        if len(args.content) > w2.NOTIFY_MAX_CONTENT_LEN:
            raise ToolError(McpErrorCode.NOTIFY_INVALID, "content 超长")

        approval = await self._require_approved(args)

        message_id = str(uuid.uuid4())
        sent_at = self._now()
        try:
            async with self.session_factory() as session:
                session.add(
                    MockMessage(
                        message_id=message_id,
                        task_id=args.task_id,
                        approval_id=approval.id,
                        channel=args.channel.value,
                        title=args.title,
                        content=args.content,
                        receivers=list(args.receivers),
                        status=NotifyStatus.SENT.value,
                        sent_at=sent_at,
                    )
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.exception("notify_persist_failed")
            raise ToolError(McpErrorCode.NOTIFY_FAILED, f"Mock 消息落库失败: {exc}") from exc

        logger.info(
            "notify_mock_sent",
            extra={
                "message_id": message_id,
                "task_id": args.task_id,
                "approval_id": approval.id,
                "channel": args.channel.value,
                "receivers": len(args.receivers),
                "title": mask_text(args.title),
                "mock": True,
                "network_egress": False,
            },
        )
        output = NotifyOutput(
            message_id=message_id,
            status=NotifyStatus.SENT,
            mock=True,
            sent_at=sent_at,
        )
        return output.model_dump(mode="json")

    async def _require_approved(self, args: NotifyInput) -> Approval:
        """审批前置校验：查不到 APPROVED 一律拒绝（不落库）。"""
        if args.approval_id is None and args.task_id is None:
            raise ToolError(
                McpErrorCode.APPROVAL_MISSING,
                "缺少 approval_id / task_id，无法校验审批，已拒绝发送",
            )

        stmt = select(Approval).where(Approval.status == ApprovalStatus.APPROVED)
        if args.approval_id is not None:
            stmt = stmt.where(Approval.id == args.approval_id)
        if args.task_id is not None:
            stmt = stmt.where(Approval.task_id == args.task_id)

        try:
            async with self.session_factory() as session:
                approval = (await session.execute(stmt.limit(1))).scalar_one_or_none()
        except Exception as exc:  # noqa: BLE001
            logger.exception("notify_approval_check_failed")
            raise ToolError(McpErrorCode.NOTIFY_FAILED, f"审批校验失败: {exc}") from exc

        if approval is None:
            raise ToolError(
                McpErrorCode.APPROVAL_MISSING,
                "未找到 APPROVED 审批记录，notify 被拦截（消息未落库、未出网）",
                data={"task_id": args.task_id, "approval_id": args.approval_id},
            )
        return approval


def build_app(tool: NotifyTool | None = None) -> MCPServerApp:
    return MCPServerApp(
        SERVER_NAME,
        [
            FunctionTool(
                name=TOOL_NAME,
                description=NotifyTool.description,
                input_model=NotifyInput,
                handler=tool or NotifyTool(),
            )
        ],
    )


def main() -> None:  # pragma: no cover - 子进程入口
    run_server(build_app())


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["SERVER_NAME", "TOOL_NAME", "NotifyTool", "build_app", "main"]
