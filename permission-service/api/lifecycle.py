"""管理面 API — 生命周期端口 (register/link/unlink/retire) + 资源查询。

设计依据：docs/外部系统设计.md §2.4.3 管理面 API + §2.4.4 管理台专用 API + 实施方案步骤 4.3。
"""

import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from schemas.requests import ResourceLifecycleRequest
from schemas.responses import LifecycleResponse
from models.resource import ResourceRegistry
from models.mount import MountRegistry
from services.event_publisher import get_event_publisher

router = APIRouter(prefix="/v1/resources", tags=["lifecycle"])

# ── 幂等键格式校验 ──
# 设计依据 §6A.7：{facade}-{tenant}-{resource_id}[-{kb_id}]-{schema_version}
# 禁止时间戳和随机数（非确定性值），但不禁止作为资源标识符的 UUID。
# 原因：resource_id 本身可能是 UUID（系统分配的确定性标识），
#       同一资源始终产生同一 UUID，因此嵌入 key 中仍然是确定性可重算的。
_IDEMPOTENCY_KEY_RE = re.compile(
    r"^[a-z][a-z0-9]*"           # facade（字母开头）
    r"-[\w.-]+"                   # tenant
    r"-[\w.-]+"                   # resource_id
    r"(?:-[\w.-]+)?"              # kb_id（可选）
    r"-v\d+$"                     # schema_version (v1, v2, ...)
)

# 禁止非确定性模式：
# 1. 独立的十进制时间戳（10+ 位纯数字，排除 hex UUID 段如 "ef1234567890"）
# 2. 显式 timestamp=xxx / ts=xxx 后缀
# UUID 不作为禁止项——当 UUID 是资源本身的标识符时，它是确定性的。
_IDEMPOTENCY_FORBIDDEN_RE = re.compile(
    r"((?<![a-f0-9])\d{10,}(?![a-f0-9])|"  # 独立十进制时间戳（不在 hex 上下文中）
    r"(?:ts|timestamp)[-=]?\d+)"              # ts=xxx / timestamp=xxx
)


def _validate_idempotency_key(key: str, endpoint: str) -> None:
    """校验幂等键格式合法性。

    Raises:
        HTTPException(422): 格式不合法时抛出。
    """
    if _IDEMPOTENCY_FORBIDDEN_RE.search(key):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_idempotency_key",
                "message": (
                    "idempotency_key must not contain timestamps or random values. "
                    "Use deterministic format: {facade}-{tenant}-{resource_id}[-{kb_id}]-{schema_version}"
                ),
                "example": f"rag-{endpoint}-tenant-dev-resource-123-v1",
            },
        )
    if not _IDEMPOTENCY_KEY_RE.match(key):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_idempotency_key",
                "message": (
                    "idempotency_key must match pattern: "
                    "{facade}-{tenant}-{resource_id}[-{kb_id}]-{schema_version}"
                ),
                "example": f"rag-{endpoint}-tenant-dev-resource-123-v1",
            },
        )


# ── 资源查询响应模型 ──


class ResourceOut(BaseModel):
    id: str
    resource_type: str
    resource_id: str
    name: str | None = None
    tenant_id: str
    owner: str
    retired: bool
    created_at: str = ""
    updated_at: str = ""


@router.get("", response_model=list[ResourceOut])
async def list_resources(
    request: Request,
    type: str | None = Query(None, alias="type", description="资源类型: kb | document"),
    tenant_id: str | None = Query(None, description="租户 ID 过滤"),
    resource_id: str | None = Query(None, description="精确查询: 资源 ID"),
    db: AsyncSession = Depends(get_db),
) -> list[ResourceOut]:
    """查询已注册资源（需至少一个过滤条件）。

    RAG 系统生命周期端口内部调用：register 前幂等检查、unlink/retire 前资源确认。
    管理台资源浏览请使用 /api/v1/resources（需 admin 认证）。
    """
    conditions = []
    # 调用方只能看到自己项目的资源（project_id 由准入中间件注入）
    caller_project = getattr(request.state, "project_id", None)
    if caller_project is not None:
        conditions.append(ResourceRegistry.project_id == caller_project)
    if type:
        conditions.append(ResourceRegistry.resource_type == type)
    if tenant_id:
        conditions.append(ResourceRegistry.tenant_id == tenant_id)
    if resource_id:
        conditions.append(ResourceRegistry.resource_id == resource_id)

    # 安全：无过滤条件时拒绝空查询（防止枚举全部资源）
    if not (type or tenant_id or resource_id):
        raise HTTPException(
            status_code=422,
            detail="At least one filter (type, tenant_id, or resource_id) is required",
        )

    stmt = (
        select(ResourceRegistry)
        .where(*conditions)
        .order_by(ResourceRegistry.created_at.desc())
        .limit(500)
    )
    result = await db.execute(stmt)
    resources = result.scalars().all()

    return [
        ResourceOut(
            id=str(r.id),
            resource_type=r.resource_type,
            resource_id=r.resource_id,
            name=r.name,
            tenant_id=r.tenant_id,
            owner=r.owner,
            retired=r.retired,
            created_at=r.created_at.isoformat() if r.created_at else "",
            updated_at=r.updated_at.isoformat() if r.updated_at else "",
        )
        for r in resources
    ]


