"""ACL 解析器 — 权限 + 角色 + 封禁的统一查询。

设计依据：docs/外部系统设计.md §2.5.1 权限判定流程 + §2.5.2 可见性投影。
"""

from sqlalchemy import select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from models.acl import ACLEntry
from models.role_binding import RoleBinding
from models.restriction import Restriction
from models.resource import ResourceRegistry


async def resolve_granted_actions_by_principal(
    db: AsyncSession,
    principals: list[str],
    action: str,
    resource_type: str,
    resource_id: str,
    channel_kb: str | None = None,
) -> dict[str, list[str]]:
    """查询 ACL + role_bindings 中匹配任意 principal 的 action 列表。

    返回 {principal: [action, ...]} 映射。
    """
    from datetime import datetime, timezone
    from services.cerbos_policy_parser import get_role_actions_map
    role_actions_map = get_role_actions_map()

    now = datetime.now(timezone.utc)

    # 1. 直接 ACL 查询
    stmt = select(ACLEntry.principal, ACLEntry.action).where(
        ACLEntry.principal.in_(principals),
        ACLEntry.resource_type == resource_type,
        ACLEntry.resource_id == resource_id,
        ACLEntry.revoked == False,  # noqa: E712
        or_(
            ACLEntry.expires_at.is_(None),
            ACLEntry.expires_at > now,
        ),
    )
    result = await db.execute(stmt)
    rows = result.fetchall()

    mapping: dict[str, list[str]] = {}
    for row in rows:
        p = row[0]
        a = row[1]
        mapping.setdefault(p, []).append(a)

    # 2. 角色绑定展开：将 role_bindings 展开为隐式动作
    #    设计依据：docs/权限管理系统架构设计.md §2.3 准入矩阵
    #    - 无范围绑定 (resource_type IS NULL) → 适用于所有资源
    #    - KB 范围绑定 → 适用于该 KB 及其下文档（通过 channel.kb 匹配）
    #    - Document 范围绑定 → 适用于该文档
    #    P3 修复：角色→动作映射从 Cerbos YAML 动态解析，消除硬编码同步风险。
    rb_stmt = select(
        RoleBinding.principal, RoleBinding.role,
        RoleBinding.resource_type, RoleBinding.resource_id,
    ).where(
        RoleBinding.principal.in_(principals),
        RoleBinding.revoked == False,  # noqa: E712
    )
    rb_result = await db.execute(rb_stmt)
    role_rows = rb_result.fetchall()

    for row in role_rows:
        rb_principal = row[0]
        rb_role = row[1]
        rb_res_type = row[2]
        rb_res_id = row[3]

        # 判断此角色绑定是否适用于当前资源
        applies = False
        if rb_res_type is None and rb_res_id is None:
            # 无范围绑定 → 适用于所有资源
            applies = True
        elif rb_res_type == resource_type and rb_res_id == resource_id:
            # 精确匹配 → 适用于该资源
            applies = True
        elif resource_type == "document" and channel_kb:
            # 文档资源 → 检查 KB 级绑定是否匹配该文档的 channel.kb
            if rb_res_type == "kb" and rb_res_id == channel_kb:
                applies = True

        if not applies:
            continue

        # 展开角色隐式动作（从 Cerbos YAML 动态解析，非硬编码）
        implicit_actions = role_actions_map.get(rb_role, [])
        for ia in implicit_actions:
            mapping.setdefault(rb_principal, [])
            if ia not in mapping[rb_principal]:
                mapping[rb_principal].append(ia)

    return mapping


async def check_subject_ban(
    db: AsyncSession,
    principals: list[str],
    tenant_id: str,
) -> bool:
    """检查主体是否被型一封禁。

    Returns:
        True 如果任一 principal 被 active 封禁。
    """
    stmt = select(Restriction.id).where(
        Restriction.tenant_id == tenant_id,
        Restriction.restriction_type == "subject_ban",
        Restriction.principal.in_(principals),
        Restriction.removed == False,  # noqa: E712
    ).limit(1)
    result = await db.execute(stmt)
    return result.first() is not None


async def check_resource_restriction(
    db: AsyncSession,
    resource_type: str,
    resource_id: str,
    tenant_id: str,
) -> list[str]:
    """查询资源的型二封禁主体列表。"""
    stmt = select(Restriction.principal).where(
        Restriction.tenant_id == tenant_id,
        Restriction.restriction_type == "resource_restriction",
        Restriction.resource_type == resource_type,
        Restriction.resource_id == resource_id,
        Restriction.removed == False,  # noqa: E712
    )
    result = await db.execute(stmt)
    return [row[0] for row in result.fetchall() if row[0]]


