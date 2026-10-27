"""ORM 模型包：导入所有模型，保证 Base.metadata 完整注册。"""
from .conversation import Conversation
from .document import PARSE_DONE, PARSE_FAILED, PARSE_PENDING, Document
from .document_chunk import DocumentChunk
from .eval_dataset import EvalDataset, EvalDatasetItem
from .eval_run import EvalRun, EvalRunScore
from .knowledge_base import KB_STATUS_ACTIVE, KB_STATUS_ARCHIVED, KnowledgeBase
from .kb_permission import KBPermission, PERM_READ, PERM_WRITE
from .message import MSG_DONE, MSG_FAILED, MSG_RUNNING, Message
from .message_feedback import FEEDBACK_DISLIKE, FEEDBACK_LIKE, MessageFeedback
from .operation_log import OperationLog
from .trace_log import TraceLog
from .trace_tool_call import TraceToolCall
from .user import ROLE_ADMIN, ROLE_USER, STATUS_ACTIVE, STATUS_DISABLED, User

__all__ = [
    "User",
    "OperationLog",
    "KnowledgeBase",
    "Document",
    "DocumentChunk",
    "Conversation",
    "Message",
    "MessageFeedback",
    "KBPermission",
    # 【v1.7 补全】eval / trace 模型此前未在此导出，仅靠路由间接导入才注册到
    # Base.metadata —— 导致「只导入 app.models」时 create_all 会漏建这 6 张表。
    "EvalDataset",
    "EvalDatasetItem",
    "EvalRun",
    "EvalRunScore",
    "TraceLog",
    "TraceToolCall",
    "PERM_READ",
    "PERM_WRITE",
    "FEEDBACK_LIKE",
    "FEEDBACK_DISLIKE",
    "ROLE_ADMIN",
    "ROLE_USER",
    "STATUS_ACTIVE",
    "STATUS_DISABLED",
    "KB_STATUS_ACTIVE",
    "KB_STATUS_ARCHIVED",
    "PARSE_PENDING",
    "PARSE_DONE",
    "PARSE_FAILED",
    "MSG_RUNNING",
    "MSG_DONE",
    "MSG_FAILED",
]
