"""权限动词目录 — 单一权威源。

设计依据：docs/权限管理系统架构设计.md §2.1 动词目录。

此文件定义所有有效动作和资源类型，用于：
- CSV 导入校验（acl_routes.py）
- 前端资源类型/动作下拉选项
- /api/v1/auth/config 端点动态生成 resource_actions

注意：角色→权限映射的权威源是 Cerbos YAML，
由 services/cerbos_policy_parser.py 解析，不在此文件维护。
"""

# ══════════════════════════════════════════════════════════════
# 平台动作
# ══════════════════════════════════════════════════════════════

PLATFORM_ACTIONS: set[str] = {"platform:read", "platform:write"}

# ══════════════════════════════════════════════════════════════
# 项目权限数据动作（Layer 2 · project_permission 资源）
# ══════════════════════════════════════════════════════════════

PROJECT_PERMISSION_ACTIONS: set[str] = {"permission:read", "permission:write"}

# 权限数据类别（member_list / role_assignment / ...）不在此维护：
# 它们是 project_permission.yaml 中的 resource.id，取值由策略文件决定。

# ══════════════════════════════════════════════════════════════
# 项目资源动作（kb/document）
# ══════════════════════════════════════════════════════════════

_KB_DOC_ACTIONS: set[str] = {
    "kb:read", "kb:write", "kb:manage", "kb:grant",
    "doc:view", "doc:download", "doc:retrieve", "doc:unmount",
    "doc:purge", "doc:share",
}

VALID_ACTIONS: set[str] = (
    _KB_DOC_ACTIONS | PLATFORM_ACTIONS | PROJECT_PERMISSION_ACTIONS
)

VALID_RESOURCE_TYPES: set[str] = {
    "kb", "document", "platform", "project_permission",
}

# ══════════════════════════════════════════════════════════════
# 资源类型 → 人类可读标签
# ══════════════════════════════════════════════════════════════

RESOURCE_TYPE_LABELS: dict[str, str] = {
    "kb": "知识库",
    "document": "文档",
    "platform": "平台功能",
    "project_permission": "项目权限数据",
}

# ══════════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════════



def _prefix_map(project_id: str | None = None) -> dict[str, str]:
    """action 前缀 → 资源类型映射。

    仅对 VALID_ACTIONS 中的已知 action 家族前缀建立映射（如 doc → document）。
    自定义类型中 "read:own" 这类作用域后缀，因 "read" 不是已知家族前缀而被过滤。
    数据来自共享的策略索引，不再单独遍历磁盘。
    """
    from services.cerbos_policy_parser import get_policy_index

    known_prefixes = {a.split(":")[0] for a in VALID_ACTIONS if ":" in a}
    index = get_policy_index()

    mapping: dict[str, str] = {}
    for resource_type in index.visible_resources(project_id):
        for action in index.actions_for_resource(resource_type, project_id):
            if ":" not in action:
                continue
            prefix = action.split(":")[0]
            if prefix in known_prefixes:
                mapping[prefix] = resource_type
    return mapping


def get_resource_actions(project_id: str | None = None) -> dict[str, list[str]]:
    """按资源类型分组返回所有有效 action。

    资源类型由 Cerbos YAML 策略文件定义（唯一权威源）：
    - platform 始终包含（全局平台管理功能）；
    - 其他资源类型全部从策略索引中动态发现，不做硬编码类型名假设；
    - 对发现的每个类型，补充 VALID_ACTIONS 中前缀匹配的 action
      （策略可能只声明了子集）。

    Args:
        project_id: 指定时只返回该项目及无项目归属的类型；None 返回全部。
    """
    from services.cerbos_policy_parser import get_resource_actions_map

    result: dict[str, set[str]] = {"platform": set(PLATFORM_ACTIONS)}

    for resource_type, actions in get_resource_actions_map(project_id).items():
        result.setdefault(resource_type, set()).update(actions)

    prefix_map = _prefix_map(project_id)
    for resource_type in list(result):
        if resource_type == "platform":
            continue
        for action in VALID_ACTIONS:
            parts = action.split(":", 1)
            if len(parts) != 2:
                continue
            if prefix_map.get(parts[0], parts[0]) == resource_type:
                result[resource_type].add(action)

    return {rt: sorted(actions) for rt, actions in sorted(result.items())}


def get_valid_actions(project_id: str | None = None) -> set[str]:
    """指定项目下全部合法 action 的并集。

    用于 CSV 导入等需要校验 action 取值的场景。取值范围随项目策略变化，
    不再受 VALID_ACTIONS 中 kb/doc 硬编码集合的限制。
    """
    actions: set[str] = set()
    for action_list in get_resource_actions(project_id).values():
        actions.update(action_list)
    return actions


def get_valid_resource_types(project_id: str | None = None) -> set[str]:
    """指定项目下全部合法资源类型。"""
    return set(get_resource_actions(project_id))
