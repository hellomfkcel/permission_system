"""Cerbos 策略解析器 — 策略结构索引 + 指纹缓存。

只回答结构性问题：有哪些角色、角色能执行哪些动作、角色由什么条件激活。
不读授权记录，不参与运行时判定。

命名空间：
    {root}/platform/{derived_roles,resource_policies}/*.yaml   平台层，全局可见
    {root}/{project_id}/{...}/*.yaml                           项目层，仅该项目可见
    {root}/{derived_roles,resource_policies}/*.yaml            无归属，全局可见

作用域：动作按 (角色, 命名空间) 二元索引。查询某项目时只合并该项目命名空间与
全局命名空间，跨项目的动作不会串味。

角色语义：
    identity  身份角色，Keycloak 侧的入场资格，只出现在 parentRoles 或规则的
              roles 字段。
    derived   派生角色，权限持有者，由 granted_actions 或 resource.attr.acl 激活。

角色的有效权限只计策略中直接授予它的动作，不做 parentRoles 并集 ——
parentRoles 是激活条件而非权限继承。

缓存按策略目录指纹（路径 + mtime + 大小）失效，指纹探测有最小间隔，超过 TTL
强制重新探测，可感知带外修改与跨进程写入。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# 无项目归属的策略在索引中的命名空间取值
UNSCOPED_PROJECT = ""

# 平台层命名空间目录名
PLATFORM_NAMESPACE = "platform"

# 对全部项目可见的命名空间
_GLOBAL_NAMESPACES = frozenset({UNSCOPED_PROJECT, PLATFORM_NAMESPACE})

# 策略子目录名
_DERIVED_ROLES_DIR = "derived_roles"
_RESOURCE_POLICIES_DIR = "resource_policies"

# 归档目录，不参与解析
_ARCHIVE_DIR = ".versions"

# 缓存参数
_CACHE_TTL_S = 60.0            # 超过此时长强制重新探测指纹
_FINGERPRINT_INTERVAL_S = 5.0  # 两次指纹探测的最小间隔

_lock = threading.Lock()

# ── 角色激活方式 ──
ACTIVATION_IDENTITY = "identity"  # 持有 Keycloak 角色即激活
ACTIVATION_GRANT = "grant"        # 需要 granted_actions 中的授权记录
ACTIVATION_ACL = "acl"            # 需要资源实例上的 ACL 记录，权限随资源动态变化

# 条件表达式中出现这些标识即表示该规则/角色依赖运行时授权记录
_GRANT_MARKERS = ("granted_actions",)
_ACL_MARKERS = ("attr.acl", "role_acl")


def _condition_expressions(node) -> list[str]:
    """递归收集 condition 结构中的全部 expr 字符串。"""
    exprs: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "expr" and isinstance(value, str):
                exprs.append(value)
            else:
                exprs.extend(_condition_expressions(value))
    elif isinstance(node, list):
        for item in node:
            exprs.extend(_condition_expressions(item))
    return exprs


def _classify_condition(condition) -> str:
    """按条件表达式判断激活方式。"""
    blob = " ".join(_condition_expressions(condition))
    if any(marker in blob for marker in _ACL_MARKERS):
        return ACTIVATION_ACL
    if any(marker in blob for marker in _GRANT_MARKERS):
        return ACTIVATION_GRANT
    return ACTIVATION_IDENTITY


@dataclass
class PolicyIndex:
    """策略目录的解析结果。

    动作按 (名称, 命名空间) 索引，使同一角色名在不同项目中的动作互不串味。
    """
    # (role, namespace) → actions
    role_actions_scoped: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    # (role, namespace) → 需要运行时授权记录才生效的动作子集
    role_conditional_scoped: dict[tuple[str, str], set[str]] = field(
        default_factory=dict
    )
    # (resource_type, namespace) → actions
    resource_actions_scoped: dict[tuple[str, str], set[str]] = field(
        default_factory=dict
    )
    # (派生角色, 命名空间) → parentRoles，即激活条件而非权限来源。
    # 按命名空间索引：派生角色定义归属于 derivedRoles 集合，两个项目定义同名角色
    # 是合法的（集合名不同），只按角色名单键会互相覆盖。
    derived_role_parents_scoped: dict[tuple[str, str], list[str]] = field(
        default_factory=dict
    )
    # (派生角色, 命名空间) → 激活方式（identity / grant / acl）
    role_activation_scoped: dict[tuple[str, str], str] = field(default_factory=dict)
    # 角色 → 出现过的命名空间集合
    role_namespaces: dict[str, set[str]] = field(default_factory=dict)
    # 资源类型 → 出现过的命名空间集合
    resource_namespaces: dict[str, set[str]] = field(default_factory=dict)
    # 在 derivedRoles 文档中定义过的 (角色名, 命名空间)
    defined_derived_roles_scoped: set[tuple[str, str]] = field(default_factory=set)
    # 解析失败的文件，供 /api/v1/policies 侧暴露给管理员
    parse_errors: list[str] = field(default_factory=list)

    # ── 命名空间可见性 ──

    @staticmethod
    def _namespaces_for(project_id: str | None) -> frozenset[str] | None:
        """返回查询该项目时应合并的命名空间集合；None 表示不限制。"""
        if project_id is None:
            return None
        return frozenset({project_id}) | _GLOBAL_NAMESPACES

    def visible_roles(self, project_id: str | None) -> set[str]:
        """返回指定项目可见的角色名集合（含平台层与无归属的角色）。"""
        allowed = self._namespaces_for(project_id)
        if allowed is None:
            return set(self.role_namespaces)
        return {
            name
            for name, namespaces in self.role_namespaces.items()
            if namespaces & allowed
        }

    def visible_resources(self, project_id: str | None) -> set[str]:
        """返回指定项目可见的资源类型集合（含平台层与无归属的资源）。"""
        allowed = self._namespaces_for(project_id)
        if allowed is None:
            return set(self.resource_namespaces)
        return {
            name
            for name, namespaces in self.resource_namespaces.items()
            if namespaces & allowed
        }

    def resources_owned_by(self, namespace: str) -> set[str]:
        """返回归属于该命名空间的资源类型，不含全局可见的平台层资源。

        与 visible_resources 的区别是可见与可写：平台层资源对每个项目都可见，
        但项目目录里不得出现它们的资源策略，否则与 platform/ 下的策略构成
        同一个 Cerbos 模块 ID。生成项目级策略须用本方法而非 visible_resources。
        """
        return {
            name
            for name, namespaces in self.resource_namespaces.items()
            if namespace in namespaces
        }

    # ── 作用域内的动作 ──

    def _scoped_union(
        self,
        table: dict[tuple[str, str], set[str]],
        name: str,
        project_id: str | None,
    ) -> set[str]:
        allowed = self._namespaces_for(project_id)
        union: set[str] = set()
        for (key_name, namespace), actions in table.items():
            if key_name != name:
                continue
            if allowed is not None and namespace not in allowed:
                continue
            union |= actions
        return union

    def actions_for_role(self, name: str, project_id: str | None) -> set[str]:
        """角色在该项目作用域内被策略直接授予的动作。"""
        return self._scoped_union(self.role_actions_scoped, name, project_id)

    def conditional_actions_for_role(
        self, name: str, project_id: str | None
    ) -> set[str]:
        """其中需要运行时授权记录（granted_actions / ACL）才生效的动作。"""
        return self._scoped_union(self.role_conditional_scoped, name, project_id)

    def actions_for_resource(self, name: str, project_id: str | None) -> set[str]:
        """资源类型在该项目作用域内声明的动作。"""
        return self._scoped_union(self.resource_actions_scoped, name, project_id)

    # ── 角色分类 ──

    def resource_layer(self, resource_type: str) -> str:
        """资源类型属于哪一层：platform / project / unknown。

        层级取自策略文件所在的命名空间，代码中不维护第二份资源类型清单。
        授权记录须与层级对齐：平台层资源的授权 project_id 为 NULL，
        项目层资源的授权必须带项目。
        """
        namespaces = self.resource_namespaces.get(resource_type)
        if not namespaces:
            return "unknown"
        if PLATFORM_NAMESPACE in namespaces:
            return "platform"
        return "project"

    def _namespaces_in_scope(self, name: str, project_id: str | None) -> list[str]:
        """该角色在查询作用域内出现过的命名空间，项目命名空间优先。"""
        allowed = self._namespaces_for(project_id)
        namespaces = self.role_namespaces.get(name, set())
        if allowed is not None:
            namespaces = namespaces & allowed
        scoped = sorted(namespaces - _GLOBAL_NAMESPACES)
        globals_ = sorted(namespaces & _GLOBAL_NAMESPACES)
        return scoped + globals_

    def role_kind(self, name: str, project_id: str | None = None) -> str:
        """derived = 策略中定义的派生角色；identity = Keycloak 身份角色。

        按作用域判断：同一个名字可能在 A 项目是派生角色、在 B 项目只是被引用的
        父角色，查 A 时应答 derived，查 B 时应答 identity。
        """
        for namespace in self._namespaces_in_scope(name, project_id):
            if (name, namespace) in self.defined_derived_roles_scoped:
                return "derived"
        return "identity"

    def parents_of(self, name: str, project_id: str | None = None) -> list[str]:
        """激活该角色的身份角色（parentRoles），按作用域取。"""
        for namespace in self._namespaces_in_scope(name, project_id):
            parents = self.derived_role_parents_scoped.get((name, namespace))
            if parents is not None:
                return parents
        return []

    def activation_of(self, name: str, project_id: str | None = None) -> str:
        """角色的激活方式。

        派生角色取其激活条件的分类；身份角色若其被授予的动作全部带授权条件，
        同样记为 grant —— 例如 project_permission.yaml 中委托给 user 的规则。
        """
        for namespace in self._namespaces_in_scope(name, project_id):
            activation = self.role_activation_scoped.get((name, namespace))
            if activation is not None:
                return activation
        actions = self.actions_for_role(name, project_id)
        conditional = self.conditional_actions_for_role(name, project_id)
        if actions and actions == conditional:
            return ACTIVATION_GRANT
        return ACTIVATION_IDENTITY


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


def _namespace_of(policies_dir: Path, path: Path) -> str:
    """从文件路径推导所属命名空间。

    {root}/platform/resource_policies/x.yaml → "platform"（全局可见）
    {root}/{project}/derived_roles/x.yaml    → project
    {root}/derived_roles/x.yaml              → UNSCOPED_PROJECT（全局可见）
    """
    try:
        rel = path.relative_to(policies_dir)
    except ValueError:
        return UNSCOPED_PROJECT
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
        namespace = _namespace_of(policies_dir, path)
        try:
            documents = _load_documents(path)
        except Exception as exc:
            index.parse_errors.append(f"{path}: {exc}")
            continue

        for doc in documents:
            _index_derived_roles(index, doc, namespace)
            _index_resource_policy(index, doc, namespace)

    return index


def _index_derived_roles(index: PolicyIndex, doc: dict, namespace: str) -> None:
    derived = doc.get("derivedRoles") or doc.get("derived_roles")
    if not isinstance(derived, dict):
        return
    for definition in derived.get("definitions", []) or []:
        name = definition.get("name")
        if not name:
            continue
        parents = definition.get("parentRoles") or []
        index.defined_derived_roles_scoped.add((name, namespace))
        index.derived_role_parents_scoped[(name, namespace)] = parents
        index.role_activation_scoped[(name, namespace)] = _classify_condition(
            definition.get("condition")
        )
        index.role_namespaces.setdefault(name, set()).add(namespace)
        # parentRoles 中引用的身份角色也要进入索引：它们是入场资格，
        # 需要在角色列表中可见，即便自身不持有任何动作。
        for parent in parents:
            index.role_namespaces.setdefault(parent, set()).add(namespace)


def _index_resource_policy(index: PolicyIndex, doc: dict, namespace: str) -> None:
    policy = doc.get("resourcePolicy") or doc.get("resource_policy")
    if not isinstance(policy, dict):
        return

    resource_type = policy.get("resource", "")
    if resource_type:
        index.resource_actions_scoped.setdefault((resource_type, namespace), set())
        index.resource_namespaces.setdefault(resource_type, set()).add(namespace)

    for rule in policy.get("rules", []) or []:
        actions = set(rule.get("actions", []) or [])
        if not actions:
            continue
        if resource_type:
            index.resource_actions_scoped[(resource_type, namespace)] |= actions

        rule_activation = _classify_condition(rule.get("condition"))

        # Cerbos 规则中角色可出现在 roles 或 derivedRoles 字段，
        # 两者都表示"该角色在此规则下拥有这些动作"，统一收集。
        for role in (rule.get("roles") or []) + (rule.get("derivedRoles") or []):
            key = (role, namespace)
            index.role_actions_scoped.setdefault(key, set()).update(actions)
            index.role_namespaces.setdefault(role, set()).add(namespace)
            if rule_activation != ACTIVATION_IDENTITY:
                index.role_conditional_scoped.setdefault(key, set()).update(actions)


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
        project_id: 指定时只合并该项目与全局命名空间；None 返回全部。

    Returns:
        {role_name: [action, ...]}，如 {"editor": ["read", "write"]}
    """
    index = get_policy_index()
    return {
        name: sorted(index.actions_for_role(name, project_id))
        for name in index.visible_roles(project_id)
    }


