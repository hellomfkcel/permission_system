# 角色管理模块设计 — 诊断与方案

> **诊断日期**：2026-08-01
> **问题来源**：没有角色管理模块——不知道系统有哪些角色、角色有哪些权限、无法创建删除角色、无法更改角色权限。
> **设计依据**：`docs/RAG系统设计v14.md`、`docs/权限管理系统架构设计.md`、`docs/外部系统设计.md`

---

## 一、现状诊断

### 1.1 角色定义分布在三个地方

```
┌─────────────────────────────────────────────────────────────────┐
│                     角色体系全景（当前 — 无统一管理）               │
│                                                                 │
│  ┌──────────────────────┐                                       │
│  │ Keycloak             │  ← user, system_admin 定义在这里       │
│  │ (IdP 管理台)          │    管理员需登录 Keycloak 才能查看/编辑   │
│  │                      │    非技术人员根本找不到                  │
│  └──────────┬───────────┘                                       │
│             │ JWT roles: ["user"]                               │
│             ▼                                                   │
│  ┌──────────────────────┐                                       │
│  │ Cerbos 策略 YAML 文件  │  ← kb_reader/writer/admin 定义在这里   │
│  │ (文件系统)             │    + 角色→权限的映射规则               │
│  │                      │    需要编辑 YAML，极易出错               │
│  └──────────┬───────────┘                                       │
│             │                                                   │
│             ▼                                                   │
│  ┌──────────────────────┐                                       │
│  │ 权限服务              │  ← role_bindings: 谁有什么角色           │
│  │ role_bindings 表     │    有 bind/unbind API                 │
│  │                      │    但没有角色定义 API                   │
│  └──────────────────────┘                                       │
│                                                                 │
│  ★ 三个地方各管一段，没有一个统一视图能看到"系统有哪些角色、       │
│    每个角色有什么权限"                                           │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 逐层诊断

#### 1.2.1 Keycloak Realm Roles（`user` / `system_admin` 的定义）

**定义位置**：Keycloak 管理台 `http://192.168.1.127:8080` → rag-v14 realm → Realm roles

| 角色 | 类型 | 描述 | 谁能看到 |
|------|------|------|---------|
| `system_admin` | 非 composite | Super administrator | 需登录 Keycloak 管理台 |
| `user` | 非 composite | Default user role | 需登录 Keycloak 管理台 |
| `default-roles-rag-v14` | composite | 默认角色 | 需登录 Keycloak 管理台 |

**问题**：
- 非技术人员根本找不到这些角色定义在哪里
- 没有任何界面展示"`system_admin` 能做什么、`user` 能做什么"
- 角色的权限（能执行哪些 action）不在 Keycloak 中，在 Cerbos 策略中

#### 1.2.2 Cerbos 派生角色（`kb_reader` / `kb_writer` / `kb_admin` / `admin`）

**定义位置**：`cerbos/policies/derived_roles/rag_roles.yaml`

```yaml
- name: kb_reader    → parentRoles: ["user"]      → 条件: granted_actions 含 "read"
- name: kb_writer    → parentRoles: ["user"]      → 条件: granted_actions 含 "write"
- name: kb_admin     → parentRoles: ["user"]      → 条件: granted_actions 含 "manage"
- name: admin        → parentRoles: ["system_admin"] → 条件: 无条件 (true)
```

**问题**：只能通过编辑 YAML 文件来修改。策略管理页面（`/policies`）提供了 YAML 查看/编辑功能，但：

- 编辑 YAML 极易出错（缩进、语法）
- 没有角色-权限矩阵视图
- 修改后需手动推送到 Cerbos PDP

#### 1.2.3 角色→权限 映射（Cerbos Resource Policies）

**定义位置**：`cerbos/policies/resource_policies/kb.yaml` + `document.yaml`

整理后的完整权限矩阵：

| Action | kb_reader | kb_writer | kb_admin | admin | 条件 |
|--------|-----------|-----------|----------|-------|------|
| `kb:read` | ✅ | ✅ | ✅ | ✅ | retired=false |
| `kb:write` | — | ✅ | ✅ | ✅ | retired=false |
| `kb:manage` | — | — | ✅ | ✅ | — |
| `kb:grant` | — | — | — | ✅ | — |
| `doc:view` | ✅ | ✅ | ✅ | ✅ | is_enabled + not retired |
| `doc:download` | ✅ | ✅ | ✅ | ✅ | is_enabled + not retired + allow_download |
| `doc:retrieve` | ✅ | ✅ | ✅ | ✅ | is_enabled + not retired |
| `doc:unmount` | — | ✅ | ✅ | ✅ | not retired |
| `doc:purge` | — | — | ✅ | ✅ | not retired |
| `doc:share` | — | — | — | ✅ | — |

