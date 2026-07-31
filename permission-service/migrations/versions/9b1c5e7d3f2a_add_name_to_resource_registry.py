"""add name column to resource_registry

Revision ID: 9b1c5e7d3f2a
Revises: 36b35f67fea5
Create Date: 2026-07-31

设计依据：docs/外部系统设计.md §2.3.1 resource_registry 表。
管理台资源管理页需要显示资源名称（KB 名称 / 文档文件名），
此前 resource_registry 只有 resource_id（UUID），不利于运维。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9b1c5e7d3f2a'
down_revision: Union[str, None] = '36b35f67fea5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'resource_registry',
        sa.Column(
            'name',
            sa.String(512),
            nullable=True,
            comment='资源名称（KB 名称 / 文档文件名），由 RAG 系统在 register 时提供',
        ),
    )


def downgrade() -> None:
    op.drop_column('resource_registry', 'name')
