"""fix kb_* roles project scope

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-08

Phase: 数据清理 — kb_admin/kb_writer/kb_reader 是 RAG v14 项目 Cerbos 策略中的
派生角色，不应作为全局内置角色（project_id=NULL）。
将它们归属到 project_id='rag-v14'。

真正的内置角色仅 3 个：
  - system_admin  (Keycloak realm role)
  - user          (Keycloak realm role)
  - admin         (Cerbos 超级管理员派生角色，parent=system_admin)
"""

from alembic import op
import sqlalchemy as sa

revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 将 RAG 项目的 kb_* 派生角色归属到 rag-v14 项目
    conn.execute(
        sa.text("""
            UPDATE role_definitions
            SET project_id = 'rag-v14'
            WHERE name IN ('kb_admin', 'kb_writer', 'kb_reader')
              AND project_id IS NULL
        """)
    )


def downgrade() -> None:
    conn = op.get_bind()

    # 回退：恢复为全局角色
    conn.execute(
        sa.text("""
            UPDATE role_definitions
            SET project_id = NULL
            WHERE name IN ('kb_admin', 'kb_writer', 'kb_reader')
              AND project_id = 'rag-v14'
        """)
    )