**问题**：
- 这个矩阵在代码中是隐式的（分散在两个 YAML 文件中），没有任何界面能直接看到
- 用户必须自己阅读 YAML 才能理解角色权限

#### 1.2.4 角色绑定（谁有什么角色）

**定义位置**：权限服务 `role_bindings` 表

现有 API：
- `POST /api/v1/roles/bind` — 绑定角色
- `POST /api/v1/roles/unbind` — 解绑角色
- `GET /api/v1/roles/bindings` — 查询绑定列表

**问题**：
- 只能做绑定操作，不能定义角色本身
- `role` 字段是自由文本（`kb_reader`/`kb_writer` 等），没有校验是否合法

### 1.3 根因总结

> **角色定义分散在 Keycloak + Cerbos YAML + 权限服务三个地方，没有统一的角色定义权威源。** 角色-权限映射只存在于 Cerbos YAML 中，普通用户无法理解。

---

## 二、架构决策：外部系统 or 内部模块？

### 2.1 与租户管理的类比

租户管理设计为权限服务的新增模块（`docs/tenant_design.md`），理由：
- 权限服务已是所有租户级数据的权威源
- 管理台是权限管理界面
- RAG 系统只消费、不管理

**角色管理同理**：

| 维度 | 外部系统（权限服务 + 管理台） | 内部模块（RAG 系统） |
|------|---------------------------|---------------------|
| 角色定义权威源 | 权限服务新增 `role_definitions` 表 | RAG 系统管理角色 |
| 权限映射 | 权限服务读取 Cerbos 策略，提供只读视图 | — |
| 角色绑定 | 权限服务已有 `role_bindings` 表 | — |
| 用户界面 | 管理台新增角色管理页 | RAG 前端 |
| 依赖方向 | 符合 §0.2.1 | 反向依赖 |

### 2.2 结论：设计为外部系统（权限服务内新增模块）

**角色管理应作为外部权限管理系统的新增模块**，新增：
1. 权限服务：`role_definitions` 表 + 角色 CRUD API + 权限矩阵只读 API
2. 管理台：角色管理页面（角色列表 + 角色详情/权限矩阵 + 绑定管理）

**Keycloak 中的 `user`/`system_admin` 保持不变**——它们是身份层角色（"你是谁"），Cerbos 派生角色是权限层角色（"你能做什么"）。

```
┌─────────────────────────────────────────────────────────────────┐
│                     角色管理体系（修改后）                         │
│                                                                 │
│  Keycloak（不变）             权限服务（新增）                     │
│  ┌──────────────────┐       ┌──────────────────────────────┐   │
│  │ Realm Roles:      │       │ role_definitions 表           │   │
│  │  user             │       │  name, description,          │   │
│  │  system_admin     │       │  parent_keycloak_roles,      │   │
│  │                   │       │  is_system (不可删除)         │   │
│  └──────────────────┘       ├──────────────────────────────┤   │
│                              │ role_permissions 视图 (只读)   │   │
│  Cerbos（不变）              │  从 Cerbos 策略解析            │   │
│  ┌──────────────────┐       │  role → [actions]             │   │
│  │ 派生角色 YAML:     │       ├──────────────────────────────┤   │
│  │  kb_reader        │       │ role_bindings 表 (已有)       │   │
│  │  kb_writer        │       │  principal → role             │   │
│  │  kb_admin         │       └──────────────────────────────┘   │
│  │  admin            │                                          │
│  │                   │       管理台（新增页面）                    │
│  │ 资源策略 YAML:     │       ┌──────────────────────────────┐   │
│  │  role → actions   │       │ /roles 页面                   │   │
│  └──────────────────┘       │  角色列表 + 创建/删除          │   │
│                              │  角色详情: 权限矩阵表格        │   │
│                              │  绑定管理: 谁有这个角色        │   │
│                              └──────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 三、详细设计

### 3.1 数据模型

```sql
-- ═══════════════════════════════════════════════════════
-- 角色定义表（权限服务权威）
-- ═══════════════════════════════════════════════════════
CREATE TABLE role_definitions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(64) NOT NULL UNIQUE,   -- kb_reader, kb_writer, admin 等
    description VARCHAR(512) NOT NULL DEFAULT '',
    parent_keycloak_roles JSONB NOT NULL DEFAULT '[]',  -- ["user"] 或 ["system_admin"]
    is_system   BOOLEAN NOT NULL DEFAULT false,  -- 系统内置角色，不可删除
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 预置种子数据：
INSERT INTO role_definitions (name, description, parent_keycloak_roles, is_system) VALUES
  ('kb_reader',  '知识库只读权限：可查看KB列表、查看文档、检索文档', '["user"]', true),
  ('kb_writer',  '知识库写入权限：可上传文档、解析文档、停用/启用文档', '["user"]', true),
  ('kb_admin',   '知识库管理权限：可删除文档、管理KB配置', '["user"]', true),
  ('admin',      '超级管理员：可创建KB、授权、删除KB、管理租户', '["system_admin"]', true);
