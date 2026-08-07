"""租户管理 API — CRUD + 成员管理。

设计依据：docs/tenant_design.md §3.2 API 设计。
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Header, Query
from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from models.tenant import Tenant, TenantMembership
from schemas.tenant_requests import (
    CreateTenantRequest,
    UpdateTenantRequest,
    AddTenantMemberRequest,
)
from schemas.tenant_responses import (
    TenantResponse,
    TenantListResponse,
    TenantMemberResponse,
    TenantMemberListResponse,
    UserTenantsResponse,
)
from api.auth_routes import get_current_admin

router = APIRouter(prefix="/api/v1/tenants", tags=["admin-tenants"])


# ── 辅助函数 ──


def _tenant_to_response(t: Tenant, member_count: int = 0) -> TenantResponse:
    return TenantResponse(
        id=t.id,
        name=t.name,
        description=t.description or "",
        status=t.status,
        member_count=member_count,
        created_by=t.created_by,
        created_at=t.created_at.isoformat() if t.created_at else "",
        updated_at=t.updated_at.isoformat() if t.updated_at else "",
    )


def _membership_to_response(m: TenantMembership) -> TenantMemberResponse:
    return TenantMemberResponse(
        id=str(m.id),
        user_id=m.user_id,
        role=m.role,
        granted_by=m.granted_by,
        granted_at=m.granted_at.isoformat() if m.granted_at else "",
        revoked=m.revoked,
    )


async def _get_tenant_or_404(db: AsyncSession, tenant_id: str) -> Tenant:
    """获取租户，不存在时抛出 404。"""
    stmt = select(Tenant).where(Tenant.id == tenant_id)
    result = await db.execute(stmt)
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"Tenant not found: {tenant_id}")
    return tenant


async def _get_member_count(db: AsyncSession, tenant_id: str) -> int:
    """获取租户活跃成员数。"""
    stmt = select(sa_func.count()).select_from(TenantMembership).where(
        TenantMembership.tenant_id == tenant_id,
        TenantMembership.revoked == False,  # noqa: E712
    )
    result = await db.execute(stmt)
    return result.scalar() or 0


# ── 租户 CRUD ──


@router.post("", response_model=TenantResponse, status_code=201)
async def create_tenant(
    body: CreateTenantRequest,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
) -> TenantResponse:
    """创建新租户。

    权限：system_admin（管理台管理员）。

    业务逻辑：
    1. 校验 id 格式
    2. INSERT tenants
    3. 自动添加创建者为 tenant_admin 成员
    4. 发布 TenantCreated 事件（通过 event_publisher）
    """
    # 检查是否已存在
    existing = await db.execute(select(Tenant).where(Tenant.id == body.id))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail=f"Tenant already exists: {body.id}")

    now = datetime.now(timezone.utc)
    tenant = Tenant(
        id=body.id,
        name=body.name,
        description=body.description or "",
        status="active",
        created_by=f"user:{admin.user_id}",
        created_at=now,
        updated_at=now,
    )
    db.add(tenant)

    # 自动将创建者加为 tenant_admin
    membership = TenantMembership(
        tenant_id=body.id,
        user_id=f"user:{admin.user_id}",
        role="tenant_admin",
        granted_by=f"user:{admin.user_id}",
        granted_at=now,
    )
    db.add(membership)

    # 发布事件：在同一事务内写 permission_changes
    import uuid as _uuid
    from sqlalchemy import text as _text
    from models.change_log import PermissionChange

    ver_result = await db.execute(_text("SELECT nextval('global_permission_version')"))
    version = ver_result.scalar()
    change_entry = PermissionChange(
        id=_uuid.uuid4(),
        event_type="TenantCreated",
        resource_type="tenant",
        resource_id=body.id,
        tenant_id=body.id,
        change_detail={
            "action": "tenant_created",
            "tenant_name": body.name,
            "created_by": f"user:{admin.user_id}",
        },
        version=version,
    )
    db.add(change_entry)

    await db.commit()
    await db.refresh(tenant)

    return _tenant_to_response(tenant, member_count=1)


@router.get("", response_model=TenantListResponse)
async def list_tenants(
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
    status: str | None = Query(None, description="筛选状态"),
    search: str | None = Query(None, description="搜索 ID 或名称"),
    limit: int = Query(50, ge=1, le=200, description="每页条数"),
    offset: int = Query(0, ge=0, description="偏移量"),
) -> TenantListResponse:
    """获取租户列表。需要管理员认证。

    system_admin 可查看全部租户；普通用户仅返回自己所属的租户。
    """
    conditions = []
    if status:
        conditions.append(Tenant.status == status)
    if search:
        conditions.append(
            (Tenant.id.ilike(f"%{search}%")) | (Tenant.name.ilike(f"%{search}%"))
        )

    # Total count
    count_stmt = select(sa_func.count()).select_from(Tenant)
    if conditions:
        from sqlalchemy import and_
        count_stmt = count_stmt.where(and_(*conditions))
    count_result = await db.execute(count_stmt)
    total = count_result.scalar() or 0

    # Paginated query
    stmt = select(Tenant)
    if conditions:
        from sqlalchemy import and_
        stmt = stmt.where(and_(*conditions))
    stmt = stmt.order_by(Tenant.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(stmt)
    tenants = result.scalars().all()

    # 批量获取各租户的成员数
    tenant_ids = [t.id for t in tenants]
    member_counts: dict[str, int] = {}
    if tenant_ids:
        mc_stmt = (
            select(
                TenantMembership.tenant_id,
                sa_func.count().label("cnt"),
            )
            .where(
                TenantMembership.tenant_id.in_(tenant_ids),
                TenantMembership.revoked == False,  # noqa: E712
            )
            .group_by(TenantMembership.tenant_id)
        )
        mc_result = await db.execute(mc_stmt)
        for row in mc_result:
            member_counts[row.tenant_id] = row.cnt

    return TenantListResponse(
        tenants=[
            _tenant_to_response(t, member_count=member_counts.get(t.id, 0))
            for t in tenants
        ],
        total=total,
    )


@router.get("/{tenant_id}", response_model=TenantResponse)
async def get_tenant(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
) -> TenantResponse:
    """获取单个租户详情。需要管理员认证。"""
    tenant = await _get_tenant_or_404(db, tenant_id)
    mc = await _get_member_count(db, tenant_id)
    return _tenant_to_response(tenant, member_count=mc)


@router.patch("/{tenant_id}", response_model=TenantResponse)
async def update_tenant(
    tenant_id: str,
    body: UpdateTenantRequest,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
) -> TenantResponse:
    """更新租户信息（需 tenant_admin 或 system_admin）。

    可更新字段：name, description, status。
    """
    tenant = await _get_tenant_or_404(db, tenant_id)

    if body.name is not None:
        tenant.name = body.name
    if body.description is not None:
        tenant.description = body.description
    if body.status is not None:
        tenant.status = body.status

    tenant.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(tenant)

    mc = await _get_member_count(db, tenant_id)
    return _tenant_to_response(tenant, member_count=mc)


@router.delete("/{tenant_id}", status_code=204)
async def delete_tenant(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
) -> None:
    """软删除租户（status=deleted）。

    前置条件：租户内无活跃资源（resource_registry 中无该租户的活跃记录）。
    """
    tenant = await _get_tenant_or_404(db, tenant_id)

    # 检查是否有活跃资源
    from models.resource import ResourceRegistry
    res_stmt = (
        select(sa_func.count())
        .select_from(ResourceRegistry)
        .where(
            ResourceRegistry.tenant_id == tenant_id,
            ResourceRegistry.retired == False,  # noqa: E712
        )
    )
    res_result = await db.execute(res_stmt)
    active_resources = res_result.scalar() or 0
    if active_resources > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete tenant with {active_resources} active resources. "
                    "Retire all resources first.",
        )

    tenant.status = "deleted"
    tenant.updated_at = datetime.now(timezone.utc)
    await db.commit()


# ── 成员管理 ──


@router.get("/{tenant_id}/members", response_model=TenantMemberListResponse)
async def list_tenant_members(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
    include_revoked: bool = Query(False, description="是否包含已移除的成员"),
) -> TenantMemberListResponse:
    """获取租户成员列表。需要管理员认证。"""
    tenant = await _get_tenant_or_404(db, tenant_id)

    conditions = [TenantMembership.tenant_id == tenant_id]
    if not include_revoked:
        conditions.append(TenantMembership.revoked == False)  # noqa: E712

    from sqlalchemy import and_
    stmt = (
        select(TenantMembership)
        .where(and_(*conditions))
        .order_by(TenantMembership.granted_at.desc())
    )
    result = await db.execute(stmt)
    members = result.scalars().all()

    return TenantMemberListResponse(
        tenant_id=tenant_id,
        members=[_membership_to_response(m) for m in members],
        total=len(members),
    )


@router.post("/{tenant_id}/members", response_model=TenantMemberResponse, status_code=201)
async def add_tenant_member(
    tenant_id: str,
    body: AddTenantMemberRequest,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
) -> TenantMemberResponse:
    """将用户加入租户。

    权限：tenant_admin 或 system_admin。

    已存在但 revoked 的绑定会被重新激活。
    """
    tenant = await _get_tenant_or_404(db, tenant_id)

    # 检查是否已有绑定（包括已 revoke 的）
    stmt = select(TenantMembership).where(
        TenantMembership.tenant_id == tenant_id,
        TenantMembership.user_id == body.user_id,
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if existing:
        if not existing.revoked:
            raise HTTPException(
                status_code=409,
                detail=f"User {body.user_id} is already a member of tenant {tenant_id}",
            )
        # 重新激活已移除的绑定
        existing.role = body.role
        existing.revoked = False
        existing.revoked_at = None
        existing.granted_by = f"user:{admin.user_id}"
        existing.granted_at = now
        await db.commit()
        await db.refresh(existing)
        return _membership_to_response(existing)

    membership = TenantMembership(
        tenant_id=tenant_id,
        user_id=body.user_id,
        role=body.role,
        granted_by=f"user:{admin.user_id}",
        granted_at=now,
    )
    db.add(membership)
    await db.commit()
    await db.refresh(membership)

    return _membership_to_response(membership)


@router.delete("/{tenant_id}/members/{user_id}", status_code=204)
async def remove_tenant_member(
    tenant_id: str,
    user_id: str,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
) -> None:
    """将用户从租户移除（revoked=true）。"""
    await _get_tenant_or_404(db, tenant_id)

    stmt = select(TenantMembership).where(
        TenantMembership.tenant_id == tenant_id,
        TenantMembership.user_id == user_id,
        TenantMembership.revoked == False,  # noqa: E712
    )
    result = await db.execute(stmt)
    membership = result.scalar_one_or_none()
    if membership is None:
        raise HTTPException(
            status_code=404,
            detail=f"User {user_id} is not an active member of tenant {tenant_id}",
        )

    membership.revoked = True
    membership.revoked_at = datetime.now(timezone.utc)
    await db.commit()


# ── 用户-租户查询 ──


@router.get("/by-user/{user_id}", response_model=UserTenantsResponse)
async def get_user_tenants(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
) -> UserTenantsResponse:
    """获取用户所属的所有活跃租户。需要管理员认证。"""
    # 查询用户的所有活跃绑定
    stmt = (
        select(TenantMembership.tenant_id)
        .where(
            TenantMembership.user_id == user_id,
            TenantMembership.revoked == False,  # noqa: E712
        )
    )
    result = await db.execute(stmt)
    tenant_ids = [row[0] for row in result.all()]

    if not tenant_ids:
        return UserTenantsResponse(user_id=user_id, tenants=[])

    # 查询对应租户
    t_stmt = select(Tenant).where(
        Tenant.id.in_(tenant_ids),
        Tenant.status == "active",
    )
    t_result = await db.execute(t_stmt)
    tenants = t_result.scalars().all()

    # 获取各租户的成员数
    member_counts: dict[str, int] = {}
    mc_stmt = (
        select(
            TenantMembership.tenant_id,
            sa_func.count().label("cnt"),
        )
        .where(
            TenantMembership.tenant_id.in_([t.id for t in tenants]),
            TenantMembership.revoked == False,  # noqa: E712
        )
        .group_by(TenantMembership.tenant_id)
    )
    mc_result = await db.execute(mc_stmt)
    for row in mc_result:
        member_counts[row.tenant_id] = row.cnt

    return UserTenantsResponse(
        user_id=user_id,
        tenants=[
            _tenant_to_response(t, member_count=member_counts.get(t.id, 0))
            for t in tenants
        ],
    )
