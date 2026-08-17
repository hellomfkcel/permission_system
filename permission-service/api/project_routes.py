"""项目管理 API — 项目/客户端/API Key/受众 CRUD。

将硬编码注册表迁移为 DB 驱动的项目管理。
所有端点需要 system_admin 认证。

"""

import hashlib
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from api.auth_routes import get_current_admin, require_project_member, require_platform_admin, require_platform_permission
from schemas.responses import Principal
from models.project import Project, ProjectClient, ProjectApiKey, ProjectAudience

router = APIRouter(prefix="/api/v1/projects", tags=["admin-projects"])


# ── 响应模型 ──


class ProjectOut(BaseModel):
    id: str
    name: str
    description: str = ""
    status: str
    client_count: int = 0
    audience_count: int = 0
    api_key_count: int = 0
    created_at: str = ""


class ProjectClientOut(BaseModel):
    id: str
    project_id: str
    client_id: str
    description: str = ""


class ProjectApiKeyOut(BaseModel):
    id: str
    project_id: str
    key_prefix: str
    description: str = ""
    revoked: bool
    created_at: str = ""
    expires_at: str | None = None


class ProjectApiKeyCreated(BaseModel):
    id: str
    project_id: str
    api_key: str = Field(..., description="原始 API Key — 仅此时显示，请妥善保存")
    key_prefix: str
    message: str = "API Key created. Save it now — it will not be shown again."


class ProjectAudienceOut(BaseModel):
    id: str
    project_id: str
    audience: str


# ── 请求模型 ──


class CreateProjectRequest(BaseModel):
    id: str = Field(..., min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9-]+$")
    name: str = Field(..., min_length=1, max_length=255)
    description: str = ""


class CreateClientRequest(BaseModel):
    client_id: str = Field(..., min_length=1, max_length=128)
    description: str = ""


class CreateApiKeyRequest(BaseModel):
    description: str = ""


class CreateAudienceRequest(BaseModel):
    audience: str = Field(..., min_length=1, max_length=128)


# ── Project CRUD ──


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    _perm: None = Depends(require_platform_permission("project_mgmt", "platform:read")),
) -> list[ProjectOut]:
    """列出当前管理员可访问的项目。

    - platform_admin → 全部项目
    - system_admin / admin → 仅 project_members 中的项目
    """
    from api.auth_routes import get_admin_project_ids

    allowed = await get_admin_project_ids(admin.user_id, admin.roles)

    query = select(Project).order_by(Project.created_at.desc())
    if allowed is not None:
        # 非 platform_admin → 仅返回所属项目
        if not allowed:
            return []
        query = query.where(Project.id.in_(allowed))

    rows = await db.execute(query)
    projects = rows.scalars().all()

    result: list[ProjectOut] = []
    for p in projects:
        client_cnt = await db.scalar(
            select(sa_func.count()).select_from(ProjectClient).where(
                ProjectClient.project_id == p.id
            )
        )
        audience_cnt = await db.scalar(
            select(sa_func.count()).select_from(ProjectAudience).where(
                ProjectAudience.project_id == p.id
            )
        )
        key_cnt = await db.scalar(
            select(sa_func.count()).select_from(ProjectApiKey).where(
                ProjectApiKey.project_id == p.id,
                ProjectApiKey.revoked == False,  # noqa: E712
            )
        )
        result.append(ProjectOut(
            id=p.id, name=p.name, description=p.description or "",
            status=p.status,
            client_count=client_cnt or 0,
            audience_count=audience_cnt or 0,
            api_key_count=key_cnt or 0,
            created_at=p.created_at.isoformat() if p.created_at else "",
        ))
    return result


@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(
    body: CreateProjectRequest,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(require_platform_admin),
) -> ProjectOut:
    """注册新项目。需要 platform_admin 权限。"""
    existing = await db.scalar(select(Project.id).where(Project.id == body.id))
    if existing:
        raise HTTPException(status_code=409, detail=f"Project '{body.id}' already exists")

    p = Project(id=body.id, name=body.name, description=body.description)
    db.add(p)

    # 自动将创建者添加为 project_admin
    from models.project import ProjectMember
    db.add(ProjectMember(
        project_id=body.id,
        user_id=admin.user_id,
        role="project_admin",
        granted_by="auto-on-create",
    ))

    await db.commit()
    await db.refresh(p)

    return ProjectOut(
        id=p.id, name=p.name, description=p.description or "",
        status=p.status,
        created_at=p.created_at.isoformat() if p.created_at else "",
    )


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(require_platform_admin),
    _check: None = Depends(require_project_member("project_id")),
) -> None:
    """软删除项目（status=deleted）。需要 platform_admin 权限。"""
    p = await db.scalar(select(Project).where(Project.id == project_id))
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    if p.id == "rag-v14":
        raise HTTPException(status_code=403, detail="Cannot delete the built-in RAG project")
    p.status = "deleted"
    p.updated_at = datetime.now(timezone.utc)
    await db.commit()


