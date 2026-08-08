"""add project_id to core tables

Revision ID: 2b3c4d5e6f7a
Revises: 1a2b3c4d5e6f
Create Date: 2026-08-07

Phase 5: 为核心权限表添加 project_id 列，实现项目级数据隔离。
- acl_entries, role_bindings, restrictions, resource_registry: NOT NULL, default 'rag-v14'
- role_definitions: NULLABLE (平台级角色不绑项目)
- 回填已有数据的 project_id = 'rag-v14'
- 新增 (project_id, ...) 复合索引

设计依据：权限平台项目级隔离系统性优化方案 Step 1。
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '2b3c4d5e6f7a'
down_revision = '1a2b3c4d5e6f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── acl_entries ──
    op.add_column(
        'acl_entries',
        sa.Column(
            'project_id', sa.String(64),
            sa.ForeignKey('projects.id', name='fk_acl_project'),
            nullable=True,  # 先允许 NULL，回填后设置 NOT NULL
            comment='所属项目 ID',
        ),
    )
    # 回填已有数据
    op.execute("UPDATE acl_entries SET project_id = 'rag-v14' WHERE project_id IS NULL")
    op.alter_column('acl_entries', 'project_id', nullable=False)
    op.create_index('idx_acl_project', 'acl_entries', ['project_id'], postgresql_where='NOT revoked')

    # ── role_bindings ──
    op.add_column(
        'role_bindings',
        sa.Column(
            'project_id', sa.String(64),
            sa.ForeignKey('projects.id', name='fk_role_binding_project'),
            nullable=True,
            comment='所属项目 ID',
        ),
    )
    op.execute("UPDATE role_bindings SET project_id = 'rag-v14' WHERE project_id IS NULL")
    op.alter_column('role_bindings', 'project_id', nullable=False)
    op.create_index('idx_role_binding_project', 'role_bindings', ['project_id'], postgresql_where='NOT revoked')

    # ── restrictions ──
    op.add_column(
        'restrictions',
        sa.Column(
            'project_id', sa.String(64),
            sa.ForeignKey('projects.id', name='fk_restriction_project'),
            nullable=True,
            comment='所属项目 ID',
        ),
    )
    op.execute("UPDATE restrictions SET project_id = 'rag-v14' WHERE project_id IS NULL")
    op.alter_column('restrictions', 'project_id', nullable=False)
    op.create_index('idx_restriction_project', 'restrictions', ['project_id'], postgresql_where='NOT removed')

    # ── resource_registry ──
    op.add_column(
        'resource_registry',
        sa.Column(
            'project_id', sa.String(64),
            sa.ForeignKey('projects.id', name='fk_resource_project'),
            nullable=True,
            comment='所属项目 ID',
        ),
    )
    op.execute("UPDATE resource_registry SET project_id = 'rag-v14' WHERE project_id IS NULL")
    op.alter_column('resource_registry', 'project_id', nullable=False)
    op.create_index('idx_resource_project', 'resource_registry', ['project_id'], postgresql_where='NOT retired')

    # ── role_definitions ──
    op.add_column(
        'role_definitions',
        sa.Column(
            'project_id', sa.String(64),
            sa.ForeignKey('projects.id', name='fk_role_def_project'),
            nullable=True,  # NULL = 平台级角色，所有项目共享
            comment='所属项目 ID（NULL=平台级角色）',
        ),
    )
    op.create_index('idx_role_def_project', 'role_definitions', ['project_id'])


def downgrade() -> None:
    # ── role_definitions ──
    op.drop_index('idx_role_def_project', table_name='role_definitions')
    op.drop_constraint('fk_role_def_project', 'role_definitions', type_='foreignkey')
    op.drop_column('role_definitions', 'project_id')

    # ── resource_registry ──
    op.drop_index('idx_resource_project', table_name='resource_registry', postgresql_where='NOT retired')
    op.drop_constraint('fk_resource_project', 'resource_registry', type_='foreignkey')
    op.drop_column('resource_registry', 'project_id')

    # ── restrictions ──
    op.drop_index('idx_restriction_project', table_name='restrictions', postgresql_where='NOT removed')
    op.drop_constraint('fk_restriction_project', 'restrictions', type_='foreignkey')
    op.drop_column('restrictions', 'project_id')

    # ── role_bindings ──
    op.drop_index('idx_role_binding_project', table_name='role_bindings', postgresql_where='NOT revoked')
    op.drop_constraint('fk_role_binding_project', 'role_bindings', type_='foreignkey')
    op.drop_column('role_bindings', 'project_id')

    # ── acl_entries ──
    op.drop_index('idx_acl_project', table_name='acl_entries', postgresql_where='NOT revoked')
    op.drop_constraint('fk_acl_project', 'acl_entries', type_='foreignkey')
    op.drop_column('acl_entries', 'project_id')
