"""角色管理 API — 角色定义 CRUD + 权限矩阵。

设计依据：docs/manage_role_design.md §3.2 API 设计。

一致性约定（修复角色定义双写分叉）：
- 权限的唯一权威源是 Cerbos 策略文件；role_definitions 表保存档案信息
  （描述、父角色、项目归属、是否内置），不再作为权限的独立数据源。
- 写路径先写策略文件，再提交数据库；数据库失败时回滚文件。
- 读路径统一走 _role_permissions()，管理台展示与判定链路取值同源。
"""

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from models.role_definition import RoleDefinition
from models.role_binding import RoleBinding
from api.auth_routes import (
    get_current_admin, get_project_scope, ProjectScope, require_platform_permission,
)
from schemas.responses import Principal
from services.cerbos_policy_parser import (
    get_policy_index,
    get_role_effective_permissions,
    invalidate_role_actions_cache,
    parse_permissions_matrix,
)
from services.role_policy_writer import (
    PolicyWriteError,
    remove_role_policies,
    write_role_policies,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/roles", tags=["admin-roles"])

# Keycloak 身份角色：权限由继承它的派生角色决定，自身不展示权限列表
_KEYCLOAK_IDENTITY_ROLES = ("user", "system_admin")


# ── 响应模型 ──


class RoleDefOut(BaseModel):
    id: str
    name: str
    description: str
    parent_keycloak_roles: list[str] = []
    is_system: bool
    is_keycloak_role: bool = False  # Keycloak 身份角色（user/system_admin）
    permissions: list[str] = []
    binding_count: int = 0
    project_id: str | None = None   # NULL=平台级角色，否则为项目级角色
    policy_synced: bool = True      # 策略文件中是否存在该角色
    created_at: str = ""


class RoleDetailOut(RoleDefOut):
    pass


class PermissionMatrixOut(BaseModel):
    roles: list[dict]


class CreateRoleRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]+$")
    description: str = Field("", max_length=512)
    parent_keycloak_roles: list[str] = Field(default=["user"])
    permissions: list[str] = Field(
        default=[], description="角色权限列表，取值须已在目标项目的资源策略中声明"
    )
    project_id: str | None = Field(None, description="所属项目 ID（NULL=平台级角色）")


class UpdateRoleRequest(BaseModel):
    description: str | None = Field(None, max_length=512)
    parent_keycloak_roles: list[str] | None = None
    permissions: list[str] | None = None


# ── 权限取值：管理台与判定链路同源 ──


def _role_permissions(name: str, project_id: str | None) -> list[str]:
    """返回角色的**有效权限**列表（含身份角色的继承并集）。

    取值只来自 Cerbos 策略文件（唯一权威源）：
    - 派生角色（admin/kb_reader 等）→ 策略中声明的动作；
    - Keycloak 身份角色（system_admin/user 等被 parentRoles 引用的角色）
      → 所有以其为父的派生角色权限的**并集**。

    修复：此前对身份角色硬编码返回 []，管理台显示"无权限"，但判定链路
    （system_admin → admin 派生角色）实际授予全部权限 —— 展示与判定脱节。
    现在展示 = 策略解析出来的有效权限，与判定同源。
    """
    return get_role_effective_permissions(name, project_id)


def _to_out(
    r: RoleDefinition, binding_count: int, permissions: list[str], synced: bool,
) -> RoleDefOut:
    return RoleDefOut(
        id=str(r.id),
        name=r.name,
        description=r.description or "",
        parent_keycloak_roles=r.parent_keycloak_roles or [],
        is_system=r.is_system,
        is_keycloak_role=(r.name in _KEYCLOAK_IDENTITY_ROLES),
        permissions=permissions,
        binding_count=binding_count,
        project_id=r.project_id,
        policy_synced=synced,
        created_at=r.created_at.isoformat() if r.created_at else "",
    )


async def _binding_counts(db: AsyncSession, names: list[str]) -> dict[str, int]:
    if not names:
        return {}
    stmt = (
        select(RoleBinding.role, sa_func.count().label("cnt"))
        .where(
            RoleBinding.role.in_(names),
            RoleBinding.revoked == False,  # noqa: E712
        )
        .group_by(RoleBinding.role)
    )
    result = await db.execute(stmt)
    return {row.role: row.cnt for row in result}


# ── 端点 ──