def get_resource_actions_map(project_id: str | None = None) -> dict[str, list[str]]:
    """资源类型→动作映射。

    Args:
        project_id: 指定时只合并该项目与全局命名空间；None 返回全部。
    """
    index = get_policy_index()
    return {
        name: sorted(index.actions_for_resource(name, project_id))
        for name in index.visible_resources(project_id)
    }


def get_resource_layer(resource_type: str) -> str:
    """资源类型所属的授权层级：platform / project / unknown。"""
    return get_policy_index().resource_layer(resource_type)


def get_role_effective_permissions(
    name: str, project_id: str | None = None
) -> list[str]:
    """返回角色在指定项目作用域内的有效权限。

    只计策略直接授予该角色的动作：派生角色取资源策略中授予它的动作，
    身份角色取规则 roles 字段直接授予它的动作。不做 parentRoles 并集。

    与 acl_resolver 展开角色绑定时使用的映射同源。
    """
    return sorted(get_policy_index().actions_for_role(name, project_id))


def describe_role(name: str, project_id: str | None = None) -> dict:
    """返回角色的完整结构描述，角色相关展示的统一取值入口。

    Returns:
        {
            "name": "editor",
            "kind": "derived" | "identity",
            "activated_by": ["user"],          # parentRoles，激活条件而非权限来源
            "activation": "identity" | "grant" | "acl",
            "permissions": ["read", ...],      # 该作用域内策略授予的动作
            "conditional_permissions": [...],  # 其中需运行时授权记录才生效的子集
            "project_id": "demo2" | "platform" | "",
        }
    """
    index = get_policy_index()
    namespaces = index.role_namespaces.get(name, set())
    # 归属命名空间：全局（platform / 无归属）优先。
    # 身份角色 user / system_admin 会被多个项目的 parentRoles 引用，若取"第一个
    # 项目命名空间"会把它们错标成某个项目专属，管理台据此分组就会张冠李戴。
    if PLATFORM_NAMESPACE in namespaces:
        namespace = PLATFORM_NAMESPACE
    elif UNSCOPED_PROJECT in namespaces:
        namespace = UNSCOPED_PROJECT
    else:
        # 项目级角色：取查询作用域内的那一个，而不是字典序第一个 ——
        # 否则查 B 项目的同名角色会显示成 A 项目的归属。
        in_scope = index._namespaces_in_scope(name, project_id)
        namespace = in_scope[0] if in_scope else (
            sorted(namespaces)[0] if namespaces else UNSCOPED_PROJECT
        )

    return {
        "name": name,
        "kind": index.role_kind(name, project_id),
        "activated_by": index.parents_of(name, project_id),
        "activation": index.activation_of(name, project_id),
        "permissions": sorted(index.actions_for_role(name, project_id)),
        "conditional_permissions": sorted(
            index.conditional_actions_for_role(name, project_id)
        ),
        "project_id": namespace,
    }


