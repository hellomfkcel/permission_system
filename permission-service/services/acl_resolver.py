"""ACL 解析器 — 权限 + 角色 + 封禁的统一查询。

设计依据：docs/外部系统设计.md §2.5.1 权限判定流程 + §2.5.2 可见性投影。

项目隔离（修复判定期不按项目过滤）：
所有查询接受可选的 project_id。调用方从 request.state.project_id 取值
（由 X-Api-Key / X-Client-Id 解析得出）。传入时按项目过滤授权数据，
不再依赖资源类型与资源 ID 在全平台唯一。project_id 为 None 时不过滤，
供策略模拟等跨项目场景使用。
"""

from datetime import datetime, timezone

from sqlalchemy import select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from models.acl import ACLEntry
from models.role_binding import RoleBinding
from models.restriction import Restriction
from models.resource import ResourceRegistry


def _scoped(conditions: list, model, project_id: str | None) -> list:
    """按项目过滤条件。project_id 为 None 时不追加条件。"""
    if project_id is not None:
        conditions.append(model.project_id == project_id)
    return conditions


def _binding_applies(
    rb_res_type: str | None,
    rb_res_id: str | None,
    resource_type: str,
    resource_id: str,
    channel_kb: str | None,
) -> bool:
    """判断一条角色绑定是否适用于当前资源。

    - 无范围绑定（resource_type 与 resource_id 均为 NULL）适用于全部资源
    - 精确匹配资源类型与 ID
    - 容器范围：绑定到某 KB 时，对该 KB 通道下的文档生效
    """
    if rb_res_type is None and rb_res_id is None:
        return True
    if rb_res_type == resource_type and rb_res_id == resource_id:
        return True
    if resource_type == "document" and channel_kb:
        if rb_res_type == "kb" and rb_res_id == channel_kb:
            return True
    return False


async def _applicable_bindings(
    db: AsyncSession,
    principals: list[str],
    resource_type: str,
    resource_id: str,
    channel_kb: str | None = None,
    project_id: str | None = None,
) -> list[tuple[str, str]]:
    """查询适用于当前资源的角色绑定。

    Returns:
        [(principal, role), ...]
    """
    conditions = [
        RoleBinding.principal.in_(principals),
        RoleBinding.revoked == False,  # noqa: E712
    ]
    _scoped(conditions, RoleBinding, project_id)

    stmt = select(
        RoleBinding.principal, RoleBinding.role,
        RoleBinding.resource_type, RoleBinding.resource_id,
    ).where(*conditions)
    result = await db.execute(stmt)

    return [
        (row[0], row[1])
        for row in result.fetchall()
        if _binding_applies(row[2], row[3], resource_type, resource_id, channel_kb)
    ]


async def resolve_bound_roles(
    db: AsyncSession,
    principals: list[str],
    resource_type: str,
    resource_id: str,
    channel_kb: str | None = None,
    project_id: str | None = None,
) -> set[str]:
    """返回主体通过角色绑定持有、且适用于当前资源的角色名集合。

    调用方把结果并入 Cerbos principal.roles，使匹配 `roles:` 字段的策略
    （静态角色式项目）也能消费平台侧的角色绑定。此前 principal.roles 只来自
    JWT，这类项目的角色绑定在判定中完全不生效。
    """
    bindings = await _applicable_bindings(
        db, principals, resource_type, resource_id, channel_kb, project_id,
    )
    return {role for _, role in bindings}


def _scope_key(
    resource_type: str, resource_id: str, channel_kb: str | None,
) -> str:
    """granted_actions 的作用域键。

    文档挂在 KB 通道下，其 ABAC 授权记在 KB 上（策略里 kb_* 派生角色对文档
    资源读 resource.attr.kb_id）；其余资源类型以自身 ID 为作用域。
    """
    if resource_type == "document" and channel_kb:
        return channel_kb
    return resource_id


