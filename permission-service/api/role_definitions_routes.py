"""角色管理 API — 角色定义 CRUD + 权限矩阵。

设计依据：docs/manage_role_design.md §3.2 API 设计。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from models.role_definition import RoleDefinition
from models.role_binding import RoleBinding
from api.auth_routes import get_current_admin
from schemas.responses import Principal

router = APIRouter(prefix="/api/v1/roles", tags=["admin-roles"])


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
    created_at: str = ""


class RoleDetailOut(BaseModel):
    id: str
    name: str
    description: str
    parent_keycloak_roles: list[str] = []
    is_system: bool
    is_keycloak_role: bool = False
    permissions: list[str] = []
    binding_count: int = 0
    created_at: str = ""


class PermissionMatrixOut(BaseModel):
    roles: list[dict]


class CreateRoleRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]+$")
    description: str = Field("", max_length=512)
    parent_keycloak_roles: list[str] = Field(default=["user"])
    permissions: list[str] = Field(default=[], description="角色权限列表: ['kb:read', 'doc:view', ...]")


# ── 端点 ──


@router.get("/definitions", response_model=list[RoleDefOut])
async def list_role_definitions(
    db: AsyncSession = Depends(get_db),
) -> list[RoleDefOut]:
    """获取所有角色定义列表（含绑定计数）。"""
    stmt = select(RoleDefinition).order_by(RoleDefinition.name)
    result = await db.execute(stmt)
    roles = result.scalars().all()

    # 批量获取各角色的绑定数
    role_names = [r.name for r in roles]
    binding_counts: dict[str, int] = {}
    if role_names:
        bc_stmt = (
            select(RoleBinding.role, sa_func.count().label("cnt"))
            .where(
                RoleBinding.role.in_(role_names),
                RoleBinding.revoked == False,  # noqa: E712
            )
            .group_by(RoleBinding.role)
        )
        bc_result = await db.execute(bc_stmt)
        for row in bc_result:
            binding_counts[row.role] = row.cnt

    # 从 Cerbos YAML 解析权限矩阵（用于 Cerbos 派生角色）
    from services.cerbos_policy_parser import parse_permissions_matrix
    matrix = parse_permissions_matrix()
    cerbos_perms: dict[str, list[str]] = {}
    for ri in matrix.get("roles", []):
        cerbos_perms[ri["name"]] = ri.get("permissions", [])

    return [
        RoleDefOut(
            id=str(r.id),
            name=r.name,
            description=r.description or "",
            parent_keycloak_roles=r.parent_keycloak_roles or [],
            is_system=r.is_system,
            is_keycloak_role=(r.name in ("user", "system_admin")),
            permissions=(
                # Keycloak 身份角色：不显示权限（权限由继承的派生角色决定）
                [] if r.name in ("user", "system_admin")
                # 自定义角色：取自 DB 中显式存储的权限
                else list(r.permissions) if r.permissions
                # Cerbos 系统角色：从 YAML 解析
                else cerbos_perms.get(r.name, [])
            ),
            binding_count=binding_counts.get(r.name, 0),
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in roles
    ]


@router.get("/definitions/{name}", response_model=RoleDetailOut)
async def get_role_definition(
    name: str,
    db: AsyncSession = Depends(get_db),
) -> RoleDetailOut:
    """获取单个角色详情（含权限列表）。"""
    stmt = select(RoleDefinition).where(RoleDefinition.name == name)
    result = await db.execute(stmt)
    r = result.scalar_one_or_none()
    if r is None:
        raise HTTPException(status_code=404, detail=f"Role not found: {name}")

    # 解析权限矩阵
    from services.cerbos_policy_parser import parse_permissions_matrix
    matrix = parse_permissions_matrix()
    permissions: list[str] = []
    if r.name in ("user", "system_admin"):
        # Keycloak 身份角色：不显示权限
        permissions = []
    elif r.permissions:
        # 自定义角色：取自 DB 中显式存储的权限
        permissions = list(r.permissions)
    else:
        # Cerbos 系统角色：从 YAML 解析
        for role_info in matrix.get("roles", []):
            if role_info["name"] == name:
                permissions = role_info.get("permissions", [])
                break

    # 绑定计数
    bc_stmt = select(sa_func.count()).select_from(RoleBinding).where(
        RoleBinding.role == name,
        RoleBinding.revoked == False,  # noqa: E712
    )
    bc_result = await db.execute(bc_stmt)
    binding_count = bc_result.scalar() or 0

    return RoleDetailOut(
        id=str(r.id),
        name=r.name,
        description=r.description or "",
        parent_keycloak_roles=r.parent_keycloak_roles or [],
        is_system=r.is_system,
        is_keycloak_role=(r.name in ("user", "system_admin")),
        permissions=permissions,
        binding_count=binding_count,
        created_at=r.created_at.isoformat() if r.created_at else "",
    )


@router.post("/definitions", response_model=RoleDefOut, status_code=201)
async def create_role_definition(
    body: CreateRoleRequest,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
) -> RoleDefOut:
    """创建自定义角色（需 system_admin 权限）。

    同时写入 Cerbos YAML 策略文件，使角色在 Cerbos 判定中生效。
    """
    # 检查是否已存在
    existing = await db.execute(
        select(RoleDefinition).where(RoleDefinition.name == body.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Role already exists: {body.name}")

    # 权限以用户显式指定为准，不自动填充
    permissions = list(body.permissions) if body.permissions else []

    # 写入 role_definitions 表
    rd = RoleDefinition(
        name=body.name,
        description=body.description or "",
        parent_keycloak_roles=body.parent_keycloak_roles,
        permissions=permissions,
        is_system=False,
    )
    db.add(rd)
    await db.commit()
    await db.refresh(rd)

    # 写入 Cerbos YAML 策略文件
    try:
        _write_cerbos_yaml_for_role(
            body.name, body.parent_keycloak_roles, permissions
        )
    except Exception as e:
        import structlog
        _logger = structlog.get_logger(__name__)
        _logger.warning("cerbos_yaml_write_failed", role=body.name, error=str(e)[:200])
        # YAML 写入失败不阻止角色创建（管理员可手动编辑）

    return RoleDefOut(
        id=str(rd.id),
        name=rd.name,
        description=rd.description or "",
        parent_keycloak_roles=rd.parent_keycloak_roles or [],
        is_system=rd.is_system,
        is_keycloak_role=False,
        permissions=list(rd.permissions) if rd.permissions else [],
        binding_count=0,
        created_at=rd.created_at.isoformat() if rd.created_at else "",
    )


def _cleanup_cerbos_yaml_for_role(name: str) -> None:
    """删除角色时清理对应的 Cerbos YAML 文件。"""
    import os as _os, re as _re
    policies_dir = _os.getenv(
        "CERBOS_POLICIES_DIR",
        "/home/mfkcel/proj_rag_dev/cerbos/policies",
    )

    # 清理 derived_roles YAML 中的条目
    dr_path = _os.path.join(policies_dir, "derived_roles", "custom_roles.yaml")
    if _os.path.exists(dr_path):
        with open(dr_path) as f:
            content = f.read()
        # 移除该角色的定义块
        pattern = rf"    - name: {_re.escape(name)}\n.*?(?=\n    - name: |\Z)"
        new_content = _re.sub(pattern, "", content, flags=_re.DOTALL)
        new_content = _re.sub(r"\n{3,}", "\n\n", new_content)  # 清理多余空行
        with open(dr_path, "w") as f:
            f.write(new_content)

    # 清理 resource_policies YAML 文件
    rp_dir = _os.path.join(policies_dir, "resource_policies")
    if _os.path.exists(rp_dir):
        for suffix in [f"custom_kb_{name}.yaml", f"custom_doc_{name}.yaml"]:
            path = _os.path.join(rp_dir, suffix)
            if _os.path.exists(path):
                _os.remove(path)


def _write_cerbos_yaml_for_role(
    name: str, parent_roles: list[str], permissions: list[str],
) -> None:
    """为自定义角色生成 Cerbos YAML 策略文件。"""
    import os as _os

    policies_dir = _os.getenv(
        "CERBOS_POLICIES_DIR",
        "/home/mfkcel/proj_rag_dev/cerbos/policies",
    )

    # ── 1. Derived roles ──
    dr_dir = _os.path.join(policies_dir, "derived_roles")
    _os.makedirs(dr_dir, exist_ok=True)
    custom_dr_path = _os.path.join(dr_dir, "custom_roles.yaml")

    existing_dr = ""
    if _os.path.exists(custom_dr_path):
        with open(custom_dr_path) as f:
            existing_dr = f.read()

    dr_entry = f"""    - name: {name}
      parentRoles: {parent_roles}
      condition:
        match:
          expr: "true"
