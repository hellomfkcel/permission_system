"""Cerbos 策略解析器 — 策略文件的统一索引 + 指纹缓存。

设计依据：docs/manage_role_design.md §4.2 权限矩阵的数据来源。

Cerbos YAML 是角色→动作、资源类型→动作两组映射的唯一权威源。本模块把策略目录
解析成一份 PolicyIndex，供以下消费者共用：

- services/acl_resolver.py   角色绑定展开为 granted_actions
- api/role_definitions_routes.py  角色列表 / 详情 / 权限矩阵
- app/role_actions_config.py      资源类型与动作目录

缓存策略（修复：缓存无 TTL、跨进程与带外改文件不自愈）：
- 解析结果按策略目录指纹（文件路径 + mtime + 大小）缓存；
- 指纹探测有最小间隔，避免高频判定路径上产生 stat 风暴；
- 超过 TTL 强制重新探测；
- invalidate_role_actions_cache() 仍可由写操作主动失效（本进程即时生效）。
指纹机制使得其他进程写入的策略、以及绕过 API 直接编辑的文件都能被自动感知。

命名空间（修复：扫描路径与写入路径错位）：
策略目录下的布局有两种，均被识别：
- {root}/{project_id}/{derived_roles,resource_policies}/*.yaml  项目级策略
- {root}/{derived_roles,resource_policies}/*.yaml               无项目归属（平台级）
无项目归属的策略对全部项目可见，与 role_definitions.project_id IS NULL 的语义一致。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# 无项目归属的策略在索引中的 project_id 取值
UNSCOPED_PROJECT = ""

# 策略子目录名
_DERIVED_ROLES_DIR = "derived_roles"
_RESOURCE_POLICIES_DIR = "resource_policies"

# 归档目录，不参与解析
_ARCHIVE_DIR = ".versions"

# 缓存参数
_CACHE_TTL_S = 60.0            # 超过此时长强制重新探测指纹
_FINGERPRINT_INTERVAL_S = 5.0  # 两次指纹探测的最小间隔

_lock = threading.Lock()


@dataclass
class PolicyIndex:
    """策略目录的解析结果。

    role_actions / resource_actions 的键在全平台唯一（Cerbos 单策略根的固有约束），
    role_project / resource_project 记录其来源项目，供按项目过滤。
    """

    role_actions: dict[str, set[str]] = field(default_factory=dict)
    resource_actions: dict[str, set[str]] = field(default_factory=dict)
    derived_role_parents: dict[str, list[str]] = field(default_factory=dict)
    role_project: dict[str, str] = field(default_factory=dict)
    resource_project: dict[str, str] = field(default_factory=dict)
    # 解析失败的文件，供 /api/v1/policies 侧暴露给管理员
    parse_errors: list[str] = field(default_factory=list)

    def visible_roles(self, project_id: str | None) -> set[str]:
        """返回指定项目可见的角色名集合（含无项目归属的角色）。"""
        if project_id is None:
            return set(self.role_actions)
        return {
            name
            for name in self.role_actions
            if self.role_project.get(name, UNSCOPED_PROJECT)
            in (project_id, UNSCOPED_PROJECT)
        }

    def visible_resources(self, project_id: str | None) -> set[str]:
        """返回指定项目可见的资源类型集合（含无项目归属的资源）。"""
        if project_id is None:
            return set(self.resource_actions)
        return {
            name
            for name in self.resource_actions
            if self.resource_project.get(name, UNSCOPED_PROJECT)
            in (project_id, UNSCOPED_PROJECT)
        }


# ── 缓存状态 ──
_index: PolicyIndex | None = None
_index_fingerprint: tuple | None = None
_index_parsed_at: float = 0.0
_last_fingerprint_check: float = 0.0


def _policy_files(policies_dir: Path) -> list[Path]:
    """列出参与解析的全部策略文件。

    识别 derived_roles/ 与 resource_policies/ 两类父目录，忽略 .versions/ 归档。
    """
    if not policies_dir.exists():
        return []
    files: list[Path] = []
    for path in policies_dir.rglob("*.y*ml"):
        if _ARCHIVE_DIR in path.parts:
            continue
        if path.parent.name not in (_DERIVED_ROLES_DIR, _RESOURCE_POLICIES_DIR):
            continue
        files.append(path)
    return sorted(files)


def _project_of(policies_dir: Path, path: Path) -> str:
    """从文件路径推导所属项目。

    {root}/{project}/derived_roles/x.yaml → project
    {root}/derived_roles/x.yaml           → UNSCOPED_PROJECT
    """
    try:
        rel = path.relative_to(policies_dir)
    except ValueError:
        return UNSCOPED_PROJECT
    # rel 形如 (project, 'derived_roles', 'x.yaml') 或 ('derived_roles', 'x.yaml')
    if len(rel.parts) >= 3:
        return rel.parts[0]
    return UNSCOPED_PROJECT


def _fingerprint(files: list[Path]) -> tuple:
    """策略目录指纹：文件路径、修改时间、大小。

    任一文件新增、删除、被改写都会导致指纹变化。
    """
    entries: list[tuple[str, int, int]] = []
    for f in files:
        try:
            st = f.stat()
            entries.append((str(f), st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    return tuple(entries)


def _load_documents(path: Path) -> list[dict]:
    """加载 YAML 文件中的全部文档（兼容多文档文件）。"""
    with open(path) as fh:
        return [doc for doc in yaml.safe_load_all(fh) if isinstance(doc, dict)]


def _build_index(policies_dir: Path, files: list[Path]) -> PolicyIndex:
    """解析全部策略文件，构建索引。"""
    index = PolicyIndex()

    for path in files:
        project = _project_of(policies_dir, path)
        try:
            documents = _load_documents(path)
        except Exception as exc:
            index.parse_errors.append(f"{path}: {exc}")
            continue

        for doc in documents:
            derived = doc.get("derivedRoles") or doc.get("derived_roles")
            if isinstance(derived, dict):
                for definition in derived.get("definitions", []) or []:
                    name = definition.get("name")
                    if not name:
                        continue
                    index.derived_role_parents[name] = (
                        definition.get("parentRoles") or []
                    )
                    index.role_project.setdefault(name, project)

            policy = doc.get("resourcePolicy") or doc.get("resource_policy")
            if isinstance(policy, dict):
                resource_type = policy.get("resource", "")
                if resource_type:
                    index.resource_actions.setdefault(resource_type, set())
                    index.resource_project.setdefault(resource_type, project)
                for rule in policy.get("rules", []) or []:
                    actions = rule.get("actions", []) or []
                    if resource_type:
                        index.resource_actions[resource_type].update(actions)
                    # Cerbos 规则中角色可出现在 roles 或 derivedRoles 字段，
                    # 两者都表示"持有该角色即拥有这些动作"，统一收集。
                    for role in (rule.get("roles") or []) + (
                        rule.get("derivedRoles") or []
                    ):
                        index.role_actions.setdefault(role, set()).update(actions)
                        index.role_project.setdefault(role, project)

    return index


def get_policy_index(force: bool = False) -> PolicyIndex:
    """返回策略索引，按指纹与 TTL 自动刷新。

    Args:
        force: 跳过探测间隔，立即重新计算指纹。写操作后可用。
    """
    global _index, _index_fingerprint, _index_parsed_at, _last_fingerprint_check

    from app.config import get_cerbos_policies_dir

    now = time.monotonic()

    with _lock:
        cached = _index
        checked_recently = (now - _last_fingerprint_check) < _FINGERPRINT_INTERVAL_S
        fresh = (now - _index_parsed_at) < _CACHE_TTL_S

    # 命中缓存且刚探测过 → 直接返回，避免高频路径上的 stat 开销
    if cached is not None and not force and checked_recently and fresh:
        return cached

    policies_dir = get_cerbos_policies_dir()
    files = _policy_files(policies_dir)
    fingerprint = _fingerprint(files)

    with _lock:
        _last_fingerprint_check = now
        if _index is not None and fingerprint == _index_fingerprint:
            # 内容未变，仅刷新解析时间戳
            _index_parsed_at = now
            return _index

    index = _build_index(policies_dir, files)

    with _lock:
        _index = index
        _index_fingerprint = fingerprint
        _index_parsed_at = now
        return index


def invalidate_role_actions_cache() -> None:
    """主动失效策略索引。

    在以下场景调用：
    - PUT / DELETE /api/v1/policies/{path}      策略文件写入或删除
    - POST / PUT / DELETE /api/v1/roles/definitions  自定义角色写入 Cerbos YAML

    本进程立即失效；其他进程通过指纹探测在 _FINGERPRINT_INTERVAL_S 内自动感知。
    """
    global _index, _index_fingerprint, _index_parsed_at, _last_fingerprint_check
    with _lock:
        _index = None
        _index_fingerprint = None
        _index_parsed_at = 0.0
        _last_fingerprint_check = 0.0


def get_role_actions_map(project_id: str | None = None) -> dict[str, list[str]]:
    """角色→动作映射。

    Args:
        project_id: 指定时只返回该项目及无项目归属的角色；None 返回全部。

    Returns:
        {role_name: [action, ...]}，如 {"kb_reader": ["doc:view", "kb:read"]}
    """
    index = get_policy_index()
    names = index.visible_roles(project_id)
    return {name: sorted(index.role_actions[name]) for name in names}


def get_resource_actions_map(project_id: str | None = None) -> dict[str, list[str]]:
    """资源类型→动作映射。

    Args:
        project_id: 指定时只返回该项目及无项目归属的资源类型；None 返回全部。
    """
    index = get_policy_index()
    names = index.visible_resources(project_id)
    return {name: sorted(index.resource_actions[name]) for name in names}


def get_role_effective_permissions(
    name: str, project_id: str | None = None
) -> list[str]:
    """返回角色在策略文件中的**有效权限**（含身份角色的继承并集）。

    取值只来自 Cerbos 策略文件（唯一权威源）：
    - 派生角色（策略中直接出现）：直接返回其 role_actions；
    - 父角色（被派生角色 parentRoles 引用，如 Keycloak 身份角色 system_admin/user）：
      所有以其为 parentRole 的派生角色权限的**并集**。

    与 acl_resolver 展开角色绑定时使用的映射同源。
    修复：此前 /definitions 对身份角色硬编码返回 []，管理台显示"无权限"，
    但判定链路（system_admin → admin 派生角色）实际授予全部权限 —— 展示与判定脱节。
    """
    index = get_policy_index()
    visible = index.visible_roles(project_id)

    # 1. 角色本身就是策略中的派生角色 → 直接取其动作
    if name in visible:
        return sorted(index.role_actions[name])

    # 2. 角色是被引用的父角色（身份角色）→ 以其为父的派生角色权限并集
    union: set[str] = set()
    for derived_name, parents in index.derived_role_parents.items():
        if derived_name not in visible:
            continue
        if name in parents:
            union.update(index.role_actions.get(derived_name, set()))
    return sorted(union)


def parse_permissions_matrix(project_id: str | None = None) -> dict:
    """角色-权限矩阵。

    Returns:
        {
            "roles": [
                {
                    "name": "kb_reader",
                    "parent_keycloak_roles": ["user"],
                    "permissions": ["doc:view", "kb:read"],
                    "source": "cerbos" | "keycloak",
                    "project_id": "rag-v14",
                },
                ...
            ]
        }

    source=cerbos 为策略中直接出现的角色；source=keycloak 为派生角色的父角色，
    其权限是所有以它为父的派生角色权限的并集。
    """
    index = get_policy_index()
    visible = index.visible_roles(project_id)

    roles_list: list[dict] = []
    for name in sorted(visible):
        roles_list.append({
            "name": name,
            "parent_keycloak_roles": index.derived_role_parents.get(name, []),
            "permissions": sorted(index.role_actions[name]),
            "source": "cerbos",
            "project_id": index.role_project.get(name, UNSCOPED_PROJECT),
        })

    # 父角色（Keycloak 身份角色等）的权限 = 以其为 parentRole 的派生角色权限并集
    # 复用 get_role_effective_permissions 保证与 /definitions 展示同源（单一权威）。
    parent_names: set[str] = set()
    for derived_name, parents in index.derived_role_parents.items():
        if derived_name not in visible:
            continue
        parent_names.update(parents)

    for parent in sorted(parent_names):
        roles_list.append({
            "name": parent,
            "parent_keycloak_roles": [],
            "permissions": get_role_effective_permissions(parent, project_id),
            "source": "keycloak",
            "project_id": UNSCOPED_PROJECT,
        })

    return {"roles": roles_list}


def get_parse_errors() -> list[str]:
    """返回最近一次解析中失败的文件列表。"""
    return list(get_policy_index().parse_errors)