async def _project_manage_grant(
    db: AsyncSession, principals: list[str], project_id: str | None,
) -> bool:
    """当前主体是否是该项目的管理员（project_members.role=project_admin）。

    项目级 manage 授权的事实来源就是项目成员表，不另存一份授权记录。
    """
    if not project_id:
        return False

    from models.project import ProjectMember
    from models.user_cache import UserCache

    user_ids = [p.split(":", 1)[1] for p in principals if p.startswith("user:")]
    if not user_ids:
        return False

    # project_members.user_id 可能存用户名，也可能存 UUID，两种都要匹配上
    alias_result = await db.execute(
        select(UserCache.user_id).where(UserCache.username.in_(user_ids))
    )
    candidates = set(user_ids) | {row[0] for row in alias_result.fetchall() if row[0]}

    member_role = await db.scalar(
        select(ProjectMember.role)
        .where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id.in_(candidates),
            ProjectMember.role.in_(["project_admin", "owner"]),
        )
        .limit(1)
    )
    return member_role is not None


async def resolve_granted_actions(
    db: AsyncSession,
    principals: list[str],
    resource_type: str,
    resource_id: str,
    channel_kb: str | None = None,
    project_id: str | None = None,
) -> dict[str, list[str]]:
    """构造 Cerbos 的 principal.attr.granted_actions（ABAC 路唯一入口）。

    两级作用域并存，与 rag_roles.yaml 中派生角色的取键方式一一对应：

        {"<kb_id>":     ["read", "write", ...]}  KB 级 → kb_reader/writer/admin
        {"<project_id>": ["manage"]}             项目级 → project_admin

    动作以策略里派生角色比对的后缀形式给出（"kb:read" → "read"）。

    事实来源：
      - KB 级 → acl_entries 中记在该 KB 上的授权 + 适用于该资源的角色绑定；
      - 项目级 → project_members 中的 project_admin 成员资格。

    资源实例级的直授（文档级 ACL）不在这里 —— 它走 resource.attr.acl 的
    ACL 路（见 get_resource_acl）。此前把文档级 ACL 也折进本函数的结果里，
    /filter 甚至把单篇文档的 doc:retrieve 映射成整个 KB 的 "read"，
    一条文档授权会放大成 KB 级可见性。两路分开后不再有这种放大。
    """
    scope = _scope_key(resource_type, resource_id, channel_kb)
    granted: dict[str, list[str]] = {}

    def _add(key: str, action: str) -> None:
        suffix = action.split(":", 1)[-1] if ":" in action else action
        bucket = granted.setdefault(key, [])
        if suffix not in bucket:
            bucket.append(suffix)

    # ── KB / 容器级 ACL ──
    scope_type = "kb" if (resource_type == "document" and channel_kb) else resource_type
    scope_actions = await _container_acl_actions(
        db, principals, scope_type, scope, project_id,
    )
    for action in scope_actions:
        _add(scope, action)

    # ── 角色绑定展开 ──
    from services.cerbos_policy_parser import get_role_actions_map

    role_actions_map = get_role_actions_map(project_id)
    bindings = await _applicable_bindings(
        db, principals, resource_type, resource_id, channel_kb, project_id,
    )
    for _, role in bindings:
        for implicit in role_actions_map.get(role, []):
            _add(scope, implicit)

    # ── 项目级 manage ──
    if await _project_manage_grant(db, principals, project_id):
        granted.setdefault(project_id, [])
        if "manage" not in granted[project_id]:
            granted[project_id].append("manage")

    return granted


async def _container_acl_actions(
    db: AsyncSession,
    principals: list[str],
    resource_type: str,
    resource_id: str,
    project_id: str | None,
) -> list[str]:
    """查询记在某容器（KB / 平台功能等）上的、属于当前主体的 ACL 动作。"""
    now = datetime.now(timezone.utc)
    conditions = [
        ACLEntry.principal.in_(principals),
        ACLEntry.resource_type == resource_type,
        ACLEntry.resource_id == resource_id,
        ACLEntry.revoked == False,  # noqa: E712
        or_(ACLEntry.expires_at.is_(None), ACLEntry.expires_at > now),
    ]
    _scoped(conditions, ACLEntry, project_id)
    result = await db.execute(select(ACLEntry.action).where(*conditions))
    return [row[0] for row in result.fetchall()]


