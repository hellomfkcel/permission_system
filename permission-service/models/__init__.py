"""数据模型层 — 7 张核心表。

设计依据：docs/外部系统设计.md §2.3.1 核心表设计 + §4.2 Keycloak 同步。
"""

from models.resource import ResourceRegistry
from models.mount import MountRegistry
from models.acl import ACLEntry
from models.role_binding import RoleBinding
from models.restriction import Restriction
from models.change_log import PermissionChange
from models.user_cache import UserCache

__all__ = [
    "ResourceRegistry",
    "MountRegistry",
    "ACLEntry",
    "RoleBinding",
    "Restriction",
    "PermissionChange",
    "UserCache",
]
