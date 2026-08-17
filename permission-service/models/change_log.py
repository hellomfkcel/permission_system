"""变更事件日志表 — 用于对账 + 审计。"""

import uuid
from datetime import datetime

from sqlalchemy import String, BigInteger, DateTime, func, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PermissionChange(Base):
    __tablename__ = "permission_changes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_type: Mapped[str] = mapped_column(
        String(128), nullable=False,
        comment="ACL_GRANTED | ACL_REVOKED | ROLE_BOUND | RESTRICTION_ADDED | ...",
    )
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    kb_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="用于 KB 粒度事件展开"
    )
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    change_detail: Mapped[dict] = mapped_column(
        JSONB, nullable=False, comment="变更详情"
    )
    version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="全局单调递增版本号"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("idx_pc_resource", "resource_type", "resource_id"),
        Index("idx_pc_kb", "kb_id"),
        Index("idx_pc_version", "version"),
    )
