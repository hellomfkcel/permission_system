# 租户管理模块设计 — 诊断与方案

> **诊断日期**：2026-08-01
> **问题来源**：RAG 系统和权限管理平台都在使用租户（`tenant_id`），但整个系统没有管理租户的模块——租户如何创建、租户如何与用户绑定等均缺失。
> **设计依据**：`docs/RAG系统设计v14.md`、`docs/frontend-design.md`、`docs/外部系统设计.md`、`docs/权限管理系统架构设计.md`

---

## 一、现状诊断

### 1.1 租户使用全景（当前状态）

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                         当前租户使用全景（有缺陷）                               │
│                                                                              │
│  JWT 签发                                    JWT 消费                         │
│  ┌──────────────────┐                       ┌──────────────────────────┐    │
│  │ Keycloak         │  tenant claim ──────► │ RAG 系统 (FastAPI)        │    │
│  │ • 用户属性:       │                       │ • auth.py: JWT 解析       │    │
│  │   "tenant"=???   │                       │   tenant → ctx.tenant_id │    │
│  │ • 无租户管理界面   │                       │ • 所有查询 WHERE          │    │
│  │ • 无租户创建流程   │                       │   tenant_id = $1        │    │
│  └──────────────────┘                       │ • knowledge_bases 表      │    │
│                                              │   有 tenant_id 列         │    │
│  ┌──────────────────┐                       └──────────────────────────┘    │
│  │ 权限服务后端       │                                                       │
│  │ • 所有表有         │  ★ 核心问题：                                         │
│  │   tenant_id 列    │  1. 无 tenants 表 — 没有租户定义权威源                 │
│  │ • 无 tenants 表   │  2. 无租户 CRUD API                                   │
│  │ • 无租户管理 API   │  3. user_cache.tenant_id 全部为 NULL                 │
│  │ • 无用户-租户绑定   │  4. 前端硬编码 "tenant-dev"                           │
│  └──────────────────┘  5. GET /api/v1/tenants 从 knowledge_bases 反查        │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 逐层诊断

#### 1.2.1 数据库层 — 无 tenants 表

| 数据库 | 位置 | 有 tenants 表？ | tenant_id 列分布 |
|--------|------|----------------|------------------|
| **RAG 系统 PG** (`:25432`) | `proj_rag_dev` | ❌ 无 | `knowledge_bases`, `documents`, `conversations`, `audit_logs`, `document_kb_mounts` 等 |
| **权限服务 PG** (`:25433`) | `permission_db` | ❌ 无 | `resource_registry`, `mount_registry`, `acl_entries`, `role_bindings`, `restrictions`, `permission_changes`, `user_cache` |

**实测数据**：
```sql
-- RAG 系统：仅 1 个租户（且是硬编码的）
SELECT tenant_id, count(*) FROM knowledge_bases GROUP BY tenant_id;
-- tenant-dev | 1

-- 权限服务：所有用户的 tenant_id 均为 NULL
SELECT id, username, tenant_id FROM user_cache LIMIT 4;
-- alice     | NULL
-- bob       | NULL
-- charlie   | NULL
-- testuser  | NULL
```

#### 1.2.2 API 层 — 无租户管理端点

| 系统 | 端点 | 现状 | 问题 |
|------|------|------|------|
| **RAG 系统** | `GET /api/v1/tenants` | 从 `knowledge_bases` 表 `SELECT DISTINCT tenant_id` | 只能看到"有 KB 的租户"，新租户（无 KB）不可见 |
| **RAG 系统** | `GET /api/v1/tenants/{id}/stats` | 统计 KB 数 + 文档数 | 可用，但数据源错误 |
| **权限服务** | 无 `/api/v1/tenants` | 不存在 | 无 CRUD |
| **管理台** | 无租户管理页 | 不存在 | 管理台侧边栏仅展示当前用户的 `tenant_id` |

#### 1.2.3 用户-租户绑定 — 无管理机制

当前用户与租户的关联**仅依赖两个非权威通道**：

| 通道 | 机制 | 问题 |
|------|------|------|
| **Keycloak 用户属性** | `attributes.tenant` → `user_cache.tenant_id` | 同步已实现但**无界面配置**；实际数据全为 NULL |
| **JWT claims** | `tenant` claim → `ctx.tenant_id` | 开发模式由登录表单自由输入，生产模式依赖 IdP |

