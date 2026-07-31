"""角色→隐式权限映射配置。

设计依据：docs/权限管理系统架构设计.md §2.3 准入矩阵。

角色绑定赋予的隐式权限必须与 Cerbos 策略保持一致。
当 Cerbos 策略变更时，此文件需同步更新。

来源：
  - cerbos/policies/resource_policies/kb.yaml (kb:read/write/manage/grant)
  - cerbos/policies/resource_policies/document.yaml (doc:view/download/retrieve/unmount/purge/share)
"""

# ══════════════════════════════════════════════════════════════
# 角色 → 动作映射
# ══════════════════════════════════════════════════════════════
#
# 每个角色的隐式权限由其 Cerbos derived role 和资源策略规则定义。
# 此映射用于管理台「有效权限计算」功能，将角色绑定解析为具体动作列表。
#
# 更新纪律：
#   1. Cerbos 策略变更时，必须同步更新此映射
#   2. 新增动作时，必须同时更新 _VALID_ACTIONS 集合
#   3. 联合契约测试 J-1~J-20 应在策略变更后重新运行

ROLE_ACTIONS_MAP: dict[str, list[str]] = {
    "admin": [
        # KB 资源 — 全部动作
        "kb:read", "kb:write", "kb:manage", "kb:grant",
        # Document 资源 — 全部动作
        "doc:view", "doc:download", "doc:retrieve",
        "doc:unmount", "doc:purge", "doc:share",
    ],
    "kb_admin": [
        # KB 资源
        "kb:read", "kb:write", "kb:manage",
        # Document 资源
        "doc:view", "doc:download", "doc:retrieve",
        "doc:unmount", "doc:purge",
    ],
    "kb_writer": [
        # KB 资源
        "kb:read", "kb:write",
        # Document 资源
        "doc:view", "doc:download", "doc:retrieve",
        "doc:unmount",
    ],
    "kb_reader": [
        # KB 资源
        "kb:read",
        # Document 资源
        "doc:view", "doc:download", "doc:retrieve",
    ],
}


# ══════════════════════════════════════════════════════════════
# 动词目录 — 有效的 action 值
# ══════════════════════════════════════════════════════════════
#
# 设计依据：docs/权限管理系统架构设计.md §2.1 动词目录（10 个 action）
# 用于 CSV 导入校验和管理台权限授予下拉选项。

VALID_ACTIONS: set[str] = {
    "kb:read", "kb:write", "kb:manage", "kb:grant",
    "doc:view", "doc:download", "doc:retrieve", "doc:unmount",
    "doc:purge", "doc:share",
}

VALID_RESOURCE_TYPES: set[str] = {"kb", "document"}


def get_implicit_actions_for_role(role: str) -> list[str]:
    """获取角色绑定隐式授予的动作列表。

    Args:
        role: 角色名（admin / kb_admin / kb_writer / kb_reader）

    Returns:
        该角色隐式授予的所有 action 列表（按字母序排序）。
        若角色名不在映射中，返回空列表。
    """
    return sorted(ROLE_ACTIONS_MAP.get(role, []))
