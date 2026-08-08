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
# 项目资源动作（kb/document）
# ══════════════════════════════════════════════════════════════

_KB_DOC_ACTIONS: set[str] = {
    "kb:read", "kb:write", "kb:manage", "kb:grant",
    "doc:view", "doc:download", "doc:retrieve", "doc:unmount",
    "doc:purge", "doc:share",
}

VALID_ACTIONS: set[str] = _KB_DOC_ACTIONS | PLATFORM_ACTIONS

VALID_RESOURCE_TYPES: set[str] = {"kb", "document", "platform"}

# ══════════════════════════════════════════════════════════════
# 资源类型 → 人类可读标签
# ══════════════════════════════════════════════════════════════

RESOURCE_TYPE_LABELS: dict[str, str] = {
    "kb": "知识库",
    "document": "文档",
    "platform": "平台功能",
}

# ══════════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════════


def _build_action_prefix_map() -> dict[str, str]:
    """从 Cerbos YAML + VALID_ACTIONS 动态推导 action 前缀 → 资源类型映射。

    扫描 resource_policies/*.yaml，当 action 含 ":" 且其前缀对应 VALID_ACTIONS
    中的已知 action 家族时（如 doc:view 对应 doc:download 等同族 action），
    推导出 前缀 → resource_type 的映射。

    对于自定义类型中 "read:own" 这类作用域后缀（非资源类型前缀），
    因 "read" 不是 VALID_ACTIONS 中的已知 action 家族前缀，会被自动过滤。
    """
    import os
    from pathlib import Path

    # 从 VALID_ACTIONS 提取已知的 action 前缀集合
    known_prefixes = {a.split(":")[0] for a in VALID_ACTIONS if ":" in a}

    from app.config import get_cerbos_policies_dir
    policies_dir = get_cerbos_policies_dir()
    if not policies_dir.exists():
        return {}

    mapping: dict[str, str] = {}
    try:
        import yaml
        for yaml_file in policies_dir.rglob("resource_policies/*.yaml"):
            if ".versions" in yaml_file.parts:
                continue
            try:
                with open(yaml_file) as f:
                    for doc in yaml.safe_load_all(f):
                        if not isinstance(doc, dict):
                            continue
                        rp = doc.get("resourcePolicy", {})
                        resource_type = rp.get("resource", "")
                        if not resource_type:
                            continue
                        for rule in rp.get("rules", []):
                            for action in rule.get("actions", []):
                                if ":" in action:
                                    prefix = action.split(":")[0]
                                    # 仅映射 VALID_ACTIONS 中的已知 action 前缀
                                    if prefix in known_prefixes:
                                        mapping[prefix] = resource_type
            except Exception:
                pass
    except ImportError:
        pass

    return mapping


def _scan_cerbos_yaml_actions(project_id: str | None = None) -> dict[str, set[str]]:
    """从 Cerbos 策略 YAML 文件中扫描出所有 resource_type → {actions} 映射。

    Args:
        project_id: 若指定，仅扫描该项目目录；若 None，扫描所有项目。

    补充 VALID_ACTIONS 中未硬编码的项目自定义资源类型（如 OA 系统资源）。
    """
    import os
    from pathlib import Path

    from app.config import get_cerbos_policies_dir
    policies_dir = get_cerbos_policies_dir()
    if not policies_dir.exists():
        return {}

    # 确定扫描范围
    if project_id:
        search_roots = [policies_dir / project_id]
    else:
        search_roots = [d for d in policies_dir.iterdir() if d.is_dir() and d.name != ".versions"]

    result: dict[str, set[str]] = {}
    try:
        import yaml
        for root in search_roots:
            if not root.exists():
                continue
            for yaml_file in root.rglob("resource_policies/*.yaml"):
                if ".versions" in yaml_file.parts:
                    continue
                try:
                    with open(yaml_file) as f:
                        docs = list(yaml.safe_load_all(f))
                        for doc in docs:
                            if not isinstance(doc, dict):
                                continue
                            rp = doc.get("resourcePolicy") or doc.get("resource_policy")
                            if not rp:
                                continue
                            resource_type = rp.get("resource", "")
                            if not resource_type:
                                continue
                            if resource_type not in result:
                                result[resource_type] = set()
                            for rule in rp.get("rules", []):
                                for action in rule.get("actions", []):
                                    result[resource_type].add(action)
                except Exception:
                    pass
    except ImportError:
        pass

    return result


def get_resource_actions(project_id: str | None = None) -> dict[str, list[str]]:
    """按资源类型分组返回所有有效 action。

    资源类型由 Cerbos YAML 策略文件定义（唯一权威源）。
    - platform 始终包含（全局平台管理功能）。
    - 其他资源类型全部从项目 YAML 中动态发现，不做任何硬编码类型名假设。
    - 对 YAML 中发现的每个类型，补充 VALID_ACTIONS 中前缀匹配的 action（YAML 可能只定义子集）。

    Args:
        project_id: 若指定，仅返回该项目下的类型；若 None，返回全部项目的类型。
    """
    result: dict[str, set[str]] = {}

    # 1. platform 始终包含（全局类型）
    result["platform"] = set(PLATFORM_ACTIONS)

    # 2. 从 Cerbos YAML 扫描资源类型（唯一权威源，按项目范围）
    cerbos_actions = _scan_cerbos_yaml_actions(project_id)
    for rt, actions in cerbos_actions.items():
        if rt not in result:
            result[rt] = set()
        result[rt].update(actions)

    # 3. 对每个从 YAML 发现的资源类型，动态查找 VALID_ACTIONS 中前缀匹配的 action
    #    前缀→类型映射由 _build_action_prefix_map() 从 YAML 数据动态推导，不硬编码
    prefix_map = _build_action_prefix_map()
    for rt in list(result.keys()):
        if rt == "platform":
            continue
        for action in VALID_ACTIONS:
            parts = action.split(":", 1)
            if len(parts) == 2:
                mapped_rt = prefix_map.get(parts[0], parts[0])
                if mapped_rt == rt:
                    result[rt].add(action)

    return {rt: sorted(actions) for rt, actions in sorted(result.items())}