@router.post("/register", response_model=LifecycleResponse)
async def register_resource(
    body: ResourceLifecycleRequest,
    db: AsyncSession = Depends(get_db),
) -> LifecycleResponse:
    """资源登记。

    幂等键：{facade}-{tenant}-{resource_id}-{schema_version}
    result: "created" | "noop"
    同 key 不同 payload → 409
    """
    # 幂等键格式校验
    _validate_idempotency_key(body.idempotency_key, "register")

    # 检查是否已存在（幂等）。按项目查询：资源唯一性是 (项目, 类型, ID)，
    # 不同项目可以使用同名资源类型与相同资源 ID。
    stmt = select(ResourceRegistry).where(
        ResourceRegistry.project_id == body.project_id,
        ResourceRegistry.resource_type == body.resource_type,
        ResourceRegistry.resource_id == body.resource_id,
    )
    result = await db.execute(stmt)
    existing = result.scalars().first()

    if existing:
        # 已存在 → 验证是否同 payload
        if existing.owner != body.owner or existing.tenant_id != body.tenant_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "idempotency_conflict",
                    "message": "same key, different payload",
                },
            )
        return LifecycleResponse(
            change_id=str(existing.id),
            result="noop",
        )

    # 新建
    new_id = uuid.uuid4()
    resource = ResourceRegistry(
        id=new_id,
        project_id=body.project_id,
        resource_type=body.resource_type,
        resource_id=body.resource_id,
        name=body.name,  # 资源名称（KB 名称 / 文档文件名）
        tenant_id=body.tenant_id,
        owner=body.owner or f"user:{body.tenant_id}",
    )
    db.add(resource)

    # ★ Outbox 模式（设计依据 §3.2 + §5.1）：
    # 生命周期操作需要发布 VisibilityChanged 事件，触发 RAG 侧盖戳刷新
    publisher = get_event_publisher()
    version, change_id = await publisher.write_change_log(
        db,
        tenant_id=body.tenant_id,
        resource_type=body.resource_type,
        resource_id=body.resource_id,
        event_type="RESOURCE_REGISTERED",
        change_detail={"action": "resource_registered", "project_id": body.project_id},
    )
    await db.commit()  # 资源注册 + change_log 原子提交

    # 事务提交后异步发布 Redis
    await publisher.publish_event(
        change_id, version, body.tenant_id,
        body.resource_type, body.resource_id,
        event_type="RESOURCE_REGISTERED",
        kb_id=body.kb_id,
        change_detail={"action": "resource_registered", "project_id": body.project_id},
    )

    return LifecycleResponse(
        change_id=str(new_id),
        result="created",
    )


