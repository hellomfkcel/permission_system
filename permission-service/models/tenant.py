"""租户与用户-租户绑定 — 租户管理权威源。

设计依据：docs/tenant_design.md §3.1.1 tenants 表 + §3.1.2 tenant_memberships 表。
"""

import uuid
from datetime import datetime

from sqlalchemy import String, Boolean, DateTime, ForeignKey, func, UniqueConstraint, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Tenant(Base):
    """租户定义表 — 整个多租户系统的租户权威源。"""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True,
        comment="租户唯一标识（如 tenant-dev, acme-corp）"
    )
    name: Mapped[str] = mapped_column(
        String(255), nullable=False,
        comment="显示名称（如 开发测试租户）"
    )
    description: Mapped[str] = mapped_column(
        String(512), default="", nullable=False,
        comment="租户描述"
    )
    status: Mapped[str] = mapped_column(
        String(16), default="active", nullable=False,
        comment="active | suspended | deleted"
    )
    created_by: Mapped[str] = mapped_column(
        String(255), nullable=False,
        comment="创建者 (user:xxx)"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("idx_tenants_status", "status"),
    )


class TenantMembership(Base):
    """用户-租户绑定表 — 记录用户在各租户中的成员身份与角色。"""

    __tablename__ = "tenant_memberships"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
        comment="所属租户"
    )
    user_id: Mapped[str] = mapped_column(
        String(255), nullable=False,
        comment="用户标识 (user:xxx 格式，来自 IdP)"
    )
    role: Mapped[str] = mapped_column(
        String(64), default="member", nullable=False,
        comment="tenant_admin | member"
    )
    granted_by: Mapped[str] = mapped_column(
        String(255), nullable=False,
        comment="授予者"
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    revoked: Mapped[bool] = mapped_column(default=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_tm_tenant_user"),
        Index("idx_tm_tenant", "tenant_id", postgresql_where="NOT revoked"),
        Index("idx_tm_user", "user_id", postgresql_where="NOT revoked"),
    )