"""

    if name not in existing_dr:
        if not existing_dr:
            new_dr = f"""# Auto-generated custom roles - DO NOT EDIT MANUALLY
# Managed by Permission Service role management API
apiVersion: api.cerbos.dev/v1
derivedRoles:
  name: custom_roles
  definitions:
{dr_entry}"""
        else:
            new_dr = existing_dr.rstrip() + "\n" + dr_entry
        with open(custom_dr_path, "w") as f:
            f.write(new_dr)

    # ── 2. Resource policies ──
    kb_actions = [p for p in permissions if p.startswith("kb:")]
    doc_actions = [p for p in permissions if p.startswith("doc:")]

    rp_dir = _os.path.join(policies_dir, "resource_policies")
    _os.makedirs(rp_dir, exist_ok=True)

    if kb_actions:
        _append_resource_policy(rp_dir, f"custom_kb_{name}.yaml",
                               "kb", name, kb_actions)

    if doc_actions:
        _append_resource_policy(rp_dir, f"custom_doc_{name}.yaml",
                               "document", name, doc_actions)


def _append_resource_policy(rp_dir: str, filename: str,
                            resource: str, role: str, actions: list[str]) -> None:
    """生成单个资源策略 YAML 文件。"""
    import os as _os
    path = _os.path.join(rp_dir, filename)
    yaml_content = f"""# Auto-generated custom role policy for {role}
