"""Cerbos 策略解析器 — 从 YAML 文件解析角色→权限矩阵 + 运行时缓存。

设计依据：docs/manage_role_design.md §4.2 权限矩阵的数据来源。

P3 修复：消除 ROLE_ACTIONS_MAP 硬编码副本风险。
Cerbos YAML 是角色→权限映射的唯一权威源。
get_role_actions_map() 从 YAML 解析 + 内存缓存 + 写时失效，
替换原来的 ROLE_ACTIONS_MAP 硬编码字典。
"""

import os
import threading
from pathlib import Path

import yaml


# Cerbos 策略文件路径（从环境变量或默认值）
_CERBOS_POLICIES_DIR = os.getenv(
    "CERBOS_POLICIES_DIR",
    "/home/mfkcel/proj_rag_dev/cerbos/policies",
)

# ── 角色动作映射缓存 ──
# 从 Cerbos YAML 解析得到的 {role_name: [action_strings]} 映射。
# 首次调用 get_role_actions_map() 时解析并缓存。
# 策略文件变更后调用 invalidate_role_actions_cache() 失效缓存，
# 下次查询时自动重新解析。
_cache_lock = threading.Lock()
_role_actions_cache: dict[str, list[str]] | None = None


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


def get_role_actions_map() -> dict[str, list[str]]:
    """从 Cerbos YAML 解析角色→权限动作映射（带缓存）。

    替代原来的 app/role_actions_config.py:ROLE_ACTIONS_MAP 硬编码字典。
    Cerbos YAML 是唯一权威源，消除手动同步风险。

    首次调用时解析 YAML 并缓存。缓存通过 invalidate_role_actions_cache()
    主动失效（策略文件变更时触发），也可等待 TTL 自然过期。

    Returns:
        {role_name: [action_strings]}，如 {"kb_reader": ["kb:read", "doc:view", ...]}
    """
    global _role_actions_cache

    with _cache_lock:
        if _role_actions_cache is not None:
            return _role_actions_cache

    # 缓存未命中 → 从 Cerbos YAML 解析
    mapping: dict[str, list[str]] = {}
    matrix = parse_permissions_matrix()
    for role_info in matrix.get("roles", []):
        if role_info.get("source") == "cerbos":
            name = role_info["name"]
            mapping[name] = sorted(role_info.get("permissions", []))

    with _cache_lock:
        _role_actions_cache = mapping

    return mapping


def invalidate_role_actions_cache() -> None:
    """主动失效角色动作映射缓存。

    在以下场景调用：
    - PUT /api/v1/policies/{path}  — 策略文件写入
    - DELETE /api/v1/policies/{path} — 策略文件删除
    - POST /api/v1/roles/definitions — 创建自定义角色（写入 Cerbos YAML）
    - DELETE /api/v1/roles/definitions/{name} — 删除自定义角色（清理 YAML）

    缓存失效后，下次 get_role_actions_map() 调用时自动重新解析。
    """
    global _role_actions_cache
    with _cache_lock:
        _role_actions_cache = None
