"""管理台 API — ACL 权限授予/回收/查询。

设计依据：docs/外部系统设计.md §2.4.4 管理台专用 API。
"""

import csv
import io
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from pydantic import BaseModel, Field
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from models.acl import ACLEntry
from models.role_binding import RoleBinding
from services.event_publisher import get_event_publisher
from api.auth_routes import get_current_admin, get_project_scope, ProjectScope, require_platform_permission
from schemas.responses import Principal
from app.role_actions_config import (
    VALID_ACTIONS,
    VALID_RESOURCE_TYPES,
    get_valid_actions,
    get_valid_resource_types,
)

router = APIRouter(prefix="/api/v1/acl", tags=["admin-acl"])


# ── 请求/响应模型 ──


class GrantRequest(BaseModel):
    tenant_id: str = Field(...)
    principal: str = Field(..., description="user:xxx | group:xxx | role:xxx")
    resource_type: str = Field(..., description="kb | document | platform | ...")
    resource_id: str = Field(...)
    action: str = Field(..., description="kb:read | doc:view | platform:read | ...")
    granted_by: str = Field(...)
    expires_at: str | None = Field(None, description="过期时间 ISO 8601")
    project_id: str | None = Field(
        None,
        description="所属项目 ID。项目层资源必填；平台层资源（platform / "
                    "project_permission）必须留空 —— 平台授权是平台级的。",
    )


class GrantResponse(BaseModel):
    grant_id: str
    version: int


class RevokeRequest(BaseModel):
    principal: str
    resource_type: str
    resource_id: str
    action: str
    project_id: str | None = Field(
        None,
        description="所属项目 ID。唯一性按项目隔离后，跨项目同名资源需靠此定位；"
                    "不传时要求匹配结果唯一。",
    )


class ACLEntryOut(BaseModel):
    id: str
    project_id: str = ""                    # 所属项目 ID
    tenant_id: str
    principal: str
    resource_type: str
    resource_id: str
    action: str
    granted_by: str
    granted_at: str
    expires_at: str | None
    revoked: bool


# ── 授权层级校验 ──


async def _validate_grant_scope(
    db: AsyncSession,
    scope: ProjectScope,
    resource_type: str,
    action: str,
    project_id: str | None,
) -> str | None:
    """校验授权记录与资源所在层级对齐，返回落库用的 project_id。

    层级由策略文件所在的命名空间决定（唯一权威源，见 cerbos_policy_parser）：

      平台层资源（platform / project_permission）
        → 授权是平台级的，project_id 必须为空。
          否则一条挂在某个项目下的 platform 授权会被平台判定路径读到，
          等于用项目级权限换到了平台级权限。
      项目层资源（kb / document / 各项目自定义类型）
        → 必须带 project_id，且该项目要存在、在管理员范围内、
          并且这个动作确实在该项目的策略里声明过。

    Raises:
        HTTPException 403/404/422
    """
    from services.cerbos_policy_parser import get_resource_layer
    from app.role_actions_config import get_resource_actions

    layer = get_resource_layer(resource_type)

    if layer == "unknown":
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown resource type '{resource_type}': no Cerbos policy declares it."
            ),
        )

    if layer == "platform":
        if project_id:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Resource type '{resource_type}' is a platform-layer resource; "
                    "its grants are platform-scoped and must omit project_id."
                ),
            )
        if action not in get_resource_actions(None).get(resource_type, []):
            raise HTTPException(
                status_code=422,
                detail=f"Action '{action}' is not declared for '{resource_type}'.",
            )
        return None

    # ── 项目层 ──
    if not project_id:
        raise HTTPException(
            status_code=422,
            detail=f"project_id is required for project-layer resource '{resource_type}'.",
        )
    if not scope.can_access(project_id):
        raise HTTPException(
            status_code=403, detail=f"No access to project '{project_id}'"
        )

    from models.project import Project
    proj = await db.scalar(select(Project.id).where(Project.id == project_id))
    if not proj:
        raise HTTPException(
            status_code=404, detail=f"Project '{project_id}' not found"
        )

    declared = get_resource_actions(project_id).get(resource_type, [])
    if action not in declared:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Action '{action}' is not declared for resource type "
                f"'{resource_type}' in project '{project_id}'."
            ),
        )
    return project_id


