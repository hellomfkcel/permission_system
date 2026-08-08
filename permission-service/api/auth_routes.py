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
from app.role_actions_config import (
    VALID_ACTIONS,
    get_resource_actions,
    RESOURCE_TYPE_LABELS as _RESOURCE_TYPE_LABELS,
)

router = APIRouter(prefix="/api/v1/auth", tags=["admin-auth"])


# ══════════════════════════════════════════════════════════════
# 管理台 API 鉴权依赖
# ══════════════════════════════════════════════════════════════


# ── 管理员角色名（设计依据 §2.2 角色层级）──
_ADMIN_ROLES: set[str] = {"system_admin", "admin"}
_PLATFORM_ADMIN_ROLE = "platform_admin"


async def get_admin_project_ids(user_id: str, roles: list[str]) -> set[str] | None:
    """获取管理员用户可访问的项目 ID 集合。

    - platform_admin → 返回 None（表示"全部项目"，不设过滤）
    - 其他用户 → 查询 project_members 表

    project_members.user_id 存的是 Keycloak UUID，而 JWT sub 可能是用户名。
    因此需要同时按 user_id 和 user_cache.username 匹配。

    Returns:
        None: 平台超管，可访问全部项目
        set[str]: 该管理员所属的项目 ID 集合（可能为空集）
    """
    # platform_admin → 全部项目
    if _PLATFORM_ADMIN_ROLE in roles:
        return None

    from app.database import async_session
    from sqlalchemy import select
    from models.project import ProjectMember
    from models.user_cache import UserCache

    async with async_session() as db:
        # 尝试直接用 user_id 查询
        result = await db.execute(
            select(ProjectMember.project_id).where(ProjectMember.user_id == user_id)
        )
        project_ids = {row.project_id for row in result}
        if project_ids:
            return project_ids

        # 如果 user_id 查不到，可能是用户名 → 从 user_cache 查 UUID
        uc = await db.scalar(
            select(UserCache.user_id).where(UserCache.username == user_id)
        )
        if uc:
            result = await db.execute(
                select(ProjectMember.project_id).where(ProjectMember.user_id == uc)
            )
            return {row.project_id for row in result}

        return set()