**缺失能力**：
- 多租户用户（一个用户属于多个租户）：无法管理
- 租户默认角色：无法配置
- 租户管理员任命：无机制
- 租户启用/停用：无机制

#### 1.2.4 前端层 — 硬编码回退

| 系统 | 文件 | 硬编码 |
|------|------|--------|
| **RAG 前端** | `app/login/page.tsx:23` | `useState("tenant-dev")` |
| **RAG 前端** | `lib/auth.ts:27` | `decoded.tenant \|\| "tenant-dev"` |
| **管理台** | `app/login/page.tsx:32` | `useState("tenant-dev")` |
| **管理台** | `components/acl/PermissionGrantDialog.tsx:196` | `user?.tenant_id \|\| "tenant-dev"` |
| **管理台** | `components/acl/RoleBindingManager.tsx:133` | `user?.tenant_id \|\| "tenant-dev"` |
| **RAG 后端** | `api/deps.py:28` | `tenant_id="tenant-dev"` |

### 1.3 根因总结

> **租户是系统中最基础的多租户隔离维度，但整个系统没有"租户"的权威定义源。** tenant_id 以 VARCHAR 字符串的形式自由流动，任何模块都可以写入任意 tenant_id 值，没有校验、没有创建、没有绑定管理。

---

## 二、架构决策：外部系统 or 内部模块？

### 2.1 决策框架

根据四份设计文档的架构原则，从以下维度分析：

| 维度 | 外部系统（权限服务） | 内部模块（RAG 新增 P-TENANT） |
|------|---------------------|------------------------------|
| **与身份认证的关系** | 租户 = 组织容器，与 IdP 用户属性紧密关联 | 租户仅限于 RAG 业务隔离 |
| **与权限的关系** | ACL/角色绑定/限制全部按 tenant_id 隔离 | — |
| **依赖方向** | 符合 §0.2.1：「业务模块 → 平台模块 → 外部权限服务」 | RAG 成为租户权威源，权限服务反问 RAG |
| **数据权威源** | 权限服务已有所有租户级数据的权威表 | RAG 系统声明自己是 identity 的一部分 |
| **管理台归属** | 管理台是权限管理界面，租户管理是管理台的自然职责 | RAG 前端只有"跳转管理台"的入口 |

### 2.2 结论：设计为外部系统（权限服务内）

**租户管理应作为外部权限管理系统的子系统**，原因：

1. **架构红线对齐**（§0.2.1）：
   ```
   业务模块（B-*）──单向依赖──▶ 平台模块（P-*）──单向依赖──▶ 外部权限服务
   ```
   租户是比 KB/文档更高层级的容器，RAG 应是租户信息的消费者，不是权威源。

2. **已有基础**：权限服务已管理所有 tenant-scoped 数据：
   - `resource_registry.tenant_id` — 资源归属哪个租户
   - `acl_entries.tenant_id` — 权限授予发生在哪个租户
   - `role_bindings.tenant_id` — 角色绑定作用于哪个租户
   - `restrictions.tenant_id` — 封禁作用于哪个租户

3. **管理台职责一致**：设计文档 §13.4 明确"授予/回收权限不在本系统——管理台直连权限服务"。同理，租户管理也应在管理台中进行。

4. **IdP 集成边界清晰**：Keycloak 管理用户身份（sub/email/roles/groups），权限服务管理组织归属（tenant），分工明确。

5. **RAG 系统的正确角色**（§1.2）：
   > `ctx.tenant_id` 来自 JWT `tenant`，**仅供本系统业务查询与审计；绝不作为权限判定输入**
   
   RAG 系统不应拥有租户的创建/管理能力。