async def get_resource_attr(
    db: AsyncSession,
    resource_type: str,
    resource_id: str,
) -> dict:
    """查询 resource_registry 获取资源的 Cerbos attr。

    Returns:
        {retired, owner, tenant_id, is_enabled, allow_download} 或空 dict。

    设计依据：Cerbos 策略 document.yaml 要求 is_enabled/allow_download 字段。
    这些字段默认为 true（文档注册时通常是启用的），retired 以 DB 为准。
    """
    stmt = select(ResourceRegistry).where(
        ResourceRegistry.resource_type == resource_type,
        ResourceRegistry.resource_id == resource_id,
    )
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        # 资源未注册 → 视为活跃（未退役、启用、允许下载）。
        # 必须显式设置 retired=False，否则 Cerbos CEL 条件
        # "retired == false" 在属性缺失时求值为 null == false → false，
        # 导致所有规则（doc:view/unmount 等）判定 deny。
        return {
            "retired": False,
            "is_enabled": True,
            "allow_download": True,
        }
    return {
        "retired": row.retired,
        "owner": row.owner,
        "tenant_id": row.tenant_id,
        "is_enabled": row.is_enabled,
        "allow_download": row.allow_download,
    }


async def get_active_kbs_for_principal(
    db: AsyncSession,
    principals: list[str],
    tenant_id: str,
) -> list[str]:
    """查询 principal 有权限的所有活跃 KB 列表。

    用于 prefilter 端点。
    system_admin → 返回该租户下全部活跃 KB（无需逐条 ACL）。
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    # system_admin / admin 角色 → 全部活跃 KB
    if "role:system_admin" in principals or "role:admin" in principals:
        stmt = select(ResourceRegistry.resource_id).where(
            ResourceRegistry.resource_type == "kb",
            ResourceRegistry.tenant_id == tenant_id,
            ResourceRegistry.retired == False,  # noqa: E712
        )
        result = await db.execute(stmt)
        return [row[0] for row in result.fetchall()]

    # 普通用户：获取 ACL 中有 kb:read 或更强权限的 KB 列表
    stmt = (
        select(ACLEntry.resource_id)
        .where(
            ACLEntry.principal.in_(principals),
            ACLEntry.resource_type == "kb",
            ACLEntry.tenant_id == tenant_id,
            ACLEntry.revoked == False,  # noqa: E712
            or_(
                ACLEntry.expires_at.is_(None),
                ACLEntry.expires_at > now,
            ),
        )
        .distinct()
    )
    result = await db.execute(stmt)
    kb_ids = [row[0] for row in result.fetchall()]

    # ── 补充：文档级授权反查 ──
    # 如果用户只有 doc 级 ACL（非 KB 级），prefilter 应仍包含该 doc 所在的 KB。
    # 设计依据：J-1 联合契约测试 — "文档点得开，搜得到"。
    doc_stmt = (
        select(ACLEntry.resource_id)
        .where(
            ACLEntry.principal.in_(principals),
            ACLEntry.resource_type == "document",
            ACLEntry.tenant_id == tenant_id,
            ACLEntry.action.in_(["doc:view", "doc:download", "doc:retrieve"]),
            ACLEntry.revoked == False,  # noqa: E712
            or_(
                ACLEntry.expires_at.is_(None),
                ACLEntry.expires_at > now,
            ),
        )
        .distinct()
    )
    doc_result = await db.execute(doc_stmt)
    doc_ids = [row[0] for row in doc_result.fetchall()]

    if doc_ids:
        # 通过 mount_registry 反查这些文档所属的 KB
        from models.mount import MountRegistry as MountModel
        mount_stmt = (
            select(MountModel.kb_id)
            .where(
                MountModel.doc_id.in_(doc_ids),
                MountModel.unlinked == False,  # noqa: E712
            )
            .distinct()
        )
        mount_result = await db.execute(mount_stmt)
        for row in mount_result.fetchall():
            if row[0] not in kb_ids:
                kb_ids.append(row[0])

    # 过滤 retired=false
    if kb_ids:
        stmt2 = select(ResourceRegistry.resource_id).where(
            ResourceRegistry.resource_type == "kb",
            ResourceRegistry.resource_id.in_(kb_ids),
            ResourceRegistry.retired == False,  # noqa: E712
        )
        result2 = await db.execute(stmt2)
        kb_ids = [row[0] for row in result2.fetchall()]

    return kb_ids


async def get_allow_stamps_for_channel(
    db: AsyncSession,
    doc_id: str,
    kb_id: str,
    tenant_id: str,
) -> list[str]:
    """查询对 (doc_id, kb_id) 通道有可见性的主体列表。

    可见性来源（三源聚合，设计依据 §14.5.1 + J-6）：
    1. 文档级 ACL：直接对该文档有 doc:retrieve/doc:view 权限的主体
    2. KB 级 ACL：对该 KB 有 kb:read（或更强）权限的主体——
       拥有 KB 级读取权限即隐式拥有该 KB 下所有文档的可见性
    3. 角色绑定：对该 KB 有 kb_reader/kb_writer/kb_admin/admin 角色的主体

    返回的戳记只含原始主体（user:/group:/role: 前缀），禁止展开成员。
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    stamps: list[str] = []
    seen: set[str] = set()

    def _add(p: str) -> None:
        """去重添加戳记。"""
        if p and p not in seen:
            stamps.append(p)
            seen.add(p)

    # ── 源 1：文档级 ACL（doc:retrieve + doc:view）──
    doc_stmt = select(ACLEntry.principal, ACLEntry.action).where(
        ACLEntry.resource_type == "document",
        ACLEntry.resource_id == doc_id,
        ACLEntry.tenant_id == tenant_id,
        ACLEntry.action.in_(["doc:retrieve", "doc:view"]),
        ACLEntry.revoked == False,  # noqa: E712
        or_(
            ACLEntry.expires_at.is_(None),
            ACLEntry.expires_at > now,
        ),
    )
    doc_result = await db.execute(doc_stmt)
    for row in doc_result.fetchall():
        _add(row[0])

    # ── 源 2：KB 级 ACL（kb:read/kb:write/kb:manage/kb:grant）──
    # 拥有 KB 级读取权限的主体，对该 KB 下所有文档自动有 doc:retrieve 可见性。
    # 设计依据：设计文档 §2.3 准入矩阵 — kb_reader → doc:retrieve。
    kb_stmt = select(ACLEntry.principal).where(
        ACLEntry.resource_type == "kb",
        ACLEntry.resource_id == kb_id,
        ACLEntry.tenant_id == tenant_id,
        ACLEntry.action.in_(["kb:read", "kb:write", "kb:manage", "kb:grant"]),
        ACLEntry.revoked == False,  # noqa: E712
        or_(
            ACLEntry.expires_at.is_(None),
            ACLEntry.expires_at > now,
        ),
    )
    kb_result = await db.execute(kb_stmt)
    for row in kb_result.fetchall():
        _add(row[0])

    # ── 源 3：角色绑定 ——
    # 角色绑定可能限定到特定 KB（resource_id == kb_id）或全租户（resource_id IS NULL）。
    role_stmt = select(RoleBinding.principal, RoleBinding.role, RoleBinding.resource_id).where(
        RoleBinding.revoked == False,  # noqa: E712
        RoleBinding.tenant_id == tenant_id,
        and_(
            or_(
                RoleBinding.resource_id.is_(None),
                RoleBinding.resource_id == kb_id,
            ),
        ),
    )
    role_result = await db.execute(role_stmt)
    for row in role_result.fetchall():
        principal_val = row[0]
        # P1-3 修复：只使用原始 principal 作为戳记，不添加 role: 前缀。
        # 角色绑定表示某个 principal 拥有某个角色，该 principal 本身就有可见性。
        # 错误示例：role:user:alice（畸形戳记，与 JWT principals 中 user:alice 不匹配）
        # 正确示例：user:alice（与 JWT principals 中的 user:alice 可直接匹配）
        # 如果 principal 本身已是 role: 前缀（如 role:viewer），则直接使用。
        _add(principal_val)

    return stamps


async def get_deny_stamps_for_channel(
    db: AsyncSession,
    doc_id: str,
    kb_id: str,
    tenant_id: str,
) -> list[str]:
    """查询对该 (doc_id, kb_id) 有型二封禁的主体列表。"""
    # 型二封禁针对文档
    stamps = await check_resource_restriction(db, "document", doc_id, tenant_id)
    stamps = [s for s in stamps if s]
    # 型二封禁针对 KB
    kb_stamps = await check_resource_restriction(db, "kb", kb_id, tenant_id)
    stamps.extend([s for s in kb_stamps if s])
    return stamps