async def get_current_admin(
    authorization: str | None = Header(None, alias="Authorization"),
) -> Principal:
    """从 Authorization Bearer header 解析当前管理员身份。

    所有管理台写操作 (grant/revoke/bind/unbind/add restriction) 须通过此依赖注入。

    Phase 4: 项目级隔离。返回的 Principal 包含 admin_project_ids 属性。

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

    # P0-1: 验证访问权限
    # 优先检查 JWT 角色（system_admin/admin/platform_admin → 直接通过）
    if _ADMIN_ROLES.intersection(principal.roles) or _PLATFORM_ADMIN_ROLE in principal.roles:
        return principal

    # 回退检查：用户是否在 project_members 表中（项目成员）
    # 注意：project_members.user_id 存的是 Keycloak UUID，JWT sub 可能是用户名
    from app.database import async_session
    from sqlalchemy import select
    from models.project import ProjectMember
    from models.user_cache import UserCache
    async with async_session() as db:
        member = await db.scalar(
            select(ProjectMember.id).where(ProjectMember.user_id == principal.user_id)
        )
        if not member:
            # 可能是用户名 → 从 user_cache 查 UUID
            uc = await db.scalar(
                select(UserCache.user_id).where(UserCache.username == principal.user_id)
            )
            if uc:
                member = await db.scalar(
                    select(ProjectMember.id).where(ProjectMember.user_id == uc)
                )
    if member:
        return principal

    raise HTTPException(
        status_code=403,
        detail="Access denied. You need an admin role (system_admin/admin/platform_admin) or be a project member.",
    )


async def require_platform_admin(
    admin: Principal = Depends(get_current_admin),
) -> Principal:
    """依赖注入 — 仅 platform_admin 可通过。

    用于: 项目创建/删除等平台级操作。
    """
    if _PLATFORM_ADMIN_ROLE not in admin.roles:
        raise HTTPException(
            status_code=403,
            detail="platform_admin role required for this operation.",
        )
    return admin


def require_project_member(project_id_param: str = "project_id"):
    """FastAPI 依赖工厂 — 校验当前管理员是否属于指定项目。

    用法:
        @router.get("/{project_id}/clients")
        async def list_clients(project_id: str, admin=Depends(get_current_admin),
                               _check: None = Depends(require_project_member("project_id"))):
            ...

    若管理员是 platform_admin → 直接通过。
    否则检查 project_members 表中是否有该用户的记录。
    """
    from fastapi import Request

    async def _check(
        request: Request,
        admin: Principal = Depends(get_current_admin),
    ) -> None:
        # platform_admin → 全部通过
        if _PLATFORM_ADMIN_ROLE in admin.roles:
            return

        project_id_val = request.path_params.get(project_id_param, "")
        if not project_id_val:
            raise HTTPException(status_code=400, detail="Missing project_id")

        from app.database import async_session
        from sqlalchemy import select
        from models.project import ProjectMember

        async with async_session() as db:
            row = await db.scalar(
                select(ProjectMember.id).where(
                    ProjectMember.project_id == project_id_val,
                    ProjectMember.user_id == admin.user_id,
                )
            )
            if not row:
                raise HTTPException(
                    status_code=403,
                    detail=f"You are not a member of project '{project_id_val}'.",
                )

    return _check  # 返回函数本身，让端点参数层的 Depends() 来包装


class ProjectScope:
    """当前管理员的项目访问范围。

    由 get_project_scope 依赖注入填充。
    - is_platform_admin=True → 可访问全部项目
    - project_ids 为空集 → 无项目归属，看不到任何项目数据
    """

    def __init__(self, project_ids: set[str] | None, is_platform_admin: bool):
        self._project_ids = project_ids
        self.is_platform_admin = is_platform_admin

    @property
    def project_ids(self) -> set[str] | None:
        """None = 全部项目（platform_admin）。空集 = 无可用项目。"""
        return self._project_ids

    def can_access(self, project_id: str) -> bool:
        """检查是否可以访问指定项目。"""
        if self.is_platform_admin:
            return True
        if self._project_ids is None:
            return True
        return project_id in self._project_ids

    def filter_condition(self, model_class):
        """返回 SQLAlchemy 过滤条件 — 用于查询时按项目范围过滤。

        用法:
            scope = await get_project_scope(admin=admin)
            stmt = select(ACLEntry)
            if scope.filter_condition(ACLEntry) is not None:
                stmt = stmt.where(scope.filter_condition(ACLEntry))
        """
        if self.is_platform_admin:
            return None  # 不过滤
        if self._project_ids is not None and len(self._project_ids) == 0:
            # 无项目 → 返回恒假条件
            from sqlalchemy import false
            return false()
        if self._project_ids is not None:
            return model_class.project_id.in_(self._project_ids)
        return None


async def get_project_scope(
    admin: Principal = Depends(get_current_admin),
) -> ProjectScope:
    """FastAPI 依赖注入 — 获取当前管理员可访问的项目范围。

    用法（在路由函数中）:
        @router.get("/something")
        async def list_something(scope: ProjectScope = Depends(get_project_scope), ...):
            if not scope.is_platform_admin:
                query = query.where(Model.project_id.in_(scope.project_ids))

    Returns:
        ProjectScope 对象，封装了项目访问范围。
    """
    project_ids = await get_admin_project_ids(admin.user_id, admin.roles)
    is_platform = _PLATFORM_ADMIN_ROLE in admin.roles
    return ProjectScope(project_ids=project_ids, is_platform_admin=is_platform)


# ══════════════════════════════════════════════════════════════
# 平台功能权限依赖（Phase 2a：平台级权限管理）
# ══════════════════════════════════════════════════════════════


async def _get_platform_permissions(
    db: AsyncSession, user_id: str, roles: list[str],
) -> dict[str, list[str]]:
    """查询当前管理员对平台功能的权限映射。

    返回 {feature_id: [actions]}，如 {"audit_mgmt": ["platform:read"]}。
    """
    from app.platform_features import PLATFORM_FEATURES

    permissions: dict[str, list[str]] = {}

    # platform_admin JWT 角色 → 全部功能 + 全部权限
    if _PLATFORM_ADMIN_ROLE in roles or "system_admin" in roles or "admin" in roles:
        for fid in PLATFORM_FEATURES:
            permissions[fid] = ["platform:read", "platform:write"]
        return permissions

    # 查询 platform 资源的 ACL
    from sqlalchemy import or_
    from models.acl import ACLEntry
    principals_to_check = [f"user:{user_id}"]
    if "system_admin" in roles:
        principals_to_check.append("role:system_admin")
    if "admin" in roles:
        principals_to_check.append("role:admin")

    acl_stmt = select(ACLEntry).where(
        ACLEntry.resource_type == "platform",
        ACLEntry.principal.in_(principals_to_check),
        ACLEntry.revoked == False,  # noqa: E712
    )
    acl_result = await db.execute(acl_stmt)
    for entry in acl_result.scalars():
        if entry.resource_id not in permissions:
            permissions[entry.resource_id] = []
        if entry.action not in permissions[entry.resource_id]:
            permissions[entry.resource_id].append(entry.action)

    # 查询平台角色绑定（project_id IS NULL 的角色）
    from models.role_binding import RoleBinding
    rb_stmt = select(RoleBinding).where(
        RoleBinding.principal.in_(principals_to_check),
        RoleBinding.project_id.is_(None),
        RoleBinding.revoked == False,  # noqa: E712
    )
    rb_result = await db.execute(rb_stmt)
    platform_roles: set[str] = {rb.role for rb in rb_result.scalars()}

    # platform_admin 角色绑定 → 全部权限
    if "platform_admin" in platform_roles:
        for fid in PLATFORM_FEATURES:
            permissions[fid] = ["platform:read", "platform:write"]
        return permissions

    # platform_viewer → 全部功能的 platform:read
    if "platform_viewer" in platform_roles:
        for fid in PLATFORM_FEATURES:
            if fid not in permissions:
                permissions[fid] = []
            if "platform:read" not in permissions[fid]:
                permissions[fid].append("platform:read")

    # platform_auditor → 仅审计日志和策略模拟的 platform:read
    if "platform_auditor" in platform_roles:
        for fid in ("audit_mgmt", "playground", "dashboard"):
            if fid not in permissions:
                permissions[fid] = []
            if "platform:read" not in permissions[fid]:
                permissions[fid].append("platform:read")

    # 项目成员回退：在 project_members 表中但无平台角色 → 授予基本只读权限
    # 注意：project_members.user_id 可能是 UUID，需要从 user_cache 反查
    from models.project import ProjectMember
    from models.user_cache import UserCache as _UC
    is_member = await db.scalar(
        select(ProjectMember.id).where(ProjectMember.user_id == user_id)
    )
    if not is_member:
        uc_id = await db.scalar(select(_UC.user_id).where(_UC.username == user_id))
        if uc_id:
            is_member = await db.scalar(
                select(ProjectMember.id).where(ProjectMember.user_id == uc_id)
            )
    if is_member and not permissions:
        for fid in ("dashboard", "resource_mgmt", "user_mgmt", "role_mgmt"):
            permissions[fid] = ["platform:read"]

    return permissions


def require_platform_permission(feature_id: str, action: str = "platform:read"):
    """FastAPI 依赖工厂 — 检查当前管理员是否有指定平台功能的访问权限。

    用法:
        @router.get("/something")
        async def list_something(
            admin: Principal = Depends(get_current_admin),
            _perm: None = Depends(require_platform_permission("resource_mgmt")),
        ):
            ...

    platform_admin/system_admin/admin JWT 角色 → 直接通过（不查 DB）。
    其他管理员 → 查询 platform ACL 和平台角色绑定。
    """
    async def _check(
        admin: Principal = Depends(get_current_admin),
    ) -> None:
        # 管理员角色直接通过（platform_admin, system_admin, admin）
        if _ADMIN_ROLES.intersection(admin.roles) or _PLATFORM_ADMIN_ROLE in admin.roles:
            return

        # 查询平台权限
        from app.database import async_session
        async with async_session() as db:
            perms = await _get_platform_permissions(db, admin.user_id, admin.roles)
            actions = perms.get(feature_id, [])
            if action not in actions:
                # platform:write 隐含 platform:read
                if action == "platform:read" and "platform:write" in actions:
                    return
                raise HTTPException(
                    status_code=403,
                    detail=f"Insufficient platform permission: {action} on {feature_id}",
                )

    return _check


class PlatformAccessResponse(BaseModel):
    """当前管理员的平台访问权限响应。"""
    user_id: str
    roles: list[str]
    is_platform_admin: bool
    features: dict[str, str] = Field(default_factory=dict)  # feature_id → display_name
    permissions: dict[str, list[str]] = Field(default_factory=dict)  # feature_id → [actions]
    project_ids: list[str] = Field(default_factory=list)  # 可访问的项目 ID 列表 (null=all)


@router.get("/me/access", response_model=PlatformAccessResponse)
async def get_my_platform_access(
    admin: Principal = Depends(get_current_admin),
) -> PlatformAccessResponse:
    """返回当前管理员的平台访问权限、角色和项目范围。

    管理台前端在登录后调用此端点以决定：
    - 侧边栏显示哪些功能
    - 每个功能是只读还是可写
    - 可访问的项目列表
    """
    from app.platform_features import PLATFORM_FEATURES
    from app.database import async_session

    is_platform_admin = _PLATFORM_ADMIN_ROLE in admin.roles or "system_admin" in admin.roles

    async with async_session() as db:
        perms = await _get_platform_permissions(db, admin.user_id, admin.roles)

    # 项目列表
    project_ids_list: list[str] = []
    if is_platform_admin:
        # platform_admin → 返回 None 表示全部项目（JSON 中用特殊标记）
        project_ids_list = []  # 空列表 = 全部项目
    else:
        pids = await get_admin_project_ids(admin.user_id, admin.roles)
        if pids is not None:
            project_ids_list = sorted(pids)

    return PlatformAccessResponse(
        user_id=admin.user_id,
        roles=admin.roles,
        is_platform_admin=is_platform_admin,
        features=PLATFORM_FEATURES,
        permissions=perms,
        project_ids=project_ids_list,
    )


# ══════════════════════════════════════════════════════════════
# 请求/响应模型
# ══════════════════════════════════════════════════════════════


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
    projects: list[str] = []      # 用户所属的项目 ID 列表（来自 project_members）
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
    # 开发模式：system_admin 自动获得 platform_admin（平台超管）
    if "system_admin" in roles and "platform_admin" not in roles:
        roles.append("platform_admin")

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
    admin: Principal = Depends(get_current_admin),
    project_id: str | None = Query(None, description="按项目 ID 过滤用户（可选）"),
) -> list[UserInfo]:
    """查询用户列表。需要管理员认证。

    三级用户模型：租户 → 项目 → 用户
    - 平台模式（不传 project_id）：返回 user_cache 全部用户 + 租户归属 + 项目归属
    - 项目模式（传 project_id）：仅返回 project_members 中的项目成员
    """
    from models.tenant import TenantMembership, Tenant
    from models.project import ProjectMember as _PM
    from sqlalchemy import func as sa_func

    # ── 项目模式：从 project_members 获取用户 ID 列表 ──
    project_user_ids: set[str] | None = None
    if project_id:
        pm_rows = await db.execute(
            select(_PM.user_id).where(_PM.project_id == project_id)
        )
        project_user_ids = {row[0] for row in pm_rows}

    # ── 查询用户 ──
    stmt = select(UserCache).where(UserCache.enabled == True).order_by(UserCache.username).limit(500)  # noqa: E712
    result = await db.execute(stmt)
    all_users = result.scalars().all()

    # 项目模式：过滤到仅项目成员
    if project_user_ids is not None:
        users = [
            u for u in all_users
            if u.user_id in project_user_ids or (u.username and u.username in project_user_ids)
        ]
    else:
        users = all_users

    # ── 批量查询项目归属（所有模式）──
    user_ids = [u.user_id for u in users]
    usernames = [u.username for u in users if u.username]
    user_refs = [f"user:{uid}" for uid in user_ids] + [f"user:{uname}" for uname in usernames]

    # project_members → user_id → [(project_id, role)]
    project_map: dict[str, list[tuple[str, str]]] = {}
    if user_refs:
        # project_members.user_id 存的可能是 "admin" 而非 "user:admin"
        pm_user_ids = set(user_ids) | set(usernames)
        pm_result = await db.execute(
            select(_PM.user_id, _PM.project_id, _PM.role).where(
                _PM.user_id.in_(pm_user_ids)
            )
        )
        for row in pm_result:
            pm_user = row[0]
            if pm_user not in project_map:
                project_map[pm_user] = []
            project_map[pm_user].append((row[1], row[2]))

    # tenant_memberships → user_ref → [(tenant_id, tenant_name)]
    memberships: dict[str, list[tuple[str, str]]] = {}
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
            tm_user_id = row[0]
            tm_tenant_id = row[1]
            tm_tenant_name = row[2]
            if tm_user_id not in memberships:
                memberships[tm_user_id] = []
            memberships[tm_user_id].append((tm_tenant_id, tm_tenant_name))

    def _lookup_tenants(u: UserCache) -> tuple[str, list[str]]:
        candidates = [f"user:{u.user_id}"]
        if u.username:
            candidates.append(f"user:{u.username}")
        all_tenants: list[str] = []
        for ref in candidates:
            if ref in memberships:
                for tid, _tname in memberships[ref]:
                    if tid not in all_tenants:
                        all_tenants.append(tid)
        primary = all_tenants[0] if all_tenants else (u.tenant_id or "")
        return primary, all_tenants

    def _lookup_projects(u: UserCache) -> list[str]:
        """查找用户所属的项目 ID 列表。"""
        result: list[str] = []
        if u.user_id in project_map:
            result.extend([pid for pid, _role in project_map[u.user_id]])
        if u.username and u.username in project_map:
            for pid, _role in project_map[u.username]:
                if pid not in result:
                    result.append(pid)
        return result

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
            projects=_lookup_projects(u),
            roles=_extract_names(u.roles),
            groups=groups,
            principals=[f"user:{u.user_id}"] + [f"group:{g}" for g in groups],
        ))
    return result_list


@router.get("/groups")
async def list_groups_from_keycloak(
    admin: Principal = Depends(get_current_admin),
) -> list[dict]:
    """从 Keycloak 实时获取组列表（管理台用户/组管理页使用）。需要管理员认证。"""
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


# ── 数据驱动统计模型 ──


class ResourceStat(BaseModel):
    """单个资源类型的统计条目。"""
    resource_type: str       # "kb", "document" 等
    label: str               # 人类可读标签
    count: int


class RestrictionStat(BaseModel):
    """单个封禁类型的统计条目。"""
    restriction_type: str    # "subject_ban", "resource_restriction"
    label: str
    count: int


class DashboardStats(BaseModel):
    """Dashboard 数据驱动统计响应。

    不再硬编码 kb_count/document_count 等字段；
    资源统计按 resource_type GROUP BY 动态返回，
    新增资源类型时无需修改此模型。
    """
    project_count: int = 0              # 平台模式：项目总数（项目模式为0）
    resource_stats: list[ResourceStat] = Field(default_factory=list)
    user_count: int = 0
    acl_count: int = 0
    restriction_stats: list[RestrictionStat] = Field(default_factory=list)
    recent_changes: int = 0             # 24 小时内变更数


# ── 资源类型 → 标签映射（与 VALID_RESOURCE_TYPES 对齐）──
_RESOURCE_LABELS: dict[str, str] = {
    "kb": "知识库",
    "document": "文档",
    "platform": "平台功能",
}

# ── 封禁类型 → 标签映射 ──
_RESTRICTION_LABELS: dict[str, str] = {
    "subject_ban": "主体封禁",
    "resource_restriction": "资源限制",
}


@router.get("/stats", response_model=DashboardStats)
async def get_dashboard_stats(
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    project_id: str | None = Query(None, description="按项目 ID 过滤统计（可选）"),
) -> DashboardStats:
    """Dashboard 数据驱动概览统计。需要管理员认证。

    平台模式（不传 project_id）：
      - project_count = 活跃项目总数
      - resource_stats = 全部项目按 resource_type 分组的统计
    项目模式（传 project_id）：
      - project_count = 0
      - resource_stats = 仅该项目的资源统计

    所有统计走 GROUP BY 动态查询，新增资源类型无需修改代码。
    """
    from models.resource import ResourceRegistry
    from models.acl import ACLEntry
    from models.restriction import Restriction
    from models.change_log import PermissionChange
    from models.project import Project
    from sqlalchemy import func as sa_func

    resource_stats: list[ResourceStat] = []
    project_count = 0

    # ── 平台模式：项目总数 ──
    if not project_id:
        proj_res = await db.execute(
            select(sa_func.count()).select_from(Project).where(
                Project.status == "active",
            )
        )
        project_count = proj_res.scalar() or 0

    # ── 资源统计：按 resource_type GROUP BY ──
    res_conditions = [ResourceRegistry.retired == False]  # noqa: E712
    if project_id:
        res_conditions.append(ResourceRegistry.project_id == project_id)

    res_stmt = (
        select(
            ResourceRegistry.resource_type,
            sa_func.count(),
        )
        .where(*res_conditions)
        .group_by(ResourceRegistry.resource_type)
        .order_by(ResourceRegistry.resource_type)
    )
    res_rows = await db.execute(res_stmt)
    for row in res_rows:
        rtype, cnt = row[0], row[1]
        resource_stats.append(ResourceStat(
            resource_type=rtype,
            label=_RESOURCE_LABELS.get(rtype, rtype),
            count=cnt or 0,
        ))

    # ── 用户计数 ──
    # 项目模式：统计该项目成员数（project_members 表）
    # 平台模式：统计全部 Keycloak 同步用户（user_cache 表）
    user_count = 0
    if project_id:
        from models.project import ProjectMember as _PM
        user_res = await db.execute(
            select(sa_func.count()).select_from(_PM).where(
                _PM.project_id == project_id,
            )
        )
        user_count = user_res.scalar() or 0
    else:
        user_res = await db.execute(
            select(sa_func.count()).select_from(UserCache).where(
                UserCache.enabled == True  # noqa: E712
            )
        )
        user_count = user_res.scalar() or 0

    # ── ACL 计数 ──
    acl_conditions = [ACLEntry.revoked == False]  # noqa: E712
    if project_id:
        acl_conditions.append(ACLEntry.project_id == project_id)
    acl_res = await db.execute(
        select(sa_func.count()).select_from(ACLEntry).where(*acl_conditions)
    )
    acl_count = acl_res.scalar() or 0

    # ── 封禁统计：按 restriction_type GROUP BY ──
    restriction_stats: list[RestrictionStat] = []
    restr_conditions = [Restriction.removed == False]  # noqa: E712
    if project_id:
        restr_conditions.append(Restriction.project_id == project_id)

    restr_stmt = (
        select(
            Restriction.restriction_type,
            sa_func.count(),
        )
        .where(*restr_conditions)
        .group_by(Restriction.restriction_type)
        .order_by(Restriction.restriction_type)
    )
    restr_rows = await db.execute(restr_stmt)
    for row in restr_rows:
        rtype, cnt = row[0], row[1]
        restriction_stats.append(RestrictionStat(
            restriction_type=rtype,
            label=_RESTRICTION_LABELS.get(rtype, rtype),
            count=cnt or 0,
        ))

    # ── 最近变更（24 小时内）──
    # 项目模式：通过 change_detail JSONB 中的 project_id 过滤
    from datetime import datetime, timedelta, timezone
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    changes_conditions = [PermissionChange.created_at >= since]
    if project_id:
        changes_conditions.append(
            PermissionChange.change_detail["project_id"].astext == project_id
        )
    changes_res = await db.execute(
        select(sa_func.count()).select_from(PermissionChange).where(*changes_conditions)
    )
    recent_changes = changes_res.scalar() or 0

    return DashboardStats(
        project_count=project_count,
        resource_stats=resource_stats,
        user_count=user_count,
        acl_count=acl_count,
        restriction_stats=restriction_stats,
        recent_changes=recent_changes,
    )


# ══════════════════════════════════════════════════════════════
# 系统配置端点（P1-6：动态配置暴露，替代 Settings 页面硬编码）
# ══════════════════════════════════════════════════════════════


async def filter_by_project_scope(
    admin: Principal,
    db: AsyncSession,
) -> set[str] | None:
    """返回当前管理员可访问的项目 ID 集合。

    None = platform_admin，不设过滤。
    空集 = 普通管理员但无项目归属，看不到任何数据。
    """
    project_ids = await get_admin_project_ids(admin.user_id, admin.roles)
    if project_ids is not None and len(project_ids) == 0:
        return set()  # 无项目归属的普通管理员
    return project_ids  # None (platform_admin) 或项目ID集合


class SystemConfigResponse(BaseModel):
    """系统运行时配置 — 供管理台 Settings 页面动态展示。"""
    service_port: int
    cerbos_pdp_url: str
    keycloak_server_url: str
    keycloak_realm: str
    rate_limits: dict
    derived_roles_count: int
    resource_rules: list[str]
    resource_actions: dict[str, list[str]] = Field(default_factory=dict)
    resource_type_labels: dict[str, str] = Field(default_factory=dict)
    platform_features: dict[str, str] = Field(default_factory=dict)


def _count_derived_roles() -> int:
    """动态统计 Cerbos YAML 中定义的派生角色数。"""
    try:
        from services.cerbos_policy_parser import parse_permissions_matrix
        matrix = parse_permissions_matrix()
        return len({r["name"] for r in matrix.get("roles", [])})
    except Exception:
        return 0


def _get_platform_features() -> dict[str, str]:
    """获取平台功能资源 ID → 显示名称映射。"""
    try:
        from app.platform_features import PLATFORM_FEATURES
        return dict(PLATFORM_FEATURES)
    except Exception:
        return {}


def _get_resource_actions(project_id: str | None = None) -> dict[str, list[str]]:
    """从 role_actions_config 动态生成 resource_type → [actions] 映射。

    project_id=None（平台模式）→ 返回全部项目的自定义资源类型；
    project_id="demo2" → 仅返回 demo2 项目的自定义类型 + 内置类型 (kb/document/platform)。
    """
    try:
        return get_resource_actions(project_id)
    except Exception:
        return {}


def _get_resource_type_labels() -> dict[str, str]:
    """资源类型 → 人类可读标签。"""
    try:
        return dict(_RESOURCE_TYPE_LABELS)
    except Exception:
        return {}


@router.get("/config", response_model=SystemConfigResponse)
async def get_system_config(
    admin: Principal = Depends(get_current_admin),
    project_id: str | None = Query(None, description="按项目过滤自定义资源类型（不传=全部）"),
) -> SystemConfigResponse:
    """返回系统运行时配置（管理台 Settings 页面动态展示）。需要管理员认证。

    project_id 由前端 API 拦截器自动注入（从 localStorage admin_current_project 读取）。
    - 平台模式（不传）：resource_actions 包含全部项目的自定义资源类型
    - 项目模式（传 project_id）：resource_actions 仅包含该项目的自定义类型 + 内置类型

    P1-6 修复：替代 Settings 页面中硬编码的端口号、限流值、策略规则数。
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
        derived_roles_count=_count_derived_roles(),
        resource_rules=sorted(VALID_ACTIONS),
        resource_actions=_get_resource_actions(project_id),
        resource_type_labels=_get_resource_type_labels(),
        platform_features=_get_platform_features(),
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
    admin: Principal = Depends(get_current_admin),
    limit: int = Query(20, description="返回条数上限"),
    project_id: str | None = Query(None, description="按项目 ID 过滤变更（可选）"),
) -> RecentChangesResponse:
    """返回最近权限变更时间线和待处理告警。需要管理员认证。

    项目模式下通过 change_detail JSONB 中的 project_id 过滤。
    """
    from models.change_log import PermissionChange
    from models.acl import ACLEntry
    from sqlalchemy import func as sa_func

    # ── 最近变更（按版本降序）──
    changes_conditions = []
    if project_id:
        changes_conditions.append(
            PermissionChange.change_detail["project_id"].astext == project_id
        )
    stmt = (
        select(PermissionChange)
        .where(*changes_conditions)
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