# ── 端点 ──


@router.post("/grant", response_model=GrantResponse)
async def grant_acl(
    body: GrantRequest,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    scope: ProjectScope = Depends(get_project_scope),
    _perm: None = Depends(require_platform_permission("permission_mgmt", "platform:write")),
) -> GrantResponse:
    """授予权限（需要管理员认证）。

    流程：
    1. 验证管理员身份（JWT Bearer token）
    2. 校验授权层级与项目范围（_validate_grant_scope）
    3. 检查是否已有相同 active 记录
    4. INSERT acl_entries（granted_by 使用管理员身份）
    5. 递增 global_permission_version
    6. 发布 VisibilityChanged 事件
    """
    project_id = await _validate_grant_scope(
        db, scope, body.resource_type, body.action, body.project_id,
    )

    # 检查重复（唯一性按项目隔离，不同项目的同名资源互不冲突）
    stmt = select(ACLEntry).where(
        ACLEntry.project_id.is_(None)
        if project_id is None
        else ACLEntry.project_id == project_id,
        ACLEntry.principal == body.principal,
        ACLEntry.resource_type == body.resource_type,
        ACLEntry.resource_id == body.resource_id,
        ACLEntry.action == body.action,
        ACLEntry.revoked == False,  # noqa: E712
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="grant already exists")

    # 解析过期时间
    expires_at = None
    if body.expires_at:
        expires_at = datetime.fromisoformat(body.expires_at)

    # INSERT — granted_by 使用已验证的管理员身份
    new_id = uuid.uuid4()
    entry = ACLEntry(
        id=new_id,
        project_id=project_id,
        tenant_id=body.tenant_id,
        principal=body.principal,
        resource_type=body.resource_type,
        resource_id=body.resource_id,
        action=body.action,
        granted_by=f"user:{admin.user_id}",  # 从 JWT 提取，忽略请求中的 granted_by
        expires_at=expires_at,
    )
    db.add(entry)

    # ★ Outbox 模式（设计依据 §3.2）：
    # 在同一事务内写 ACL + permission_changes，原子提交
    publisher = get_event_publisher()
    kb_id = body.resource_id if body.resource_type == "kb" else None
    version, change_id = await publisher.write_change_log(
        db,
        tenant_id=body.tenant_id,
        resource_type=body.resource_type,
        resource_id=body.resource_id,
        project_id=project_id,
        kb_id=kb_id,
        change_detail={
            "action": "acl_granted",
            "principal": body.principal,
            "permission": body.action,
            "project_id": project_id,
        },
    )
    await db.commit()  # ACL + change_log 原子提交

    # 事务提交后异步发布 Redis（失败不影响已提交数据）
    await publisher.publish_event(
        change_id, version, body.tenant_id,
        body.resource_type, body.resource_id,
        kb_id=kb_id,
        change_detail={
            "action": "acl_granted",
            "principal": body.principal,
            "permission": body.action,
            "project_id": project_id,
        },
    )

    return GrantResponse(grant_id=str(new_id), version=version)


