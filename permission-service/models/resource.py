"""资源注册表 — 结构镜像的权威源。

设计依据：docs/外部系统设计.md §2.3.1 resource_registry 表定义。
"""

import uuid
from datetime import datetime

from sqlalchemy import String, Boolean, DateTime, func, UniqueConstraint, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ResourceRegistry(Base):
    __tablename__ = "resource_registry"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id"), nullable=False,
        comment="所属项目 ID",
    )
    resource_type: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="kb|document|directory"
    )
    resource_id: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="RAG 系统中的资源 ID"
    )
    name: Mapped[str | None] = mapped_column(
        String(512), nullable=True,
        comment="资源名称（KB 名称 / 文档文件名），由 RAG 系统在 register 时提供。可为空（兼容旧数据）。"
    )
    tenant_id: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="所属租户"
    )
    owner: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="user:xxx 格式"
    )
    retired: Mapped[bool] = mapped_column(default=False)
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False,
        comment="文档是否启用（运营停用=不可检索）。由 B-DOC MountEnabledChanged 事件同步维护。"
    )
    allow_download: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False,
        comment="是否允许下载。仅对 document 类型生效，Cerbos doc:download 规则使用。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id", "resource_type", "resource_id",
            name="uq_resource_project_type_id",
        ),
        Index(
            "idx_resource_project",
            "project_id",
            postgresql_where="NOT retired",
        ),
    )
