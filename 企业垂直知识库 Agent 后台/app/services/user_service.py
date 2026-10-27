"""用户管理业务编排（仅管理员）。"""
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..core import security
from ..core.exceptions import ConflictError, NotFoundError
from ..models import OperationLog, User
from ..schemas.user import user_to_dict


def _log_operation(
    db: Session,
    user_id: int,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict | None = None,
    ip: str | None = None,
) -> None:
    """写入操作审计日志；审计失败不影响主流程（best-effort）。"""
    try:
        db.add(
            OperationLog(
                user_id=user_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                detail=detail,
                ip=ip,
            )
        )
        db.commit()
    except Exception:
        db.rollback()


def list_users(
    db: Session, page: int, size: int, keyword: str | None = None, role: str | None = None
) -> dict:
    """分页查询用户列表。"""
    query = db.query(User)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            or_(
                User.username.like(like),
                User.display_name.like(like),
                User.email.like(like),
            )
        )
    if role:
        query = query.filter(User.role == role)

    total = query.count()
    items = query.order_by(User.id.desc()).offset((page - 1) * size).limit(size).all()
    return {"total": total, "items": [user_to_dict(u) for u in items]}


def create_user(db: Session, data, operator_id: int, ip: str | None = None) -> dict:
    """新建用户（角色受 Pydantic 校验约束，仅 admin/user）。"""
    if db.query(User).filter(User.username == data.username).first():
        raise ConflictError("用户名已存在")

    user = User(
        username=data.username,
        password_hash=security.hash_password(data.password),
        display_name=data.display_name,
        email=data.email,
        role=data.role,
        status="active",
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    _log_operation(db, operator_id, "create_user", "user", str(user.id), {"username": user.username}, ip)
    return user_to_dict(user)


def update_user(db: Session, user_id: int, data, operator_id: int, ip: str | None = None) -> dict:
    """更新用户（角色仅 admin/user，状态仅 active/disabled）。"""
    user = db.get(User, user_id)
    if not user:
        raise NotFoundError("用户不存在")

    if data.display_name is not None:
        user.display_name = data.display_name
    if data.email is not None:
        user.email = data.email
    if data.role is not None:
        user.role = data.role
    if data.status is not None:
        user.status = data.status

    db.commit()
    db.refresh(user)
    _log_operation(
        db, operator_id, "update_user", "user", str(user.id),
        {"role": user.role, "status": user.status}, ip,
    )
    return user_to_dict(user)


def delete_user(db: Session, user_id: int, operator_id: int, ip: str | None = None) -> dict:
    """删除用户：采用软删除（禁用账号），避免破坏外键与历史审计。"""
    user = db.get(User, user_id)
    if not user:
        raise NotFoundError("用户不存在")
    if user.id == operator_id:
        raise ConflictError("不能禁用当前登录的管理员账号")

    user.status = "disabled"
    db.commit()
    _log_operation(db, operator_id, "delete_user", "user", str(user.id), None, ip)
    return {}


def reset_password(
    db: Session, user_id: int, new_password: str, operator_id: int, ip: str | None = None
) -> dict:
    """重置指定用户密码（bcrypt 哈希存储）。"""
    user = db.get(User, user_id)
    if not user:
        raise NotFoundError("用户不存在")

    user.password_hash = security.hash_password(new_password)
    db.commit()
    _log_operation(db, operator_id, "reset_password", "user", str(user.id), None, ip)
    return {}