@router.post("/revoke")
async def revoke_acl(
    body: RevokeRequest,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    scope: ProjectScope = Depends(get_project_scope),
    _perm: None = Depends(require_platform_permission("permission_mgmt", "platform:write")),
) -> dict:
    """回收权限（需要管理员认证）。"""
    conditions = [
        ACLEntry.principal == body.principal,
        ACLEntry.resource_type == body.resource_type,
        ACLEntry.resource_id == body.resource_id,
        ACLEntry.action == body.action,
        ACLEntry.revoked == False,  # noqa: E712
    ]
    if body.project_id:
        conditions.append(ACLEntry.project_id == body.project_id)
    result = await db.execute(select(ACLEntry).where(*conditions))
    entries = result.scalars().all()

    if not entries:
        raise HTTPException(status_code=404, detail="grant not found")
    if len(entries) > 1:
        raise HTTPException(
            status_code=409,
            detail=(
                "Multiple grants match across projects: "
                f"{sorted({e.project_id for e in entries})}. Specify project_id."
            ),
        )
    entry = entries[0]

    # 验证项目访问权限
    if not scope.can_access(entry.project_id):
        raise HTTPException(status_code=403, detail=f"No access to project '{entry.project_id}'")

    entry.revoked = True
    entry.revoked_at = datetime.now(timezone.utc)

    # ★ Outbox 模式：同一事务内写 revoke + permission_changes
    publisher = get_event_publisher()
    version, change_id = await publisher.write_change_log(
        db,
        tenant_id=entry.tenant_id,
        resource_type=body.resource_type,
        resource_id=body.resource_id,
        project_id=entry.project_id,
        change_detail={
            "action": "acl_revoked",
            "principal": body.principal,
            "permission": body.action,
            "project_id": entry.project_id,
        },
    )
    await db.commit()

    await publisher.publish_event(
        change_id, version, entry.tenant_id,
        body.resource_type, body.resource_id,
        change_detail={
            "action": "acl_revoked",
            "principal": body.principal,
            "permission": body.action,
            "project_id": entry.project_id,
        },
    )

    return {"grant_id": str(entry.id), "result": "revoked", "version": version}


@router.get("", response_model=list[ACLEntryOut])
async def list_acl(
    resource_type: str | None = Query(None),
    resource_id: str | None = Query(None),
    principal: str | None = Query(None),
    revoked: bool | None = Query(None),
    project_id: str | None = Query(None, description="按项目 ID 过滤"),
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    scope: ProjectScope = Depends(get_project_scope),
    _perm: None = Depends(require_platform_permission("permission_mgmt", "platform:read")),
) -> list[ACLEntryOut]:
    """查询 ACL 列表（需要管理员认证）。按管理员项目范围自动过滤。"""
    conditions = []
    if resource_type:
        conditions.append(ACLEntry.resource_type == resource_type)
    if resource_id:
        conditions.append(ACLEntry.resource_id == resource_id)
    if principal:
        conditions.append(ACLEntry.principal == principal)
    if revoked is not None:
        conditions.append(ACLEntry.revoked == revoked)

    # 项目范围过滤（platform_admin 不过滤）
    # 平台级条目（project_id=NULL）始终对所有管理员可见
    from sqlalchemy import or_
    if project_id:
        conditions.append(
            or_(ACLEntry.project_id == project_id, ACLEntry.project_id.is_(None))
        )
    elif not scope.is_platform_admin:
        scope_filter = scope.filter_condition(ACLEntry)
        if scope_filter is not None:
            conditions.append(
                or_(scope_filter, ACLEntry.project_id.is_(None))
            )

    stmt = select(ACLEntry).where(*conditions).order_by(ACLEntry.granted_at.desc())
    result = await db.execute(stmt)
    entries = result.scalars().all()

    return [
        ACLEntryOut(
            id=str(e.id),
            project_id=e.project_id or "",
            tenant_id=e.tenant_id,
            principal=e.principal,
            resource_type=e.resource_type,
            resource_id=e.resource_id,
            action=e.action,
            granted_by=e.granted_by,
            granted_at=e.granted_at.isoformat() if e.granted_at else "",
            expires_at=e.expires_at.isoformat() if e.expires_at else None,
            revoked=e.revoked,
        )
        for e in entries
    ]


# ── 资源实例 ACL 投影 ──


class ResourceACLOut(BaseModel):
    """判定期注入 Cerbos 的 resource.attr.acl / role_acl 原样投影。"""
    resource_type: str
    resource_id: str
    acl: dict[str, list[str]] = Field(
        default_factory=dict,
        description="用户级：{principal: [action, ...]}，principal 含 user:/group: 前缀",
    )
    role_acl: dict[str, list[str]] = Field(
        default_factory=dict, description="角色级：{role: [action, ...]}",
    )


