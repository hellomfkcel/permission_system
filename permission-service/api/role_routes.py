"""管理台 API — 角色绑定管理。

设计依据：docs/外部系统设计.md §2.4.4 管理台专用 API。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from models.role_binding import RoleBinding
from services.event_publisher import get_event_publisher
from api.auth_routes import get_current_admin
from schemas.responses import Principal

router = APIRouter(prefix="/api/v1/roles", tags=["admin-roles"])


# ── 请求模型 ──


class BindRoleRequest(BaseModel):
    tenant_id: str
    principal: str = Field(..., description="user:xxx | group:xxx")
    role: str = Field(..., description="kb_reader | kb_writer | kb_admin | admin")
    resource_type: str | None = Field(None)
    resource_id: str | None = Field(None)
    granted_by: str


class UnbindRoleRequest(BaseModel):
    principal: str
    role: str
    resource_type: str | None = None
    resource_id: str | None = None


class RoleBindingOut(BaseModel):
    id: str
    tenant_id: str
    principal: str
    role: str
    resource_type: str | None
    resource_id: str | None
    granted_by: str
    granted_at: str
    revoked: bool


# ── 端点 ──


@router.post("/bind")
async def bind_role(
    body: BindRoleRequest,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
) -> dict:
    """绑定角色（需要管理员认证）。"""
    # 检查重复
    stmt = select(RoleBinding).where(
        RoleBinding.principal == body.principal,
        RoleBinding.role == body.role,
    )
    if body.resource_type:
        stmt = stmt.where(RoleBinding.resource_type == body.resource_type)
    if body.resource_id:
        stmt = stmt.where(RoleBinding.resource_id == body.resource_id)
    stmt = stmt.where(RoleBinding.revoked == False)  # noqa: E712

    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="binding already exists")

    new_id = uuid.uuid4()
    binding = RoleBinding(
        id=new_id,
        tenant_id=body.tenant_id,
        principal=body.principal,
        role=body.role,
        resource_type=body.resource_type,
        resource_id=body.resource_id,
        granted_by=f"user:{admin.user_id}",  # 从 JWT 提取
    )
    db.add(binding)

    # ★ Outbox 模式（设计依据 §3.2）：
    # 在同一事务内写角色绑定 + permission_changes，原子提交
    publisher = get_event_publisher()
    version, change_id = await publisher.write_change_log(
        db,
        tenant_id=body.tenant_id,
        resource_type=body.resource_type or "kb",
        resource_id=body.resource_id or "",
        event_type="ROLE_BOUND",
        change_detail={
            "action": "role_bound",
            "principal": body.principal,
            "role": body.role,
        },
    )
    await db.commit()  # 角色绑定 + change_log 原子提交

    # 事务提交后异步发布 Redis（失败不影响已提交数据）
    await publisher.publish_event(
        change_id, version, body.tenant_id,
        body.resource_type or "kb", body.resource_id or "",
        event_type="ROLE_BOUND",
        change_detail={
            "action": "role_bound",
            "principal": body.principal,
            "role": body.role,
        },
    )

    return {"binding_id": str(new_id), "result": "bound", "version": version}


@router.post("/unbind")
async def unbind_role(
    body: UnbindRoleRequest,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
) -> dict:
    """解除角色绑定（需要管理员认证）。"""
    stmt = select(RoleBinding).where(
        RoleBinding.principal == body.principal,
        RoleBinding.role == body.role,
        RoleBinding.revoked == False,  # noqa: E712
    )
    if body.resource_type:
        stmt = stmt.where(RoleBinding.resource_type == body.resource_type)
    if body.resource_id:
        stmt = stmt.where(RoleBinding.resource_id == body.resource_id)

    result = await db.execute(stmt)
    binding = result.scalar_one_or_none()

    if not binding:
        raise HTTPException(status_code=404, detail="binding not found")

    binding.revoked = True

    # ★ Outbox 模式（设计依据 §3.2）：
    # 在同一事务内写角色解绑 + permission_changes，原子提交
    publisher = get_event_publisher()
    version, change_id = await publisher.write_change_log(
        db,
        tenant_id=binding.tenant_id,
        resource_type=binding.resource_type or "kb",
        resource_id=binding.resource_id or "",
        event_type="ROLE_UNBOUND",
        change_detail={
            "action": "role_unbound",
            "principal": body.principal,
            "role": body.role,
        },
    )
    await db.commit()  # 角色解绑 + change_log 原子提交

    # 事务提交后异步发布 Redis（失败不影响已提交数据）
    await publisher.publish_event(
        change_id, version, binding.tenant_id,
        binding.resource_type or "kb", binding.resource_id or "",
        event_type="ROLE_UNBOUND",
        change_detail={
            "action": "role_unbound",
            "principal": body.principal,
            "role": body.role,
        },
    )

    return {"binding_id": str(binding.id), "result": "unbound", "version": version}


@router.get("/bindings", response_model=list[RoleBindingOut])
async def list_bindings(
    principal: str | None = Query(None),
    resource_type: str | None = Query(None),
    resource_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> list[RoleBindingOut]:
    """查询角色绑定列表。"""
    conditions = [RoleBinding.revoked == False]  # noqa: E712
    if principal:
        conditions.append(RoleBinding.principal == principal)
    if resource_type:
        conditions.append(RoleBinding.resource_type == resource_type)
    if resource_id:
        conditions.append(RoleBinding.resource_id == resource_id)

    stmt = select(RoleBinding).where(*conditions).order_by(RoleBinding.granted_at.desc())
    result = await db.execute(stmt)
    bindings = result.scalars().all()

    return [
        RoleBindingOut(
            id=str(b.id),
            tenant_id=b.tenant_id,
            principal=b.principal,
            role=b.role,
            resource_type=b.resource_type,
            resource_id=b.resource_id,
            granted_by=b.granted_by,
            granted_at=b.granted_at.isoformat() if b.granted_at else "",
            revoked=b.revoked,
        )
        for b in bindings
    ]
