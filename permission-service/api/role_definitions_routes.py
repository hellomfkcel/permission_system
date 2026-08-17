"""角色管理 API — 角色定义 CRUD + 权限矩阵。

设计依据：docs/permission_model_v2.md §3 数据来源边界。

一致性约定：
- 权限的唯一权威源是 Cerbos 策略文件。role_definitions 表只保存档案信息
  （描述、激活角色、项目归属、是否内置），表里已无 permissions 列
  （迁移 f1a2b3c4d5e6），因此不存在需要同步的副本。
- 写路径先写策略文件，再提交数据库；数据库失败时回滚文件。
- 读路径统一走 _role_view()，管理台展示与判定链路取值同源。

作用域约定（修复"无策略项目显示其他项目权限"）：
角色的权限按**查询所处的项目**解析，而不是按角色自身的 project_id。
平台级角色（project_id IS NULL）在 demo-project 下只应显示 demo-project 与
平台层策略授予它的动作；此前一律按全局并集计算，rag-v14 的 kb:read / doc:*
会出现在一个连策略目录都没有的项目里。
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
    describe_role,
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
    # 激活该角色的身份角色。字段名保留以兼容既有前端，语义是"激活条件"而非
    # "权限继承"：activated_by 是同一份数据的正名。
    parent_keycloak_roles: list[str] = []
    activated_by: list[str] = []
    kind: str = "derived"           # derived=派生角色（持权） identity=身份角色（入场资格）
    activation: str = "identity"    # identity | grant | acl，见 cerbos_policy_parser
    is_system: bool
    is_keycloak_role: bool = False  # Keycloak 身份角色（user/system_admin）
    permissions: list[str] = []     # 查询作用域内策略授予的动作
    conditional_permissions: list[str] = []  # 其中需运行时授权记录才生效的子集
    binding_count: int = 0
    project_id: str | None = None   # NULL=平台级角色，否则为项目级角色
    policy_synced: bool = True      # 策略文件中是否存在该角色（计算得出，不入库）
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
    """返回角色在指定作用域内被策略直接授予的动作。

    取值只来自 Cerbos 策略文件（唯一权威源），且不做 parentRoles 并集 ——
    parentRoles 是激活条件，把子角色权限并给父角色会让 user 显示成全权角色，
    反过来又让"继承了 user 的" kb_reader 看起来权限反而更少。
    """
    return get_role_effective_permissions(name, project_id)


def _to_out(
    r: RoleDefinition,
    binding_count: int,
    scope_project_id: str | None,
) -> RoleDefOut:
    """组装角色视图。

    Args:
        scope_project_id: 查询所处的项目作用域（不是角色自身的 project_id），
            权限按它解析，避免跨项目串味。
    """
    view = describe_role(r.name, scope_project_id)
    index = get_policy_index()
    synced = (
        r.name in _KEYCLOAK_IDENTITY_ROLES
        or r.name in index.visible_roles(scope_project_id)
    )
    return RoleDefOut(
        id=str(r.id),
        name=r.name,
        description=r.description or "",
        parent_keycloak_roles=view["activated_by"] or (r.parent_keycloak_roles or []),
        activated_by=view["activated_by"] or (r.parent_keycloak_roles or []),
        kind=view["kind"],
        activation=view["activation"],
        is_system=r.is_system,
        is_keycloak_role=(r.name in _KEYCLOAK_IDENTITY_ROLES),
        permissions=view["permissions"],
        conditional_permissions=view["conditional_permissions"],
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

    # 权限按查询作用域解析：项目模式下平台级角色只显示该项目内的动作。
    return [_to_out(r, counts.get(r.name, 0), project_id) for r in roles]


@router.get("/definitions/{name}", response_model=RoleDetailOut)
async def get_role_definition(
    name: str,
    project_id: str | None = Query(
        None, description="查询作用域；不传则按角色自身归属解析权限",
    ),
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
) -> RoleDetailOut:
    """获取单个角色详情。需要管理员认证。

    权限按 project_id 指定的作用域解析，与列表页取值同源；不传时退回角色
    自身的 project_id（平台级角色即全局视图）。
    """
    result = await db.execute(
        select(RoleDefinition).where(RoleDefinition.name == name)
    )
    r = result.scalar_one_or_none()
    if r is None:
        raise HTTPException(status_code=404, detail=f"Role not found: {name}")

    counts = await _binding_counts(db, [name])
    scope = project_id or r.project_id
    return RoleDetailOut(**_to_out(r, counts.get(name, 0), scope).model_dump())


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
    # 表里不存 permissions：动作已写进策略文件，那是唯一权威源。
    rd = RoleDefinition(
        name=body.name,
        description=body.description or "",
        parent_keycloak_roles=body.parent_keycloak_roles,
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
    return _to_out(rd, 0, rd.project_id)


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
    # 未指定权限时，从策略索引取该角色当前的动作作为基线 ——
    # DB 里已不存权限副本，策略文件就是当前值。
    permissions = (
        body.permissions
        if body.permissions is not None
        else _role_permissions(name, r.project_id)
    )

    try:
        tx = write_role_policies(name, parents, permissions, r.project_id)
    except PolicyWriteError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        if body.description is not None:
            r.description = body.description
        r.parent_keycloak_roles = parents
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
    return _to_out(r, counts.get(name, 0), r.project_id)


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
