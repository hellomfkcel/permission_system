"""管理台 API — 封禁/限制管理。"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from models.restriction import Restriction
from services.event_publisher import get_event_publisher
from api.auth_routes import assert_project_scope, get_current_admin, get_project_scope, ProjectScope, require_platform_permission
from schemas.responses import Principal

router = APIRouter(prefix="/api/v1/restrictions", tags=["admin-restrictions"])


# ── 请求模型 ──


class AddRestrictionRequest(BaseModel):
    tenant_id: str
    restriction_type: str = Field(
        ..., description="subject_ban | resource_restriction"
    )
    principal: str | None = Field(None, description="型一：被封禁的主体")
    resource_type: str | None = Field(None, description="型二：受限的资源类型")
    resource_id: str | None = Field(None, description="型二：受限的资源 ID")
    reason: str | None = Field(None)
    created_by: str
    project_id: str = Field(..., description="所属项目 ID")


class RestrictionOut(BaseModel):
    id: str
    tenant_id: str
    restriction_type: str
    principal: str | None
    resource_type: str | None
    resource_id: str | None
    reason: str | None
    created_by: str
    created_at: str
    removed: bool


# ── 端点 ──


@router.post("/add")
async def add_restriction(
    body: AddRestrictionRequest,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    scope: ProjectScope = Depends(get_project_scope),
    _perm: None = Depends(require_platform_permission("restriction_mgmt", "platform:write")),
) -> dict:
    """添加封禁/限制（需要管理员认证）。"""
    # 验证项目访问权限
    if not scope.can_access(body.project_id):
        raise HTTPException(status_code=403, detail=f"No access to project '{body.project_id}'")

    # 验证
    if body.restriction_type == "subject_ban" and not body.principal:
        raise HTTPException(
            status_code=422,
            detail="subject_ban requires principal",
        )
    if (
        body.restriction_type == "resource_restriction"
        and not body.resource_type
    ):
        raise HTTPException(
            status_code=422,
            detail="resource_restriction requires resource_type",
        )

    new_id = uuid.uuid4()
    restriction = Restriction(
        id=new_id,
        project_id=body.project_id,
        tenant_id=body.tenant_id,
        restriction_type=body.restriction_type,
        principal=body.principal,
        resource_type=body.resource_type,
        resource_id=body.resource_id,
        reason=body.reason,
        created_by=f"user:{admin.user_id}",  # 从 JWT 提取
    )
    db.add(restriction)

    # Outbox 模式：
    # 在同一事务内写封禁 + permission_changes，原子提交
    publisher = get_event_publisher()
    # 对于型一封禁(subject_ban)，resource 信息用 principal 标识
    res_type = body.resource_type or "kb"
    res_id = body.resource_id or body.principal or ""
    version, change_id = await publisher.write_change_log(
        db,
        tenant_id=body.tenant_id,
        resource_type=res_type,
        resource_id=res_id,
        project_id=body.project_id,
        event_type="RESTRICTION_ADDED",
        change_detail={
            "action": "restriction_added",
            "restriction_type": body.restriction_type,
            "principal": body.principal,
            "resource_id": body.resource_id,
        },
    )
    await db.commit()  # 封禁 + change_log 原子提交

    # 事务提交后异步发布 Redis（失败不影响已提交数据）
    await publisher.publish_event(
        change_id, version, body.tenant_id,
        res_type, res_id,
        event_type="RESTRICTION_ADDED",
        change_detail={
            "action": "restriction_added",
            "restriction_type": body.restriction_type,
            "principal": body.principal,
            "resource_id": body.resource_id,
        },
    )

    return {
        "restriction_id": str(new_id),
        "result": "restricted",
        "version": version,
    }


@router.post("/remove")
async def remove_restriction(
    restriction_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    scope: ProjectScope = Depends(get_project_scope),
    _perm: None = Depends(require_platform_permission("restriction_mgmt", "platform:write")),
) -> dict:
    """解除封禁/限制（需要管理员认证）。"""
    try:
        rid = uuid.UUID(restriction_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid restriction_id") from None

    stmt = select(Restriction).where(
        Restriction.id == rid,
        Restriction.removed == False,  # noqa: E712
    )
    result = await db.execute(stmt)
    restriction = result.scalar_one_or_none()

    if not restriction:
        raise HTTPException(status_code=404, detail="restriction not found")

    # 验证项目访问权限
    if not scope.can_access(restriction.project_id):
        raise HTTPException(status_code=403, detail=f"No access to project '{restriction.project_id}'")

    restriction.removed = True
    restriction.removed_at = datetime.now(timezone.utc)

    # Outbox 模式：
    # 在同一事务内写封禁解除 + permission_changes，原子提交
    res_type = restriction.resource_type or "kb"
    res_id = restriction.resource_id or restriction.principal or ""
    publisher = get_event_publisher()
    version, change_id = await publisher.write_change_log(
        db,
        tenant_id=restriction.tenant_id,
        resource_type=res_type,
        resource_id=res_id,
        project_id=restriction.project_id,
        event_type="RESTRICTION_REMOVED",
        change_detail={
            "action": "restriction_removed",
            "restriction_type": restriction.restriction_type,
            "principal": restriction.principal,
            "resource_id": restriction.resource_id,
        },
    )
    await db.commit()  # 封禁解除 + change_log 原子提交

    # 事务提交后异步发布 Redis（失败不影响已提交数据）
    await publisher.publish_event(
        change_id, version, restriction.tenant_id,
        res_type, res_id,
        event_type="RESTRICTION_REMOVED",
        change_detail={
            "action": "restriction_removed",
            "restriction_type": restriction.restriction_type,
            "principal": restriction.principal,
            "resource_id": restriction.resource_id,
        },
    )

    return {"restriction_id": str(rid), "result": "removed", "version": version}


@router.get("", response_model=list[RestrictionOut])
async def list_restrictions(
    principal: str | None = Query(None),
    resource_type: str | None = Query(None),
    resource_id: str | None = Query(None),
    project_id: str | None = Query(None, description="按项目 ID 过滤"),
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    scope: ProjectScope = Depends(get_project_scope),
    _perm: None = Depends(require_platform_permission("restriction_mgmt", "platform:read")),
) -> list[RestrictionOut]:
    """查询封禁/限制列表（需要管理员认证）。按管理员项目范围自动过滤。"""
    conditions = [Restriction.removed == False]  # noqa: E712
    if principal:
        conditions.append(Restriction.principal == principal)
    if resource_type:
        conditions.append(Restriction.resource_type == resource_type)
    if resource_id:
        conditions.append(Restriction.resource_id == resource_id)

    # 项目范围过滤
    if project_id:
        assert_project_scope(scope, project_id)
        conditions.append(Restriction.project_id == project_id)
    elif not scope.is_platform_admin:
        scope_filter = scope.filter_condition(Restriction)
        if scope_filter is not None:
            conditions.append(scope_filter)

    stmt = (
        select(Restriction)
        .where(*conditions)
        .order_by(Restriction.created_at.desc())
    )
    result = await db.execute(stmt)
    restrictions = result.scalars().all()

    return [
        RestrictionOut(
            id=str(r.id),
            tenant_id=r.tenant_id,
            restriction_type=r.restriction_type,
            principal=r.principal,
            resource_type=r.resource_type,
            resource_id=r.resource_id,
            reason=r.reason,
            created_by=r.created_by,
            created_at=r.created_at.isoformat() if r.created_at else "",
            removed=r.removed,
        )
        for r in restrictions
    ]
