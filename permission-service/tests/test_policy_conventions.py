"""策略文件约定检查 — 对仓库里真实的 cerbos/policies/ 目录跑。

这些约定是本次重构的结构性保证，靠人工评审守不住，放进 CI：

  A. 全库没有显式 DENY 规则（未命中即隐式拒绝）；
  B. 平台层只住在 platform/ 目录，且不 import 任何项目派生角色；
  C. 项目层不放平台层资源（platform / project_permission）；
  D. 项目层的授权规则不直接授给身份角色（user / system_admin / admin）；
  E. 全部策略文件可解析，且 importDerivedRoles 引用的角色集合确实存在。
"""

from pathlib import Path

import pytest
import yaml

from services.cerbos_policy_parser import PLATFORM_NAMESPACE

# 身份角色 = Keycloak 侧的入场资格，只能出现在 parentRoles，不能在项目层持权
_IDENTITY_ROLES = {"user", "system_admin", "admin"}

# 只属于平台层的资源类型
_PLATFORM_RESOURCES = {"platform", "project_permission"}

_ARCHIVE_DIR = ".versions"


def _policies_root() -> Path:
    return Path(__file__).resolve().parents[2] / "cerbos" / "policies"


def _policy_files() -> list[Path]:
    root = _policies_root()
    return sorted(
        p
        for p in root.rglob("*.y*ml")
        if _ARCHIVE_DIR not in p.parts
        and p.parent.name in ("derived_roles", "resource_policies")
    )


def _documents(path: Path) -> list[dict]:
    with open(path) as fh:
        return [d for d in yaml.safe_load_all(fh) if isinstance(d, dict)]


def _namespace(path: Path) -> str:
    rel = path.relative_to(_policies_root())
    return rel.parts[0] if len(rel.parts) >= 3 else ""


@pytest.fixture(scope="module")
def policies() -> list[tuple[Path, str, dict]]:
    """[(路径, 命名空间, 文档), ...]"""
    out = []
    for path in _policy_files():
        for doc in _documents(path):
            out.append((path, _namespace(path), doc))
    assert out, "策略目录为空，检查 cerbos/policies/ 是否存在"
    return out


# ── A. 无显式 DENY ──


def test_no_explicit_deny_rules(policies):
    offenders = [
        f"{path}:{rule.get('name', '<unnamed>')}"
        for path, _, doc in policies
        for rule in ((doc.get("resourcePolicy") or {}).get("rules") or [])
        if rule.get("effect") == "EFFECT_DENY"
    ]
    assert not offenders, (
        "策略中不应出现显式 DENY —— Cerbos 未命中即拒绝，"
        f"显式 DENY 会与 ACL 等并行授权路径互相覆盖：{offenders}"
    )


# ── B. 平台层的位置与独立性 ──


def test_platform_resources_only_live_in_platform_namespace(policies):
    misplaced = [
        f"{path} (resource={resource}, namespace={ns or '<root>'})"
        for path, ns, doc in policies
        for resource in [(doc.get("resourcePolicy") or {}).get("resource")]
        if resource in _PLATFORM_RESOURCES and ns != PLATFORM_NAMESPACE
    ]
    assert not misplaced, (
        "平台层策略必须放在 cerbos/policies/platform/ 下，"
        f"不能挂在某个具体项目里：{misplaced}"
    )


def test_platform_namespace_imports_no_derived_roles(policies):
    offenders = [
        str(path)
        for path, ns, doc in policies
        if ns == PLATFORM_NAMESPACE
        and (doc.get("resourcePolicy") or {}).get("importDerivedRoles")
    ]
    assert not offenders, (
        "平台层不得依赖任何项目的派生角色，否则新增/删除项目会影响平台准入："
        f"{offenders}"
    )


def test_platform_namespace_defines_no_derived_roles(policies):
    offenders = [
        str(path)
        for path, ns, doc in policies
        if ns == PLATFORM_NAMESPACE and doc.get("derivedRoles")
    ]
    assert not offenders, f"平台层只用 roles 字段，不定义派生角色：{offenders}"


# ── C/D. 项目层的约束 ──


