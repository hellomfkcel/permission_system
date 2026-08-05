"""租户管理 — 请求 Pydantic 模型。

设计依据：docs/tenant_design.md §3.2.1 租户 CRUD + §3.2.2 成员管理。
"""

from pydantic import BaseModel, Field


_TENANT_ID_PATTERN = r"^[a-z][a-z0-9-]{1,63}$"


class CreateTenantRequest(BaseModel):
    """创建租户请求。"""
    id: str = Field(
        ...,
        min_length=2,
        max_length=64,
        pattern=_TENANT_ID_PATTERN,
        description="租户唯一标识（小写字母+数字+连字符，2-64字符）",
        examples=["acme-corp"],
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="租户显示名称",
        examples=["ACME 公司"],
    )
    description: str = Field(
        default="",
        max_length=512,
        description="租户描述",
    )


class UpdateTenantRequest(BaseModel):
    """更新租户请求（全部可选，只更新传入的字段）。"""
    name: str | None = Field(None, min_length=1, max_length=255, description="新显示名称")
    description: str | None = Field(None, max_length=512, description="新描述")
    status: str | None = Field(
        None,
        pattern=r"^(active|suspended)$",
        description="新状态: active | suspended",
    )


class AddTenantMemberRequest(BaseModel):
    """添加租户成员请求。"""
    user_id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="用户标识 (user:xxx 格式)",
        examples=["user:alice"],
    )
    role: str = Field(
        default="member",
        pattern=r"^(tenant_admin|member)$",
        description="租户内角色: tenant_admin | member",
    )


class ListTenantsParams(BaseModel):
    """租户列表查询参数。"""
    status: str | None = Field(None, description="筛选状态: active | suspended")
    search: str | None = Field(None, description="搜索 ID 或名称（模糊匹配）")
    limit: int = Field(default=50, ge=1, le=200, description="每页条数")
    offset: int = Field(default=0, ge=0, description="偏移量")
