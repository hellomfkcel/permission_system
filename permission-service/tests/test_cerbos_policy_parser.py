"""策略解析器单元测试 — 不需要运行中的服务或数据库。

覆盖本次重构要守住的四条性质：
1. 项目隔离：某项目的动作不会出现在别的项目的角色权限里；
2. 平台命名空间：platform/ 下的策略对全部项目可见；
3. 无 parentRoles 并集：身份角色不会因为"有子角色"而获得权限；
4. 矩阵去重：同一角色名只出现一次。
"""

import textwrap
from pathlib import Path

import pytest

from services import cerbos_policy_parser as parser


PLATFORM_POLICY = """
apiVersion: api.cerbos.dev/v1
resourcePolicy:
  version: "default"
  resource: "platform"
  rules:
    - actions: ["platform:read", "platform:write"]
      effect: EFFECT_ALLOW
      roles: ["system_admin"]
    - actions: ["platform:read"]
      effect: EFFECT_ALLOW
      roles: ["user"]
      condition:
        match:
          expr: >
            has(request.principal.attr.granted_actions) &&
            request.resource.id in request.principal.attr.granted_actions
"""

RAG_DERIVED = """
apiVersion: api.cerbos.dev/v1
derivedRoles:
  name: rag_roles
  definitions:
    - name: kb_reader
      parentRoles: ["user"]
      condition:
        match:
          expr: >
            request.principal.attr.granted_actions[request.resource.id]
              .exists(x, x == "read")
    - name: acl_user
      parentRoles: ["user"]
      condition:
        match:
          expr: request.principal.id in request.resource.attr.acl
    - name: super
      parentRoles: ["system_admin"]
      condition:
        match:
          expr: "true"
"""

RAG_RESOURCE = """
apiVersion: api.cerbos.dev/v1
resourcePolicy:
  version: "default"
  resource: "kb"
  importDerivedRoles: ["rag_roles"]
  rules:
    - actions: ["kb:read"]
      effect: EFFECT_ALLOW
      derivedRoles: ["kb_reader", "super"]
    - actions: ["kb:grant"]
      effect: EFFECT_ALLOW
      derivedRoles: ["super"]
    - actions: ["kb:read"]
      effect: EFFECT_ALLOW
      derivedRoles: ["acl_user"]
"""

OTHER_DERIVED = """
apiVersion: api.cerbos.dev/v1
derivedRoles:
  name: oa_roles
  definitions:
    - name: oa_employee
      parentRoles: ["user"]
"""

OTHER_RESOURCE = """
apiVersion: api.cerbos.dev/v1
resourcePolicy:
  version: "default"
  resource: "oa_leave"
  importDerivedRoles: ["oa_roles"]
  rules:
    - actions: ["create", "read:own"]
      effect: EFFECT_ALLOW
      roles: ["oa_employee"]
"""


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip())


@pytest.fixture
def policies_dir(tmp_path, monkeypatch):
    """搭一个三命名空间的策略目录：platform / rag / oa。"""
    root = tmp_path / "policies"
    _write(root, "platform/resource_policies/platform.yaml", PLATFORM_POLICY)
    _write(root, "rag/derived_roles/rag_roles.yaml", RAG_DERIVED)
    _write(root, "rag/resource_policies/kb.yaml", RAG_RESOURCE)
    _write(root, "oa/derived_roles/oa_roles.yaml", OTHER_DERIVED)
    _write(root, "oa/resource_policies/leave.yaml", OTHER_RESOURCE)
    # 归档目录不应参与解析
    _write(root, "rag/.versions/resource_policies/old.yaml", RAG_RESOURCE)

    monkeypatch.setattr(
        "app.config.get_cerbos_policies_dir", lambda: root, raising=False
    )
    parser.invalidate_role_actions_cache()
    yield root
    parser.invalidate_role_actions_cache()


# ── 1. 项目隔离 ──


def test_project_scope_does_not_leak_other_projects(policies_dir):
    """oa 项目里看不到 rag 的动作，反之亦然。"""
    oa_roles = parser.get_role_actions_map("oa")
    assert "kb_reader" not in oa_roles
    assert all("kb:" not in a for actions in oa_roles.values() for a in actions)

    rag_roles = parser.get_role_actions_map("rag")
    assert "oa_employee" not in rag_roles


def test_project_without_policies_sees_only_platform(policies_dir):
    """连策略目录都没有的项目，只应看到平台层角色。"""
    roles = parser.get_role_actions_map("brand-new-project")
    assert set(roles) == {"system_admin", "user"}
    # 且只有平台动作，不含任何项目动作
    for actions in roles.values():
        assert all(a.startswith("platform:") for a in actions)


def test_identity_role_actions_are_scoped(policies_dir):
    """身份角色的权限也按作用域算，不是全平台并集。"""
    assert parser.get_role_effective_permissions("system_admin", "oa") == [
        "platform:read", "platform:write",
    ]
    assert parser.get_role_effective_permissions("system_admin", "rag") == [
        "platform:read", "platform:write",
    ]


# ── 2. 平台命名空间 ──


def test_platform_namespace_visible_everywhere(policies_dir):
    for project in ("rag", "oa", "anything"):
        assert "platform" in parser.get_resource_actions_map(project)


def test_platform_roles_report_platform_namespace(policies_dir):
    assert parser.describe_role("system_admin", "rag")["project_id"] == "platform"


# ── 3. 不做 parentRoles 并集 ──


def test_identity_role_does_not_inherit_child_permissions(policies_dir):
    """user 是 kb_reader / acl_user 的 parentRole，但不因此获得 kb:read。

    这是"14 个权限 vs 4 个权限"那类困惑的根源：把激活关系当成继承关系。
    """
    user_perms = parser.get_role_effective_permissions("user", "rag")
    assert "kb:read" not in user_perms
    assert user_perms == ["platform:read"]


def test_derived_role_keeps_its_own_permissions(policies_dir):
    assert parser.get_role_effective_permissions("kb_reader", "rag") == ["kb:read"]
    assert parser.get_role_effective_permissions("super", "rag") == [
        "kb:grant", "kb:read",
    ]


# ── 4. 矩阵去重与角色分类 ──


def test_matrix_has_no_duplicate_role_entries(policies_dir):
    names = [r["name"] for r in parser.parse_permissions_matrix("rag")["roles"]]
    assert len(names) == len(set(names))


def test_role_kind_and_activation(policies_dir):
    by_name = {
        r["name"]: r for r in parser.parse_permissions_matrix("rag")["roles"]
    }
    assert by_name["kb_reader"]["kind"] == "derived"
    assert by_name["kb_reader"]["activation"] == "grant"
    assert by_name["acl_user"]["activation"] == "acl"
    assert by_name["super"]["activation"] == "identity"
    assert by_name["user"]["kind"] == "identity"
    # 身份角色被条件规则授予的动作要标成"需授权记录"
    assert by_name["user"]["conditional_permissions"] == ["platform:read"]


def test_archived_versions_are_ignored(policies_dir):
    """.versions/ 下的历史快照不参与解析。"""
    assert parser.get_parse_errors() == []
    index = parser.get_policy_index()
    assert index.resource_namespaces["kb"] == {"rag"}
