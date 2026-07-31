"""add_global_permission_version_sequence

Revision ID: 8c8a369d3fea
Revises: ff26c6d76167
Create Date: 2026-07-30 19:58:32.904192

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8c8a369d3fea'
down_revision: Union[str, Sequence[str], None] = 'ff26c6d76167'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: create global_permission_version sequence.

    设计依据：docs/外部系统设计.md §2.3.2 全局版本号。
    用于 prefilter 的 policy_version 和 visibility 的 version 单调递增。
    """
    op.execute("CREATE SEQUENCE IF NOT EXISTS global_permission_version START 1")


def downgrade() -> None:
    """Downgrade schema: drop global_permission_version sequence."""
    op.execute("DROP SEQUENCE IF EXISTS global_permission_version")
