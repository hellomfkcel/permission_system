"""平台管理功能目录 — 管理台功能资源 ID 与权限常量。

设计依据：平台级权限管理 — 每个管理台侧边栏功能对应一个 platform 资源。
此模块是后端路由和前端侧边栏的共享权威定义。
"""

# ══════════════════════════════════════════════════════════════
# 平台功能资源 ID → 显示名称映射
# ══════════════════════════════════════════════════════════════
PLATFORM_FEATURES: dict[str, str] = {
    "dashboard": "Dashboard 概览",
    "project_mgmt": "项目管理",
    "tenant_mgmt": "租户管理",
    "resource_mgmt": "资源管理",
    "user_mgmt": "用户与组",
    "role_mgmt": "角色管理",
    "permission_mgmt": "权限管理",
    "restriction_mgmt": "封禁管理",
    "policy_mgmt": "策略管理",
    "audit_mgmt": "审计日志",
    "playground": "策略模拟",
    "settings": "系统设置",
}

# ══════════════════════════════════════════════════════════════
# 平台角色名常量
# ══════════════════════════════════════════════════════════════
PLATFORM_ROLE_NAMES: set[str] = {
    "platform_admin",
    "platform_viewer",
    "platform_auditor",
}

# ══════════════════════════════════════════════════════════════
# 平台功能 → 侧边栏路径映射（供前端使用）
# ══════════════════════════════════════════════════════════════
PLATFORM_FEATURE_PATHS: dict[str, str] = {
    "dashboard": "/dashboard",
    "project_mgmt": "/projects",
    "tenant_mgmt": "/tenants",
    "resource_mgmt": "/resources",
    "user_mgmt": "/users-groups",
    "role_mgmt": "/roles",
    "permission_mgmt": "/permissions",
    "restriction_mgmt": "/restrictions",
    "policy_mgmt": "/policies",
    "audit_mgmt": "/audit",
    "playground": "/playground",
    "settings": "/settings",
}