### 2.3 架构变更后全景

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                         租户管理架构（修改后）                                   │
│                                                                              │
│  ┌──────────────────┐        ┌──────────────────────────────────────────┐   │
│  │ IdP (Keycloak)    │        │ 权限服务 (Permission Service)              │   │
│  │                  │        │                                          │   │
│  │ • 用户 CRUD       │        │ ★ 新增：tenants 表                       │   │
│  │ • 组管理          │        │ ★ 新增：tenant_memberships 表             │   │
│  │ • JWT 签发        │        │ ★ 新增：租户管理 API                      │   │
│  │ • 用户属性:       │        │   POST   /api/v1/tenants                │   │
│  │   tenant(可选)    │        │   GET    /api/v1/tenants                │   │
│  └──────┬───────────┘        │   GET    /api/v1/tenants/{id}           │   │
│         │                    │   PATCH  /api/v1/tenants/{id}           │   │
│         │ JWT                 │   DELETE /api/v1/tenants/{id}           │   │
│         │ {tenant: "xxx"}    │   POST   /api/v1/tenants/{id}/members   │   │
│         ▼                    │   DELETE /api/v1/tenants/{id}/members/{uid} │
│  ┌──────────────────┐        │   GET    /api/v1/tenants/{id}/members   │   │
│  │ RAG v14 本系统    │        │                                          │   │
│  │                  │        │ • 已有的租户级数据不变：                    │   │
│  │ ctx.tenant_id     │        │   resource_registry, acl_entries 等      │   │
│  │ (只读消费)        │        │   均引用 tenants.id（外键约束）           │   │
│  │                  │        └──────────────────────────────────────────┘   │
│  │ GET /api/v1/     │                         │                            │
│  │   tenants        │                         │ 管理 API                   │
│  │ → 转发权限服务    │                         ▼                            │
│  └──────────────────┘        ┌──────────────────────────────────────────┐   │
│                              │ 管理台 (Admin Console)                    │   │
│                              │                                          │   │
│                              │ ★ 新增：/tenants 页面                     │   │
│                              │   • 租户列表 + 创建/编辑/删除             │   │
│                              │   • 租户成员管理                          │   │
│                              │   • 租户级统计                            │   │
│                              └──────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 三、详细设计方案

### 3.1 数据模型（权限服务新增表）

#### 3.1.1 tenants 表

```sql
-- ═══════════════════════════════════════════════════════
-- 租户定义表（权限服务权威）
-- ═══════════════════════════════════════════════════════
CREATE TABLE tenants (
    id          VARCHAR(64) PRIMARY KEY,              -- 租户唯一标识（如 "tenant-dev", "acme-corp"）
    name        VARCHAR(255) NOT NULL,                -- 显示名称（如 "开发测试租户"）
    description TEXT DEFAULT '',                       -- 描述
    status      VARCHAR(16) NOT NULL DEFAULT 'active', -- active | suspended | deleted
    created_by  VARCHAR(255) NOT NULL,                 -- 创建者 (user:xxx)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_tenants_status ON tenants(status);
```

#### 3.1.2 tenant_memberships 表

```sql
-- ═══════════════════════════════════════════════════════
-- 用户-租户绑定表（权限服务权威）
-- ═══════════════════════════════════════════════════════
CREATE TABLE tenant_memberships (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   VARCHAR(64) NOT NULL REFERENCES tenants(id),
    user_id     VARCHAR(255) NOT NULL,               -- user:xxx 格式（来自 IdP）
    role        VARCHAR(64) NOT NULL DEFAULT 'member', -- tenant_admin | member
    granted_by  VARCHAR(255) NOT NULL,                -- 授予者
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked     BOOLEAN NOT NULL DEFAULT false,
    revoked_at  TIMESTAMPTZ,

    UNIQUE (tenant_id, user_id)                       -- 一个用户在一个租户内只有一种角色
);

CREATE INDEX idx_tm_tenant ON tenant_memberships(tenant_id) WHERE NOT revoked;
CREATE INDEX idx_tm_user ON tenant_memberships(user_id) WHERE NOT revoked;
```

> **设计说明**：
> - `tenants.id` 使用 VARCHAR 而非 UUID：与现有 `tenant_id` 列的类型一致（`VARCHAR(255)`），避免迁移成本。
> - `tenant_memberships.role` = `tenant_admin` 表示租户管理员（可管理该租户内的 KB/用户），`member` = 普通成员。
> - 这是**权限服务内部的租户角色**，与 Cerbos 派生角色（`kb_reader`/`kb_writer` 等）是不同层级的概念。

#### 3.1.3 与现有表的关系

| 现有表 | tenant_id 列变更 | 说明 |
|--------|-----------------|------|
| `resource_registry.tenant_id` | 改为 `REFERENCES tenants(id)`（可选外键） | 保证引用的租户必须存在 |
| `acl_entries.tenant_id` | 同上 | — |
| `role_bindings.tenant_id` | 同上 | — |
| `restrictions.tenant_id` | 同上 | — |
| `permission_changes.tenant_id` | 同上 | — |
| `mount_registry.tenant_id` | 同上 | — |
| `user_cache.tenant_id` | 从 Keycloak 同步 + 可从 memberships 聚合 | 多租户用户展示所有归属 |