def parse_permissions_matrix(project_id: str | None = None) -> dict:
    """角色-权限矩阵。

    每个角色名只出现一次：同名角色即使既出现在 roles 字段又被引作 parentRoles，
    也合并为一条，由 kind 字段区分派生角色与身份角色。

    Returns:
        {
            "roles": [
                {
                    "name": "editor",
                    "kind": "derived",
                    "activated_by": ["user"],
                    "activation": "grant",
                    "permissions": ["read", "write"],
                    "conditional_permissions": [...],
                    "parent_keycloak_roles": ["user"],   # 兼容旧字段
                    "source": "cerbos",                  # 兼容旧字段
                    "project_id": "demo2",
                },
                ...
            ]
        }
    """
    index = get_policy_index()

    roles_list: list[dict] = []
    for name in sorted(index.visible_roles(project_id)):
        entry = describe_role(name, project_id)
        # 兼容字段：老前端与 /policies 同步逻辑仍读这两个键
        entry["parent_keycloak_roles"] = entry["activated_by"]
        entry["source"] = "cerbos" if entry["kind"] == "derived" else "keycloak"
        roles_list.append(entry)

    return {"roles": roles_list}


def get_parse_errors() -> list[str]:
    """返回最近一次解析中失败的文件列表。"""
    return list(get_policy_index().parse_errors)
