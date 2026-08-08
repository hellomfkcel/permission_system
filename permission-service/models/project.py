"""项目与客户端注册模型。

Phase 1: 将硬编码注册表（ALLOWED_CLIENTS, _ALLOWED_AUDIENCES, SERVICE_API_KEY）
迁移到 DB 驱动，实现多项目独立权限平台的基础。

设计依据：docs/权限管理系统架构设计.md §6.5 + 独立权限平台升级方案 Phase 1。
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    String, Text, Boolean, DateTime, ForeignKey,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _uuid():
    return uuid4()


def _now():
    return datetime.utcnow()


class Project(Base):
    """项目注册表 — 每个接入权限平台的项目一条记录。"""
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # "rag-v14", "project-b"
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ProjectClient(Base):
    """项目客户端注册表 — 替代 ALLOWED_CLIENTS 硬编码。

    每个 client_id 属于一个 project，调用 /v1/* 端点时必须携带已注册的 client_id。
    """
    __tablename__ = "project_clients"

    id: Mapped[uuid4] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id"), nullable=False)
    client_id: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("project_id", "client_id", name="uq_project_client"),
    )


class ProjectApiKey(Base):
    """项目 API Key 表 — 替代单全局 SERVICE_API_KEY。

    每个项目可有多个 API key，支持轮换和吊销。
    key_hash 存 SHA-256 哈希，原始 key 仅在签发时显示一次。
    """
    __tablename__ = "project_api_keys"

    id: Mapped[uuid4] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id"), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProjectAudience(Base):
    """ctx_token 受众注册表 — 替代 _ALLOWED_AUDIENCES 硬编码。

    各项目声明自己的合法 audience，ctx_token 铸造时校验。
    """
    __tablename__ = "project_audiences"

    id: Mapped[uuid4] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id"), nullable=False)
    audience: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("project_id", "audience", name="uq_project_audience"),
    )


class ProjectMember(Base):
    """项目成员表 — 管理员用户的项目归属。

    system_admin/admin 用户只能管理自己所属项目的资源。
    platform_admin 角色可管理全部项目（不受此表限制）。

    设计依据：通用权限平台多项目隔离。
    """
    __tablename__ = "project_members"

    id: Mapped[uuid4] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="project_admin")
    granted_by: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_member"),
    )
