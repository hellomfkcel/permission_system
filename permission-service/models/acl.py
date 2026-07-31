"""ACL 表 — 权限授予记录。

设计依据：docs/外部系统设计.md §2.3.1 acl_entries 表定义。
"""

import uuid
from datetime import datetime

from sqlalchemy import String, Boolean, DateTime, func, UniqueConstraint, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ACLEntry(Base):
    __tablename__ = "acl_entries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    principal: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="user:alice | group:eng | role:viewer"
    )
    resource_type: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="kb | document"
    )
    resource_id: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="资源 ID"
    )
    action: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="kb:read | doc:view | doc:download | ..."
    )
    granted_by: Mapped[str] = mapped_column(String(255), nullable=False, comment="授予者")
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="可选的过期时间"
    )
    revoked: Mapped[bool] = mapped_column(default=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "principal", "resource_type", "resource_id", "action",
            name="uq_acl_principal_resource_action",
        ),
        Index(
            "idx_acl_resource",
            "resource_type", "resource_id",
            postgresql_where="NOT revoked",
        ),
        Index(
            "idx_acl_principal",
            "principal",
            postgresql_where="NOT revoked",
        ),
        Index("idx_acl_tenant", "tenant_id"),
    )
