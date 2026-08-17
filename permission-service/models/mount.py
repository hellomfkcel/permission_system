"""挂载关系表。

设计依据：docs/外部系统设计.md §2.3.1 mount_registry 表定义
          + docs/permission_model_v2.md §3 数据来源边界（项目隔离）。

项目隔离：resource_registry 的唯一性是 (project_id, resource_type, resource_id)，
即**不同项目允许使用相同的资源 ID**。挂载关系必须同样按项目隔离，否则一个项目的
doc_id/kb_id 组合会命中另一个项目的挂载记录 —— link 幂等误判、retire 级联误伤、
visibility 误判 unmounted 都源于此。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    String, Boolean, DateTime, ForeignKey, Index, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MountRegistry(Base):
    __tablename__ = "mount_registry"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id"), nullable=False,
        comment="所属项目 ID。挂载关系永远属于某个项目，无平台级挂载。",
    )
    doc_id: Mapped[str] = mapped_column(String(255), nullable=False)
    kb_id: Mapped[str] = mapped_column(String(255), nullable=False)
    unlinked: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("project_id", "doc_id", "kb_id", name="uq_mount_project_doc_kb"),
        Index("idx_mount_project_doc", "project_id", "doc_id"),
        Index("idx_mount_project_kb", "project_id", "kb_id"),
    )
