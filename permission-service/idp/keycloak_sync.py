"""Keycloak IdP 用户/组同步服务。

设计依据：docs/外部系统设计.md §4.2 权限服务与 Keycloak 的数据同步
—— 每 15 分钟定时同步用户/组数据到本地缓存表。
"""

import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from models.user_cache import UserCache


class KeycloakSync:
    """Keycloak 用户/组同步器。

    调用 Keycloak Admin REST API 获取用户和组数据，
    写入本地 user_cache 表供管理台读取。
    """

    def __init__(self) -> None:
        self._admin_token: str | None = None
        self._token_expiry: float = 0
        self._http = httpx.AsyncClient(timeout=30.0)

    # ══════════════════════════════════════════════════════════════
    # Admin Token 管理
    # ══════════════════════════════════════════════════════════════

    async def _get_admin_token(self) -> str:
        """获取 Keycloak Admin Token。

        优先级：
        1. permission-service 的 service account（client_credentials 方式）
           —— 需要 KEYCLOAK_CLIENT_SECRET 且 service account 已授予
           realm-management 角色（view-users, query-users, view-groups,
           query-groups, view-realm）。
        2. master realm 管理员凭据（password 方式）
           —— 仅在显式配置 KEYCLOAK_ADMIN_USERNAME + KEYCLOAK_ADMIN_PASSWORD
           时启用，作为 service account 不可用时的回退。
           —— 生产环境通过 K8s Secret / Vault 注入，禁止在 .env 中明文存放。

        Raises:
            RuntimeError: 所有认证方式均失败或未配置时抛出。
        """
        import time

        # 缓存未过期
        if self._admin_token and time.time() < self._token_expiry - 60:
            return self._admin_token

        errors: list[str] = []

        # ── 方式 1：service account (client_credentials) ──
        if settings.keycloak_client_secret:
            try:
                resp = await self._http.post(
                    f"{settings.keycloak_server_url}/realms/{settings.keycloak_realm}"
                    f"/protocol/openid-connect/token",
                    data={
                        "client_id": settings.keycloak_client_id,
                        "client_secret": settings.keycloak_client_secret,
                        "grant_type": "client_credentials",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                self._admin_token = data["access_token"]
                self._token_expiry = time.time() + data.get("expires_in", 300)
                return self._admin_token
            except Exception as exc:
                errors.append(
                    f"service_account({settings.keycloak_client_id}): "
                    f"{type(exc).__name__}: {str(exc)[:200]}"
                )

        # ── 方式 2：master realm admin 回退（仅当显式配置时）──
        if settings.keycloak_admin_username and settings.keycloak_admin_password:
            try:
                resp = await self._http.post(
                    f"{settings.keycloak_server_url}/realms/master"
                    f"/protocol/openid-connect/token",
                    data={
                        "client_id": "admin-cli",
                        "username": settings.keycloak_admin_username,
                        "password": settings.keycloak_admin_password,
                        "grant_type": "password",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                self._admin_token = data["access_token"]
                self._token_expiry = time.time() + data.get("expires_in", 60)
                return self._admin_token
            except Exception as exc:
                errors.append(
                    f"master_admin({settings.keycloak_admin_username}): "
                    f"{type(exc).__name__}: {str(exc)[:200]}"
                )

        # 所有方式均失败
        error_detail = "; ".join(errors) if errors else (
            "No Keycloak admin credentials configured. "
            "Set KEYCLOAK_CLIENT_SECRET for service account, "
            "or KEYCLOAK_ADMIN_USERNAME + KEYCLOAK_ADMIN_PASSWORD for master realm fallback."
        )
        raise RuntimeError(
            f"Failed to obtain Keycloak admin token: {error_detail}"
        )

    # ══════════════════════════════════════════════════════════════
    # 用户同步
    # ══════════════════════════════════════════════════════════════

    async def sync_users(self) -> dict[str, int]:
        """同步 Keycloak 用户到本地 user_cache 表。

        Returns:
            {"created": N, "updated": M, "deleted": K}
        """
        token = await self._get_admin_token()
        realm = settings.keycloak_realm
        base = settings.keycloak_server_url

        # 获取所有用户
        all_users: list[dict[str, Any]] = []
        offset = 0
        while True:
            resp = await self._http.get(
                f"{base}/admin/realms/{realm}/users",
                params={"max": 100, "first": offset},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            all_users.extend(batch)
            offset += len(batch)

        created = 0
        updated = 0

        async with async_session() as db:
            existing_ids = set()
            # 获取当前已有的 user_ids
            stmt = select(UserCache.user_id)
            result = await db.execute(stmt)
            current_ids = {row[0] for row in result.fetchall()}

            for user_data in all_users:
                user_id = user_data["id"]
                existing_ids.add(user_id)

                # 解析 groups（从用户属性或单独查询）
                groups = user_data.get("groups", [])

                # 解析 roles
                try:
                    role_resp = await self._http.get(
                        f"{base}/admin/realms/{realm}/users/{user_id}/role-mappings/realm",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    role_resp.raise_for_status()
                    roles = role_resp.json()
                except Exception:
                    roles = []

                # 解析租户
                attrs = user_data.get("attributes", {})
                tenant_id = None
                if attrs and "tenant" in attrs:
                    tenant_id = attrs["tenant"][0] if attrs["tenant"] else None

                # Upsert
                stmt_existing = select(UserCache).where(UserCache.user_id == user_id)
                res = await db.execute(stmt_existing)
                existing = res.scalar_one_or_none()

                if existing:
                    existing.username = user_data.get("username", existing.username)
                    existing.email = user_data.get("email")
                    existing.first_name = user_data.get("firstName")
                    existing.last_name = user_data.get("lastName")
                    existing.tenant_id = tenant_id
                    existing.roles = roles if isinstance(roles, list) else [roles]
                    existing.groups = groups if isinstance(groups, list) else [groups]
                    existing.attributes = attrs
                    existing.enabled = user_data.get("enabled", True)
                    existing.last_synced_at = datetime.now(timezone.utc)
                    updated += 1
                else:
                    new_user = UserCache(
                        id=uuid.uuid4(),
                        user_id=user_id,
                        username=user_data.get("username", "unknown"),
                        email=user_data.get("email"),
                        first_name=user_data.get("firstName"),
                        last_name=user_data.get("lastName"),
                        tenant_id=tenant_id,
                        roles=roles if isinstance(roles, list) else [roles],
                        groups=groups if isinstance(groups, list) else [groups],
                        attributes=attrs,
                        enabled=user_data.get("enabled", True),
                    )
                    db.add(new_user)
                    created += 1

            # 删除 Keycloak 中已不存在的用户
            deleted_ids = current_ids - existing_ids
            if deleted_ids:
                stmt_del = delete(UserCache).where(
                    UserCache.user_id.in_(deleted_ids)
                )
                await db.execute(stmt_del)

            await db.commit()

        return {"created": created, "updated": updated, "deleted": len(deleted_ids)}

    # ══════════════════════════════════════════════════════════════
    # 组同步
    # ══════════════════════════════════════════════════════════════

    async def get_groups(self) -> list[dict[str, Any]]:
        """获取 Keycloak 所有组及其成员。

        Returns:
            [{id, name, path, member_count}]
        """
        token = await self._get_admin_token()
        realm = settings.keycloak_realm
        base = settings.keycloak_server_url

        resp = await self._http.get(
            f"{base}/admin/realms/{realm}/groups",
            params={"briefRepresentation": "false"},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        groups = resp.json()

        result = []
        for g in groups:
            result.append({
                "id": g["id"],
                "name": g["name"],
                "path": g.get("path", "/" + g["name"]),
                "member_count": len(g.get("members", [])),
            })
        return result

    async def close(self) -> None:
        await self._http.aclose()


# 全局单例
_sync: KeycloakSync | None = None


def get_keycloak_sync() -> KeycloakSync:
    global _sync
    if _sync is None:
        _sync = KeycloakSync()
    return _sync
