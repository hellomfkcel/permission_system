"""seed platform permissions

Revision ID: a1b2c3d4e5f6
Revises: 2b3c4d5e6f7a
Create Date: 2026-08-07

Phase 1b: 种子数据 — 平台级角色定义 + admin 用户的平台权限 ACL + 角色绑定。
"""

from alembic import op
import sqlalchemy as sa
from datetime import datetime, timezone
import uuid

revision = 'a1b2c3d4e5f6'
down_revision = '2b3c4d5e6f7a'
branch_labels = None
depends_on = None


# ── 平台资源 ID 列表（与 app/role_actions_config.py PLATFORM_RESOURCES 一致）──
_PLATFORM_RESOURCE_IDS = [
    "dashboard", "project_mgmt", "tenant_mgmt", "resource_mgmt",
    "user_mgmt", "role_mgmt", "permission_mgmt", "restriction_mgmt",
    "policy_mgmt", "audit_mgmt", "playground", "settings",
]

# ── 12 个平台功能对应的全部 platform:read + platform:write 权限 ──
_ALL_KBDOC_ACTIONS = [
    "kb:read", "kb:write", "kb:manage", "kb:grant",
    "doc:view", "doc:download", "doc:retrieve",
    "doc:unmount", "doc:purge", "doc:share",
]


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def upgrade() -> None:
    conn = op.get_bind()

    # dddcf4c5164c 创建 role_definitions 时未包含 permissions 列，
    # 而下面的种子数据要写入它。此处补建，使空库可以完整执行迁移链。
    op.execute(
        "ALTER TABLE role_definitions "
        "ADD COLUMN IF NOT EXISTS permissions JSONB NOT NULL DEFAULT '[]'::jsonb"
    )

    # 平台级授权（platform 资源的 ACL、平台角色绑定）不属于任何项目，
    # 以 project_id IS NULL 表示，与 role_definitions 的平台级语义一致。
    # 2b3c4d5e6f7a 把这两列设为 NOT NULL，与此处的种子数据冲突，故放开。
    op.execute("ALTER TABLE acl_entries ALTER COLUMN project_id DROP NOT NULL")
    op.execute("ALTER TABLE role_bindings ALTER COLUMN project_id DROP NOT NULL")

    # ══════════════════════════════════════════════════════════
    # 1. 插入平台级角色定义（project_id=NULL）
    # ══════════════════════════════════════════════════════════
    now = _now_iso()

    # platform_admin — 拥有所有平台功能的完全访问权限
    conn.execute(
        sa.text("""
            INSERT INTO role_definitions (id, project_id, name, description, parent_keycloak_roles, is_system, permissions, created_at, updated_at)
            VALUES (:id, NULL, 'platform_admin', '平台管理员 — 拥有所有平台管理功能的完全访问权限',
                    '["system_admin"]'\:\:jsonb, true,
                    :perms\:\:jsonb, :now, :now)
            ON CONFLICT (name) DO NOTHING
        """),
        {
            "id": str(uuid.uuid4()),
            "perms": '["platform:write"]',
            "now": now,
        },
    )

    # platform_viewer — 可以查看所有平台管理功能但不可修改
    conn.execute(
        sa.text("""
            INSERT INTO role_definitions (id, project_id, name, description, parent_keycloak_roles, is_system, permissions, created_at, updated_at)
            VALUES (:id, NULL, 'platform_viewer', '平台查看者 — 可以查看所有平台管理功能但不可修改',
                    '["user"]'\:\:jsonb, true,
                    :perms\:\:jsonb, :now, :now)
            ON CONFLICT (name) DO NOTHING
        """),
        {
            "id": str(uuid.uuid4()),
            "perms": '["platform:read"]',
            "now": now,
        },
    )

    # platform_auditor — 只能查看审计日志和策略模拟
    conn.execute(
        sa.text("""
            INSERT INTO role_definitions (id, project_id, name, description, parent_keycloak_roles, is_system, permissions, created_at, updated_at)
            VALUES (:id, NULL, 'platform_auditor', '平台审计员 — 可以查看审计日志和策略模拟',
                    '["user"]'\:\:jsonb, true,
                    :perms\:\:jsonb, :now, :now)
            ON CONFLICT (name) DO NOTHING
        """),
        {
            "id": str(uuid.uuid4()),
            "perms": '["platform:read"]',
            "now": now,
        },
    )

    # ══════════════════════════════════════════════════════════
    # 2. 为 admin 用户授予所有平台资源的 platform:write 权限
    #    （platform:write 隐含 platform:read）
    # ══════════════════════════════════════════════════════════
    for res_id in _PLATFORM_RESOURCE_IDS:
        conn.execute(
            sa.text("""
                INSERT INTO acl_entries (id, project_id, tenant_id, principal, resource_type, resource_id, action, granted_by, granted_at, revoked)
                VALUES (:id, NULL, 'tenant-dev', 'user:admin', 'platform', :res_id, 'platform:write', 'system:seed', :now, false)
                ON CONFLICT (principal, resource_type, resource_id, action) DO NOTHING
            """),
            {
                "id": str(uuid.uuid4()),
                "res_id": res_id,
                "now": now,
            },
        )

    # ══════════════════════════════════════════════════════════
    # 3. 为 admin 用户绑定 platform_admin 角色
    # ══════════════════════════════════════════════════════════
    conn.execute(
        sa.text("""
            INSERT INTO role_bindings (id, project_id, tenant_id, principal, role, granted_by, granted_at, revoked)
            VALUES (:id, NULL, 'tenant-dev', 'user:admin', 'platform_admin', 'system:seed', :now, false)
            ON CONFLICT DO NOTHING
        """),
        {
            "id": str(uuid.uuid4()),
            "now": now,
        },
    )

    # ══════════════════════════════════════════════════════════
    # 4. 确保 admin 用户在 project_members 中
    # ══════════════════════════════════════════════════════════
    # 仅当项目已存在时插入（project_members.project_id 有外键约束）。
    # 空库迁移时 projects 表为空，此步跳过；首启动引导会补建成员关系。
    conn.execute(
        sa.text("""
            INSERT INTO project_members (id, project_id, user_id, role, granted_by, created_at)
            SELECT :id, p.id, 'admin', 'project_admin', 'system:seed', :now
            FROM projects p WHERE p.id = 'rag-v14'
            ON CONFLICT (project_id, user_id) DO NOTHING
        """),
        {
            "id": str(uuid.uuid4()),
            "now": now,
        },
    )


def downgrade() -> None:
    conn = op.get_bind()

    # 清理种子数据（按插入的逆序）
    conn.execute(
        sa.text("DELETE FROM project_members WHERE granted_by = 'system:seed'")
    )
    conn.execute(
        sa.text("DELETE FROM role_bindings WHERE principal = 'user:admin' AND role = 'platform_admin' AND granted_by = 'system:seed'")
    )
    conn.execute(
        sa.text("DELETE FROM acl_entries WHERE principal = 'user:admin' AND resource_type = 'platform' AND granted_by = 'system:seed'")
    )
    conn.execute(
        sa.text("DELETE FROM role_definitions WHERE name IN ('platform_admin', 'platform_viewer', 'platform_auditor') AND project_id IS NULL")
    )