### 3.2 API 设计（权限服务新增）

#### 3.2.1 租户 CRUD

```
┌─────────────────────────────────────────────────────────────────────┐
│ POST /api/v1/tenants                                                 │
├─────────────────────────────────────────────────────────────────────┤
│ 权限: system_admin（管理台管理员）                                     │
│                                                                     │
│ Request:                                                            │
│   {                                                                 │
│     "id": "acme-corp",            // 唯一标识（小写字母+数字+连字符） │
│     "name": "ACME 公司",                                             │
│     "description": "ACME 公司的知识库空间"                             │
│   }                                                                 │
│                                                                     │
│ Response (201):                                                     │
│   {                                                                 │
│     "id": "acme-corp",                                              │
│     "name": "ACME 公司",                                             │
│     "status": "active",                                             │
│     "created_by": "user:admin",                                     │
│     "created_at": "2026-08-01T12:00:00Z"                            │
│   }                                                                 │
│                                                                     │
│ 业务逻辑:                                                            │
│  1. 校验 id 格式（^[a-z][a-z0-9-]{2,63}$）                          │
│  2. INSERT tenants                                                  │
│  3. 自动添加创建者为 tenant_admin 成员                               │
│  4. 若 Keycloak 已配置 → 在 Keycloak 创建对应 group:                 │
│     /tenants/{id}（用于 JWT claims 映射）                            │
│  5. 发布 TenantCreated 事件                                         │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ GET /api/v1/tenants                                                  │
├─────────────────────────────────────────────────────────────────────┤
│ Query: ?status=active&search=acme&limit=50&offset=0                  │
│ 权限: 需认证（返回用户所属的租户列表；system_admin 可看全部）          │
│                                                                     │
│ Response:                                                           │
│   {                                                                 │
│     "tenants": [                                                    │
│       {                                                             │
│         "id": "acme-corp",                                          │
│         "name": "ACME 公司",                                         │
│         "status": "active",                                         │
│         "member_count": 42,                                         │
│         "kb_count": 8,           // 从 RAG 系统或 resource_registry │
│         "created_at": "..."                                        │
│       }                                                             │
│     ],                                                              │
│     "total": 1                                                      │
│   }                                                                 │
└─────────────────────────────────────────────────────────────────────┘

GET    /api/v1/tenants/{id}           → 单个租户详情
PATCH  /api/v1/tenants/{id}           → 更新 name/description
DELETE /api/v1/tenants/{id}           → 软删除（status=deleted），
                                        前置：租户内无活跃资源
```

#### 3.2.2 用户-租户绑定管理

```
POST   /api/v1/tenants/{id}/members
  → { "user_id": "user:alice", "role": "member" }
  → 将用户加入租户

DELETE /api/v1/tenants/{id}/members/{user_id}
  → 将用户从租户移除（revoked=true）

GET    /api/v1/tenants/{id}/members
  → 租户成员列表（含角色、加入时间）

GET    /api/v1/users/{user_id}/tenants
  → 用户所属的租户列表（多租户）
```

#### 3.2.3 Keycloak Claims 映射

创建租户时同步在 Keycloak 中创建对应 **Group**：

```
Keycloak Group 结构:
  /tenants/{tenant_id}     ← 每个租户对应一个 Group
    /tenants/{tenant_id}/admins    ← 租户管理员子组
    /tenants/{tenant_id}/members   ← 租户成员子组

JWT Claims 扩展:
  "tenant": "acme-corp",                  // 用户当前活跃租户
  "tenants": ["acme-corp", "tenant-dev"], // 用户所属的全部租户
  "tenant_roles": {                       // 用户在各租户的角色
    "acme-corp": "member",
    "tenant-dev": "tenant_admin"
  }
```

### 3.3 RAG 系统变更

RAG 系统**不新增租户管理能力**，仅做以下调整：

#### 3.3.1 GET /api/v1/tenants — 改为转发权限服务

