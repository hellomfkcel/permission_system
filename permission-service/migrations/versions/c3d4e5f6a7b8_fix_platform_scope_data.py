"""fix platform scope data

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-08

Phase: 数据清理 — platform 资源类型的 ACL 和 platform_* 角色绑定应归属到
平台级（project_id=NULL），而非 rag-v14 项目级。
"""

from alembic import op
import sqlalchemy as sa

revision = 'c3d4e5f6a7b8'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 移除 NOT NULL 约束（平台级条目 project_id=NULL）
    op.alter_column('acl_entries', 'project_id', nullable=True)
    op.alter_column('role_bindings', 'project_id', nullable=True)

    # platform 资源类型的 ACL → 平台级
    conn.execute(
        sa.text("""
            UPDATE acl_entries
            SET project_id = NULL
            WHERE resource_type = 'platform'
              AND project_id IS NOT NULL
        """)
    )

    # platform_* 角色绑定 → 平台级
    conn.execute(
        sa.text("""
            UPDATE role_bindings
            SET project_id = NULL
            WHERE role IN ('platform_admin', 'platform_viewer', 'platform_auditor')
              AND project_id IS NOT NULL
        """)
    )


def downgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text("""
            UPDATE acl_entries
            SET project_id = 'rag-v14'
            WHERE resource_type = 'platform'
              AND project_id IS NULL
        """)
    )
    conn.execute(
        sa.text("""
            UPDATE role_bindings
            SET project_id = 'rag-v14'
            WHERE role IN ('platform_admin', 'platform_viewer', 'platform_auditor')
              AND project_id IS NULL
        """)
    )

    # 恢复 NOT NULL 约束
    op.alter_column('acl_entries', 'project_id', nullable=False)
    op.alter_column('role_bindings', 'project_id', nullable=False)
