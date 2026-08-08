"""管理台 API — 资源查询、所有权管理与所有权转移。

设计依据：docs/外部系统设计.md §2.4.4 管理台专用 API — 资源管理。
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from models.resource import ResourceRegistry
from api.auth_routes import get_current_admin, get_project_scope, ProjectScope, require_platform_permission
from schemas.responses import Principal
from services.event_publisher import get_event_publisher

router = APIRouter(prefix="/api/v1/resources", tags=["admin-resources"])


class ResourceOut(BaseModel):
    id: str
    resource_type: str
    resource_id: str
    name: str | None = None
    tenant_id: str
    owner: str
    retired: bool
    created_at: str = ""
    updated_at: str = ""


@router.get("", response_model=list[ResourceOut])
async def list_resources(
    type: str | None = Query(None, alias="type", description="资源类型: kb | document"),
    tenant_id: str | None = Query(None, description="租户 ID 过滤"),
    project_id: str | None = Query(None, description="按项目 ID 过滤"),
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    scope: ProjectScope = Depends(get_project_scope),
    _perm: None = Depends(require_platform_permission("resource_mgmt", "platform:read")),
) -> list[ResourceOut]:
    """列出已注册资源 — 供管理台资源管理页使用。需要管理员认证。

    支持按 type、tenant_id、project_id 过滤，最多返回 500 条。
    按管理员项目范围自动过滤。
    """
    conditions = []
    if type:
        conditions.append(ResourceRegistry.resource_type == type)
    if tenant_id:
        conditions.append(ResourceRegistry.tenant_id == tenant_id)

    # 项目范围过滤
    if project_id:
        conditions.append(ResourceRegistry.project_id == project_id)
    elif not scope.is_platform_admin:
        scope_filter = scope.filter_condition(ResourceRegistry)
        if scope_filter is not None:
            conditions.append(scope_filter)

    stmt = (
        select(ResourceRegistry)
        .where(*conditions)
        .order_by(ResourceRegistry.created_at.desc())
        .limit(500)
    )
    result = await db.execute(stmt)
    resources = result.scalars().all()

    return [
        ResourceOut(
            id=str(r.id),
            resource_type=r.resource_type,
            resource_id=r.resource_id,
            name=r.name,
            tenant_id=r.tenant_id,
            owner=r.owner,
            retired=r.retired,
            created_at=r.created_at.isoformat() if r.created_at else "",
            updated_at=r.updated_at.isoformat() if r.updated_at else "",
        )
        for r in resources
    ]


# ── 所有权转移 ──


class TransferOwnershipRequest(BaseModel):
    resource_type: str = Field(..., description="kb | document")
    resource_id: str = Field(..., description="资源 ID")
    new_owner: str = Field(..., description="新所有者: user:xxx")


class TransferResult(BaseModel):
    change_id: str
    resource_type: str
    resource_id: str
    previous_owner: str
    new_owner: str


@router.post("/transfer-ownership", response_model=TransferResult)
async def transfer_ownership(
    body: TransferOwnershipRequest,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    _perm: None = Depends(require_platform_permission("resource_mgmt", "platform:write")),
) -> TransferResult:
    """转移资源所有权（需要管理员认证）。

    设计依据：docs/外部系统设计.md §2.4.4 资源管理 — 所有权转移。
    """
    stmt = select(ResourceRegistry).where(
        ResourceRegistry.resource_type == body.resource_type,
        ResourceRegistry.resource_id == body.resource_id,
    )
    result = await db.execute(stmt)
    resource = result.scalar_one_or_none()

    if not resource:
        raise HTTPException(status_code=404, detail="Resource not found")

    previous_owner = resource.owner
    resource.owner = body.new_owner
    resource.updated_at = datetime.now(timezone.utc)
    await db.commit()

    # 发布事件
    publisher = get_event_publisher()
    await publisher.publish_visibility_changed(
        tenant_id=resource.tenant_id,
        resource_type=body.resource_type,
        resource_id=body.resource_id,
        event_type="OWNERSHIP_TRANSFERRED",
        change_detail={
            "previous_owner": previous_owner,
            "new_owner": body.new_owner,
            "transferred_by": f"user:{admin.user_id}",
        },
    )

    return TransferResult(
        change_id=str(resource.id),
        resource_type=body.resource_type,
        resource_id=body.resource_id,
        previous_owner=previous_owner,
        new_owner=body.new_owner,
    )


# ── 资源所有者查询（管理台 API）──
# P0-4 修复：从 /v1/resources/{type}/{id}/owners（无 admin auth）
# 迁移到 /api/v1/resources/{type}/{id}/owners（需 admin 认证）。
# 设计依据：docs/外部系统设计.md §2.4.4 管理台专用 API。


class ResourceOwnerResponse(BaseModel):
    """资源所有者信息响应。

    设计依据：docs/外部系统设计.md §2.4.4 管理台专用 API
         GET /api/v1/resources/{type}/{id}/owners — 查看资源所有权。
    """
    resource_type: str
    resource_id: str
    owner: str
    tenant_id: str
    retired: bool
    is_enabled: bool
    allow_download: bool
    created_at: str | None = None
    updated_at: str | None = None


@router.get("/{resource_type}/{resource_id}/owners", response_model=ResourceOwnerResponse)
async def get_resource_owners(
    resource_type: str,
    resource_id: str,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    _perm: None = Depends(require_platform_permission("resource_mgmt", "platform:read")),
) -> ResourceOwnerResponse:
    """查询资源所有者信息（管理台 API，需管理员认证）。

    管理台用于展示资源的所有权归属。
    设计依据：docs/外部系统设计.md §2.4.4。

    P0-4 修复：此端点原先仅在 /v1/resources/{type}/{id}/owners（无 admin auth）。
    现在同时在 /api/v1/resources/{type}/{id}/owners 提供管理员认证版本。
    """
    stmt = select(ResourceRegistry).where(
        ResourceRegistry.resource_type == resource_type,
        ResourceRegistry.resource_id == resource_id,
    )
    result = await db.execute(stmt)
    resource = result.scalar_one_or_none()

    if not resource:
        raise HTTPException(status_code=404, detail="resource not found")

    return ResourceOwnerResponse(
        resource_type=resource.resource_type,
        resource_id=resource.resource_id,
        owner=resource.owner,
        tenant_id=resource.tenant_id,
        retired=resource.retired,
        is_enabled=resource.is_enabled,
        allow_download=resource.allow_download,
        created_at=resource.created_at.isoformat() if resource.created_at else None,
        updated_at=resource.updated_at.isoformat() if resource.updated_at else None,
    )
