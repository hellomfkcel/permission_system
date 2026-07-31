"""挂载关系表。

设计依据：docs/外部系统设计.md §2.3.1 mount_registry 表定义。
"""

import uuid
from datetime import datetime

from sqlalchemy import String, Boolean, DateTime, func, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MountRegistry(Base):
    __tablename__ = "mount_registry"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
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
        UniqueConstraint("doc_id", "kb_id", name="uq_doc_kb"),
    )
