"""接口依赖注入：当前用户解析与角色鉴权。"""
from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from ..core import security
from ..core.database import get_db
from ..core.exceptions import ForbiddenError, NotFoundError, UnauthorizedError
from ..models import (
    STATUS_ACTIVE,
    Conversation,
    Document,
    KBPermission,
    KnowledgeBase,
    User,
)

# auto_error=False：未携带 token 时不自动返回 401，交由统一异常处理。
_bearer = HTTPBearer(auto_error=False)


@dataclass
class CurrentUser:
    """从 Redis session 解析出的当前登录用户。"""

    id: int
    username: str
    display_name: str | None
    role: str
    status: str


def _load_user_state(db: Session, user_id: int) -> tuple[int, str, str | None, str, str] | None:
    """按主键读取用户权威状态（status / role / 展示名）。

    【批次1-修复A】鉴权必须回查数据库的原因：
    Redis session 只是「登录凭据」，不是「权限凭据」。账号在 token 有效期内可能被
    管理员禁用（软删除置 disabled）、降级角色或物理删除，若只信 session 快照，
    对方只要持续访问（get_session 每次都会滑动续期）就能一直留在系统里。

    【A3 之后】滑动续期已不再是无限期：TOKEN_MAX_LIFETIME_SECONDS 兜住了单次登录的
    最长寿命。但两者解决的不是同一件事——绝对上限是「最多 30 天后必须重登」，
    而账号被禁用要求「下一次请求立刻失效」，最长 30 天的窗口不可接受，
    因此这里的数据库回查不能省。

    性能取舍（实测，见 docs/项目一-现状梳理-2026-10-23.md）：
    - 只投影鉴权所需的 5 个字段，不加载完整 ORM 实体（不读 password_hash）；
    - WHERE id = ? 走主键索引，单行点查，不扫表；实测 SQLAlchemy 全链路 0.32ms/次，
      已包含在每请求本就持有的 db 会话内（不额外开连接），相对接口本身耗时（列表类 5~20ms、
      问答类数百毫秒到数秒）可忽略；
    - 刻意**不**加 60s 状态缓存：那会让「禁用后下一次请求立刻失效」退化为「最长 60s 内仍可用」，
      与验收口径冲突，而收益仅 0.32ms。
    """
    return (
        db.query(User.id, User.username, User.display_name, User.role, User.status)
        .filter(User.id == user_id)
        .first()
    )


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> CurrentUser:
    """解析当前登录用户；token 缺失 / 无效 / 过期均抛 401。

    【批次1-修复A】在 session 解析之后增加一次数据库权威校验：
    - 账号已不存在（物理删除）→ 清除会话并 401；
    - 账号 status != active（被禁用/软删除）→ 清除会话并 401，旧 token 下一次请求即刻失效；
    - 角色以数据库当前值为准（session 里的 role 不再作为权限依据），使降级/升权立即生效。

    【工程化收尾 新增】解析成功后把 user_id 挂到 request.state，
    全局异常处理器打印错误日志时自动带上用户ID，便于排障定位。
    """
    token = credentials.credentials if credentials else None
    if not token:
        raise UnauthorizedError()

    data = security.get_session(token)
    if not data:
        raise UnauthorizedError()

    try:
        user_id = int(data["user_id"])
    except (KeyError, TypeError, ValueError):
        # session 数据结构异常按未登录处理
        raise UnauthorizedError()

    # 先挂 user_id：即使下面判定为禁用，异常日志也能定位到具体账号
    request.state.user_id = user_id

    row = _load_user_state(db, user_id)
    if row is None:
        # 账号已在库中物理删除：顺手清掉这个已成孤儿 token，避免反复回查
        security.delete_session(token)
        raise UnauthorizedError("账号不存在或已被删除，请重新登录")

    uid, username, display_name, role, status = row
    if status != STATUS_ACTIVE:
        # 禁用/软删除：立即踢下线（删会话比单纯拒绝更彻底，响应与登出完全一致）
        security.delete_session(token)
        raise UnauthorizedError("账号已被禁用，请联系管理员")

    return CurrentUser(
        id=uid,
        username=username,
        display_name=display_name,
        role=role,
        status=status,
    )


