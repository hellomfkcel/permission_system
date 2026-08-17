"""平台功能权限判定。

角色 → 功能 → 动作的映射只写在 platform.yaml，运行时统一由 Cerbos 判定；
本模块只负责把事实组装进请求，拿到结果后无条件遵从，不做二次推导：

    principal.roles                JWT 角色 + 平台角色绑定（project_id IS NULL）
    principal.attr.granted_actions platform 资源的 ACL，按功能 ID 分组
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.platform_features import PLATFORM_FEATURES
from app.role_actions_config import PLATFORM_ACTIONS

logger = structlog.get_logger(__name__)

# 平台功能资源在 Cerbos 中的资源类型
PLATFORM_RESOURCE_KIND = "platform"

_ACTIONS = sorted(PLATFORM_ACTIONS)


class PlatformAuthorizationUnavailable(RuntimeError):
    """Cerbos 不可达，平台权限无法判定。

    调用方须 fail-closed：拿不到判定就不放行，不得回退到本地推断。
    """
def _principals_of(user_id: str) -> list[str]:
    return [f"user:{user_id}"]


async def _platform_role_bindings(
    db: AsyncSession, principals: list[str],
) -> set[str]:
    """平台级角色绑定（project_id IS NULL）。"""
    from models.role_binding import RoleBinding

    result = await db.execute(
        select(RoleBinding.role).where(
            RoleBinding.principal.in_(principals),
            RoleBinding.project_id.is_(None),
            RoleBinding.revoked == False,  # noqa: E712
        )
    )
    return {row[0] for row in result.fetchall() if row[0]}


async def _project_member_roles(db: AsyncSession, user_id: str) -> set[str]:
    """项目成员 → 合成平台角色（区分管理员与只读成员）。

    project_members.role → 合成角色（"成员能进哪些模块"的规则写在 platform.yaml）：
        project_admin  → "project_admin"   （项目 7 模块读写）
        project_viewer → "project_member"   （只读子集）

    取全部 membership 的并集：模块准入表达的是"能进这类模块"，具体操作哪个项目
    由数据层按 project_id 过滤，因此在任一项目是管理员即获得管理员的模块准入。

    project_members.user_id 可能存用户名，也可能存 Keycloak UUID，两种都要匹配。
    """
    from models.project import ProjectMember
    from models.user_cache import UserCache

    candidates = {user_id}
    alias = await db.scalar(
        select(UserCache.user_id).where(UserCache.username == user_id)
    )
    if alias:
        candidates.add(alias)

    result = await db.execute(
        select(ProjectMember.role).where(ProjectMember.user_id.in_(candidates))
    )
    synthetic: set[str] = set()
    for (role,) in result.fetchall():
        synthetic.add("project_admin" if role == "project_admin" else "project_member")
    return synthetic


async def _platform_feature_grants(
    db: AsyncSession, principals: list[str],
) -> dict[str, list[str]]:
    """platform 资源的 ACL → {feature_id: [platform:read, ...]}。

    与 Cerbos 中 granted_actions 的取键方式一致（键即 resource.id）。

    只认平台级授权（project_id IS NULL）：platform 是平台层资源，它的授权不属于
    任何项目。若把挂在某个项目下的 platform 授权也读进来，项目级的写权限就变成了
    平台级的写权限。写入端由 acl_routes._validate_grant_scope 保证不会再产生这类
    记录，这里对存量数据同样做前置过滤。
    """
    from models.acl import ACLEntry

    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(ACLEntry.resource_id, ACLEntry.action).where(
            ACLEntry.resource_type == PLATFORM_RESOURCE_KIND,
            ACLEntry.project_id.is_(None),
            ACLEntry.principal.in_(principals),
            ACLEntry.revoked == False,  # noqa: E712
            or_(ACLEntry.expires_at.is_(None), ACLEntry.expires_at > now),
        )
    )
    grants: dict[str, list[str]] = {}
    for feature_id, action in result.fetchall():
        bucket = grants.setdefault(feature_id, [])
        if action not in bucket:
            bucket.append(action)
    return grants


async def resolve_platform_permissions(
    db: AsyncSession, user_id: str, jwt_roles: list[str],
) -> dict[str, list[str]]:
    """返回当前主体对全部平台功能的权限映射 {feature_id: [actions]}。

    单次 Cerbos 批量判定覆盖 12 个功能 × 2 个动作。

    Raises:
        PlatformAuthorizationUnavailable: Cerbos 调用失败。
    """
    from services.cerbos_adapter import get_cerbos

    principals = _principals_of(user_id)
    bound_roles = await _platform_role_bindings(db, principals)
    member_roles = await _project_member_roles(db, user_id)
    granted_actions = await _platform_feature_grants(db, principals)

    cerbos_principal = {
        "id": principals[0],
        # 追加 user：平台层的委托规则以 user 为入场资格，
        # 与项目层派生角色的 parentRoles 约定一致。
        "roles": sorted({*jwt_roles, *bound_roles, *member_roles, "user"}),
        "attr": {"granted_actions": granted_actions},
    }

    resources = [
        {
            "actions": _ACTIONS,
            "resource": {
                "kind": PLATFORM_RESOURCE_KIND,
                "id": feature_id,
                "attr": {"retired": False},
            },
        }
        for feature_id in sorted(PLATFORM_FEATURES)
    ]

    cerbos = get_cerbos()
    try:
        result = await cerbos.check_resources(
            request_id=f"platform-access:{user_id}",
            principal=cerbos_principal,
            resources=resources,
        )
    except Exception as exc:
        logger.warning("platform_authz_unavailable", error=str(exc)[:200])
        raise PlatformAuthorizationUnavailable(str(exc)) from exc

    ordered_ids = [r["resource"]["id"] for r in resources]
    permissions: dict[str, list[str]] = {}
    for idx, item in enumerate(result.get("results", []) or []):
        # 正常情况下 Cerbos 会回带 resource.id；缺失时按请求顺序对位。
        feature_id = (item.get("resource") or {}).get("id", "")
        if not feature_id:
            feature_id = ordered_ids[idx] if idx < len(ordered_ids) else ""
        if not feature_id:
            continue
        allowed = [
            action
            for action, verdict in (item.get("actions") or {}).items()
            if verdict == "EFFECT_ALLOW"
        ]
        if allowed:
            permissions[feature_id] = sorted(allowed)
    return permissions