# ── Client 管理 ──


@router.get("/{project_id}/clients", response_model=list[ProjectClientOut])
async def list_project_clients(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    _check: None = Depends(require_project_member("project_id")),
) -> list[ProjectClientOut]:
    """列出项目的已注册 client_id。需要项目成员权限。"""
    rows = await db.execute(
        select(ProjectClient).where(ProjectClient.project_id == project_id)
    )
    return [
        ProjectClientOut(id=str(r.id), project_id=r.project_id, client_id=r.client_id,
                         description=r.description or "")
        for r in rows.scalars()
    ]


@router.post("/{project_id}/clients", response_model=ProjectClientOut, status_code=201)
async def add_project_client(
    project_id: str,
    body: CreateClientRequest,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(require_platform_admin),
) -> ProjectClientOut:
    """为项目注册新的 client_id。需要 platform_admin（project_mgmt 平台专属）。"""
    existing = await db.scalar(
        select(ProjectClient).where(
            ProjectClient.project_id == project_id,
            ProjectClient.client_id == body.client_id,
        )
    )
    if existing:
        raise HTTPException(status_code=409, detail=f"Client '{body.client_id}' already exists for this project")

    pc = ProjectClient(project_id=project_id, client_id=body.client_id,
                       description=body.description)
    db.add(pc)
    await db.commit()
    await db.refresh(pc)

    # Invalidate client cache
    from app.client_validator import invalidate_client_cache
    invalidate_client_cache()

    return ProjectClientOut(id=str(pc.id), project_id=pc.project_id,
                            client_id=pc.client_id, description=pc.description or "")


@router.delete("/{project_id}/clients/{client_id}", status_code=204)
async def remove_project_client(
    project_id: str,
    client_id: str,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(require_platform_admin),
) -> None:
    """删除项目的 client_id 注册。需要 platform_admin（project_mgmt 平台专属）。"""
    pc = await db.scalar(
        select(ProjectClient).where(
            ProjectClient.project_id == project_id,
            ProjectClient.client_id == client_id,
        )
    )
    if not pc:
        raise HTTPException(status_code=404, detail="Client not found")
    await db.delete(pc)
    await db.commit()

    # Invalidate client cache
    from app.client_validator import invalidate_client_cache
    invalidate_client_cache()


# ── API Key 管理 ──


@router.get("/{project_id}/api-keys", response_model=list[ProjectApiKeyOut])
async def list_project_api_keys(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    _check: None = Depends(require_project_member("project_id")),
) -> list[ProjectApiKeyOut]:
    """列出项目的 API Keys（不返回原始 key）。需要该项目成员。"""
    rows = await db.execute(
        select(ProjectApiKey).where(ProjectApiKey.project_id == project_id)
        .order_by(ProjectApiKey.created_at.desc())
    )
    return [
        ProjectApiKeyOut(
            id=str(r.id), project_id=r.project_id,
            key_prefix=r.key_prefix, description=r.description or "",
            revoked=r.revoked,
            created_at=r.created_at.isoformat() if r.created_at else "",
            expires_at=r.expires_at.isoformat() if r.expires_at else None,
        )
        for r in rows.scalars()
    ]


@router.post("/{project_id}/api-keys", response_model=ProjectApiKeyCreated, status_code=201)
async def create_project_api_key(
    project_id: str,
    body: CreateApiKeyRequest,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(require_platform_admin),
) -> ProjectApiKeyCreated:
    """为项目签发新的 API Key。需要 platform_admin（project_mgmt 平台专属）。

    API Key 是 /v1 外部鉴权凭证，签发即等于放行以该项目身份调用判定链路，
    因此归入平台级 provisioning，只有平台管理员可签发；项目成员不得自签，避免
    项目管理员借此为其它项目铸造凭证造成跨项目越权。

    返回原始 key —— 仅此一次，之后不可获取。
    """
    # 生成 API Key: psk_ + 64 hex chars
    raw_key = "psk_" + secrets.token_hex(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:16]

    pk = ProjectApiKey(
        project_id=project_id,
        key_hash=key_hash,
        key_prefix=key_prefix,
        description=body.description,
    )
    db.add(pk)
    await db.commit()
    await db.refresh(pk)

    # Invalidate API key cache
    from app.client_validator import invalidate_api_key_cache
    invalidate_api_key_cache()

    return ProjectApiKeyCreated(
        id=str(pk.id), project_id=pk.project_id,
        api_key=raw_key, key_prefix=key_prefix,
    )