async def get_resource_acl(
    db: AsyncSession,
    resource_type: str,
    resource_id: str,
    principals: list[str],
    principal_id: str,
    project_id: str | None = None,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """读取资源实例级 ACL，构造 Cerbos 的 resource.attr.acl / role_acl。

    对应策略中的 acl_user / acl_role 两个派生角色（见 rag_roles.yaml）：
    ACL 是"资源 → 主体"方向的授权，与 granted_actions 的"主体 → 资源"方向
    完全独立，任一命中即放行。

    数据来源是既有的 acl_entries 表 —— 它的 (principal, resource_type,
    resource_id, action) 已经完整表达了"哪个资源实例允许谁做什么"。
    不另建 ACL 表：同一事实存在于两处就必然会产生不一致。

    Returns:
        (acl, role_acl)
        acl      {principal_id: [action, ...]}  当前主体（含其所属组）被直授的动作。
                 键统一为 Cerbos principal.id，因为策略只能拿到这一个标识；
                 group: 前缀的授权在此并入当前主体，无需在策略里表达组模型。
        role_acl {role_name: [action, ...]}     该资源上按角色直授的动作。
                 键为角色名，Cerbos 侧与 principal.roles 求交集。
    """
    now = datetime.now(timezone.utc)

    conditions = [
        ACLEntry.resource_type == resource_type,
        ACLEntry.resource_id == resource_id,
        ACLEntry.revoked == False,  # noqa: E712
        or_(ACLEntry.expires_at.is_(None), ACLEntry.expires_at > now),
        or_(
            ACLEntry.principal.in_(principals),
            ACLEntry.principal.startswith("role:"),
        ),
    ]
    _scoped(conditions, ACLEntry, project_id)

    result = await db.execute(
        select(ACLEntry.principal, ACLEntry.action).where(*conditions)
    )

    acl: dict[str, list[str]] = {}
    role_acl: dict[str, list[str]] = {}
    for principal, action in result.fetchall():
        if principal.startswith("role:"):
            role = principal.split(":", 1)[1]
            if not role:
                continue
            bucket = role_acl.setdefault(role, [])
        else:
            bucket = acl.setdefault(principal_id, [])
        if action not in bucket:
            bucket.append(action)

    return acl, role_acl


async def check_subject_ban(
    db: AsyncSession,
    principals: list[str],
    tenant_id: str,
    project_id: str | None = None,
) -> bool:
    """检查主体是否被型一封禁。

    Returns:
        True 如果任一 principal 被 active 封禁。
    """
    conditions = [
        Restriction.tenant_id == tenant_id,
        Restriction.restriction_type == "subject_ban",
        Restriction.principal.in_(principals),
        Restriction.removed == False,  # noqa: E712
    ]
    _scoped(conditions, Restriction, project_id)

    result = await db.execute(select(Restriction.id).where(*conditions).limit(1))
    return result.first() is not None


async def check_resource_restriction(
    db: AsyncSession,
    resource_type: str,
    resource_id: str,
    tenant_id: str,
    project_id: str | None = None,
) -> list[str]:
    """查询资源的型二封禁主体列表。"""
    conditions = [
        Restriction.tenant_id == tenant_id,
        Restriction.restriction_type == "resource_restriction",
        Restriction.resource_type == resource_type,
        Restriction.resource_id == resource_id,
        Restriction.removed == False,  # noqa: E712
    ]
    _scoped(conditions, Restriction, project_id)

    result = await db.execute(select(Restriction.principal).where(*conditions))
    return [row[0] for row in result.fetchall() if row[0]]


async def get_resource_attr(
    db: AsyncSession,
    resource_type: str,
    resource_id: str,
    project_id: str | None = None,
) -> dict:
    """查询 resource_registry 获取资源的 Cerbos attr。

    Returns:
        {retired, owner, tenant_id, is_enabled, allow_download, project_id}。

    project_id 必须随属性下发：project_admin 派生角色按
    request.resource.attr.project_id 查 granted_actions，缺失则项目级授权
    在判定期完全不生效。

    资源未注册时必须显式返回 retired=False：Cerbos CEL 条件 "retired == false"
    在属性缺失时求值为 null == false → false，会导致全部规则判 deny。
    """
    conditions = [
        ResourceRegistry.resource_type == resource_type,
        ResourceRegistry.resource_id == resource_id,
    ]
    _scoped(conditions, ResourceRegistry, project_id)

    result = await db.execute(select(ResourceRegistry).where(*conditions))
    row = result.scalars().first()
    if row is None:
        return {
            "retired": False,
            "is_enabled": True,
            "allow_download": True,
            "project_id": project_id or "",
        }
    return {
        "retired": row.retired,
        "owner": row.owner,
        "tenant_id": row.tenant_id,
        "is_enabled": row.is_enabled,
        "allow_download": row.allow_download,
        "project_id": row.project_id or project_id or "",
    }


async def get_active_kbs_for_principal(
    db: AsyncSession,
    principals: list[str],
    tenant_id: str,
    project_id: str | None = None,
) -> list[str]:
    """查询 principal 有权限的所有活跃 KB 列表。

    用于 prefilter 端点。
    system_admin → 返回该租户下全部活跃 KB（无需逐条 ACL）。
    """
    now = datetime.now(timezone.utc)

    # system_admin / admin 角色 → 全部活跃 KB
    if "role:system_admin" in principals or "role:admin" in principals:
        conditions = [
            ResourceRegistry.resource_type == "kb",
            ResourceRegistry.tenant_id == tenant_id,
            ResourceRegistry.retired == False,  # noqa: E712
        ]
        _scoped(conditions, ResourceRegistry, project_id)
        result = await db.execute(
            select(ResourceRegistry.resource_id).where(*conditions)
        )
        return [row[0] for row in result.fetchall()]

    # 普通用户：KB 级 ACL
    kb_conditions = [
        ACLEntry.principal.in_(principals),
        ACLEntry.resource_type == "kb",
        ACLEntry.tenant_id == tenant_id,
        ACLEntry.revoked == False,  # noqa: E712
        or_(ACLEntry.expires_at.is_(None), ACLEntry.expires_at > now),
    ]
    _scoped(kb_conditions, ACLEntry, project_id)
    result = await db.execute(
        select(ACLEntry.resource_id).where(*kb_conditions).distinct()
    )
    kb_ids = [row[0] for row in result.fetchall()]

    # 文档级授权反查：只有 doc 级 ACL 的用户，prefilter 仍应包含该 doc 所在的 KB
    doc_conditions = [
        ACLEntry.principal.in_(principals),
        ACLEntry.resource_type == "document",
        ACLEntry.tenant_id == tenant_id,
        ACLEntry.action.in_(["doc:view", "doc:download", "doc:retrieve"]),
        ACLEntry.revoked == False,  # noqa: E712
        or_(ACLEntry.expires_at.is_(None), ACLEntry.expires_at > now),
    ]
    _scoped(doc_conditions, ACLEntry, project_id)
    doc_result = await db.execute(
        select(ACLEntry.resource_id).where(*doc_conditions).distinct()
    )
    doc_ids = [row[0] for row in doc_result.fetchall()]

    if doc_ids:
        from models.mount import MountRegistry as MountModel
        mount_result = await db.execute(
            select(MountModel.kb_id)
            .where(
                MountModel.doc_id.in_(doc_ids),
                MountModel.unlinked == False,  # noqa: E712
            )
            .distinct()
        )
        for row in mount_result.fetchall():
            if row[0] not in kb_ids:
                kb_ids.append(row[0])

    # 过滤 retired
    if kb_ids:
        active_conditions = [
            ResourceRegistry.resource_type == "kb",
            ResourceRegistry.resource_id.in_(kb_ids),
            ResourceRegistry.retired == False,  # noqa: E712
        ]
        _scoped(active_conditions, ResourceRegistry, project_id)
        result2 = await db.execute(
            select(ResourceRegistry.resource_id).where(*active_conditions)
        )
        kb_ids = [row[0] for row in result2.fetchall()]

    return kb_ids


async def get_allow_stamps_for_channel(
    db: AsyncSession,
    doc_id: str,
    kb_id: str,
    tenant_id: str,
    project_id: str | None = None,
) -> list[str]:
    """查询对 (doc_id, kb_id) 通道有可见性的主体列表。

    三源聚合（设计依据 §14.5.1 + J-6）：
    1. 文档级 ACL：对该文档有 doc:retrieve / doc:view 权限的主体
    2. KB 级 ACL：对该 KB 有 kb:read 或更强权限的主体
    3. 角色绑定：绑定到该 KB 或全租户的主体

    返回的戳记只含原始主体（user: / group: / role: 前缀），禁止展开成员。
    """
    now = datetime.now(timezone.utc)
    stamps: list[str] = []
    seen: set[str] = set()

    def _add(p: str) -> None:
        if p and p not in seen:
            stamps.append(p)
            seen.add(p)

    # ── 源 1：文档级 ACL ──
    doc_conditions = [
        ACLEntry.resource_type == "document",
        ACLEntry.resource_id == doc_id,
        ACLEntry.tenant_id == tenant_id,
        ACLEntry.action.in_(["doc:retrieve", "doc:view"]),
        ACLEntry.revoked == False,  # noqa: E712
        or_(ACLEntry.expires_at.is_(None), ACLEntry.expires_at > now),
    ]
    _scoped(doc_conditions, ACLEntry, project_id)
    doc_result = await db.execute(select(ACLEntry.principal).where(*doc_conditions))
    for row in doc_result.fetchall():
        _add(row[0])

    # ── 源 2：KB 级 ACL ──
    # 拥有 KB 级读取权限的主体，对该 KB 下全部文档自动有 doc:retrieve 可见性。
    kb_conditions = [
        ACLEntry.resource_type == "kb",
        ACLEntry.resource_id == kb_id,
        ACLEntry.tenant_id == tenant_id,
        ACLEntry.action.in_(["kb:read", "kb:write", "kb:manage", "kb:grant"]),
        ACLEntry.revoked == False,  # noqa: E712
        or_(ACLEntry.expires_at.is_(None), ACLEntry.expires_at > now),
    ]
    _scoped(kb_conditions, ACLEntry, project_id)
    kb_result = await db.execute(select(ACLEntry.principal).where(*kb_conditions))
    for row in kb_result.fetchall():
        _add(row[0])

    # ── 源 3：角色绑定 ──
    # 只使用原始 principal 作为戳记，不加 role: 前缀：
    # 角色绑定表示该 principal 持有某角色，戳记须与 JWT principals 中的形式一致。
    role_conditions = [
        RoleBinding.revoked == False,  # noqa: E712
        RoleBinding.tenant_id == tenant_id,
        or_(
            RoleBinding.resource_id.is_(None),
            RoleBinding.resource_id == kb_id,
        ),
    ]
    _scoped(role_conditions, RoleBinding, project_id)
    role_result = await db.execute(
        select(RoleBinding.principal).where(*role_conditions)
    )
    for row in role_result.fetchall():
        _add(row[0])

    return stamps


async def get_deny_stamps_for_channel(
    db: AsyncSession,
    doc_id: str,
    kb_id: str,
    tenant_id: str,
    project_id: str | None = None,
) -> list[str]:
    """查询对该 (doc_id, kb_id) 有型二封禁的主体列表。"""
    stamps = await check_resource_restriction(
        db, "document", doc_id, tenant_id, project_id,
    )
    kb_stamps = await check_resource_restriction(
        db, "kb", kb_id, tenant_id, project_id,
    )
    return [s for s in stamps if s] + [s for s in kb_stamps if s]