def get_current_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """【RBAC 新增】管理员鉴权依赖：在 get_current_user 基础上额外校验角色。

    - 角色 role != "admin" 直接抛出 ForbiddenError（全局异常处理器统一返回 403）；
    - 系统配置修改等管理接口统一使用 Depends(get_current_admin)。
    - 【批次1-修复A】这里的 user.role 取自 get_current_user 的数据库回查结果，
      故管理员被降级后旧 token 立即失去管理权限（不再等 session 自然过期）。
    """
    if user.role != "admin":
        raise ForbiddenError("仅管理员可执行该操作")
    return user


# 向后兼容别名：既有接口的 require_admin 与 get_current_admin 语义完全一致，
# 保留旧名避免全量替换（旧引用一行不动，新代码统一使用 get_current_admin）。
require_admin = get_current_admin


def _kb_permission(db: Session, kb_id: int, user_id: int) -> str | None:
    """查询用户对知识库的授权级别；无授权返回 None。"""
    row = (
        db.query(KBPermission)
        .filter(KBPermission.kb_id == kb_id, KBPermission.user_id == user_id)
        .first()
    )
    return row.permission if row else None


def get_accessible_kb(
    kb_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> KnowledgeBase:
    """知识库访问鉴权：管理员全部 / 创建者本人 / 被 kb_permissions 授权（read 或 write）的用户。"""
    kb = db.get(KnowledgeBase, kb_id)
    if not kb:
        raise NotFoundError("知识库不存在")
    if user.role != "admin" and kb.owner_id != user.id and not _kb_permission(db, kb.id, user.id):
        raise ForbiddenError("无权访问该知识库")
    return kb


def get_writable_kb(
    kb_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> KnowledgeBase:
    """知识库写权限鉴权：管理员 / 创建者 / 被授予 write 的用户（上传/改配/删除用）。"""
    kb = db.get(KnowledgeBase, kb_id)
    if not kb:
        raise NotFoundError("知识库不存在")
    if user.role != "admin" and kb.owner_id != user.id:
        if _kb_permission(db, kb.id, user.id) != "write":
            raise ForbiddenError("无权管理该知识库（需要 write 授权）")
    return kb


def get_accessible_document(
    doc_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Document:
    """文档访问鉴权：通过文档所属知识库判定权限（含授权用户）。"""
    doc = db.get(Document, doc_id)
    if not doc:
        raise NotFoundError("文档不存在")
    kb = db.get(KnowledgeBase, doc.kb_id)
    if not kb or (
        user.role != "admin"
        and kb.owner_id != user.id
        and not _kb_permission(db, kb.id, user.id)
    ):
        raise ForbiddenError("无权访问该文档")
    return doc


def get_writable_document(
    doc_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Document:
    """文档写权限鉴权（删除/重新解析）：需要库级 write 权限。"""
    doc = db.get(Document, doc_id)
    if not doc:
        raise NotFoundError("文档不存在")
    kb = db.get(KnowledgeBase, doc.kb_id)
    if not kb or (
        user.role != "admin"
        and kb.owner_id != user.id
        and _kb_permission(db, kb.id, user.id) != "write"
    ):
        raise ForbiddenError("无权管理该文档（需要 write 授权）")
    return doc


def get_own_conversation(
    conversation_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Conversation:
    """会话归属鉴权：用户只能访问自己的会话，越权返回 403（管理员也不例外）。"""
    conv = db.get(Conversation, conversation_id)
    if not conv:
        raise NotFoundError("会话不存在")
    if conv.user_id != user.id:
        raise ForbiddenError("无权访问该会话")
    return conv
