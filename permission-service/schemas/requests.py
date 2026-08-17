"""请求 Pydantic 模型。"""

from pydantic import BaseModel, Field


class ResourceRef(BaseModel):
    """资源引用。"""
    type: str = Field(..., description="资源类型: kb | document | directory")
    id: str = Field(..., description="资源 ID")


class ChannelRef(BaseModel):
    """通道引用 — 通道类动词必须携带。"""
    kb: str = Field(..., description="通道 KB ID")


class CheckRequest(BaseModel):
    """POST /v1/check 请求体。"""
    request_id: str = Field(..., description="请求追踪 ID")
    credential: str = Field(..., description="JWT 原文")
    action: str = Field(..., description="动词: kb:read | kb:write | ...")
    resource: ResourceRef = Field(..., description="目标资源")
    channel: ChannelRef | None = Field(None, description="通道类动词必带")


class FilterItem(BaseModel):
    """批量过滤的单条 item。"""
    resource_type: str = Field("document", description="资源类型")
    resource_id: str = Field(..., description="资源 ID")
    channel: ChannelRef = Field(..., description="所属通道")


class FilterRequest(BaseModel):
    """POST /v1/filter 请求体。"""
    request_id: str = Field(..., description="请求追踪 ID")
    credential: str = Field(..., description="JWT 原文或 ctx_token")
    items: list[FilterItem] = Field(
        ..., max_length=200, description="待判定项，≤200 条"
    )


class ContextRequest(BaseModel):
    """POST /v1/context 请求体。"""
    request_id: str = Field(..., description="请求追踪 ID")
    credential: str = Field(..., description="JWT 原文")
    audience: str = Field(..., description="目标服务名")
    ttl_s: int = Field(600, ge=1, le=600, description="有效期秒数，上限 600")


class ResourceLifecycleRequest(BaseModel):
    """资源生命周期端点请求体 (register/link/unlink/retire 共用)。"""
    resource_type: str = Field(..., description="kb | document | directory")
    resource_id: str = Field(..., description="资源 ID")
    name: str | None = Field(None, description="资源名称（KB 名称 / 文档文件名，仅 register 使用）")
    owner: str | None = Field(None, description="owner principal (仅 register)")
    tenant_id: str = Field(..., description="所属租户")
    kb_id: str | None = Field(None, description="KB ID (link/unlink)")
    idempotency_key: str = Field(..., description="幂等键")
    project_id: str = Field(..., description="所属项目 ID（必填）")


class CheckBatchItem(BaseModel):
    """批量 check 的单条资源。"""
    action: str = Field(..., description="动词")
    resource: ResourceRef = Field(..., description="目标资源")
    channel: ChannelRef | None = Field(None, description="通道类动词必带")


class CheckBatchRequest(BaseModel):
    """POST /v1/check/batch 请求体。

    单批 ≤200 条，逐资源独立决策。
    """
    request_id: str = Field(..., description="请求追踪 ID")
    credential: str = Field(..., description="JWT 原文")
    items: list[CheckBatchItem] = Field(
        ..., min_length=1, max_length=200, description="待判定项，≤200 条"
    )


class VisibilityRequest(BaseModel):
    """POST /v1/visibility 请求体。"""
    tenant: str = Field(..., description="租户 ID")
    doc_id: str = Field(..., description="文档 ID")
    channel: ChannelRef = Field(..., description="所属 KB 通道")