@router.get("/resources/{resource_type}/{resource_id}", response_model=ResourceACLOut)
async def get_resource_acl_view(
    resource_type: str,
    resource_id: str,
    project_id: str | None = Query(None, description="按项目 ID 过滤"),
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    scope: ProjectScope = Depends(get_project_scope),
    _perm: None = Depends(require_platform_permission("permission_mgmt", "platform:read")),
) -> ResourceACLOut:
    """查看某个资源实例上的 ACL —— 即 Cerbos 判定时看到的 acl / role_acl。

    这是 acl_entries 的一个只读投影，不是另一份存储：写入仍走
    POST /api/v1/acl/grant，撤销走 /revoke。同一事实只有一处出处，
    因此这里显示的内容与判定期使用的内容不可能不一致。

    与 GET /api/v1/acl?resource_id=... 的区别：那个返回授权记录的原始行，
    这个按 Cerbos 的注入格式分组（用户级 / 角色级），用于排查"为什么这条
    ACL 没生效"。
    """
    if project_id and not scope.can_access(project_id):
        raise HTTPException(
            status_code=403, detail=f"No access to project '{project_id}'"
        )

    now = datetime.now(timezone.utc)
    conditions = [
        ACLEntry.resource_type == resource_type,
        ACLEntry.resource_id == resource_id,
        ACLEntry.revoked == False,  # noqa: E712
        or_(ACLEntry.expires_at.is_(None), ACLEntry.expires_at > now),
    ]
    if project_id:
        conditions.append(ACLEntry.project_id == project_id)

    result = await db.execute(
        select(ACLEntry.principal, ACLEntry.action).where(*conditions)
    )

    acl: dict[str, list[str]] = {}
    role_acl: dict[str, list[str]] = {}
    for entry_principal, action in result.fetchall():
        if entry_principal.startswith("role:"):
            bucket = role_acl.setdefault(entry_principal.split(":", 1)[1], [])
        else:
            bucket = acl.setdefault(entry_principal, [])
        if action not in bucket:
            bucket.append(action)

    return ResourceACLOut(
        resource_type=resource_type,
        resource_id=resource_id,
        acl={k: sorted(v) for k, v in sorted(acl.items())},
        role_acl={k: sorted(v) for k, v in sorted(role_acl.items())},
    )


# ── 有效权限计算 ──


class EffectivePermission(BaseModel):
    principal: str
    resource_type: str
    resource_id: str
    effective_actions: list[str] = Field(default_factory=list)
    source: str = Field("", description="acl | role_binding | combined")


