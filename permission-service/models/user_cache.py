"""Keycloak 用户/组同步缓存模型。

设计依据：docs/外部系统设计.md §4.2 权限服务与 Keycloak 的数据同步。
"""

import uuid
from datetime import datetime

from sqlalchemy import String, DateTime, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UserCache(Base):
    """Keycloak 用户本地缓存表。

    权限服务不维护用户数据，只读同步自 Keycloak。
    管理台展示用户列表时从此表读取（避免每次调 Keycloak API）。
    """

    __tablename__ = "user_cache"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, comment="Keycloak user ID (sub)"
    )
    username: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="preferred_username"
    )
    email: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="用户邮箱"
    )
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="租户 ID（从用户属性映射）"
    )
    roles: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="Realm roles 列表"
    )
    groups: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="所属组列表"
    )
    attributes: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="用户属性原始 JSON"
    )
    enabled: Mapped[bool] = mapped_column(default=True, comment="Keycloak 中是否启用")
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="最后同步时间"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