@router.get("/definitions", response_model=list[RoleDefOut])
async def list_role_definitions(
    project_id: str | None = Query(None, description="按项目 ID 过滤"),
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    scope: ProjectScope = Depends(get_project_scope),
    _perm: None = Depends(require_platform_permission("role_mgmt", "platform:read")),
) -> list[RoleDefOut]:
    """获取角色定义列表（含绑定计数与策略同步状态）。需要管理员认证。

    按管理员项目范围自动过滤。project_id 为 NULL 的平台级角色始终可见。
    policy_synced=false 表示表中有该角色但策略文件中没有，其绑定在判定时不生效。
    """
    from sqlalchemy import or_

    stmt = select(RoleDefinition)

    if project_id:
        stmt = stmt.where(
            or_(
                RoleDefinition.project_id == project_id,
                RoleDefinition.project_id.is_(None),
            )
        )
    elif not scope.is_platform_admin and scope.project_ids is not None:
        if scope.project_ids:
            stmt = stmt.where(
                or_(
                    RoleDefinition.project_id.in_(scope.project_ids),
                    RoleDefinition.project_id.is_(None),
                )
            )

    result = await db.execute(stmt.order_by(RoleDefinition.name))
    roles = result.scalars().all()

    counts = await _binding_counts(db, [r.name for r in roles])
    index = get_policy_index()

    out: list[RoleDefOut] = []
    for r in roles:
        permissions = _role_permissions(r.name, r.project_id)
        synced = (
            r.name in _KEYCLOAK_IDENTITY_ROLES
            or r.name in index.visible_roles(r.project_id)
        )
        out.append(_to_out(r, counts.get(r.name, 0), permissions, synced))
    return out


@router.get("/definitions/{name}", response_model=RoleDetailOut)
async def get_role_definition(
    name: str,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
) -> RoleDetailOut:
    """获取单个角色详情。需要管理员认证。"""
    result = await db.execute(
        select(RoleDefinition).where(RoleDefinition.name == name)
    )
    r = result.scalar_one_or_none()
    if r is None:
        raise HTTPException(status_code=404, detail=f"Role not found: {name}")

    counts = await _binding_counts(db, [name])
    index = get_policy_index()
    synced = (
        name in _KEYCLOAK_IDENTITY_ROLES
        or name in index.visible_roles(r.project_id)
    )
    return RoleDetailOut(
        **_to_out(
            r, counts.get(name, 0), _role_permissions(name, r.project_id), synced,
        ).model_dump()
    )


@router.post("/definitions", response_model=RoleDefOut, status_code=201)
async def create_role_definition(
    body: CreateRoleRequest,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    scope: ProjectScope = Depends(get_project_scope),
    _perm: None = Depends(require_platform_permission("role_mgmt", "platform:write")),
) -> RoleDefOut:
    """创建自定义角色。

    先写 Cerbos 策略文件再提交数据库；数据库提交失败时回滚文件，
    避免出现表中有角色而策略中没有的分叉状态。

    project_id=NULL → 平台级角色（写入策略根目录，全部项目可见）
    project_id 指定 → 项目级角色（写入该项目的策略目录）
    """
    if body.project_id and not scope.can_access(body.project_id):
        raise HTTPException(
            status_code=403, detail=f"No access to project '{body.project_id}'"
        )

    existing = await db.scalar(
        select(RoleDefinition).where(
            RoleDefinition.name == body.name,
            RoleDefinition.project_id.is_(None)
            if body.project_id is None
            else RoleDefinition.project_id == body.project_id,
        )
    )
    if existing:
        raise HTTPException(
            status_code=409, detail=f"Role already exists: {body.name}"
        )

    # 1. 写策略文件（动作无法归属到资源类型时在此拒绝）
    try:
        tx = write_role_policies(
            body.name, body.parent_keycloak_roles, body.permissions, body.project_id,
        )
    except PolicyWriteError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # 2. 写数据库；失败则回滚文件
    rd = RoleDefinition(
        name=body.name,
        description=body.description or "",
        parent_keycloak_roles=body.parent_keycloak_roles,
        permissions=list(body.permissions),
        is_system=False,
        project_id=body.project_id,
    )
    try:
        db.add(rd)
        await db.commit()
        await db.refresh(rd)
    except Exception as exc:
        await db.rollback()
        tx.restore()
        logger.warning("role_create_rolled_back", role=body.name, error=str(exc)[:200])
        raise HTTPException(
            status_code=500, detail=f"Role creation failed, policy files restored: {exc}"
        ) from exc

    invalidate_role_actions_cache()
    return _to_out(rd, 0, _role_permissions(rd.name, rd.project_id), True)


