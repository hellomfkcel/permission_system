"""permission model v2 — 单一数据来源

Revision ID: f1a2b3c4d5e6
Revises: d4f1a7c9b2e3
Create Date: 2026-08-17

设计依据：docs/permission_model_v2.md

设计底线：策略文件描述"结构"，DB 记录"事实"，Cerbos 做"决策"。
本迁移把 DB 侧残留的"结构"数据清掉，使权限映射只有策略文件一个出处：

1. 删除 role_definitions.permissions
   角色能执行哪些动作是策略结构，由 Cerbos YAML 定义。此列是同一事实的第二
   份副本，改策略不改列（或反之）就会产生管理台与判定链路不一致。
   删除后 /definitions 的 permissions 字段实时从策略索引解析得出。

2. 平台角色名与策略对齐
   platform.yaml 现在按 system_admin / platform_admin / platform_viewer /
   platform_auditor 四个角色名授权。清理历史上名称脱节的记录：
   - platform_* 角色的 parent_keycloak_roles 统一为 ["user"]（入场资格）；
   - 删除策略中已不存在的 platform_admin_role / platform_viewer_role /
     platform_granted 三个旧派生角色的 role_definitions 记录。

注意：本迁移不新建 ACL 表。文档实例级 ACL 复用既有 acl_entries
（principal / resource_type / resource_id / action 已完整表达"哪个资源允许谁
做什么"），由 services/acl_resolver.get_resource_acl() 读出后注入 Cerbos 的
resource.attr.acl / role_acl。新建一张 resource_acl 会让同一事实存在于两处，
与本次重构的目标相反。
"""

from alembic import op
import sqlalchemy as sa

revision = 'f1a2b3c4d5e6'
down_revision = 'd4f1a7c9b2e3'
branch_labels = None
depends_on = None


# 策略重构后不再存在的旧派生角色名
_RETIRED_POLICY_ROLES = (
    'platform_admin_role',
    'platform_viewer_role',
    'platform_granted',
)


def upgrade() -> None:
    conn = op.get_bind()

    # ── 1. 删除权限列（策略文件为唯一来源）──
    op.execute("ALTER TABLE role_definitions DROP COLUMN IF EXISTS permissions")

    # ── 2. 清理策略中已不存在的角色定义 ──
    # 这些名字来自旧 platform_roles.yaml；该文件已随平台层重构删除。
    # 有活跃绑定的记录保留，避免绑定指向不存在的角色定义造成孤儿数据，
    # 由管理员在管理台显式迁移。
    conn.execute(
        sa.text("""
            DELETE FROM role_definitions rd
            WHERE rd.name = ANY(:names)
              AND NOT EXISTS (
                  SELECT 1 FROM role_bindings rb
                  WHERE rb.role = rd.name AND rb.revoked = false
              )
        """),
        {"names": list(_RETIRED_POLICY_ROLES)},
    )

    # ── 3. 平台角色的 parentRoles 语义还原为"入场资格" ──
    # platform_admin 此前记为 parent=["system_admin"]，读起来像"继承 system_admin
    # 的权限"；实际它是独立的 Keycloak / 平台绑定角色，与 system_admin 平级。
    conn.execute(
        sa.text("""
            UPDATE role_definitions
            SET parent_keycloak_roles = '["user"]'::jsonb
            WHERE project_id IS NULL
              AND name IN ('platform_admin', 'platform_viewer', 'platform_auditor')
        """)
    )

    # ── 4. 描述补齐，说明各平台角色的实际覆盖范围（与 platform.yaml 一致）──
    for name, description in (
        ('platform_admin', '平台管理员 — 全部平台功能模块读写（platform.yaml）'),
        ('platform_viewer', '平台查看者 — 全部平台功能模块只读（platform.yaml）'),
        ('platform_auditor',
         '平台审计员 — 仅审计日志 / 策略模拟 / 概览只读（platform.yaml）'),
    ):
        conn.execute(
            sa.text("""
                UPDATE role_definitions SET description = :description
                WHERE project_id IS NULL AND name = :name
            """),
            {"name": name, "description": description},
        )


def downgrade() -> None:
    # permissions 列可以重建，但列中的数据无法还原：它原本就是策略文件的副本。
    # 重建为空列后由 /api/v1/policies 的角色同步逻辑回填。
    op.execute(
        "ALTER TABLE role_definitions "
        "ADD COLUMN IF NOT EXISTS permissions JSONB NOT NULL DEFAULT '[]'::jsonb"
    )