```python
# src/api/auth.py (修改现有实现)
@tenant_router.get("/tenants", response_model=list[TenantInfo])
async def list_tenants(authorization: str = Header(...)):
    """返回当前用户所属的租户列表。
    
    当前实现：从 knowledge_bases 表反查（不完整）。
    修复后：转发到权限服务 /api/v1/tenants，使用用户 JWT 中的 tenants claim。
    """
    # 方式 1（推荐）：从 JWT claims 提取用户所属租户列表
    # 权限服务已在 JWT 中注入 "tenants" claim
    
    # 方式 2（过渡）：通过 permission_service_client 调用权限服务 API
    from src.permission.permission_service_client import get_permission_client
    client = get_permission_client()
    return await client.list_tenants(credential=token)
```

#### 3.3.2 中间件 ctx 构建 — 校验租户有效性

```python
# src/permission/context.py
# 在 build_context() 中增加：校验 JWT tenant claim 对应的租户在权限服务中确实存在且为 active
# 若租户不存在 → 401 "invalid tenant"
```

#### 3.3.3 前端 — 移除硬编码

| 文件 | 变更 |
|------|------|
| `app/login/page.tsx` | 移除 `"tenant-dev"` 默认值，从 API 动态加载租户列表 |
| `lib/auth.ts` | 移除 `"tenant-dev"` 回退值 |
| `stores/useAuthStore.ts` | 多租户切换从 API 获取而不是重调 dev-login |

### 3.4 管理台变更

#### 3.4.1 新增：租户管理页面 `/tenants`

```
管理台侧边栏新增:
├── 📊 /dashboard
├── 🏢 /tenants          ← ★ 新增
│   ├── 租户列表
│   │   └── 点击 → 租户详情
│   │       ├── 基本信息（名称/ID/状态/创建时间）
│   │       ├── 成员管理（添加/移除/修改角色）
│   │       ├── 资源概览（KB 数/文档数）
│   │       └── 权限概览（ACL 数/角色绑定数）
│   ├── 创建租户 Dialog
│   └── 编辑/停用/删除
├── 📁 /resources
├── 👥 /users-groups
├── 🔑 /permissions
└── ...
```

#### 3.4.2 现有页面联动

| 页面 | 变更 |
|------|------|
| `/login` | 若用户属于多租户 → 登录后增加租户选择步骤 |
| `/dashboard` | 增加租户维度筛选 |
| `/permissions` | 权限授予 Dialog 的租户字段从硬编码改为从当前上下文获取 |
| Sidebar | 当前租户名从 API 获取，支持切换 |

### 3.5 与 Keycloak 的集成

#### 3.5.1 同步策略

```
权限服务 tenant_memberships (权威源)
        │
        │ 变更时实时同步
        ▼
Keycloak Group: /tenants/{tenant_id}/members
        │
        │ Keycloak 定时同步（现有 15 分钟周期）
        ▼
权限服务 user_cache（本地缓存）
        │
        │ 构建 JWT claims
        ▼
JWT { tenants, tenant_roles }
```

#### 3.5.2 初始数据迁移

```sql
-- 1. 从现有数据提取租户
INSERT INTO tenants (id, name, status, created_by)
SELECT DISTINCT tenant_id, tenant_id, 'active', 'system'
FROM resource_registry
WHERE tenant_id IS NOT NULL
ON CONFLICT (id) DO NOTHING;

-- 2. 为 "tenant-dev" 创建初始管理员
INSERT INTO tenant_memberships (tenant_id, user_id, role, granted_by)
VALUES ('tenant-dev', 'user:admin', 'tenant_admin', 'system')
ON CONFLICT (tenant_id, user_id) DO NOTHING;
```

---

## 四、实施计划

### 4.1 阶段划分

```
阶段 P0 — 租户基础定义（1-2 天）
├── 1. 权限服务: 创建 tenants 表 + tenant_memberships 表
├── 2. 权限服务: POST/GET/PATCH/DELETE /api/v1/tenants
├── 3. 权限服务: POST/DELETE/GET /api/v1/tenants/{id}/members
├── 4. 数据迁移: 从现有 resource_registry 提取 tenant_id
└── 5. Keycloak: 创建 /tenants Group + Protocol Mapper

阶段 P1 — 管理台租户管理（2-3 天）
├── 6. 管理台: /tenants 页面（列表 + 创建 Dialog）
├── 7. 管理台: 租户详情页（基本信息 + 成员管理）
├── 8. 管理台: 修改现有页面的租户硬编码为动态值
└── 9. 管理台: 登录流程加入租户选择（多租户用户）

阶段 P2 — RAG 系统适配（1-2 天）
├── 10. RAG: GET /api/v1/tenants 改为转发权限服务
├── 11. RAG: build_context 增加租户有效性校验
├── 12. RAG: 前端移除所有 "tenant-dev" 硬编码
└── 13. RAG: Header 租户切换器从 API 获取租户列表

阶段 P3 — 完善体验（后续）
├── 14. IdP: 生产模式 Keycloak 配置完整 SSO + tenants claims
├── 15. 租户级配额管理
├── 16. 租户级审计报表
└── 17. 跨租户操作审计
```

