"""清理遗留角色定义的作用域与描述（去 RAG 耦合）

Revision ID: c4d5e6f7a8b9
Revises: b8c9d0e1f2a3
Create Date: 2026-08-18


背景
----
role_definitions 是"角色管理"页的档案索引，权威在 Cerbos 策略文件。早期单项目
（RAG）时代手工种下的三条记录把 RAG 专属概念带进了平台层：

1. `admin`（project_id IS NULL）
   - 旧模型里 admin 是 RAG 的超级管理员派生角色，后更名为 platform_admin_role
     （见 permission_model_v2 §9）；现在 admin 仅作为 rag-v14 中
     platform_admin_role 的 parentRoles 保留，兼容存量绑定。
   - 描述"可创建KB、授权、删除KB、管理租户"是 RAG 专属文案，且 role_definitions
     里的归属却标成平台级（NULL）——于是 demo2 / demo3 等非 RAG 项目的角色列表
     也出现"可创建KB"的超级管理员卡片。
   - 存量 admin 绑定只有 rag-v14 一处（见 role_bindings），策略引用也只在
     rag-v14。归位到 rag-v14，与 b2c3d4e5f6a7 把 kb_* 归位同一口径。

2. `user` / `system_admin`（身份角色，平台级归属正确，保留 NULL）
   - 描述分别写死"可继承 kb_reader/kb_writer/kb_admin"与"可继承 admin"，
     把 RAG 角色名写进全局身份角色的档案里；且"可继承"语义也过时——
     身份角色是派生角色的入场资格（parentRoles），不是权限继承。
   - 描述改为项目无关、语义正确的文案（对齐 docs/keycloak-realm-setup.md）。

只改档案数据，不碰策略文件、不碰绑定、不碰判定逻辑。
"""

from alembic import op
import sqlalchemy as sa

revision = 'c4d5e6f7a8b9'
down_revision = 'b8c9d0e1f2a3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. admin：历史超管派生角色归位到其唯一归属项目 rag-v14，
    #    描述改为 rag-v14 准确文案并标明历史属性，避免再被当成平台级角色。
    conn.execute(
        sa.text("""
            UPDATE role_definitions
            SET project_id = 'rag-v14',
                description = '超级管理员（历史角色名）— 可创建 KB、授权、删除 KB；'
                              '兼容存量绑定，新绑定请用 platform_admin_role'
            WHERE name = 'admin' AND project_id IS NULL
        """)
    )

    # 2. user：全局身份角色，描述去 RAG 化、语义对齐"入场资格"而非"继承"。
    conn.execute(
        sa.text("""
            UPDATE role_definitions
            SET description = '普通用户身份角色（Keycloak realm role）— 默认身份角色，'
                              '作为项目派生角色的入场资格'
            WHERE name = 'user' AND project_id IS NULL
        """)
    )

    # 3. system_admin：全局身份角色，描述去 RAG 化（不再提历史角色名 admin）。
    conn.execute(
        sa.text("""
            UPDATE role_definitions
            SET description = '超级管理员身份角色（Keycloak realm role）— 平台全量权限，'
                              '可激活项目级超管派生角色'
            WHERE name = 'system_admin' AND project_id IS NULL
        """)
    )


def downgrade() -> None:
    conn = op.get_bind()

    # 回退仅还原描述与作用域；admin 的绑定/策略引用不受影响。
    conn.execute(
        sa.text("""
            UPDATE role_definitions
            SET project_id = NULL,
                description = '超级管理员：可创建KB、授权、删除KB、管理租户'
            WHERE name = 'admin' AND project_id = 'rag-v14'
        """)
    )
    conn.execute(
        sa.text("""
            UPDATE role_definitions
            SET description = '普通用户身份角色（Keycloak realm role）— 可继承 '
                              'kb_reader/kb_writer/kb_admin'
            WHERE name = 'user' AND project_id IS NULL
        """)
    )
    conn.execute(
        sa.text("""
            UPDATE role_definitions
            SET description = '超级管理员身份角色（Keycloak realm role）— 可继承 admin，'
                              '拥有全部权限'
            WHERE name = 'system_admin' AND project_id IS NULL
        """)
    )
