"""seed RAG super admin binding

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-08-23

补齐 RAG 超管授予：user:admin → platform_admin_role（NULL/NULL = 全部资源）。

背景：seed a1b2c3d4e5f6 只授了平台级权限（platform_admin 角色 + platform ACL），
而 RAG 的 Cerbos 策略（kb.yaml / document.yaml）认的超管角色是 platform_admin_role
（授 kb:read/kb:write/kb:grant/doc:* 等）。RAG 判定时 principal 为 user:admin（
SSO 经 preferred_username 归一），此绑定使其获得全部 RAG 资源动作。

幂等：role_bindings 无唯一约束，用 NOT EXISTS 防重插。
"""

from alembic import op
import sqlalchemy as sa
from datetime import datetime, timezone
import uuid

revision = 'd5e6f7a8b9c0'
down_revision = 'c4d5e6f7a8b9'  # head at seed time
branch_labels = None
depends_on = None


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def upgrade() -> None:
    conn = op.get_bind()
    now = _now_iso()

    conn.execute(
        sa.text("""
            INSERT INTO role_bindings (id, project_id, tenant_id, principal, role, granted_by, granted_at, revoked)
            SELECT :id, NULL, 'tenant-dev', 'user:admin', 'platform_admin_role', 'system:seed', :now, false
            WHERE NOT EXISTS (
                SELECT 1 FROM role_bindings
                WHERE tenant_id = 'tenant-dev'
                  AND principal = 'user:admin'
                  AND role = 'platform_admin_role'
                  AND resource_type IS NULL
                  AND resource_id IS NULL
                  AND NOT revoked
            )
        """),
        {
            "id": str(uuid.uuid4()),
            "now": now,
        },
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "DELETE FROM role_bindings "
            "WHERE principal = 'user:admin' AND role = 'platform_admin_role' AND granted_by = 'system:seed'"
        )
    )
