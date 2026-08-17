"""add project tables

Revision ID: ee7f3c8d5a1b
Revises: dddcf4c5164c
Create Date: 2026-08-07

新增 projects, project_clients, project_api_keys, project_audiences 四张表。
替代硬编码的 ALLOWED_CLIENTS, _ALLOWED_AUDIENCES, SERVICE_API_KEY 注册表。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'ee7f3c8d5a1b'
down_revision = 'dddcf4c5164c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated ###
    op.create_table(
        'projects',
        sa.Column('id', sa.String(64), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), server_default='', nullable=True),
        sa.Column('status', sa.String(32), server_default='active', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'project_clients',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('project_id', sa.String(64), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column('client_id', sa.String(128), nullable=False),
        sa.Column('description', sa.Text(), server_default='', nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_id', 'client_id', name='uq_project_client'),
    )

    op.create_table(
        'project_api_keys',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('project_id', sa.String(64), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column('key_hash', sa.String(128), nullable=False),
        sa.Column('key_prefix', sa.String(16), nullable=False),
        sa.Column('description', sa.Text(), server_default='', nullable=True),
        sa.Column('revoked', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'project_audiences',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('project_id', sa.String(64), sa.ForeignKey('projects.id'), nullable=False),
        sa.Column('audience', sa.String(128), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_id', 'audience', name='uq_project_audience'),
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated ###
    op.drop_table('project_audiences')
    op.drop_table('project_api_keys')
    op.drop_table('project_clients')
    op.drop_table('projects')
    # ### end Alembic commands ###
