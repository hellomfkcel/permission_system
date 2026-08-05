"""角色定义表 — 角色元数据权威源。

设计依据：docs/manage_role_design.md §3.1 role_definitions 表。
"""

import uuid
from datetime import datetime

from sqlalchemy import String, Boolean, DateTime, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RoleDefinition(Base):
    """角色定义 — 描述系统中的角色及其元数据。

    Cerbos YAML 为权限映射的权威源；此表为角色元数据的权威源。
    is_system=true 的角色不可删除（对应 Cerbos 中预定义的派生角色）。
    """

    __tablename__ = "role_definitions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True,
        comment="角色名: kb_reader, kb_writer, kb_admin, admin 等"
    )
    description: Mapped[str] = mapped_column(
        String(512), nullable=False, default="",
        comment="角色描述"
    )
    parent_keycloak_roles: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=list,
        comment='父级 Keycloak 角色: ["user"] 或 ["system_admin"]'
    )
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="系统内置角色（不可删除）"
    )
    permissions: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=list,
        comment="角色拥有的权限列表: [\"kb:read\", \"doc:view\", ...]"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