```

### 3.2 API 设计

```
GET    /api/v1/roles/definitions         → 角色列表（含 description）
GET    /api/v1/roles/definitions/{name}  → 单个角色详情 + 权限矩阵
POST   /api/v1/roles/definitions         → 创建自定义角色（需 system_admin）
DELETE  /api/v1/roles/definitions/{name} → 删除自定义角色（is_system=false 才可删）

GET    /api/v1/roles/permissions         → 完整的角色-权限矩阵（只读，从 Cerbos 策略解析）

已有的 bind/unbind/bindings 端点保持不变
```

### 3.3 角色-权限矩阵 API 返回格式

```json
GET /api/v1/roles/permissions
{
  "roles": [
    {
      "name": "kb_reader",
      "description": "知识库只读权限",
      "parent_keycloak_roles": ["user"],
      "is_system": true,
      "permissions": ["kb:read", "doc:view", "doc:download", "doc:retrieve"]
    },
    {
      "name": "kb_writer",
      "permissions": ["kb:read", "kb:write", "doc:view", "doc:download", "doc:retrieve", "doc:unmount"]
    },
    {
      "name": "kb_admin",
      "permissions": ["kb:read", "kb:write", "kb:manage", "doc:view", "doc:download", "doc:retrieve", "doc:unmount", "doc:purge"]
    },
    {
      "name": "admin",
      "permissions": ["kb:read", "kb:write", "kb:manage", "kb:grant", "doc:view", "doc:download", "doc:retrieve", "doc:unmount", "doc:purge", "doc:share"]
    }
  ]
}
```

### 3.4 管理台页面

```
/roles 页面:
┌────────────────────────────────────────────────────────────┐
│  🔑 角色管理                                                │
│                                                            │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ 角色          父角色         权限数   类型    操作     │  │
│  ├──────────────────────────────────────────────────────┤  │
│  │ kb_reader     user           4      系统    查看     │  │
│  │ kb_writer     user           6      系统    查看     │  │
│  │ kb_admin      user           8      系统    查看     │  │
│  │ admin         system_admin   10     系统    查看     │  │
│  │ custom_role   user           2      自定义  查看 删除│  │
│  └──────────────────────────────────────────────────────┘  │
│                                                            │
│  [+ 创建角色]                                               │
└────────────────────────────────────────────────────────────┘