apiVersion: api.cerbos.dev/v1
resourcePolicy:
  version: "default"
  resource: "{resource}"
  importDerivedRoles:
    - custom_roles
  rules:
    - actions: {actions}
      effect: EFFECT_ALLOW
      derivedRoles: ["{role}"]
"""
    with open(path, "w") as f:
        f.write(yaml_content)


@router.delete("/definitions/{name}", status_code=204)
async def delete_role_definition(
    name: str,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
) -> None:
    """删除自定义角色（系统内置角色不可删除）。"""
    stmt = select(RoleDefinition).where(RoleDefinition.name == name)
    result = await db.execute(stmt)
    r = result.scalar_one_or_none()
    if r is None:
        raise HTTPException(status_code=404, detail=f"Role not found: {name}")
    if r.is_system:
        raise HTTPException(
            status_code=403,
            detail=f"System role '{name}' cannot be deleted.",
        )

    # 检查是否有活跃绑定
    bc_stmt = select(sa_func.count()).select_from(RoleBinding).where(
        RoleBinding.role == name,
        RoleBinding.revoked == False,  # noqa: E712
    )
    bc_result = await db.execute(bc_stmt)
    if (bc_result.scalar() or 0) > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete role '{name}': it has active bindings. Unbind all users first.",
        )

    await db.delete(r)
    await db.commit()

    # 清理 Cerbos YAML 策略文件
    try:
        _cleanup_cerbos_yaml_for_role(name)
    except Exception as e:
        import structlog
        _logger = structlog.get_logger(__name__)
        _logger.warning("cerbos_yaml_cleanup_failed", role=name, error=str(e)[:200])

@router.get("/permissions", response_model=PermissionMatrixOut)
async def get_permissions_matrix() -> PermissionMatrixOut:
    """获取完整的角色-权限矩阵（从 Cerbos 策略 YAML 解析）。

    只读视图。仅包含权限角色（Cerbos 派生角色 + 自定义角色），
    不包含 Keycloak 身份角色（user / system_admin）。
    """
    from services.cerbos_policy_parser import parse_permissions_matrix
    matrix = parse_permissions_matrix()
    # 过滤掉 Keycloak 身份角色
    matrix["roles"] = [r for r in matrix["roles"] if r.get("source") != "keycloak"]
    return PermissionMatrixOut(**matrix)