@router.post("/{project_id}/api-keys/{key_id}/revoke", status_code=200)
async def revoke_project_api_key(
    project_id: str,
    key_id: str,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(require_platform_admin),
) -> dict:
    """吊销 API Key。需要 platform_admin（project_mgmt 平台专属）。"""
    try:
        kid = uuid.UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid key ID")

    pk = await db.scalar(
        select(ProjectApiKey).where(
            ProjectApiKey.id == kid,
            ProjectApiKey.project_id == project_id,
            ProjectApiKey.revoked == False,  # noqa: E712
        )
    )
    if not pk:
        raise HTTPException(status_code=404, detail="API Key not found")

    pk.revoked = True
    await db.commit()

    # Invalidate API key cache
    from app.client_validator import invalidate_api_key_cache
    invalidate_api_key_cache()

    return {"key_id": str(pk.id), "result": "revoked"}


# ── Audience 管理 ──


@router.get("/{project_id}/audiences", response_model=list[ProjectAudienceOut])
async def list_project_audiences(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    _check: None = Depends(require_project_member("project_id")),
) -> list[ProjectAudienceOut]:
    """列出项目的 ctx_token 受众。需要该项目成员。"""
    rows = await db.execute(
        select(ProjectAudience).where(ProjectAudience.project_id == project_id)
    )
    return [
        ProjectAudienceOut(id=str(r.id), project_id=r.project_id, audience=r.audience)
        for r in rows.scalars()
    ]


@router.post("/{project_id}/audiences", response_model=ProjectAudienceOut, status_code=201)
async def add_project_audience(
    project_id: str,
    body: CreateAudienceRequest,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(require_platform_admin),
) -> ProjectAudienceOut:
    """为项目注册新的 ctx_token audience。需要 platform_admin（project_mgmt 平台专属）。"""
    existing = await db.scalar(
        select(ProjectAudience).where(
            ProjectAudience.project_id == project_id,
            ProjectAudience.audience == body.audience,
        )
    )
    if existing:
        raise HTTPException(status_code=409, detail=f"Audience '{body.audience}' already exists")

    pa = ProjectAudience(project_id=project_id, audience=body.audience)
    db.add(pa)
    await db.commit()
    await db.refresh(pa)

    # Invalidate audience cache
    from api.context import invalidate_audience_cache
    invalidate_audience_cache()

    return ProjectAudienceOut(id=str(pa.id), project_id=pa.project_id, audience=pa.audience)


# ── SDK 配置下载 ──


class SdkConfigResponse(BaseModel):
    project_id: str
    base_url: str = ""
    api_key: str = ""
    client_ids: list[str] = []
    audiences: list[str] = []
    sdk_install: str = "pip install perm-service-client"
    usage_example: str = ""


@router.get("/{project_id}/sdk-config", response_model=SdkConfigResponse)
async def get_sdk_config(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    _check: None = Depends(require_project_member("project_id")),
) -> SdkConfigResponse:
    """返回项目的 SDK 接入配置 JSON。

    包含 base_url、client_ids、audiences 等初始化所需信息。
    API Key 不通过此端点返回（仅签发时显示一次）。
    """
    from app.config import settings as app_settings

    # 获取 client_ids
    client_rows = await db.execute(
        select(ProjectClient.client_id).where(ProjectClient.project_id == project_id)
    )
    client_ids = [r.client_id for r in client_rows]

    # 获取 audiences
    aud_rows = await db.execute(
        select(ProjectAudience.audience).where(ProjectAudience.project_id == project_id)
    )
    audiences = [r.audience for r in aud_rows]

    primary_client = client_ids[0] if client_ids else "your-client-id"

    return SdkConfigResponse(
        project_id=project_id,
        base_url=f"http://localhost:{app_settings.port}",
        client_ids=client_ids,
        audiences=audiences,
        usage_example=(
            f"from perm_service_client import PermissionClient\n\n"
            f"client = PermissionClient(\n"
            f'    base_url="http://your-host:{app_settings.port}",\n'
            f'    api_key="(your-api-key)",\n'
            f'    client_id="{primary_client}",\n'
            f")\n"
            f'result = client.check("your-action", "your-resource-type", "resource-id")\n'
            f'print(result["decision"])  # "allow" or "deny"'
        ),
    )


