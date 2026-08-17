"""限制表 — 封禁/阻断规则。"""

import uuid
from datetime import datetime

from sqlalchemy import String, Boolean, DateTime, func, CheckConstraint, Text, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Restriction(Base):
    __tablename__ = "restrictions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id"), nullable=False,
        comment="所属项目 ID",
    )
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    restriction_type: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="subject_ban (型一) | resource_restriction (型二)",
    )
    principal: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="型一：被封禁的主体"
    )
    resource_type: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="型二：受限的资源类型"
    )
    resource_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="型二：受限的资源 ID"
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    removed: Mapped[bool] = mapped_column(default=False)
    removed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "(restriction_type = 'subject_ban' AND principal IS NOT NULL) OR "
            "(restriction_type = 'resource_restriction' AND resource_type IS NOT NULL)",
            name="ck_restriction_type",
        ),
        Index(
            "idx_restriction_project",
            "project_id",
            postgresql_where="NOT removed",
        ),
    )
