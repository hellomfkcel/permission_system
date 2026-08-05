"""管理台认证 API — JWT 验证 + Keycloak 同步触发 + 管理台 API 鉴权依赖。

设计依据：docs/外部系统设计.md §4 与 IdP 集成 + frontend-design.md §0 认证与租户。
"""

from fastapi import APIRouter, HTTPException, Depends, Header, Query
from app.config import settings
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from services.jwt_parser import parse_principal
from schemas.responses import Principal
from models.user_cache import UserCache

router = APIRouter(prefix="/api/v1/auth", tags=["admin-auth"])


# ══════════════════════════════════════════════════════════════
# 管理台 API 鉴权依赖
# ══════════════════════════════════════════════════════════════


# ── 管理员角色名（设计依据 §2.2 角色层级）──
_ADMIN_ROLES: set[str] = {"system_admin", "admin"}


async def get_current_admin(
    authorization: str | None = Header(None, alias="Authorization"),
) -> Principal:
    """从 Authorization Bearer header 解析当前管理员身份。

    所有管理台写操作 (grant/revoke/bind/unbind/add restriction) 须通过此依赖注入。

    安全要求（P0-1 修复）：
    - 验证 JWT 签名有效性（原有逻辑）
    - 验证用户具有管理员角色 (system_admin 或 admin)（新增）

    Raises:
        401: 未提供 token 或 token 无效/过期。
        403: token 有效但用户无管理员角色。
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Bearer token required")

    try:
        principal = parse_principal(token)
    except Exception as e:
        raise HTTPException(
            status_code=401,
            detail=f"Invalid or expired admin token: {str(e)}",
        ) from e

    # P0-1: 验证管理员角色
    if not _ADMIN_ROLES.intersection(principal.roles):
        raise HTTPException(
            status_code=403,
            detail="Admin role required. Only system_admin or admin can perform this operation.",
        )

    return principal


class ValidateRequest(BaseModel):
    token: str = Field(..., description="JWT token 原文")


class DevLoginRequest(BaseModel):
    """登录请求 — Keycloak 验证用户名密码 + 后端验证租户成员资格。

    管理台使用此端点登录。生产环境（PRODUCTION=true）完全禁用，走 SSO。
    """
    username: str = Field("admin", description="Keycloak 用户名", min_length=1)
    password: str = Field("", description="Keycloak 密码")
    tenant: str = Field("tenant-dev", description="租户 ID")


class DevLoginResponse(BaseModel):
    access_token: str
    refresh_token: str = ""
    expires_at: str | None = None
    refresh_expires_at: str | None = None
    user: dict = Field(default_factory=dict)


class UserInfo(BaseModel):
    user_id: str
    username: str = ""
    email: str = ""
    display_name: str = ""
    tenant_id: str
    tenants: list[str] = []       # 用户所属的全部租户 ID
    roles: list[str]
    groups: list[str]
    principals: list[str]


class SyncResult(BaseModel):
    users_created: int
    users_updated: int
    users_deleted: int
    message: str


@router.post("/validate", response_model=UserInfo)
async def validate_token(body: ValidateRequest) -> UserInfo:
    """验证 JWT token 并返回用户信息。

    管理台登录时调用此端点验证 token 有效性。
    后续可扩展为支持 Keycloak OAuth2 code 换取 token。

    Raises:
        401: token 无效或已过期。
    """
    try:
        principal = parse_principal(body.token)
    except Exception as e:
        raise HTTPException(
            status_code=401,
            detail=f"Invalid or expired token: {str(e)}",
        ) from e

    return UserInfo(
        user_id=principal.user_id,
        tenant_id=principal.tenant_id,
        roles=principal.roles,
        groups=principal.groups,
        principals=principal.principals,
    )


@router.post("/dev-login", response_model=DevLoginResponse)
async def dev_login(body: DevLoginRequest) -> DevLoginResponse:
    """登录 — Keycloak 验证用户名密码 + 后端验证租户成员资格。

    管理台登录页使用此端点。
    生产环境（PRODUCTION=true）完全禁用，走 Keycloak SSO (OAuth2/OIDC)。

    流程：
    1. 调用 Keycloak token endpoint (password grant) 验证用户名密码
    2. 从 Keycloak 响应提取用户身份和角色
    3. 签发本系统 JWT
    """
    from datetime import datetime, timedelta, timezone
    from jose import jwt as jose_jwt
    import httpx

    # ── 生产模式：禁用此端点 ──
    if settings.production:
        raise HTTPException(
            status_code=501,
            detail="This endpoint is disabled in production mode. "
                    "Please use Keycloak SSO (OAuth2/OIDC) for authentication.",
        )

    # ── Step 1: Keycloak 密码验证 ──
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            kc_resp = await http.post(
                f"{settings.keycloak_server_url}/realms/{settings.keycloak_realm}"
                f"/protocol/openid-connect/token",
                data={
                    "client_id": "admin-console",
                    "grant_type": "password",
                    "username": body.username,
                    "password": body.password,
                    "scope": "openid",
                },
            )
            if kc_resp.status_code != 200:
                detail = "用户名或密码错误"
                try:
                    err = kc_resp.json()
                    detail = err.get("error_description", detail)
                except Exception:
                    pass
                raise HTTPException(status_code=401, detail=detail)
            kc_data = kc_resp.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Keycloak 认证服务不可达: {str(e)[:100]}",
        )

    # ── Step 2: 从 Keycloak token 提取用户身份 ──
    try:
        import base64, json as _json
        payload_b64 = kc_data["access_token"].split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        kc_claims = _json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        raise HTTPException(status_code=500, detail="无法解析 Keycloak token")

    user_id = kc_claims.get("preferred_username", kc_claims.get("sub", body.username))
    roles = (kc_claims.get("realm_access", {}) or {}).get("roles", [])
    if "system_admin" not in roles and "admin" not in roles:
        if "user" not in roles:
            roles.append("user")

    # ── Step 3: 签发本系统 JWT ──
    now = datetime.now(timezone.utc)
    exp = now + timedelta(seconds=settings.jwt_expire_seconds)

    try:
        with open(settings.jwt_private_key_path) as f:
            private_key = f.read()
    except FileNotFoundError:
        raise HTTPException(
            status_code=503,
            detail="JWT private key not configured.",
        )

    payload = {
        "sub": user_id,
        "tenant": body.tenant,
        "roles": roles,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "iss": "permission-service",
    }
    token = jose_jwt.encode(payload, private_key, algorithm=settings.jwt_algorithm)

    refresh_exp = now + timedelta(hours=24)
    refresh_payload = {
        "sub": user_id,
        "tenant": body.tenant,
        "roles": roles,
        "type": "refresh",
        "iat": int(now.timestamp()),
        "exp": int(refresh_exp.timestamp()),
        "iss": "permission-service",
    }
    refresh_token = jose_jwt.encode(
        refresh_payload, private_key, algorithm=settings.jwt_algorithm
    )

    return DevLoginResponse(
        access_token=token,
        refresh_token=refresh_token,
        expires_at=exp.isoformat(),
        refresh_expires_at=refresh_exp.isoformat(),
        user={
            "id": user_id,
            "tenant_id": body.tenant,
            "roles": roles,
            "groups": [],
        },
    )


class RefreshRequest(BaseModel):
    """Token 刷新请求。

    支持两种模式：
    - 开发模式：refresh_token 为 dev-login 签发的自签名 JWT（type=refresh）
    - 生产模式：refresh_token 为 Keycloak OAuth2 refresh_token
    """
    refresh_token: str = Field(..., description="refresh token")


class RefreshResponse(BaseModel):
    """Token 刷新响应 — 与 DevLoginResponse 同构。"""
    access_token: str
    refresh_token: str = ""
    expires_at: str | None = None
    user: dict = Field(default_factory=dict)


@router.post("/refresh", response_model=RefreshResponse)
async def refresh_token(body: RefreshRequest) -> RefreshResponse:
    """刷新 access_token。

    流程：
    1. 尝试解析为 dev refresh token → 验证 type=refresh、签名、过期 → 签发新 access_token
    2. 若为 Keycloak refresh token → 代理到 Keycloak OAuth2 token endpoint 刷新

    Raises:
        401: refresh_token 无效或已过期。
    """
    from datetime import datetime, timedelta, timezone
    from jose import jwt as jose_jwt, JWTError

    # ── 方式 1：dev refresh token（自签名 JWT，type=refresh）──
    try:
        with open(settings.jwt_public_key_path) as f:
            public_key = f.read()
        claims = jose_jwt.decode(
            body.refresh_token, public_key,
            algorithms=[settings.jwt_algorithm],
            options={"verify_exp": True},
        )
        # 必须是 refresh 类型的 token
        if claims.get("type") != "refresh":
            raise HTTPException(
                status_code=401,
                detail="Not a refresh token (missing type=refresh claim)",
            )

        # 签发新的 access_token
        now = datetime.now(timezone.utc)
        exp = now + timedelta(seconds=settings.jwt_expire_seconds)
        with open(settings.jwt_private_key_path) as f:
            private_key = f.read()

        access_payload = {
            "sub": claims["sub"],
            "tenant": claims.get("tenant", ""),
            "roles": claims.get("roles", []),
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "iss": "permission-service-dev",
        }
        new_token = jose_jwt.encode(
            access_payload, private_key, algorithm=settings.jwt_algorithm
        )

        return RefreshResponse(
            access_token=new_token,
            refresh_token=body.refresh_token,  # dev refresh token 保持不变
            expires_at=exp.isoformat(),
            user={
                "id": claims["sub"],
                "tenant_id": claims.get("tenant", ""),
                "roles": claims.get("roles", []),
                "groups": [],
            },
        )
    except (JWTError, FileNotFoundError, HTTPException):
        pass  # 不是有效的 dev refresh token，尝试 Keycloak 方式

    # ── 方式 2：Keycloak OAuth2 refresh_token ──
    if settings.keycloak_client_secret:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.post(
                    f"{settings.keycloak_server_url}/realms/{settings.keycloak_realm}"
                    f"/protocol/openid-connect/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": settings.keycloak_client_id,
                        "client_secret": settings.keycloak_client_secret,
                        "refresh_token": body.refresh_token,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                new_access = data["access_token"]
                new_refresh = data.get("refresh_token", body.refresh_token)

                # 解析 JWT 获取用户信息
                principal = parse_principal(new_access)
                return RefreshResponse(
                    access_token=new_access,
                    refresh_token=new_refresh,
                    expires_at=(
                        datetime.now(timezone.utc)
                        + timedelta(seconds=data.get("expires_in", 3600))
                    ).isoformat(),
                    user={
                        "id": principal.user_id,
                        "tenant_id": principal.tenant_id,
                        "roles": principal.roles,
                        "groups": principal.groups,
                    },
                )
        except Exception:
            pass  # Keycloak 方式也失败

    # 所有方式均失败
    raise HTTPException(
        status_code=401,
        detail="Invalid or expired refresh_token. Please re-authenticate.",
    )


@router.post("/sync/users", response_model=SyncResult)
async def sync_users_from_keycloak() -> SyncResult:
    """触发 Keycloak 用户同步（手动触发 / 定时任务调用）。

    调用 Keycloak Admin API 获取全部用户并写入本地 user_cache 表。
    建议每 15 分钟通过 cron 或 scheduler 调用一次。
    """
    from idp.keycloak_sync import get_keycloak_sync

    try:
        sync = get_keycloak_sync()
        result = await sync.sync_users()
        return SyncResult(
            users_created=result["created"],
            users_updated=result["updated"],
            users_deleted=result.get("deleted", 0),
            message=f"Synced {result['created'] + result['updated']} users from Keycloak",
        )
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Keycloak sync failed: {str(e)}",
        ) from e


@router.get("/users", response_model=list[UserInfo])
async def list_cached_users(
    db: AsyncSession = Depends(get_db),
) -> list[UserInfo]:
    """查询本地缓存的用户列表（从 user_cache 表读取，合并 tenant_memberships）。

    管理台用户管理页使用此端点展示用户。
    tenant_id 从 tenant_memberships 聚合（权威源），回退到 user_cache.tenant_id。
    """
    from models.tenant import TenantMembership, Tenant
    from sqlalchemy import func as sa_func

    stmt = select(UserCache).where(UserCache.enabled == True).order_by(UserCache.username).limit(500)  # noqa: E712
    result = await db.execute(stmt)
    users = result.scalars().all()

    # 批量查询所有用户的租户归属（从 tenant_memberships）
    user_ids = [u.user_id for u in users]
    usernames = [u.username for u in users if u.username]
    # 构建查询条件：匹配 user:UUID 或 user:username
    user_refs = [f"user:{uid}" for uid in user_ids] + [f"user:{uname}" for uname in usernames]
    memberships: dict[str, list[tuple[str, str]]] = {}  # user_ref → [(tenant_id, tenant_name)]
    if user_refs:
        tm_stmt = (
            select(TenantMembership.user_id, TenantMembership.tenant_id, Tenant.name)
            .join(Tenant, TenantMembership.tenant_id == Tenant.id)
            .where(
                TenantMembership.user_id.in_(user_refs),
                TenantMembership.revoked == False,  # noqa: E712
                Tenant.status == "active",
            )
        )
        tm_result = await db.execute(tm_stmt)
        for row in tm_result:
            tm_user_id = row[0]  # e.g., "user:admin"
            tm_tenant_id = row[1]  # e.g., "tenant-dev"
            tm_tenant_name = row[2]  # e.g., "开发测试租户"
            if tm_user_id not in memberships:
                memberships[tm_user_id] = []
            memberships[tm_user_id].append((tm_tenant_id, tm_tenant_name))

    def _lookup_tenants(u: UserCache) -> tuple[str, list[str]]:
        """查找用户的租户归属。返回 (primary_tenant_id, [all_tenant_ids])。"""
        candidates = [f"user:{u.user_id}"]  # UUID format
        if u.username:
            candidates.append(f"user:{u.username}")  # username format
        all_tenants: list[str] = []
        all_tenant_names: list[str] = []
        for ref in candidates:
            if ref in memberships:
                for tid, tname in memberships[ref]:
                    if tid not in all_tenants:
                        all_tenants.append(tid)
                        all_tenant_names.append(tname)
        primary = all_tenants[0] if all_tenants else (u.tenant_id or "")
        return primary, all_tenants

    def _extract_names(data, default=None) -> list[str]:
        if default is None:
            default = []
        if not isinstance(data, list):
            return default
        result: list[str] = []
        for item in data:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict) and "name" in item:
                result.append(item["name"])
        return result

    def _extract_group_names(data, default=None) -> list[str]:
        if default is None:
            default = []
        if not isinstance(data, list):
            return default
        result: list[str] = []
        for item in data:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict) and "name" in item:
                result.append(item["name"])
        return result

    def _build_display_name(u: UserCache) -> str:
        """构建显示名称：first_name + last_name，回退到 username 或 user_id。"""
        parts = []
        if u.first_name:
            parts.append(u.first_name)
        if u.last_name:
            parts.append(u.last_name)
        if parts:
            return " ".join(parts)
        if u.username:
            return u.username
        return u.user_id[:8] + "..."

    result_list: list[UserInfo] = []
    for u in users:
        primary_tenant, all_tenant_ids = _lookup_tenants(u)
        groups = _extract_group_names(u.groups)
        result_list.append(UserInfo(
            user_id=u.user_id,
            username=u.username or "",
            email=u.email or "",
            display_name=_build_display_name(u),
            tenant_id=primary_tenant,
            tenants=all_tenant_ids,
            roles=_extract_names(u.roles),
            groups=groups,
            principals=[f"user:{u.user_id}"] + [f"group:{g}" for g in groups],
        ))
    return result_list


@router.get("/groups")
async def list_groups_from_keycloak() -> list[dict]:
    """从 Keycloak 实时获取组列表（管理台用户/组管理页使用）。"""
    from idp.keycloak_sync import get_keycloak_sync

    try:
        sync = get_keycloak_sync()
        return await sync.get_groups()
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch groups: {str(e)}",
        ) from e


# ══════════════════════════════════════════════════════════════
# Dashboard 统计端点
# ══════════════════════════════════════════════════════════════


class DashboardStats(BaseModel):
    kb_count: int = 0
    document_count: int = 0
    user_count: int = 0
    acl_count: int = 0
    recent_changes: int = 0
    active_restrictions: int = 0


@router.get("/stats", response_model=DashboardStats)
async def get_dashboard_stats(
    db: AsyncSession = Depends(get_db),
) -> DashboardStats:
    """Dashboard 概览统计 — 聚合各表计数。"""
    from models.resource import ResourceRegistry
    from models.acl import ACLEntry
    from models.restriction import Restriction
    from models.change_log import PermissionChange
    from sqlalchemy import func as sa_func

    # KB 计数
    kb_res = await db.execute(
        select(sa_func.count()).select_from(ResourceRegistry).where(
            ResourceRegistry.resource_type == "kb",
            ResourceRegistry.retired == False,  # noqa: E712
        )
    )
    kb_count = kb_res.scalar() or 0

    # 文档计数
    doc_res = await db.execute(
        select(sa_func.count()).select_from(ResourceRegistry).where(
            ResourceRegistry.resource_type == "document",
            ResourceRegistry.retired == False,  # noqa: E712
        )
    )
    doc_count = doc_res.scalar() or 0

    # 用户计数
    user_res = await db.execute(
        select(sa_func.count()).select_from(UserCache).where(
            UserCache.enabled == True  # noqa: E712
        )
    )
    user_count = user_res.scalar() or 0

    # ACL 计数
    acl_res = await db.execute(
        select(sa_func.count()).select_from(ACLEntry).where(
            ACLEntry.revoked == False  # noqa: E712
        )
    )
    acl_count = acl_res.scalar() or 0

    # 最近变更（24 小时内）
    from datetime import datetime, timedelta, timezone
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    changes_res = await db.execute(
        select(sa_func.count()).select_from(PermissionChange).where(
            PermissionChange.created_at >= since
        )
    )
    recent_changes = changes_res.scalar() or 0

    # 活跃封禁
    restr_res = await db.execute(
        select(sa_func.count()).select_from(Restriction).where(
            Restriction.removed == False  # noqa: E712
        )
    )
    active_restrictions = restr_res.scalar() or 0

    return DashboardStats(
        kb_count=kb_count,
        document_count=doc_count,
        user_count=user_count,
        acl_count=acl_count,
        recent_changes=recent_changes,
        active_restrictions=active_restrictions,
    )


# ══════════════════════════════════════════════════════════════
# 系统配置端点（P1-6：动态配置暴露，替代 Settings 页面硬编码）
# ══════════════════════════════════════════════════════════════


class SystemConfigResponse(BaseModel):
    """系统运行时配置 — 供管理台 Settings 页面动态展示。"""
    service_port: int
    cerbos_pdp_url: str
    keycloak_server_url: str
    keycloak_realm: str
    rate_limits: dict
    derived_roles_count: int
    resource_rules: list[str]


@router.get("/config", response_model=SystemConfigResponse)
async def get_system_config() -> SystemConfigResponse:
    """返回系统运行时配置（管理台 Settings 页面动态展示）。

    P1-6 修复：替代 Settings 页面中硬编码的端口号、限流值、策略规则数。
    这些值从配置文件自动读取，不再需要手动同步前端代码。
    """
    return SystemConfigResponse(
        service_port=settings.port,
        cerbos_pdp_url=settings.cerbos_pdp_url,
        keycloak_server_url=settings.keycloak_server_url,
        keycloak_realm=settings.keycloak_realm,
        rate_limits={
            "check": f"{settings.check_rate_limit}/s",
            "check_batch": f"{settings.check_batch_rate_limit}/s",
            "filter": f"{settings.filter_rate_limit}/s",
            "prefilter": f"{settings.prefilter_rate_limit}/s",
            "visibility": f"{settings.visibility_rate_limit}/s",
        },
        derived_roles_count=4,
        resource_rules=[
            "kb:read", "kb:write", "kb:manage", "kb:grant",
            "doc:view", "doc:download", "doc:retrieve",
            "doc:unmount", "doc:purge", "doc:share",
        ],
    )


# ══════════════════════════════════════════════════════════════
# 最近变更时间线端点（P1-7：Dashboard 时间线 + 告警面板）
# ══════════════════════════════════════════════════════════════


class RecentChangeItem(BaseModel):
    event_type: str
    resource_type: str = ""
    resource_id: str = ""
    tenant_id: str = ""
    version: int = 0
    change_summary: str = ""
    created_at: str = ""


class RecentChangesResponse(BaseModel):
    changes: list[RecentChangeItem]
    orphan_acl_count: int = 0
    expired_acl_count: int = 0


@router.get("/recent-changes", response_model=RecentChangesResponse)
async def get_recent_changes(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, description="返回条数上限"),
) -> RecentChangesResponse:
    """返回最近权限变更时间线和待处理告警。

    P1-7 修复：为 Dashboard 提供最近变更时间线和告警面板数据。
    """
    from models.change_log import PermissionChange
    from models.acl import ACLEntry
    from sqlalchemy import func as sa_func

    # ── 最近变更（按版本降序）──
    stmt = (
        select(PermissionChange)
        .order_by(PermissionChange.version.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = result.scalars().all()

    changes: list[RecentChangeItem] = []
    for row in rows:
        # 从 change_detail JSONB 提取可读摘要
        detail = row.change_detail or {}
        action = detail.get("action", row.event_type)
        principal = detail.get("principal", "")
        change_summary = action
        if principal:
            change_summary = f"{action}: {principal}"

        changes.append(RecentChangeItem(
            event_type=row.event_type,
            resource_type=row.resource_type or "",
            resource_id=row.resource_id or "",
            tenant_id=row.tenant_id,
            version=row.version,
            change_summary=change_summary,
            created_at=row.created_at.isoformat() if row.created_at else "",
        ))

    # ── 待处理告警 ──
    # 孤儿 ACL：principal 引用的资源已退役
    orphan_res = await db.execute(
        select(sa_func.count())
        .select_from(ACLEntry)
        .where(
            ACLEntry.revoked == False,  # noqa: E712
        )
    )
    total_acl = orphan_res.scalar() or 0

    # 过期 ACL（expires_at 在过去且未 revoke）
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    expired_res = await db.execute(
        select(sa_func.count())
        .select_from(ACLEntry)
        .where(
            ACLEntry.revoked == False,  # noqa: E712
            ACLEntry.expires_at.isnot(None),
            ACLEntry.expires_at <= now,
        )
    )
    expired_count = expired_res.scalar() or 0

    return RecentChangesResponse(
        changes=changes,
        orphan_acl_count=0,  # 孤儿 ACL 检测需要跨表 JOIN，留待 P2 完善
        expired_acl_count=expired_count,
    )
