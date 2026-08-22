"""投影面 API — GET /v1/prefilter + POST /v1/visibility。"""

from datetime import datetime, timezone

from fastapi import APIRouter, Query, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, async_session
from app.config import settings
from app.limiter import limiter
from schemas.responses import (
    PreFilterResponse,
    PreFilterSuspendedResponse,
    VisibilityResponse,
)
from schemas.requests import VisibilityRequest
from services.jwt_parser import parse_principal
from services.acl_resolver import (
    check_subject_ban,
    get_active_kbs_for_principal,
    check_resource_restriction,
    get_allow_stamps_for_channel,
    get_deny_stamps_for_channel,
)
from models.mount import MountRegistry
from models.resource import ResourceRegistry
from sqlalchemy import select, text
from app.metrics_collector import record_authz_decision

router = APIRouter(prefix="/v1", tags=["projection"])


def _project_of(request: Request) -> str | None:
    """取本次请求归属的项目 ID（由准入中间件注入）。"""
    return getattr(request.state, "project_id", None)


@router.get(
    "/prefilter",
    response_model=PreFilterResponse | PreFilterSuspendedResponse,
)
@limiter.limit(f"{settings.prefilter_rate_limit}/second")
async def get_prefilter(
    request: Request,
    credential: str = Query(..., description="JWT credential"),
    db: AsyncSession = Depends(get_db),
) -> PreFilterResponse | PreFilterSuspendedResponse:
    """检索前编译 — 返回用户有权访问的 KB 列表。

    流程：
    1. 解析 JWT → Principal
    2. 型一封禁检查 → suspended
    3. 查询 ACL + role_bindings → 有权限的 KB
    4. 过滤 retired=false
    5. 查询型二封禁 → excluded_kbs
    """
    # 1. 解析 JWT
    try:
        principal = parse_principal(credential)
    except Exception as e:
        raise HTTPException(status_code=401, detail="Invalid credential") from e

    # 2. 型一封禁检查
    project_id = _project_of(request)
    is_suspended = await check_subject_ban(
        db, principal.principals, principal.tenant_id, project_id,
    )
    if is_suspended:
        return PreFilterSuspendedResponse(suspended=True, reason="subject_banned")

    # 3-4. 查询有权限的活跃 KB
    kbs = await get_active_kbs_for_principal(
        db, principal.principals, principal.tenant_id, project_id,
    )

    # 5. 型二封禁的资源
    excluded_kbs: list[str] = []
    # 检查所有活跃 KB 中型二封禁的。banned 含 "*" 或 "{type}:*" 通配
    # （资源级限制，principal=null → user:*）→ 对全体主体排除。
    for kb_id in kbs:
        banned = await check_resource_restriction(
            db, "kb", kb_id, principal.tenant_id, project_id,
        )
        if banned and (
            "*" in banned
            or any(b in principal.principals for b in banned)
            or any(b.endswith(":*") for b in banned)
        ):
            excluded_kbs.append(kb_id)

    # 排除被型二封禁的 KB
    kbs = [k for k in kbs if k not in excluded_kbs]

    # 获取当前全局版本号
    version: int = 0
    async with async_session() as s:
        result = await s.execute(
            text("SELECT last_value FROM global_permission_version")
        )
        row = result.fetchone()
        if row:
            version = row[0]

    # TTL: 60 秒
    now = datetime.now(timezone.utc)
    expires_at = datetime.fromtimestamp(now.timestamp() + 60, tz=timezone.utc)

    # tenant_wide_read: system_admin/admin 角色 → 租户级全量读取权限
    tenant_wide = any(
        r in ("role:system_admin", "role:admin") for r in principal.principals
    )

    record_authz_decision("prefilter", "allow", project_id)

    return PreFilterResponse(
        kbs=kbs,
        excluded_kbs=excluded_kbs,
        tenant_wide_read=tenant_wide,
        policy_version=f"v{version}",
        ttl_s=60,
        expires_at=expires_at.isoformat(),
    )


@router.post("/visibility", response_model=VisibilityResponse)
@limiter.limit(f"{settings.visibility_rate_limit}/second")
async def get_visibility(
    request: Request,
    body: VisibilityRequest,
    db: AsyncSession = Depends(get_db),
) -> VisibilityResponse:
    """可见性投影 — 返回 (doc_id, kb_id) 通道的可见性戳记。

    流程：
    1. 查 mount_registry → unlinked?
    2. 查 resource_registry → retired?
    3. 查 ACL → allow_stamps
    4. 查 restrictions → deny_stamps
    5. 取当前全局版本号
    """
    project_id = _project_of(request)

    # 1. 查 mount_registry（按调用方项目：不同项目可用相同的 doc_id / kb_id，
    #    不带项目条件会读到别的项目的挂载，把本项目的文档判成 unmounted）
    mount_conditions = [
        MountRegistry.doc_id == body.doc_id,
        MountRegistry.kb_id == body.channel.kb,
    ]
    if project_id is not None:
        mount_conditions.append(MountRegistry.project_id == project_id)
    stmt = select(MountRegistry).where(*mount_conditions)
    result = await db.execute(stmt)
    mount = result.scalar_one_or_none()

    if mount and mount.unlinked:
        return VisibilityResponse(unmounted=True)

    # 2. 查 resource_registry for doc and kb
    # 检查 doc retired
    doc_conditions = [
        ResourceRegistry.resource_type == "document",
        ResourceRegistry.resource_id == body.doc_id,
    ]
    if project_id is not None:
        doc_conditions.append(ResourceRegistry.project_id == project_id)
    result_doc = await db.execute(select(ResourceRegistry).where(*doc_conditions))
    doc_reg = result_doc.scalars().first()
    if doc_reg and doc_reg.retired:
        return VisibilityResponse(unmounted=True)

    # 检查 kb retired
    kb_conditions = [
        ResourceRegistry.resource_type == "kb",
        ResourceRegistry.resource_id == body.channel.kb,
    ]
    if project_id is not None:
        kb_conditions.append(ResourceRegistry.project_id == project_id)
    result_kb = await db.execute(select(ResourceRegistry).where(*kb_conditions))
    kb_reg = result_kb.scalars().first()
    if kb_reg and kb_reg.retired:
        return VisibilityResponse(unmounted=True)

    # 3. ACL → allow_stamps
    allow_stamps = await get_allow_stamps_for_channel(
        db, body.doc_id, body.channel.kb, body.tenant, project_id,
    )

    # 4. restrictions → deny_stamps
    deny_stamps = await get_deny_stamps_for_channel(
        db, body.doc_id, body.channel.kb, body.tenant, project_id,
    )

    # 5. 全局版本号
    version: int = 0
    async with async_session() as s:
        result_v = await s.execute(
            text("SELECT last_value FROM global_permission_version")
        )
        row = result_v.fetchone()
        if row:
            version = row[0]

    return VisibilityResponse(
        allow_stamps=allow_stamps,
        deny_stamps=deny_stamps,
        version=version,
    )
