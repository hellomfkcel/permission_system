"""响应 Pydantic 模型。

设计依据：docs/外部系统设计.md §2.4 API 设计。
"""

from pydantic import BaseModel, Field


class DecisionResponse(BaseModel):
    """权限判定响应。"""
    decision: str = Field(..., description="allow | deny | indeterminate")
    decision_id: str = Field(..., description="Cerbos call ID")
    reasons: list[str] = Field(default_factory=list)


class CheckBatchResult(BaseModel):
    """单条批量 check 判定结果。"""
    action: str = Field(..., description="动词")
    resource_type: str = Field(..., description="资源类型")
    resource_id: str = Field(..., description="资源 ID")
    decision: str = Field(..., description="allow | deny | indeterminate")
    decision_id: str = Field("", description="Cerbos call ID")


class CheckBatchResponse(BaseModel):
    """POST /v1/check/batch 响应体。

    设计依据：J-14 联合契约测试 — 逐资源独立决策，共享同一 request_id。
    任一条失败不影响其余；整批传输失败/超时 → 整批判否（fail-closed）。
    """
    results: list[CheckBatchResult] = Field(default_factory=list)
    request_id: str = Field("", description="请求追踪 ID")


class FilterResponse(BaseModel):
    """批量过滤响应。"""
    allowed: list[str] = Field(default_factory=list, description="允许的 resource_id 列表")
    denied: list[str] = Field(default_factory=list, description="拒绝的 resource_id 列表")
    decision_id: str = Field("", description="Cerbos call ID")


class ContextResponse(BaseModel):
    """ctx_token 铸造响应。"""
    ctx_token: str = Field(..., description="签名 token")
    expires_at: str = Field(..., description="过期时间 ISO 8601")


class PreFilterResponse(BaseModel):
    """检索前编译响应（正常情况）。"""
    kbs: list[str] = Field(default_factory=list, description="允许的 KB 列表")
    excluded_kbs: list[str] = Field(default_factory=list, description="型二封禁的 KB")
    tenant_wide_read: bool = Field(False)
    policy_version: str = Field("", description="全局版本号字符串")
    ttl_s: int = Field(60, description="请求内缓存 TTL")
    expires_at: str = Field("", description="过期时间 ISO 8601")


class PreFilterSuspendedResponse(BaseModel):
    """型一封禁响应。"""
    suspended: bool = Field(True)
    reason: str = Field("subject_banned")


class VisibilityResponse(BaseModel):
    """可见性投影响应。"""
    allow_stamps: list[str] = Field(default_factory=list)
    deny_stamps: list[str] = Field(default_factory=list)
    version: int = Field(0, description="全局权限版本号")
    unmounted: bool = Field(False)


class LifecycleResponse(BaseModel):
    """生命周期操作响应。"""
    change_id: str = Field(..., description="变更 ID")
    result: str = Field(..., description="created | noop")


class Principal(BaseModel):
    """从 JWT 解析出的主体信息。"""
    user_id: str
    tenant_id: str = ""
    roles: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    principals: list[str] = Field(default_factory=list)
    raw_jwt: str = ""