@router.get("/effective", response_model=list[EffectivePermission])
async def get_effective_permissions(
    principal: str | None = Query(None, description="主体: user:xxx | group:xxx"),
    resource_type: str | None = Query(None),
    resource_id: str | None = Query(None),
    project_id: str | None = Query(None, description="按项目 ID 过滤"),
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    scope: ProjectScope = Depends(get_project_scope),
    _perm: None = Depends(require_platform_permission("permission_mgmt", "platform:read")),
) -> list[EffectivePermission]:
    """计算某主体对资源的有效权限（合并 ACL + 角色绑定 + 封禁）。需要管理员认证。

    设计依据：docs/外部系统设计.md §2.4.4 ACL 管理 — 有效权限计算。
    """
    conditions = [ACLEntry.revoked == False]  # noqa: E712
    if principal:
        conditions.append(ACLEntry.principal == principal)
    if resource_type:
        conditions.append(ACLEntry.resource_type == resource_type)
    if resource_id:
        conditions.append(ACLEntry.resource_id == resource_id)
    if project_id:
        conditions.append(
            or_(ACLEntry.project_id == project_id, ACLEntry.project_id.is_(None))
        )
    elif not scope.is_platform_admin:
        scope_filter = scope.filter_condition(ACLEntry)
        if scope_filter is not None:
            conditions.append(
                or_(scope_filter, ACLEntry.project_id.is_(None))
            )

    stmt = select(ACLEntry).where(*conditions).order_by(ACLEntry.granted_at.desc())
    result = await db.execute(stmt)
    entries = result.scalars().all()

    # 按 (principal, resource_type, resource_id) 聚合
    from collections import defaultdict
    grouped: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    entry_projects: dict[tuple[str, str, str], str | None] = {}
    for e in entries:
        key = (e.principal, e.resource_type, e.resource_id)
        if e.action not in grouped[key]:
            grouped[key].append(e.action)
        # 项目级条目的归属优先于平台级（NULL），用于后续按项目展开角色绑定
        if e.project_id or key not in entry_projects:
            entry_projects[key] = e.project_id

    from services.cerbos_policy_parser import get_role_actions_map

    output: list[EffectivePermission] = []
    for (p, rt, rid), actions in grouped.items():
        # 该条目所属项目 —— 角色绑定的展开必须限定在同一项目内。
        # 此前这里既不按项目过滤绑定，也用全局的角色→动作映射，
        # 于是别的项目的绑定、别的项目策略里的动作都会混进结果。
        entry_project = entry_projects.get((p, rt, rid))

        role_conditions = [
            RoleBinding.principal == p,
            RoleBinding.revoked == False,  # noqa: E712
        ]
        if entry_project:
            # 平台级绑定（project_id IS NULL）对所有项目生效，一并计入
            role_conditions.append(
                or_(
                    RoleBinding.project_id == entry_project,
                    RoleBinding.project_id.is_(None),
                )
            )
        else:
            role_conditions.append(RoleBinding.project_id.is_(None))

        role_result = await db.execute(select(RoleBinding).where(*role_conditions))
        role_bindings = role_result.scalars().all()

        # 角色→动作映射同样按该项目作用域解析
        role_actions_map = get_role_actions_map(entry_project)

        source = "acl"
        for rb in role_bindings:
            source = "combined" if source == "acl" else source
            for a in role_actions_map.get(rb.role, []):
                if a not in actions:
                    actions.append(a)

        output.append(EffectivePermission(
            principal=p,
            resource_type=rt,
            resource_id=rid,
            effective_actions=sorted(actions),
            source=source,
        ))

    return sorted(output, key=lambda x: (x.principal, x.resource_type, x.resource_id))


# ── 批量授予 ──


class BatchGrantRequest(BaseModel):
    grants: list[GrantRequest] = Field(..., max_length=100, description="批量授予列表，≤100 条")


class BatchGrantResult(BaseModel):
    success: int = 0
    failed: int = 0
    results: list[dict] = Field(default_factory=list)


