"""add project_members

Revision ID: 1a2b3c4d5e6f
Revises: ee7f3c8d5a1b
Create Date: 2026-08-07

新增 project_members 表，实现管理员用户的项目级隔离。
platform_admin 角色可管理全部项目，system_admin/admin 仅管理所属项目。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '1a2b3c4d5e6f'
down_revision = 'ee7f3c8d5a1b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'project_members',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('project_id', sa.String(64), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column('user_id', sa.String(255), nullable=False),
        sa.Column('role', sa.String(32), server_default='project_admin', nullable=False),
        sa.Column('granted_by', sa.String(255), server_default='', nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_id', 'user_id', name='uq_project_member'),
    )


def downgrade() -> None:
    op.drop_table('project_members')
