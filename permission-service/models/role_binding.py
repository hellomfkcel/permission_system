"""角色绑定表。"""

import uuid
from datetime import datetime

from sqlalchemy import String, Boolean, DateTime, func, UniqueConstraint, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RoleBinding(Base):
    __tablename__ = "role_bindings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("projects.id"), nullable=True,
        default=None, comment="所属项目 ID（NULL=平台级）",
    )
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    principal: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="user:alice | group:eng"
    )
    role: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="kb_reader | kb_writer | kb_admin | admin",
    )
    resource_type: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="可选：限定到特定资源类型的 KB"
    )
    resource_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="可选：限定到特定 KB"
    )
    granted_by: Mapped[str] = mapped_column(String(255), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    revoked: Mapped[bool] = mapped_column(default=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id", "principal", "role", "resource_type", "resource_id",
            name="uq_role_binding_project",
        ),
        Index(
            "idx_role_binding_project",
            "project_id",
            postgresql_where="NOT revoked",
        ),
    )
