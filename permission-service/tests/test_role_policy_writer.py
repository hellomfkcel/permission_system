"""自定义角色策略生成的命名空间隔离测试 — 不需要服务或数据库。

Cerbos 的模块 ID 全局唯一（资源策略按 (resource, version)，派生角色集合按 name），
目录层级不构成命名空间。生成器必须自己保证不撞车，本文件守住三条：

1. 项目级自定义角色不能对平台层资源生成策略；
2. 派生角色集合名带项目前缀，两个项目互不覆盖；
3. 自定义角色必须归属某个项目。
"""

import textwrap
from pathlib import Path

import pytest
import yaml

from services import cerbos_policy_parser as parser
from services import role_policy_writer as writer


PLATFORM_POLICY = """
apiVersion: api.cerbos.dev/v1
resourcePolicy:
  version: "default"
  resource: "platform"
  rules:
    - actions: ["platform:read", "platform:write"]
      effect: EFFECT_ALLOW
      roles: ["system_admin"]
"""

PROJECT_POLICY_TEMPLATE = """
apiVersion: api.cerbos.dev/v1
resourcePolicy:
  version: "default"
  resource: "{resource}"
  rules:
    - actions: ["{prefix}:read", "{prefix}:write"]
      effect: EFFECT_ALLOW
      roles: ["{prefix}_base"]
"""


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip())


@pytest.fixture
def policies_dir(tmp_path, monkeypatch):
    """平台层 + 两个项目，各自有一种自有资源类型。"""
    root = tmp_path / "policies"
    _write(root, "platform/resource_policies/platform.yaml", PLATFORM_POLICY)
    _write(
        root, "alpha/resource_policies/note.yaml",
        PROJECT_POLICY_TEMPLATE.format(resource="alpha_note", prefix="alpha"),
    )
    _write(
        root, "beta/resource_policies/task.yaml",
        PROJECT_POLICY_TEMPLATE.format(resource="beta_task", prefix="beta"),
    )

    monkeypatch.setattr(
        "app.config.get_cerbos_policies_dir", lambda: root, raising=False
    )
    parser.invalidate_role_actions_cache()
    yield root
    parser.invalidate_role_actions_cache()


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


# ── 1. 平台层资源不可作为目标 ──


def test_platform_actions_rejected_for_project_role(policies_dir):
    """项目级角色勾选 platform:read 必须被拒，且理由说清楚是层级问题。"""
    with pytest.raises(writer.PolicyWriteError) as exc:
        writer.write_role_policies("alpha_helper", ["user"], ["platform:read"], "alpha")
    assert "平台层资源" in str(exc.value)

    # 不得在项目目录留下 platform 资源策略（那会与 platform/ 下的文件同模块 ID）
    assert not list((policies_dir / "alpha" / "resource_policies").glob("custom_platform*"))


def test_other_project_actions_rejected(policies_dir):
    """alpha 的角色不能引用 beta 的动作 —— 那是别的项目的资源。"""
    with pytest.raises(writer.PolicyWriteError):
        writer.write_role_policies("alpha_helper", ["user"], ["beta:read"], "alpha")


def test_own_project_actions_accepted(policies_dir):
    writer.write_role_policies("alpha_helper", ["user"], ["alpha:read"], "alpha")
    generated = policies_dir / "alpha/resource_policies/custom_alpha_note_alpha_helper.yaml"
    assert generated.exists()
    policy = _load(generated)["resourcePolicy"]
    assert policy["resource"] == "alpha_note"
    assert policy["rules"][0]["derivedRoles"] == ["alpha_helper"]


# ── 2. 派生角色集合名按项目隔离 ──


def test_derived_set_names_are_project_scoped(policies_dir):
    writer.write_role_policies("alpha_helper", ["user"], ["alpha:read"], "alpha")
    writer.write_role_policies("beta_helper", ["user"], ["beta:read"], "beta")

    alpha_set = _load(policies_dir / "alpha/derived_roles/custom_roles.yaml")
    beta_set = _load(policies_dir / "beta/derived_roles/custom_roles.yaml")

    assert alpha_set["derivedRoles"]["name"] != beta_set["derivedRoles"]["name"]
    assert alpha_set["derivedRoles"]["name"] == "custom_roles_alpha"
    assert beta_set["derivedRoles"]["name"] == "custom_roles_beta"

    # 资源策略 import 的是本项目的集合
    alpha_policy = _load(
        policies_dir / "alpha/resource_policies/custom_alpha_note_alpha_helper.yaml"
    )
    assert alpha_policy["resourcePolicy"]["importDerivedRoles"] == ["custom_roles_alpha"]


def test_project_id_hyphen_normalised(policies_dir):
    """项目 ID 允许 '-'，集合名归一为 '_'；'_' 不在项目 ID 取值域内，不会撞名。"""
    assert writer.derived_set_name("rag-v14") == "custom_roles_rag_v14"


def test_legacy_import_retargeted(policies_dir):
    """同项目里引用旧全局集合名的遗留文件，写入时被改指向新集合名。"""
    legacy = policies_dir / "alpha/resource_policies/custom_alpha_note_legacy.yaml"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(textwrap.dedent("""
        apiVersion: api.cerbos.dev/v1
        resourcePolicy:
          version: default
          resource: alpha_note
          importDerivedRoles: [custom_roles]
          rules:
            - actions: [alpha:read]
              effect: EFFECT_ALLOW
              derivedRoles: [legacy]
    """).lstrip())

    writer.write_role_policies("alpha_helper", ["user"], ["alpha:read"], "alpha")

    assert _load(legacy)["resourcePolicy"]["importDerivedRoles"] == [
        "custom_roles_alpha"
    ]


# ── 3. 必须归属项目 ──


def test_platform_level_custom_role_rejected(policies_dir):
    with pytest.raises(writer.PolicyWriteError) as exc:
        writer.write_role_policies("floating", ["user"], ["alpha:read"], None)
    assert "必须归属一个项目" in str(exc.value)


# ── 回滚 ──


def test_failed_write_leaves_no_artifacts(policies_dir):
    """动作不合法时不得留下半成品文件。"""
    with pytest.raises(writer.PolicyWriteError):
        writer.write_role_policies(
            "alpha_helper", ["user"], ["alpha:read", "platform:write"], "alpha",
        )
    assert not (policies_dir / "alpha/derived_roles/custom_roles.yaml").exists()
    assert not list((policies_dir / "alpha/resource_policies").glob("custom_*"))


def test_remove_cleans_project_namespace(policies_dir):
    writer.write_role_policies("alpha_helper", ["user"], ["alpha:read"], "alpha")
    writer.remove_role_policies("alpha_helper", "alpha")
    assert not (policies_dir / "alpha/derived_roles/custom_roles.yaml").exists()
    assert not list((policies_dir / "alpha/resource_policies").glob("custom_*"))
