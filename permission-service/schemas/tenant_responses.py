"""租户管理 — 响应 Pydantic 模型。"""

from pydantic import BaseModel, Field


class TenantResponse(BaseModel):
    """单个租户响应。"""
    id: str
    name: str
    description: str = ""
    status: str = "active"
    member_count: int = 0
    created_by: str
    created_at: str
    updated_at: str


class TenantListResponse(BaseModel):
    """租户列表响应。"""
    tenants: list[TenantResponse]
    total: int


class TenantMemberResponse(BaseModel):
    """租户成员响应。"""
    id: str
    user_id: str
    role: str
    granted_by: str
    granted_at: str
    revoked: bool = False


class TenantMemberListResponse(BaseModel):
    """租户成员列表响应。"""
    tenant_id: str
    members: list[TenantMemberResponse]
    total: int


class UserTenantsResponse(BaseModel):
    """用户所属租户列表响应。"""
    user_id: str
    tenants: list[TenantResponse]