@router.post("/batch-grant", response_model=BatchGrantResult)
async def batch_grant_acl(
    body: BatchGrantRequest,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    scope: ProjectScope = Depends(get_project_scope),
    _perm: None = Depends(require_platform_permission("permission_mgmt", "platform:write")),
) -> BatchGrantResult:
    """批量授予权限（需要管理员认证）。

    逐条独立处理，单条失败不影响其余。
    最大 100 条/次。
    """
    results: list[dict] = []
    success = 0
    failed = 0

    for grant in body.grants:
        try:
            # 与单条 grant 走同一套层级 + 项目范围校验，避免批量端点成为绕行入口
            pid = await _validate_grant_scope(
                db, scope, grant.resource_type, grant.action,
                getattr(grant, "project_id", None) or None,
            )

            # 检查重复（唯一性按项目隔离）
            stmt = select(ACLEntry).where(
                ACLEntry.project_id.is_(None)
                if pid is None
                else ACLEntry.project_id == pid,
                ACLEntry.principal == grant.principal,
                ACLEntry.resource_type == grant.resource_type,
                ACLEntry.resource_id == grant.resource_id,
                ACLEntry.action == grant.action,
                ACLEntry.revoked == False,  # noqa: E712
            )
            result = await db.execute(stmt)
            existing = result.scalar_one_or_none()
            if existing:
                results.append({"principal": grant.principal, "action": grant.action, "status": "skipped", "reason": "duplicate"})
                success += 1
                continue

            expires_at = datetime.fromisoformat(grant.expires_at) if grant.expires_at else None
            new_id = uuid.uuid4()
            entry = ACLEntry(
                id=new_id,
                project_id=pid,  # _validate_grant_scope 归一后的值（平台层为 None）
                tenant_id=grant.tenant_id,
                principal=grant.principal,
                resource_type=grant.resource_type,
                resource_id=grant.resource_id,
                action=grant.action,
                granted_by=f"user:{admin.user_id}",
                expires_at=expires_at,
            )
            db.add(entry)
            results.append({"principal": grant.principal, "action": grant.action, "status": "granted"})
            success += 1
        except HTTPException as e:
            # 校验类失败（层级不符 / 无项目权限 / 动作未声明）逐条报出原因
            detail = e.detail if isinstance(e.detail, str) else str(e.detail)
            results.append({"principal": grant.principal, "action": grant.action, "status": "failed", "reason": detail[:200]})
            failed += 1
        except Exception as e:
            results.append({"principal": grant.principal, "action": grant.action, "status": "failed", "reason": str(e)[:100]})
            failed += 1

    # ★ Outbox 模式：同一事务内写 ACL + permission_changes
    publisher = get_event_publisher()
    version = 0
    change_id = None
    if success > 0:
        version, change_id = await publisher.write_change_log(
            db,
            tenant_id=body.grants[0].tenant_id,
            resource_type=body.grants[0].resource_type,
            resource_id=body.grants[0].resource_id,
            project_id=body.grants[0].project_id,
            event_type="ACL_BATCH_GRANTED",
            change_detail={"count": success, "failed": failed},
        )
    await db.commit()

    # 事务提交后异步发布 Redis
    if success > 0 and change_id:
        await publisher.publish_event(
            change_id, version, body.grants[0].tenant_id,
            body.grants[0].resource_type, body.grants[0].resource_id,
            event_type="ACL_BATCH_GRANTED",
            change_detail={"count": success, "failed": failed},
        )

    return BatchGrantResult(success=success, failed=failed, results=results)


# ══════════════════════════════════════════════════════════════
# P2-2: CSV 批量导入 ACL
# ══════════════════════════════════════════════════════════════


class CSVImportResult(BaseModel):
    """CSV 导入结果。"""
    total_rows: int = 0
    success: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[dict] = Field(default_factory=list)


# 有效的 action / resource_type 取值随目标项目的 Cerbos 策略变化，
# 在导入时按 project_id 动态解析，不使用 kb/doc 硬编码集合。


