"""mount_registry 项目隔离

Revision ID: a7b8c9d0e1f2
Revises: f1a2b3c4d5e6
Create Date: 2026-08-17

设计依据：docs/permission_model_v2.md §3 数据来源边界。

迁移 d4f1a7c9b2e3 把 acl_entries / role_bindings / resource_registry /
role_definitions 的唯一性改成了项目内唯一，即**明确允许不同项目使用相同的资源 ID**，
但漏掉了 mount_registry：该表既无 project_id，唯一约束又是全局的 (doc_id, kb_id)。
后果是所有挂载查询都在跨项目匹配裸字符串：

  - /v1/link 幂等检查命中别的项目的挂载，本项目的挂载没建成
  - 全局唯一约束让第二个项目建不了同名组合
  - /v1/retire 级联 unlink 按 kb_id 匹配，退役 A 项目的 KB 会解掉 B 项目的挂载
  - /v1/visibility 读到别的项目的挂载，把本项目的文档判成 unmounted
  - prefilter 的 doc→kb 反查会吐出别的项目的 kb_id

回填策略（三轮，从最强证据到最弱）：
  1. 文档与 KB 同时注册在同一项目 → 取该项目（唯一可靠的归属证据）
  2. 只有文档能定位到唯一项目 → 取该项目
  3. 只有 KB 能定位到唯一项目 → 取该项目

三轮都无法归属的行，说明它的两端都没有在 resource_registry 注册过，
在判定链路上本就不可用。这类行移入 mount_registry_unattributed 备查后删除 ——
不静默丢数据，也不让无法归属的行阻塞 NOT NULL 约束。
"""

from alembic import op
import sqlalchemy as sa

revision = 'a7b8c9d0e1f2'
down_revision = 'f1a2b3c4d5e6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    op.add_column(
        "mount_registry",
        sa.Column("project_id", sa.String(64), nullable=True),
    )

    # ── 第 1 轮：文档与 KB 同属一个项目 ──
    conn.execute(sa.text("""
        UPDATE mount_registry m
        SET project_id = sub.project_id
        FROM (
            SELECT d.resource_id AS doc_id, k.resource_id AS kb_id, d.project_id
            FROM resource_registry d
            JOIN resource_registry k
              ON k.project_id = d.project_id
             AND k.resource_type = 'kb'
            WHERE d.resource_type = 'document'
        ) sub
        WHERE m.project_id IS NULL
          AND m.doc_id = sub.doc_id
          AND m.kb_id = sub.kb_id
    """))

    # ── 第 2 轮：仅文档可唯一定位 ──
    conn.execute(sa.text("""
        UPDATE mount_registry m
        SET project_id = sub.project_id
        FROM (
            SELECT resource_id, MIN(project_id) AS project_id
            FROM resource_registry
            WHERE resource_type = 'document'
            GROUP BY resource_id
            HAVING COUNT(DISTINCT project_id) = 1
        ) sub
        WHERE m.project_id IS NULL AND m.doc_id = sub.resource_id
    """))

    # ── 第 3 轮：仅 KB 可唯一定位 ──
    conn.execute(sa.text("""
        UPDATE mount_registry m
        SET project_id = sub.project_id
        FROM (
            SELECT resource_id, MIN(project_id) AS project_id
            FROM resource_registry
            WHERE resource_type = 'kb'
            GROUP BY resource_id
            HAVING COUNT(DISTINCT project_id) = 1
        ) sub
        WHERE m.project_id IS NULL AND m.kb_id = sub.resource_id
    """))

    # ── 无法归属的行：移入备查表后删除 ──
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS mount_registry_unattributed (
            id            UUID PRIMARY KEY,
            doc_id        VARCHAR(255) NOT NULL,
            kb_id         VARCHAR(255) NOT NULL,
            unlinked      BOOLEAN NOT NULL,
            created_at    TIMESTAMPTZ,
            updated_at    TIMESTAMPTZ,
            quarantined_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    conn.execute(sa.text("""
        INSERT INTO mount_registry_unattributed
            (id, doc_id, kb_id, unlinked, created_at, updated_at)
        SELECT id, doc_id, kb_id, unlinked, created_at, updated_at
        FROM mount_registry WHERE project_id IS NULL
        ON CONFLICT (id) DO NOTHING
    """))
    conn.execute(sa.text("DELETE FROM mount_registry WHERE project_id IS NULL"))

    # ── 约束收紧 ──
    op.alter_column("mount_registry", "project_id", nullable=False)
    op.create_foreign_key(
        "fk_mount_project", "mount_registry", "projects", ["project_id"], ["id"],
    )
    op.execute("ALTER TABLE mount_registry DROP CONSTRAINT IF EXISTS uq_doc_kb")
    op.create_unique_constraint(
        "uq_mount_project_doc_kb", "mount_registry",
        ["project_id", "doc_id", "kb_id"],
    )
    op.create_index(
        "idx_mount_project_doc", "mount_registry", ["project_id", "doc_id"],
    )
    op.create_index(
        "idx_mount_project_kb", "mount_registry", ["project_id", "kb_id"],
    )


def downgrade() -> None:
    # 回退到全局唯一：同一 (doc_id, kb_id) 若已被多个项目使用，只能保留一条，
    # 其余行会违反 uq_doc_kb。先去重再重建约束。
    conn = op.get_bind()

    op.drop_index("idx_mount_project_kb", table_name="mount_registry")
    op.drop_index("idx_mount_project_doc", table_name="mount_registry")
    op.drop_constraint("uq_mount_project_doc_kb", "mount_registry", type_="unique")
    op.drop_constraint("fk_mount_project", "mount_registry", type_="foreignkey")

    conn.execute(sa.text("""
        DELETE FROM mount_registry m
        USING mount_registry keep
        WHERE m.doc_id = keep.doc_id
          AND m.kb_id = keep.kb_id
          AND m.ctid > keep.ctid
    """))

    op.create_unique_constraint("uq_doc_kb", "mount_registry", ["doc_id", "kb_id"])
    op.drop_column("mount_registry", "project_id")
