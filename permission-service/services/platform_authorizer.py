"""平台功能权限判定 — 唯一判定路径。

设计依据：docs/permission_model_v2.md §4 判定路径唯一化。

问题背景：平台功能（管理台侧边栏的 12 个模块）的准入此前有两套判断：
  1. cerbos/policies/.../platform.yaml 里的策略；
  2. api/auth_routes.py 里手写的 if 分支（platform_admin → 全部、
     platform_viewer → 全部只读、platform_auditor → 三个模块……）。
两套规则各自演进，策略模拟器给出的结论和管理台实际放行的结果可以不一致，
改一处不改另一处就会产生"看得见但点不动"或反之的现象。

本模块把第 2 套删掉：角色 → 功能 → 动作的映射只写在 platform.yaml 里，
运行时统一由 Cerbos 判定。后端只负责把事实喂进去：
  principal.roles                = JWT 角色 + 平台角色绑定（project_id IS NULL）
  principal.attr.granted_actions = platform 资源的 ACL（按功能 ID 分组）
拿到判定结果后无条件遵从，不做任何二次推导。
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
    """Cerbos 不可达 —— 平台权限无法判定。

    调用方必须按 fail-closed 处理：拿不到判定就不放行，
    不允许回退到本地推断，否则又出现第二条判断路径。
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


async def _project_member_role(db: AsyncSession, user_id: str) -> set[str]:
    """项目成员 → 合成角色 project_member。

    后端只提供事实（这个用户在 project_members 表里），"成员能看到哪些模块"
    的规则写在 platform.yaml 的 project_member_baseline 里。

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

    member = await db.scalar(
        select(ProjectMember.id)
        .where(ProjectMember.user_id.in_(candidates))
        .limit(1)
    )
    return {"project_member"} if member else set()


async def _platform_feature_grants(
    db: AsyncSession, principals: list[str],
) -> dict[str, list[str]]:
    """platform 资源的 ACL → {feature_id: [platform:read, ...]}。

    与 Cerbos 中 granted_actions 的取键方式一致（键即 resource.id）。
    """
    from models.acl import ACLEntry

    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(ACLEntry.resource_id, ACLEntry.action).where(
            ACLEntry.resource_type == PLATFORM_RESOURCE_KIND,
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
    member_roles = await _project_member_role(db, user_id)
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


async def check_platform_permission(
    db: AsyncSession, user_id: str, jwt_roles: list[str],
    feature_id: str, action: str,
) -> bool:
    """判定单个平台功能上的单个动作。

    Raises:
        PlatformAuthorizationUnavailable: Cerbos 调用失败。
    """
    permissions = await resolve_platform_permissions(db, user_id, jwt_roles)
    return action in permissions.get(feature_id, [])