def test_project_policies_do_not_grant_identity_roles(policies):
    """项目层的授权规则只能授给派生角色。

    身份角色一旦在项目资源策略的 roles 字段里出现，就等于"凡是登录用户都有
    这个权限"，与 granted_actions 的授权模型冲突，也是 user 看起来全权的来源。

    例外：静态角色式项目（demo2 / demo3）用 roles 字段消费平台侧的角色绑定，
    那些名字不是身份角色，不在本检查范围内。
    """
    offenders = []
    for path, ns, doc in policies:
        if ns in (PLATFORM_NAMESPACE, ""):
            continue
        for rule in ((doc.get("resourcePolicy") or {}).get("rules") or []):
            granted = set(rule.get("roles") or []) & _IDENTITY_ROLES
            if granted:
                offenders.append(
                    f"{path}:{rule.get('name', '<unnamed>')} → {sorted(granted)}"
                )
    assert not offenders, (
        "项目层资源策略不得把权限直接授给身份角色，应改为派生角色："
        f"{offenders}"
    )


# ── E. 结构完整性 ──


def test_all_policy_files_parse(policies):
    # _documents() 解析失败会直接抛异常，这里再确认每个文档都有可识别的顶层结构
    unknown = [
        str(path)
        for path, _, doc in policies
        if not (doc.get("resourcePolicy") or doc.get("derivedRoles"))
    ]
    assert not unknown, f"策略文件缺少 resourcePolicy / derivedRoles 顶层结构：{unknown}"


def test_imported_derived_role_sets_exist(policies):
    defined = {
        doc["derivedRoles"]["name"]
        for _, _, doc in policies
        if isinstance(doc.get("derivedRoles"), dict) and doc["derivedRoles"].get("name")
    }
    missing = [
        f"{path} → {name}"
        for path, _, doc in policies
        for name in ((doc.get("resourcePolicy") or {}).get("importDerivedRoles") or [])
        if name not in defined
    ]
    assert not missing, f"importDerivedRoles 引用了不存在的派生角色集合：{missing}"


def test_no_duplicate_resource_policy_module_ids(policies):
    """资源策略的模块 ID 是 (resource, version, scope)，**全局**唯一。

    目录层级不是 Cerbos 的命名空间：两个项目各写一份 resource: document 的策略，
    就是同一个模块的两份定义，加载结果不确定。
    """
    seen: dict[tuple[str, str, str], list[str]] = {}
    for path, _, doc in policies:
        policy = doc.get("resourcePolicy")
        if not isinstance(policy, dict):
            continue
        key = (
            policy.get("resource", ""),
            policy.get("version", "default"),
            policy.get("scope", ""),
        )
        seen.setdefault(key, []).append(str(path))
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    assert not dupes, f"资源策略模块 ID 重复（resource, version, scope）：{dupes}"


def test_no_duplicate_derived_role_set_names(policies):
    """派生角色集合名同样全局唯一。

    自定义角色的集合名由 role_policy_writer.derived_set_name() 按项目生成，
    手写的集合名也必须各不相同。
    """
    seen: dict[str, list[str]] = {}
    for path, _, doc in policies:
        derived = doc.get("derivedRoles")
        if isinstance(derived, dict) and derived.get("name"):
            seen.setdefault(derived["name"], []).append(str(path))
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    assert not dupes, f"派生角色集合名重复：{dupes}"


def test_referenced_derived_roles_are_defined(policies):
    """资源策略 derivedRoles 字段引用的角色都要有定义。"""
    defined_roles = {
        definition["name"]
        for _, _, doc in policies
        if isinstance(doc.get("derivedRoles"), dict)
        for definition in (doc["derivedRoles"].get("definitions") or [])
        if isinstance(definition, dict) and definition.get("name")
    }
    missing = []
    for path, _, doc in policies:
        for rule in ((doc.get("resourcePolicy") or {}).get("rules") or []):
            for role in (rule.get("derivedRoles") or []):
                if role not in defined_roles:
                    missing.append(f"{path}:{rule.get('name', '<unnamed>')} → {role}")
    assert not missing, f"规则引用了未定义的派生角色：{missing}"
