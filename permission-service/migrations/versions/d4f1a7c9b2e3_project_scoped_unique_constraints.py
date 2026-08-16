"""project-scoped unique constraints

Revision ID: d4f1a7c9b2e3
Revises: c3d4e5f6a7b8
Create Date: 2026-08-16

把资源镜像、ACL、角色绑定、角色定义的唯一性从全平台收敛到项目内。

原状态下：
- resource_registry 唯一键是 (resource_type, resource_id)，两个项目使用同名资源类型
  且资源 ID 相同时会互相覆盖；
- acl_entries 唯一键不含 project_id，同一主体在不同项目对同名资源的授权互相冲突；
- role_bindings 同上；
- role_definitions.name 全表唯一，项目级角色无法与其他项目重名。

改为按项目隔离后，跨项目的资源 ID 与角色名冲突不再相互影响。
role_definitions 用两个部分唯一索引表达：项目级角色在项目内唯一，
平台级角色（project_id IS NULL）在全平台唯一（Postgres 中 NULL 互不相等，
无法用单一复合唯一约束表达这一语义）。
"""

from alembic import op


revision = 'd4f1a7c9b2e3'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── role_definitions.permissions ──
    # 补建列：dddcf4c5164c 创建 role_definitions 时遗漏该列，而 a1b2c3d4e5f6
    # 的种子数据直接写入它，导致空库执行 alembic upgrade head 在种子步骤失败。
    # 该列在既有部署中可能已由其他途径存在，故用 IF NOT EXISTS 补建。
    op.execute(
        "ALTER TABLE role_definitions "
        "ADD COLUMN IF NOT EXISTS permissions JSONB NOT NULL DEFAULT '[]'::jsonb"
    )

    # ── resource_registry ──
    op.execute(
        "ALTER TABLE resource_registry DROP CONSTRAINT IF EXISTS uq_resource_type_id"
    )
    op.execute(
        "ALTER TABLE resource_registry "
        "ADD CONSTRAINT uq_resource_project_type_id "
        "UNIQUE (project_id, resource_type, resource_id)"
    )

    # ── acl_entries ──
    # project_id 可为 NULL（平台级授权），Postgres 中 NULL 互不相等，
    # 项目级与平台级唯一性需分别用部分唯一索引表达。
    op.execute(
        "ALTER TABLE acl_entries "
        "DROP CONSTRAINT IF EXISTS uq_acl_principal_resource_action"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_acl_project_scoped "
        "ON acl_entries (project_id, principal, resource_type, resource_id, action) "
        "WHERE project_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_acl_platform_scoped "
        "ON acl_entries (principal, resource_type, resource_id, action) "
        "WHERE project_id IS NULL"
    )

    # ── role_bindings ──
    op.execute(
        "ALTER TABLE role_bindings DROP CONSTRAINT IF EXISTS uq_role_binding"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_role_binding_project_scoped "
        "ON role_bindings (project_id, principal, role, resource_type, resource_id) "
        "WHERE project_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_role_binding_platform_scoped "
        "ON role_bindings (principal, role) "
        "WHERE project_id IS NULL"
    )

    # ── role_definitions ──
    # 列级 unique=True 由 Postgres 生成 role_definitions_name_key
    op.execute(
        "ALTER TABLE role_definitions DROP CONSTRAINT IF EXISTS role_definitions_name_key"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_role_def_project_name "
        "ON role_definitions (project_id, name) WHERE project_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_role_def_platform_name "
        "ON role_definitions (name) WHERE project_id IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_role_def_platform_name")
    op.execute("DROP INDEX IF EXISTS uq_role_def_project_name")
    op.execute(
        "ALTER TABLE role_definitions "
        "ADD CONSTRAINT role_definitions_name_key UNIQUE (name)"
    )

    op.execute("DROP INDEX IF EXISTS uq_role_binding_platform_scoped")
    op.execute("DROP INDEX IF EXISTS uq_role_binding_project_scoped")
    op.execute(
        "ALTER TABLE role_bindings ADD CONSTRAINT uq_role_binding "
        "UNIQUE (principal, role, resource_type, resource_id)"
    )

    op.execute("DROP INDEX IF EXISTS uq_acl_platform_scoped")
    op.execute("DROP INDEX IF EXISTS uq_acl_project_scoped")
    op.execute(
        "ALTER TABLE acl_entries ADD CONSTRAINT uq_acl_principal_resource_action "
        "UNIQUE (principal, resource_type, resource_id, action)"
    )

    op.execute(
        "ALTER TABLE resource_registry "
        "DROP CONSTRAINT IF EXISTS uq_resource_project_type_id"
    )
    op.execute(
        "ALTER TABLE resource_registry ADD CONSTRAINT uq_resource_type_id "
        "UNIQUE (resource_type, resource_id)"
    )
