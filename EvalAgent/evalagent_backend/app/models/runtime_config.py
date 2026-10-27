"""运行时配置表（key-value）。

为什么落库而不是继续用 .env / json 文件：
  - `.env` 只能在重启时读取，改裁判模型必须重启 8200，运维成本高；
  - json 文件（项目一 data/system_config.json 那一套）会同时被测试与生产读到，
    污染测试且与数据库口径分叉（项目一已踩过）；
  - 落库后可审计（updated_by / updated_at），且能通过唯一键保证幂等 upsert。

安全口径：secret 列（API Key）以明文存储，敏感级别等同项目一的
data/system_config.json —— 只允许 admin 写，读接口一律返回掩码。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class RuntimeConfig(Base):
    """运行时可变配置（当前用于裁判模型热配置）。"""

    __tablename__ = "runtime_configs"
    __table_args__ = ({"comment": "运行时配置：改后即时生效，无需重启服务"},)

    config_key: Mapped[str] = mapped_column(
        String(64), primary_key=True, comment="配置键，如 JUDGE_MODEL / JUDGE_API_KEY"
    )
    config_value: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="配置值；secret 键为明文，仅 admin 可写、读接口返掩码"
    )
    is_secret: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
        comment="是否敏感字段：是则不落日志、读接口脱敏",
    )
    description: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="配置说明（前端可直接展示）"
    )
    updated_by: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="最后修改人用户名（来自上游 /auth/me）"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now(),
        comment="最后更新时间（前端展示「配置生效于 …」）",
    )
