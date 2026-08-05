"""Cerbos 策略解析器 — 从 YAML 文件解析角色→权限矩阵。

设计依据：docs/manage_role_design.md §4.2 权限矩阵的数据来源。
"""

import os
from pathlib import Path

import yaml


# Cerbos 策略文件路径（从环境变量或默认值）
_CERBOS_POLICIES_DIR = os.getenv(
    "CERBOS_POLICIES_DIR",
    "/home/mfkcel/proj_rag_dev/cerbos/policies",
)


def _load_yaml(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def parse_permissions_matrix() -> dict:
    """从 Cerbos 策略 YAML 文件解析完整的角色-权限矩阵。

    Returns:
        {
            "roles": [
                {
                    "name": "kb_reader",
                    "parent_keycloak_roles": ["user"],
                    "permissions": ["kb:read", "doc:view", ...]
                },
                ...
            ]
        }
    """
    policies_dir = Path(_CERBOS_POLICIES_DIR)
    if not policies_dir.exists():
        return {"roles": []}

    # ── 1. 解析派生角色定义 ──
    derived_roles: dict[str, list[str]] = {}  # name → parentRoles
    dr_path = policies_dir / "derived_roles" / "rag_roles.yaml"
    if dr_path.exists():
        dr_data = _load_yaml(str(dr_path))
        for dr in dr_data.get("derivedRoles", {}).get("definitions", []):
            derived_roles[dr["name"]] = dr.get("parentRoles", [])

    # ── 2. 解析资源策略：role → [actions] ──
    role_actions: dict[str, set[str]] = {}  # role → set of actions
    rp_dir = policies_dir / "resource_policies"
    if rp_dir.exists():
        for yaml_file in rp_dir.glob("*.yaml"):
            rp_data = _load_yaml(str(yaml_file))
            for rule in rp_data.get("resourcePolicy", {}).get("rules", []):
                actions = rule.get("actions", [])
                for role in rule.get("derivedRoles", []):
                    if role not in role_actions:
                        role_actions[role] = set()
                    role_actions[role].update(actions)

    # ── 3. 构建矩阵（Cerbos 派生角色）──
    roles_list = []
    for name in sorted(role_actions.keys()):
        roles_list.append({
            "name": name,
            "parent_keycloak_roles": derived_roles.get(name, []),
            "permissions": sorted(role_actions[name]),
            "source": "cerbos",
        })

    # ── 4. 计算 Keycloak 身份角色的权限（union of derived roles）──
    kc_role_perms: dict[str, set[str]] = {}
    for dr_name, parent_roles in derived_roles.items():
        for pr in parent_roles:
            if pr not in kc_role_perms:
                kc_role_perms[pr] = set()
            if dr_name in role_actions:
                kc_role_perms[pr].update(role_actions[dr_name])

    for kc_name in sorted(kc_role_perms.keys()):
        roles_list.append({
            "name": kc_name,
            "parent_keycloak_roles": [],
            "permissions": sorted(kc_role_perms[kc_name]),
            "source": "keycloak",
        })

    return {"roles": roles_list}