### 4.2 非必要不动现有代码原则

| 变更类型 | 影响范围 | 风险评估 |
|---------|---------|---------|
| **权限服务新增表和 API** | 零影响现有功能 — 纯新增 | ✅ 无风险 |
| **RAG `GET /api/v1/tenants` 改造** | 前端登录页租户下拉列表 | ⚠️ 需兼容过渡：先尝试权限服务，失败回退现有逻辑 |
| **RAG `build_context` 校验** | 所有 API 请求 | ⚠️ 需功能开关 `TENANT_VALIDATION_ENABLED`（默认 false） |
| **管理台新增页面** | 零影响现有功能 — 纯新增 | ✅ 无风险 |
| **现有表加外键** | 写操作可能因租户不存在而失败 | ⚠️ 仅在数据迁移完成后执行 |

### 4.3 功能开关

```bash
# 权限服务
TENANT_MANAGEMENT_ENABLED=true   # 租户管理 API 开关

# RAG 系统
TENANT_VALIDATION_ENABLED=false  # 租户有效性校验（默认关，迁移后开）
AUTHZ_SERVICE_MODE=local         # local | remote（remote 时 tenants API 强制走权限服务）

# 管理台
NEXT_PUBLIC_TENANT_MANAGEMENT=true  # 显示租户管理菜单
```

---

## 五、与现有系统的交互契约

### 5.1 RAG 系统 ↔ 权限服务（租户相关）

```
┌─────────────────────────────────────────────────────────────┐
│ RAG 系统                           权限服务                  │
│                                                             │
│ GET /api/v1/tenants ─────────────► GET /api/v1/tenants     │
│   (credential: JWT)                  ↓                      │
│                                   从 JWT tenants claim       │
│                                   查 tenant_memberships      │
│                                   ← 返回用户所属租户列表     │
│                                                             │
│ build_context() ─────────────────► GET /api/v1/tenants/{id}│
│   校验 tenant_id 有效性             ← 200 (active)           │
│                                    ← 404 (不存在)            │
│                                                             │
│ 创建 KB ─────────────────────────► POST /v1/resources/     │
│   body.tenant_id = ctx.tenant_id     register               │
│                                     (tenant_id 来自 body，   │
│                                      已有逻辑不变)           │
└─────────────────────────────────────────────────────────────┘
```

### 5.2 管理台 ↔ 权限服务（租户管理）

```
管理台                             权限服务
POST /api/v1/tenants ────────────► INSERT tenants
                                    INSERT tenant_memberships (creator as admin)
                                    → 同步 Keycloak Group
                                    → 发布 TenantCreated 事件

GET /api/v1/tenants ─────────────► SELECT tenants (按当前用户权限过滤)

POST /api/v1/tenants/{id}/members ► INSERT tenant_memberships
                                    → 同步 Keycloak Group membership
```

### 5.3 权限服务 ↔ Keycloak（租户同步）

```
权限服务                             Keycloak
创建租户 ─────────────────────────► POST /admin/realms/{r}/groups
                                      { "name": "tenants/{tenant_id}" }

添加成员 ─────────────────────────► PUT /admin/realms/{r}/users/{uid}/groups/{gid}

Keycloak → 权限服务同步（已有）:
  每 15 分钟 GET /admin/realms/{r}/users → 更新 user_cache
  (此同步已存在，tenant_id 从 attributes.tenant 映射)
```

---

## 六、安全边界

### 6.1 租户隔离保证

