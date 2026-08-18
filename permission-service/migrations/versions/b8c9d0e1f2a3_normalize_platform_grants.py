"""平台层授权记录归一为平台级

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-08-17


platform 是平台层资源，它的授权记录不属于任何项目（project_id IS NULL）。
迁移 c3d4e5f6a7b8 归一过一次，但此后管理台在授予平台功能权限时仍会带上当前项目
（前端对 project_id 有兜底默认值），于是又产生了挂在某个项目下的 platform 授权。
这类记录会被平台判定路径读到，等于用项目级写权限换到平台级写权限。

写入端已由 acl_routes._validate_grant_scope 按资源所在命名空间校验层级，
读取端已由 platform_authorizer 过滤 project_id IS NULL；本迁移清理存量。

不建 CHECK 约束：哪些资源类型属于平台层由 Cerbos 策略文件的目录归属决定，
把这份清单复制到数据库约束里就又造出了一处需要同步的副本。
"""

from alembic import op
import sqlalchemy as sa

revision = 'b8c9d0e1f2a3'
down_revision = 'a7b8c9d0e1f2'
branch_labels = None
depends_on = None

# 平台层资源类型 —— 与 cerbos/policies/platform/resource_policies/ 下的文件对应。
# 迁移是一次性数据订正，取值在此刻是确定的；运行时的层级判断始终查策略索引。
_PLATFORM_RESOURCE_TYPES = ("platform", "project_permission")


def upgrade() -> None:
    conn = op.get_bind()

    # 同一主体在同一功能上可能既有平台级记录、又有项目级记录，
    # 直接置 NULL 会撞唯一索引 uq_acl_platform_scoped。先删掉冗余的项目级记录。
    conn.execute(
        sa.text("""
            DELETE FROM acl_entries dup
            WHERE dup.resource_type = ANY(:types)
              AND dup.project_id IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM acl_entries keep
                  WHERE keep.resource_type = dup.resource_type
                    AND keep.resource_id = dup.resource_id
                    AND keep.principal = dup.principal
                    AND keep.action = dup.action
                    AND keep.project_id IS NULL
              )
        """),
        {"types": list(_PLATFORM_RESOURCE_TYPES)},
    )

    # 同一 (principal, resource_id, action) 在多个项目下重复登记的，只保留一条
    conn.execute(
        sa.text("""
            DELETE FROM acl_entries dup
            USING acl_entries keep
            WHERE dup.resource_type = ANY(:types)
              AND keep.resource_type = dup.resource_type
              AND dup.project_id IS NOT NULL
              AND keep.project_id IS NOT NULL
              AND keep.resource_id = dup.resource_id
              AND keep.principal = dup.principal
              AND keep.action = dup.action
              AND dup.ctid > keep.ctid
        """),
        {"types": list(_PLATFORM_RESOURCE_TYPES)},
    )

    conn.execute(
        sa.text("""
            UPDATE acl_entries
            SET project_id = NULL
            WHERE resource_type = ANY(:types) AND project_id IS NOT NULL
        """),
        {"types": list(_PLATFORM_RESOURCE_TYPES)},
    )


def downgrade() -> None:
    # 平台级是这类授权的正确形态，无需回退：把它们塞回某个项目只会重新制造缺陷。
    pass