点击角色行 → /roles/{name} 详情页:
┌────────────────────────────────────────────────────────────┐
│  kb_reader — 知识库只读权限                        [系统内置] │
│                                                            │
│  权限矩阵:                                                  │
│  ┌────────────┬────┬────┬────┬────┐                       │
│  │ Action     │ r  │ w  │ adm│ sys│                       │
│  ├────────────┼────┼────┼────┼────┤                       │
│  │ kb:read    │ ✅ │ ✅ │ ✅ │ ✅ │                       │
│  │ kb:write   │ —  │ ✅ │ ✅ │ ✅ │                       │
│  │ kb:manage  │ —  │ —  │ ✅ │ ✅ │                       │
│  │ kb:grant   │ —  │ —  │ —  │ ✅ │                       │
│  │ doc:view   │ ✅ │ ✅ │ ✅ │ ✅ │                       │
│  │ doc:download│✅  │ ✅ │ ✅ │ ✅ │                       │
│  │ doc:retrieve│✅  │ ✅ │ ✅ │ ✅ │                       │
│  │ doc:unmount │ —  │ ✅ │ ✅ │ ✅ │                       │
│  │ doc:purge  │ —  │ —  │ ✅ │ ✅ │                       │
│  │ doc:share  │ —  │ —  │ —  │ ✅ │                       │
│  └────────────┴────┴────┴────┴────┘                       │
│                                                            │
│  已绑定用户/组:                                              │
│  user:alice (tenant-dev)     [解绑]                        │
│  group:engineering           [解绑]                        │
│  [+ 添加绑定]                                               │
└────────────────────────────────────────────────────────────┘
```

---

## 四、与现有系统的交互

### 4.1 角色定义 vs Cerbos 策略

```
role_definitions 表（权限服务）          Cerbos 策略 YAML（文件系统）
─────────────────────────────          ─────────────────────────
角色元数据（名称、描述、父角色）           角色定义（name, parentRoles, condition）
可 CRUD                                 只读（通过 YAML 编辑器）
is_system=true 的不可删                  修改后需手动推送 Cerbos

★ role_definitions 是对 Cerbos 策略的"元数据镜像"——
  描述信息存在权限服务，角色结构和权限映射以 Cerbos YAML 为准。
  role_definitions 中的权限数据从 Cerbos 策略解析而来（只读视图）。
```

### 4.2 权限矩阵的数据来源

```
GET /api/v1/roles/permissions
  → 权限服务后端读取 Cerbos 策略 YAML 文件
  → 解析 derived_roles + resource_policies
  → 构建角色-权限矩阵
  → 缓存 5 分钟（策略不频繁变更）
```

### 4.3 管理台侧边栏更新

```tsx
{ href: "/roles", label: "角色管理", icon: Shield },
// 放在 /permissions（权限管理）和 /restrictions（封禁管理）之间
```

---

## 五、实施计划

### P0 — 权限服务角色定义表 + API

| # | 功能 | 说明 |
|---|------|------|
| 1 | 创建 `role_definitions` 表 | Alembic 迁移 |
| 2 | 创建 `RoleDefinition` ORM 模型 | — |
| 3 | `GET /api/v1/roles/definitions` | 角色列表 |
| 4 | `GET /api/v1/roles/definitions/{name}` | 角色详情 |
| 5 | `POST /api/v1/roles/definitions` | 创建自定义角色 |
| 6 | `DELETE /api/v1/roles/definitions/{name}` | 删除自定义角色 |
| 7 | `GET /api/v1/roles/permissions` | 权限矩阵（从 Cerbos 策略解析） |
| 8 | 种子数据（4 个系统角色） | 数据迁移 |

### P1 — 管理台角色管理页面

| # | 功能 | 说明 |
|---|------|------|
| 9 | `/roles` 页面 | 角色列表 + 权限矩阵表格 |
| 10 | `/roles/{name}` 详情页 | 角色详情 + 绑定管理 |
| 11 | 侧边栏添加"角色管理" | — |
| 12 | 角色权限对比视图 | 矩阵表格，多角色对比 |

---

## 六、文件索引

```
新增/修改文件清单:

权限服务:
  models/role_definition.py         ← ★ 新增: RoleDefinition ORM
  api/role_definitions_routes.py    ← ★ 新增: 角色定义 CRUD + 权限矩阵
  schemas/role_requests.py          ← ★ 新增: 角色请求 schema
  schemas/role_responses.py         ← ★ 新增: 角色响应 schema
  services/cerbos_policy_parser.py  ← ★ 新增: Cerbos YAML → 权限矩阵解析
  migrations/versions/xxx_add_role_definitions.py ← ★ 新增
  app/main.py                       ← 修改: 注册 router

管理台:
  app/roles/page.tsx               ← ★ 新增: 角色列表页
  app/roles/[name]/page.tsx        ← ★ 新增: 角色详情页
  components/layout/Sidebar.tsx    ← 修改: 添加"角色管理"菜单项
```

---

> **核心结论**：角色管理应设计为**外部权限管理系统的扩展**（同租户管理一样）。权限服务新增 `role_definitions` 表作为角色元数据权威源，管理台新增角色管理页面提供角色-权限矩阵可视化。Cerbos YAML 仍为权限映射的权威源，权限服务从 YAML 解析生成只读权限矩阵视图。Keycloak 中的 `user`/`system_admin` 作为身份层角色保持不变。