@router.post("/import-csv", response_model=CSVImportResult)
async def import_acl_csv(
    file: UploadFile = File(...),
    project_id: str = Query(..., description="目标项目 ID"),
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    scope: ProjectScope = Depends(get_project_scope),
    _perm: None = Depends(require_platform_permission("permission_mgmt", "platform:write")),
) -> CSVImportResult:
    """CSV 批量导入 ACL 权限。

    CSV 格式（header 行必须）：
        principal,resource_type,resource_id,action,expires_at

    约束：
    - 最大 1000 行/次
    - 逐行校验（格式、action 合法值、resource_type 合法值）
    - 单行失败不影响其余
    - 返回详细错误报告

    设计依据：docs/外部系统设计.md §3.3 /permissions 页面 — 批量操作
              + docs/权限管理系统架构设计.md §2.1 动词目录。
    """
    # 验证项目访问权限
    if not scope.can_access(project_id):
        raise HTTPException(status_code=403, detail=f"No access to project '{project_id}'")

    # 读取文件内容
    try:
        content = await file.read()
        text = content.decode("utf-8-sig")  # 支持 BOM
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="CSV file must be UTF-8 encoded")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(status_code=422, detail="CSV file is empty or missing header row")

    # 校验必要列
    required_cols = {"principal", "resource_type", "resource_id", "action"}
    header_set = {h.strip().lower() for h in reader.fieldnames}
    missing = required_cols - header_set
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Missing required columns: {', '.join(sorted(missing))}. "
                   f"Required: principal, resource_type, resource_id, action. Optional: expires_at",
        )

    rows = list(reader)
    if len(rows) > 1000:
        raise HTTPException(status_code=422, detail=f"Max 1000 rows per import, got {len(rows)}")

    # 目标项目的合法取值（来自该项目的 Cerbos 资源策略）
    valid_actions = get_valid_actions(project_id)
    valid_resource_types = get_valid_resource_types(project_id)

    total_rows = len(rows)
    success = 0
    skipped = 0
    failed = 0
    errors: list[dict] = []

    for i, row in enumerate(rows):
        row_num = i + 2  # CSV 行号（header=1, data starts at 2）
        principal = (row.get("principal") or "").strip()
        resource_type = (row.get("resource_type") or "").strip().lower()
        resource_id = (row.get("resource_id") or "").strip()
        action = (row.get("action") or "").strip()
        expires_at_raw = (row.get("expires_at") or "").strip() or None

        # 字段校验
        row_errors = []
        if not principal:
            row_errors.append("principal is required")
        if resource_type not in valid_resource_types:
            row_errors.append(
                f"resource_type '{resource_type}' is not defined in project "
                f"'{project_id}' policies (available: {', '.join(sorted(valid_resource_types))})"
            )
        if not resource_id:
            row_errors.append("resource_id is required")
        if action not in valid_actions:
            row_errors.append(
                f"action '{action}' is not defined in project '{project_id}' policies "
                f"(available: {', '.join(sorted(valid_actions))})"
            )

        # 过期时间解析
        expires_at = None
        if expires_at_raw:
            try:
                expires_at = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00"))
                if expires_at < datetime.now(timezone.utc):
                    row_errors.append(f"expires_at is in the past: {expires_at_raw}")
            except ValueError:
                row_errors.append(f"Invalid expires_at format: {expires_at_raw} (expected ISO 8601)")

        if row_errors:
            failed += 1
            errors.append({"row": row_num, "principal": principal, "errors": row_errors})
            continue

        # 检查重复
        try:
            stmt = select(ACLEntry).where(
                ACLEntry.principal == principal,
                ACLEntry.resource_type == resource_type,
                ACLEntry.resource_id == resource_id,
                ACLEntry.action == action,
                ACLEntry.revoked == False,  # noqa: E712
            )
            result = await db.execute(stmt)
            if result.scalar_one_or_none():
                skipped += 1
                continue

            # 创建 ACL 条目
            new_id = uuid.uuid4()
            entry = ACLEntry(
                id=new_id,
                project_id=project_id,
                tenant_id=admin.tenant_id or "tenant-dev",
                principal=principal,
                resource_type=resource_type,
                resource_id=resource_id,
                action=action,
                granted_by=f"user:{admin.user_id}",
                expires_at=expires_at,
            )
            db.add(entry)
            success += 1
        except Exception as e:
            failed += 1
            errors.append({"row": row_num, "principal": principal, "errors": [str(e)[:200]]})

    # Outbox 模式：同一事务内写事件日志
    publisher = get_event_publisher()
    if success > 0:
        version, change_id = await publisher.write_change_log(
            db,
            tenant_id=admin.tenant_id or "tenant-dev",
            resource_type="acl",
            resource_id="csv-import",
            project_id=project_id,
            event_type="ACL_BATCH_GRANTED",
            change_detail={"import_success": success, "import_skipped": skipped, "import_failed": failed},
        )
    await db.commit()

    # 事务提交后异步发布 Redis
    if success > 0:
        await publisher.publish_event(
            change_id, version, admin.tenant_id or "tenant-dev",
            "acl", "csv-import",
            event_type="ACL_BATCH_GRANTED",
            change_detail={"import_success": success},
        )

    return CSVImportResult(
        total_rows=total_rows,
        success=success,
        skipped=skipped,
        failed=failed,
        errors=errors[:50],  # 最多返回 50 条错误
    )
