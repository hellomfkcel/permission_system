"""数据模型层 — 14 张核心表。

设计依据：docs/外部系统设计.md §2.3.1 核心表设计 + §4.2 Keycloak 同步
         + docs/tenant_design.md §3.1 数据模型
         + docs/manage_role_design.md §3.1 数据模型。
Phase 1: 新增 Project/ProjectClient/ProjectApiKey/ProjectAudience 模型。
"""

from models.resource import ResourceRegistry
from models.mount import MountRegistry
from models.acl import ACLEntry
from models.role_binding import RoleBinding
from models.restriction import Restriction
from models.change_log import PermissionChange
from models.user_cache import UserCache
from models.tenant import Tenant, TenantMembership
from models.role_definition import RoleDefinition
from models.project import Project, ProjectClient, ProjectApiKey, ProjectAudience, ProjectMember

__all__ = [
    "ResourceRegistry",
    "MountRegistry",
    "ACLEntry",
    "RoleBinding",
    "Restriction",
    "PermissionChange",
    "UserCache",
    "Tenant",
    "TenantMembership",
    "RoleDefinition",
    "Project",
    "ProjectClient",
    "ProjectApiKey",
    "ProjectAudience",
]