| 层级 | 机制 | 状态 |
|------|------|------|
| **JWT claims** | `tenant` claim 标识当前活跃租户 | 已有 |
| **RAG API** | 所有查询 `WHERE tenant_id = $1` | 已有 |
| **权限判定** | Cerbos 策略可包含 `principal.attr.tenant_id` | 已有 |
| **ACL 隔离** | acl_entries 按 tenant_id 索引 | 已有 |
| **向量库** | chunk payload.tenant_id + MetadataFilter 条件① | 已有 |
| **★ 租户切换** | 用户切换租户后，所有数据视图完全隔离 | 需前端实现 |
| **★ 跨租户访问** | 前端/API 层禁止跨租户访问（tenant_id 不可由请求参数覆盖） | 需后端校验加强 |

### 6.2 租户管理权限

| 操作 | 权限要求 | 说明 |
|------|---------|------|
| 创建租户 | `system_admin`（Keycloak Realm Role） | 仅超级管理员 |
| 编辑/停用租户 | `tenant_admin` of that tenant | 租户管理员 |
| 添加/移除租户成员 | `tenant_admin` of that tenant | 租户管理员 |
| 查看租户列表 | 已认证用户（仅返回自己所属租户） | system_admin 可看全部 |
| 查看租户成员 | `tenant_admin` or `member` of that tenant | 同租户用户 |

---

## 七、验收标准

### 7.1 功能验收

- [ ] 权限服务 `tenants` 表存在且已填充初始数据
- [ ] `POST /api/v1/tenants` 可创建新租户，返回 201
- [ ] `GET /api/v1/tenants` 返回用户所属租户列表
- [ ] `POST /api/v1/tenants/{id}/members` 可添加成员
- [ ] 创建租户后 Keycloak 自动创建对应 Group
- [ ] 管理台 `/tenants` 页面可查看、创建、编辑租户
- [ ] 管理台可管理租户成员
- [ ] RAG 前端登录页租户下拉列表不再回退到 `"tenant-dev"`
- [ ] RAG 系统 `GET /api/v1/tenants` 返回正确数据
- [ ] 所有现有硬编码 `"tenant-dev"` 已替换

### 7.2 非回归验收

- [ ] 现有 RAG API 所有端点正常（tenant_id 行为不变）
- [ ] Cerbos 策略判定结果不变
- [ ] 盖戳管道正常运行
- [ ] 前端登录/会话/租户选择流程正常
- [ ] Docker Compose 所有服务正常启动

---

## 八、文件索引

```
新增/修改文件清单:

权限服务 (permission-service/):
  models/tenant.py                 ← ★ 新增: Tenant, TenantMembership ORM 模型
  api/tenant_routes.py             ← ★ 新增: 租户 CRUD + 成员管理路由
  schemas/tenant_requests.py       ← ★ 新增: 租户相关请求 schema
  schemas/tenant_responses.py      ← ★ 新增: 租户相关响应 schema
  services/tenant_service.py       ← ★ 新增: 租户业务逻辑
  services/keycloak_sync.py        ← 修改: 同步时更新 tenant_id
  migrations/versions/xxx_add_tenants.py  ← ★ 新增: DDL migration
  app/main.py                      ← 修改: 注册 tenant_router

管理台 (admin-console/):
  app/tenants/page.tsx             ← ★ 新增: 租户管理页
  app/tenants/[id]/page.tsx       ← ★ 新增: 租户详情页
  components/tenants/              ← ★ 新增: 租户相关组件
  components/layout/Sidebar.tsx    ← 修改: 添加租户管理菜单项
  stores/useAuthStore.ts           ← 修改: 多租户支持
  lib/permission-api.ts            ← 修改: 添加租户 API 调用

RAG 系统 (proj_rag_dev/):
  src/api/auth.py                  ← 修改: GET /api/v1/tenants 转发逻辑
  src/permission/context.py        ← 修改: build_context 租户校验（可选）
  frontend/app/login/page.tsx      ← 修改: 移除硬编码
  frontend/lib/auth.ts             ← 修改: 移除硬编码
  frontend/stores/useAuthStore.ts  ← 修改: 租户切换逻辑
```

---

> **核心结论**：租户管理应设计为**外部权限管理系统的新增模块**，而非 RAG 系统的内部模块。权限服务已是所有租户级数据的权威源，自然扩展为租户的定义与绑定管理中心。RAG 系统保持"租户信息消费者"的角色不变，仅需修改 `GET /api/v1/tenants` 的数据来源和移除前端硬编码。管理台新增租户管理页面。此方案遵循四份设计文档的架构红线，对现有代码的改动最小化。