@router.put("/definitions/{name}", response_model=RoleDefOut)
async def update_role_definition(
    name: str,
    body: UpdateRoleRequest,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    scope: ProjectScope = Depends(get_project_scope),
    _perm: None = Depends(require_platform_permission("role_mgmt", "platform:write")),
) -> RoleDefOut:
    """更新自定义角色的描述、父角色与权限。

    补齐原先缺失的更新入口：此前修改权限只能删除重建，而有活跃绑定的角色
    不允许删除，导致这类角色的权限无法调整。

    内置角色（is_system=true）不可更新：其策略条件由手工维护，
    生成器无法复现（例如 rag_roles 中读 granted_actions 的表达式）。
    """
    r = await db.scalar(select(RoleDefinition).where(RoleDefinition.name == name))
    if r is None:
        raise HTTPException(status_code=404, detail=f"Role not found: {name}")
    if r.is_system:
        raise HTTPException(
            status_code=409,
            detail=f"Role '{name}' is a system role; edit its policy file directly.",
        )
    if r.project_id and not scope.can_access(r.project_id):
        raise HTTPException(
            status_code=403, detail=f"No access to project '{r.project_id}'"
        )

    parents = (
        body.parent_keycloak_roles
        if body.parent_keycloak_roles is not None
        else (r.parent_keycloak_roles or ["user"])
    )
    permissions = (
        body.permissions
        if body.permissions is not None
        else list(r.permissions or [])
    )

    try:
        tx = write_role_policies(name, parents, permissions, r.project_id)
    except PolicyWriteError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        if body.description is not None:
            r.description = body.description
        r.parent_keycloak_roles = parents
        r.permissions = permissions
        await db.commit()
        await db.refresh(r)
    except Exception as exc:
        await db.rollback()
        tx.restore()
        logger.warning("role_update_rolled_back", role=name, error=str(exc)[:200])
        raise HTTPException(
            status_code=500, detail=f"Role update failed, policy files restored: {exc}"
        ) from exc

    invalidate_role_actions_cache()
    counts = await _binding_counts(db, [name])
    return _to_out(
        r, counts.get(name, 0), _role_permissions(name, r.project_id), True,
    )


@router.delete("/definitions/{name}", status_code=204)
async def delete_role_definition(
    name: str,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    scope: ProjectScope = Depends(get_project_scope),
    _perm: None = Depends(require_platform_permission("role_mgmt", "platform:write")),
) -> None:
    """删除自定义角色（系统内置角色不可删除）。

    先清理策略文件再删除数据库记录；数据库失败时还原文件，
    避免出现策略中已无该角色而表中仍存在的分叉状态。
    """
    r = await db.scalar(select(RoleDefinition).where(RoleDefinition.name == name))
    if r is None:
        raise HTTPException(status_code=404, detail=f"Role not found: {name}")
    if r.is_system:
        raise HTTPException(
            status_code=403, detail=f"Cannot delete system role: {name}"
        )
    if r.project_id and not scope.can_access(r.project_id):
        raise HTTPException(
            status_code=403, detail=f"No access to project '{r.project_id}'"
        )

    counts = await _binding_counts(db, [name])
    if counts.get(name, 0) > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot delete role '{name}': it has active bindings. "
                "Unbind all users first."
            ),
        )

    try:
        tx = remove_role_policies(name, r.project_id)
    except PolicyWriteError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    try:
        await db.delete(r)
        await db.commit()
    except Exception as exc:
        await db.rollback()
        tx.restore()
        logger.warning("role_delete_rolled_back", role=name, error=str(exc)[:200])
        raise HTTPException(
            status_code=500, detail=f"Role deletion failed, policy files restored: {exc}"
        ) from exc

    invalidate_role_actions_cache()


@router.get("/permissions", response_model=PermissionMatrixOut)
async def get_permissions_matrix(
    project_id: str | None = Query(None, description="项目 ID，不传则返回全部"),
    admin: Principal = Depends(get_current_admin),
) -> PermissionMatrixOut:
    """获取角色-权限矩阵（从 Cerbos 策略解析）。需要管理员认证。

    - project_id 指定 → 该项目的角色加无项目归属的角色
    - project_id 不传 → 全部项目
    矩阵同时包含策略中直接出现的派生角色（source=cerbos）与 Keycloak 身份角色
    （source=keycloak，权限为其继承的派生角色并集）——与 /definitions 展示同源，
    保证"以策略文件为准，解析出来是什么就是什么"，消除 system_admin 显示无权限的误导。
    """
    matrix = parse_permissions_matrix(project_id or None)
    return PermissionMatrixOut(**matrix)