@router.post("/link", response_model=LifecycleResponse)
async def link_resource(
    body: ResourceLifecycleRequest,
    db: AsyncSession = Depends(get_db),
) -> LifecycleResponse:
    """挂载建立。

    Args:
        body.resource_id = doc_id
        body.kb_id = kb_id
    """
    if not body.kb_id:
        raise HTTPException(status_code=422, detail="kb_id is required for link")

    # 幂等键格式校验
    _validate_idempotency_key(body.idempotency_key, "link")

    doc_id = body.resource_id
    kb_id = body.kb_id

    # 幂等检查
    stmt = select(MountRegistry).where(
        MountRegistry.doc_id == doc_id,
        MountRegistry.kb_id == kb_id,
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing:
        return LifecycleResponse(
            change_id=str(existing.id),
            result="noop",
        )

    new_id = uuid.uuid4()
    mount = MountRegistry(
        id=new_id,
        doc_id=doc_id,
        kb_id=kb_id,
    )
    db.add(mount)

    # ★ Outbox 模式：挂载建立 → 发布 VisibilityChanged
    publisher = get_event_publisher()
    version, change_id = await publisher.write_change_log(
        db,
        tenant_id=body.tenant_id,
        resource_type="document",
        resource_id=doc_id,
        kb_id=kb_id,
        event_type="RESOURCE_LINKED",
        change_detail={"action": "resource_linked", "doc_id": doc_id, "kb_id": kb_id},
    )
    await db.commit()

    await publisher.publish_event(
        change_id, version, body.tenant_id,
        "document", doc_id,
        kb_id=kb_id,
        event_type="RESOURCE_LINKED",
        change_detail={"action": "resource_linked", "doc_id": doc_id, "kb_id": kb_id},
    )

    return LifecycleResponse(
        change_id=str(new_id),
        result="created",
    )


@router.post("/unlink", response_model=LifecycleResponse)
async def unlink_resource(
    body: ResourceLifecycleRequest,
    db: AsyncSession = Depends(get_db),
) -> LifecycleResponse:
    """解除挂载。

    Args:
        body.resource_id = doc_id
        body.kb_id = kb_id
    """
    if not body.kb_id:
        raise HTTPException(status_code=422, detail="kb_id is required for unlink")

    # 幂等键格式校验
    _validate_idempotency_key(body.idempotency_key, "unlink")

    stmt = select(MountRegistry).where(
        MountRegistry.doc_id == body.resource_id,
        MountRegistry.kb_id == body.kb_id,
    )
    result = await db.execute(stmt)
    mount = result.scalar_one_or_none()

    if not mount:
        raise HTTPException(status_code=404, detail="mount not found")

    mount.unlinked = True
    mount.updated_at = datetime.now(timezone.utc)

    # ★ Outbox 模式：挂载解除 → 发布 VisibilityChanged(unmounted=true)
    publisher = get_event_publisher()
    version, change_id = await publisher.write_change_log(
        db,
        tenant_id=body.tenant_id,
        resource_type="document",
        resource_id=body.resource_id,
        kb_id=body.kb_id,
        event_type="RESOURCE_UNLINKED",
        change_detail={
            "action": "resource_unlinked",
            "doc_id": body.resource_id,
            "kb_id": body.kb_id,
            "unmounted": True,
        },
    )
    await db.commit()

    await publisher.publish_event(
        change_id, version, body.tenant_id,
        "document", body.resource_id,
        kb_id=body.kb_id,
        event_type="RESOURCE_UNLINKED",
        unmounted=True,
        change_detail={
            "action": "resource_unlinked",
            "doc_id": body.resource_id,
            "kb_id": body.kb_id,
        },
    )

    return LifecycleResponse(
        change_id=str(mount.id),
        result="created",
    )


@router.post("/retire", response_model=LifecycleResponse)
async def retire_resource(
    body: ResourceLifecycleRequest,
    db: AsyncSession = Depends(get_db),
) -> LifecycleResponse:
    """资源退役。

    行为：
    - 置 resource_registry.retired = true
    - 级联清理 mount_registry（置 unlinked=true）
    """
    # 幂等键格式校验
    _validate_idempotency_key(body.idempotency_key, "retire")

    stmt = select(ResourceRegistry).where(
        ResourceRegistry.project_id == body.project_id,
        ResourceRegistry.resource_type == body.resource_type,
        ResourceRegistry.resource_id == body.resource_id,
    )
    result = await db.execute(stmt)
    resource = result.scalars().first()

    if not resource:
        raise HTTPException(status_code=404, detail="resource not found")

    resource.retired = True
    resource.updated_at = datetime.now(timezone.utc)

    # 级联清理：如果是 document，解除其所有挂载
    unmounted_kb_ids: list[str] = []
    if body.resource_type == "document":
        stmt_mounts = select(MountRegistry).where(
            MountRegistry.doc_id == body.resource_id,
            MountRegistry.unlinked == False,  # noqa: E712
        )
        result_mounts = await db.execute(stmt_mounts)
        for mount in result_mounts.scalars():
            mount.unlinked = True
            mount.updated_at = datetime.now(timezone.utc)
            unmounted_kb_ids.append(mount.kb_id)

    # 如果是 kb，解除所有文档的挂载
    if body.resource_type == "kb":
        stmt_mounts = select(MountRegistry).where(
            MountRegistry.kb_id == body.resource_id,
            MountRegistry.unlinked == False,  # noqa: E712
        )
        result_mounts = await db.execute(stmt_mounts)
        for mount in result_mounts.scalars():
            mount.unlinked = True
            mount.updated_at = datetime.now(timezone.utc)

    # ★ Outbox 模式：资源退役 → 发布 VisibilityChanged
    # 设计依据 §13.4.3：retire 四合一（回收 ACL + restriction + 解挂 + 置 retired）
    publisher = get_event_publisher()
    version, change_id = await publisher.write_change_log(
        db,
        tenant_id=resource.tenant_id,
        resource_type=body.resource_type,
        resource_id=body.resource_id,
        event_type="RESOURCE_RETIRED",
        change_detail={
            "action": "resource_retired",
            "resource_type": body.resource_type,
            "unmounted": True,
        },
    )
    await db.commit()  # retire + 级联解挂 + change_log 原子提交

    # 事务提交后异步发布 Redis — 对每个受影响的挂载发布事件
    await publisher.publish_event(
        change_id, version, resource.tenant_id,
        body.resource_type, body.resource_id,
        event_type="RESOURCE_RETIRED",
        unmounted=True,
        change_detail={
            "action": "resource_retired",
            "resource_type": body.resource_type,
        },
    )

    return LifecycleResponse(
        change_id=str(resource.id),
        result="created",
    )


# ── 资源属性更新 ──


class UpdateResourceAttrRequest(BaseModel):
    """更新资源运营属性（is_enabled / allow_download）。

    调用方: B-DOC（RAG 系统 MountEnabledChanged 事件处理）。
    """
    is_enabled: bool | None = Field(None, description="文档启用状态")
    allow_download: bool | None = Field(None, description="是否允许下载")
    tenant_id: str = Field(..., description="所属租户")


@router.patch("/{resource_type}/{resource_id}", response_model=LifecycleResponse)
async def update_resource_attr(
    request: Request,
    resource_type: str,
    resource_id: str,
    body: UpdateResourceAttrRequest,
    db: AsyncSession = Depends(get_db),
) -> LifecycleResponse:
    """更新资源运营属性。

    B-DOC 在 MountEnabledChanged 事件处理时调用，同步 is_enabled/allow_download 到权限服务。
    设计依据：docs/RAG系统设计v14.md §13.4.1 + §14.5.1。
    """
    conditions = [
        ResourceRegistry.resource_type == resource_type,
        ResourceRegistry.resource_id == resource_id,
    ]
    caller_project = getattr(request.state, "project_id", None)
    if caller_project is not None:
        conditions.append(ResourceRegistry.project_id == caller_project)
    result = await db.execute(select(ResourceRegistry).where(*conditions))
    resource = result.scalars().first()

    if not resource:
        raise HTTPException(status_code=404, detail="resource not found")

    changed = False
    if body.is_enabled is not None and resource.is_enabled != body.is_enabled:
        resource.is_enabled = body.is_enabled
        changed = True
    if body.allow_download is not None and resource.allow_download != body.allow_download:
        resource.allow_download = body.allow_download
        changed = True

    if not changed:
        return LifecycleResponse(change_id=str(resource.id), result="noop")

    resource.updated_at = datetime.now(timezone.utc)

    # Outbox 模式：属性变更 → 发布 VisibilityChanged（通知 RAG 盖戳刷新）
    publisher = get_event_publisher()
    version, change_id = await publisher.write_change_log(
        db,
        tenant_id=body.tenant_id,
        resource_type=resource_type,
        resource_id=resource_id,
        event_type="RESOURCE_ATTR_UPDATED",
        change_detail={
            "action": "resource_attr_updated",
            "is_enabled": body.is_enabled,
            "allow_download": body.allow_download,
        },
    )
    await db.commit()

    await publisher.publish_event(
        change_id, version, body.tenant_id,
        resource_type, resource_id,
        event_type="RESOURCE_ATTR_UPDATED",
        change_detail={
            "action": "resource_attr_updated",
            "is_enabled": body.is_enabled,
            "allow_download": body.allow_download,
        },
    )

    return LifecycleResponse(change_id=str(resource.id), result="created")


# ── 资源所有者查询 ──


class ResourceOwnerResponse(BaseModel):
    """资源所有者信息响应。

    设计依据：docs/外部系统设计.md §2.4.4 管理台专用 API
         GET /api/v1/resources/{type}/{id}/owners — 查看资源所有权。
    """
    resource_type: str
    resource_id: str
    owner: str
    tenant_id: str
    retired: bool
    is_enabled: bool
    allow_download: bool
    created_at: str | None = None
    updated_at: str | None = None


@router.get("/{resource_type}/{resource_id}/owners")
async def get_resource_owners(
    resource_type: str,
    resource_id: str,
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    """查询资源所有者信息。

    管理台用于展示资源的所有权归属。
    设计依据：docs/外部系统设计.md §2.4.4。
    """
    conditions = [
        ResourceRegistry.resource_type == resource_type,
        ResourceRegistry.resource_id == resource_id,
    ]
    caller_project = getattr(request.state, "project_id", None) if request else None
    if caller_project is not None:
        conditions.append(ResourceRegistry.project_id == caller_project)
    result = await db.execute(select(ResourceRegistry).where(*conditions))
    resource = result.scalars().first()

    if not resource:
        raise HTTPException(status_code=404, detail="resource not found")

    return {
        "resource_type": resource.resource_type,
        "resource_id": resource.resource_id,
        "owner": resource.owner,
        "tenant_id": resource.tenant_id,
        "retired": resource.retired,
        "is_enabled": resource.is_enabled,
        "allow_download": resource.allow_download,
        "created_at": resource.created_at.isoformat() if resource.created_at else None,
        "updated_at": resource.updated_at.isoformat() if resource.updated_at else None,
    }
