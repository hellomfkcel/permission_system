"""角色定义表 — 角色元数据权威源。"""

import uuid
from datetime import datetime

from sqlalchemy import String, Boolean, DateTime, func, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RoleDefinition(Base):
    """角色定义 — 角色元数据（不含权限）。

    职责边界：Cerbos YAML 是"角色能做什么"的唯一权威源；此表只记录角色的
    档案信息（名称、描述、激活条件、项目归属、是否内置）。两侧无交集，
    因此不存在需要手动同步的状态，也不可能出现权限展示与判定不一致。

    is_system=true 的角色不可删除（对应 Cerbos 中预定义的派生角色）。
    project_id=NULL 表示平台级角色（所有项目共享）。
    """
    __tablename__ = "role_definitions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("projects.id"), nullable=True,
        default=None, comment="所属项目 ID（NULL=平台级角色）",
    )
    name: Mapped[str] = mapped_column(
        String(64), nullable=False,
        comment="角色名（项目内派生角色或身份角色）。"
                "项目级角色在项目内唯一，平台级角色（project_id IS NULL）全平台唯一。"
    )
    description: Mapped[str] = mapped_column(
        String(512), nullable=False, default="",
        comment="角色描述"
    )
    parent_keycloak_roles: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=list,
        comment='激活该角色的身份角色（入场资格）: ["user"] 或 ["system_admin"]。'
                "注意这是激活条件，不是权限继承 —— 角色的权限由 Cerbos 策略决定，"
                "与父角色的权限无关。"
    )
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="系统内置角色（不可删除）"
    )
    # 权限列表刻意不在此表：角色能执行哪些动作属于"策略结构"，
    # 唯一来源是 Cerbos YAML（services/cerbos_policy_parser.py 解析）。
    # 迁移 f1a2b3c4d5e6 已删除原 permissions 列。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("idx_role_def_project", "project_id"),
        # Postgres 中 NULL 互不相等，项目级与平台级唯一性需分别用部分索引表达
        Index(
            "uq_role_def_project_name",
            "project_id", "name",
            unique=True,
            postgresql_where="project_id IS NOT NULL",
        ),
        Index(
            "uq_role_def_platform_name",
            "name",
            unique=True,
            postgresql_where="project_id IS NULL",
        ),
    )