@router.delete("/{project_id}/audiences/{audience}", status_code=204)
async def remove_project_audience(
    project_id: str,
    audience: str,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(require_platform_admin),
) -> None:
    """删除项目的 audience 注册。需要 platform_admin（project_mgmt 平台专属）。"""
    pa = await db.scalar(
        select(ProjectAudience).where(
            ProjectAudience.project_id == project_id,
            ProjectAudience.audience == audience,
        )
    )
    if not pa:
        raise HTTPException(status_code=404, detail="Audience not found")
    await db.delete(pa)
    await db.commit()

    # Invalidate audience cache
    from api.context import invalidate_audience_cache
    invalidate_audience_cache()


# ── 成员管理 ──


class ProjectMemberOut(BaseModel):
    id: str
    project_id: str
    user_id: str
    role: str
    granted_by: str = ""
    created_at: str = ""


class AddMemberRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=255)
    role: str = Field(default="project_admin", pattern="^(project_admin|project_viewer)$")


@router.get("/{project_id}/members", response_model=list[ProjectMemberOut])
async def list_project_members(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    _check: None = Depends(require_project_member("project_id")),
) -> list[ProjectMemberOut]:
    """列出项目成员。需要项目成员权限。"""
    from models.project import ProjectMember as PM
    rows = await db.execute(
        select(PM).where(PM.project_id == project_id).order_by(PM.created_at.desc())
    )
    return [
        ProjectMemberOut(
            id=str(r.id), project_id=r.project_id, user_id=r.user_id,
            role=r.role, granted_by=r.granted_by or "",
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in rows.scalars()
    ]


@router.post("/{project_id}/members", response_model=ProjectMemberOut, status_code=201)
async def add_project_member(
    project_id: str,
    body: AddMemberRequest,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    _check: None = Depends(require_project_member("project_id")),
) -> ProjectMemberOut:
    """添加项目成员。需要 project_admin 权限。"""
    from models.project import ProjectMember as PM

    # 检查添加者权限
    adder_role = await db.scalar(
        select(PM.role).where(PM.project_id == project_id, PM.user_id == admin.user_id)
    )
    if adder_role != "project_admin" and "platform_admin" not in admin.roles:
        raise HTTPException(status_code=403, detail="Only project_admin can add members")

    existing = await db.scalar(
        select(PM.id).where(PM.project_id == project_id, PM.user_id == body.user_id)
    )
    if existing:
        raise HTTPException(status_code=409, detail=f"User '{body.user_id}' is already a member")

    m = PM(project_id=project_id, user_id=body.user_id, role=body.role,
           granted_by=f"user:{admin.user_id}")
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return ProjectMemberOut(id=str(m.id), project_id=m.project_id, user_id=m.user_id,
                            role=m.role, granted_by=m.granted_by or "",
                            created_at=m.created_at.isoformat() if m.created_at else "")


@router.delete("/{project_id}/members/{user_id}", status_code=204)
async def remove_project_member(
    project_id: str,
    user_id: str,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    _check: None = Depends(require_project_member("project_id")),
) -> None:
    """移除项目成员。需要 project_admin 权限；不能移除自己。"""
    from models.project import ProjectMember as PM

    # 检查移除者权限：仅项目管理员（或平台管理员）可增删成员，
    # 与 add_project_member 同口径 —— 否则只读成员也能改成员表。
    remover_role = await db.scalar(
        select(PM.role).where(PM.project_id == project_id, PM.user_id == admin.user_id)
    )
    if remover_role != "project_admin" and "platform_admin" not in admin.roles:
        raise HTTPException(status_code=403, detail="Only project_admin can remove members")

    if user_id == admin.user_id:
        raise HTTPException(status_code=422, detail="Cannot remove yourself from the project")

    m = await db.scalar(
        select(PM).where(PM.project_id == project_id, PM.user_id == user_id)
    )
    if not m:
        raise HTTPException(status_code=404, detail="Member not found")
    await db.delete(m)
    await db.commit()
