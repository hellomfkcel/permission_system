# 权限模型 v2 — 三层授权 + 单一数据来源

- **日期**：2026-08-17
- **状态**：已实施
- **取代**：`docs/manage_role_design.md` 中关于"角色权限双写"的部分

---

## 0. 设计底线

> 策略文件描述**结构**，DB 记录**事实**，Cerbos 做**决策**。
> 没有一类数据同时存在于两个地方，没有第二个判断路径，没有需要手动同步的状态。

这条底线是本次重构的验收标准。下文每一节都在说明它是怎么被落实的。

---

## 1. 三层授权模型

```
┌─────────────────────────────────────────────────────────────────┐
│ Layer 1 · 平台层   cerbos/policies/platform/…/platform.yaml      │
│ 你有没有资格进入这个管理功能模块？                                │
│ → 认 Keycloak 角色 + 平台角色绑定 + 按功能的委托授权              │
│ → 不 import 任何项目派生角色，不随项目增减                        │
└──────────────────────────┬──────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│ Layer 2 · 项目权限数据层  …/platform/…/project_permission.yaml    │
│ 进门之后，你能操作这个项目的哪类权限数据？                        │
│ → 认 granted_actions[project_id]，全局唯一文件，所有项目共用      │
└──────────────────────────┬──────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│ Layer 3 · 项目业务层  cerbos/policies/{project}/…                │
│ 在这个项目里，你能对这个资源做什么？                              │
│ → 三路并行：ABAC（项目级 / KB 级）+ ACL 用户级 + ACL 角色级       │
│ → 任一命中即 ALLOW，全部未命中即隐式 DENY                        │
└─────────────────────────────────────────────────────────────────┘
```

### 目录布局

```
cerbos/policies/
├── platform/                      ← 平台层，全局唯一，不随项目增减
│   └── resource_policies/
│       ├── platform.yaml          ← 功能模块入口控制
│       └── project_permission.yaml← 项目权限数据操作控制
├── rag-v14/                       ← 项目层
│   ├── derived_roles/rag_roles.yaml
│   └── resource_policies/{kb,document}.yaml
├── demo2/  …
└── demo3/  …
```

平台层此前挂在策略根目录（更早挂在 `rag-v14/` 里）并 import 项目的派生角色，
于是"平台能不能进"取决于某个具体项目的策略文件。现在两者彻底分开。

### 三种授权模式

| 模式 | 方向 | 存储位置 | 粒度 |
|---|---|---|---|
| ABAC | 主体 → 资源 | `principal.attr.granted_actions` | 项目级 / KB 级 |
| ACL 用户级 | 资源 → 主体 | `resource.attr.acl` | 资源实例级 |
| ACL 角色级 | 资源 → 角色 | `resource.attr.role_acl` | 资源实例级 |

三路互不干扰，任一命中即放行。

---

## 2. 角色语义：激活，不是继承

这是所有"权限数看不懂"问题的根源。

| 类型 | 来源 | 语义 | 在策略中的位置 |
|---|---|---|---|
| 身份角色 | Keycloak | **入场资格**，本身不持项目权限 | `parentRoles`；platform.yaml 的 `roles` |
| 派生角色 | Cerbos | **权限持有者**，由授权记录或 ACL 激活 | `derivedRoles` |

`parentRoles: ["user"]` 的含义是"持有 user 身份的主体**有资格**被激活为该派生
角色"，**不是**"该角色继承 user 的权限"。

因此：

- `user` 在项目层的权限恒为 **0**；
- `kb_reader` 的 3 个权限就是它的全部权限，与 `user` 无关；
- 不再存在"子角色权限比父角色少"这种看起来矛盾的现象 —— 它们之间本来就没有
  多少关系。

解析器据此取消了 parentRoles 权限并集（`get_role_effective_permissions`）。
管理台把"父角色"一律改称"激活角色"，并额外显示激活方式：

| 激活方式 | 含义 |
|---|---|
| `identity` | 持有对应身份角色即激活 |
| `grant` | 需要 `granted_actions` 中的授权记录 |
| `acl` | 由资源实例上的 ACL 动态授予，权限随资源变化 |

### 命名说明

`platform_admin` 是 Keycloak / 平台绑定角色名；项目层用于中转它的派生角色叫
`platform_admin_role`。两者同名会让"身份角色"和"派生角色"在管理台混成一个条目。

---

## 3. 数据来源边界

### 策略文件管什么

- 派生角色定义（名字、激活条件）
- 动作权限矩阵（哪个角色能执行哪些 action）
- 资源属性前置条件（`is_enabled` / `retired` / `allow_download`）

**不管**：任何用户 ID、任何授权记录、任何角色的权限列表副本。

### PostgreSQL 管什么

| 表 | 记录的事实 |
|---|---|
| `acl_entries` | 哪个主体在哪个资源上被授予了什么动作 |
| `role_bindings` | 哪个主体持有什么角色（可限定资源范围） |
| `project_members` | 谁是哪个项目的管理员 |
| `role_definitions` | 角色的**档案**：名称、描述、激活角色、项目归属、是否内置 |
| `resource_registry` | 资源的业务属性 |
| `restrictions` / `audit_logs` | 封禁与审计 |

**不管**：角色的权限列表、派生角色的激活条件、任何"同步状态"。

`role_definitions.permissions` 列已由迁移 `f1a2b3c4d5e6` 删除。它此前是策略文件
的一份副本：改策略不改列、或反过来，管理台显示的权限就和判定结果对不上。

### 为什么没有新建 `resource_acl` 表

参考方案提出新建 `resource_acl(resource_kind, resource_id, principal_type,
principal_id, actions[])`。但 `acl_entries` 的 `(principal, resource_type,
resource_id, action)` 已经完整表达了同一件事："哪个资源实例允许谁做什么"。
再建一张表就等于让同一类事实存在于两处 —— 正是本次要消除的东西。

因此 ACL 路直接读 `acl_entries`：

```
acl_entries (principal='user:alice', resource_type='document',
             resource_id='doc-x',  action='doc:view')
        │
        ├─ principal 以 role: 开头 → resource.attr.role_acl["<role>"]
        └─ 其余（user: / group:）  → resource.attr.acl["<principal.id>"]
```

由 `services/acl_resolver.get_resource_acl()` 组装，写入仍走既有的
`POST /api/v1/acl/grant`。`GET /api/v1/acl/resources/{type}/{id}` 提供只读投影，
显示的就是判定期注入 Cerbos 的那份数据。

---

## 4. 判定路径唯一化

### 项目资源

```
/v1/check
  ├ resolve_granted_actions()   → principal.attr.granted_actions
  │     KB 级   ← acl_entries（记在 KB 上的授权）+ 适用的角色绑定
  │     项目级  ← project_members 中的 project_admin 成员资格
  ├ get_resource_acl()          → resource.attr.acl / role_acl  ← acl_entries
  ├ get_resource_attr()         → retired / is_enabled / allow_download / project_id
  └ Cerbos CheckResources       → ALLOW / DENY（后端无条件遵从）
```

`/v1/check`、`/v1/check/batch`、`/v1/filter`、策略模拟器四条入口现在共用同一套
组装函数，此前它们各写各的：

- `/v1/check/batch` 以 `resource_id` 为 `granted_actions` 的键，而策略按 `kb_id`
  查找 —— 批量判定和单条判定结果可以不一致；
- `/v1/filter` 把单篇文档的 `doc:retrieve` 映射成整个 KB 的 `"read"`，一条文档级
  授权被放大成 KB 级检索可见性；
- 策略模拟器直接把 `{principal: [actions]}` 当 `granted_actions` 传给 Cerbos，
  键是主体而不是资源作用域，派生角色永远匹配不上。

三处都已改为走 `resolve_granted_actions()` + `get_resource_acl()`。

### 平台功能

平台功能准入此前有两套规则：`platform.yaml`，以及 `api/auth_routes.py` 里手写的
`platform_admin → 全部 / platform_viewer → 全部只读 / platform_auditor → 三个模块`。
两套各自演进，改一处不改另一处就会出现"侧边栏能点、策略模拟器说拒绝"。

现在只剩策略文件一套。`services/platform_authorizer.py` 负责喂事实：

```
principal.roles                = JWT 角色 + 平台角色绑定 + project_member（合成角色）
principal.attr.granted_actions = platform 资源的 ACL，按功能 ID 分组
        ↓  一次 Cerbos 批量判定（12 个功能 × 2 个动作）
{feature_id: [platform:read, platform:write]}
```

`require_platform_permission` 与 `require_platform_admin` 都不再做任何角色名判断，
也没有"管理员直接放行"的快捷分支 —— 那个分支本身就是第二条判断路径。
Cerbos 不可达时 **fail-closed**（503），不回退到本地推断。

"项目成员能看到哪些模块"这条规则也搬进了策略（`project_member_baseline`
规则）；后端只提供"这个用户在 project_members 表里"这一事实。

---

## 5. 管理台展示口径

三处展示（角色卡片、角色详情、权限矩阵）现在同源、同作用域：

| 展示 | 数据来源 | 作用域 |
|---|---|---|
| 角色卡片的权限数 | `GET /roles/definitions?project_id=X` | 项目 X |
| 角色详情的权限列表 | `GET /roles/definitions/{name}?project_id=X` | 项目 X |
| 权限矩阵 | `GET /roles/permissions?project_id=X` | 项目 X |

关键修复：**权限按查询所处的项目解析，而不是按角色自身的 `project_id`。**
平台级角色（`project_id IS NULL`）在 `demo-project` 下只显示该项目与平台层策略
授予它的动作。此前一律按全局并集计算，于是一个连策略目录都没有的项目里也会
出现 `kb:read` / `doc:*`。

矩阵条目也已按角色名去重：同一角色此前会因为既出现在 `roles` 字段、又被别人
当作 `parentRoles` 而被列成 `source=cerbos` 和 `source=keycloak` 两条。

---

## 6. 行为变更（升级须知）

| 变更 | 影响 |
|---|---|
| `doc:download` 提升到 `kb_writer` 及以上 | 只有 `kb_reader` 授权的用户不再能下载；需要下载就授 `write`，或用文档级 ACL 单独放行 |
| `/v1/filter` 不再把文档级授权放大成 KB 级 `read` | 之前"只授了一篇文档却能检索整个 KB"的情况会被收紧；文档级授权改由 ACL 路精确生效 |
| 平台权限改由 Cerbos 判定 | Cerbos 不可达时管理台返回 503，而不是按 JWT 角色放行 |
| `role_definitions.permissions` 列删除 | 直接读该列的外部查询需改为调用 `/api/v1/roles/permissions` |
| 派生角色 `admin` → `platform_admin_role` | 存量以 `admin` 名义的角色绑定仍生效（`platform_admin_role` 的 parentRoles 保留了 `admin`） |

升级顺序：先执行 `alembic upgrade head`，再部署服务（代码已不再读 `permissions` 列）。

---

## 7. 约定与守护

策略文件的结构性约定由 `tests/test_policy_conventions.py` 在 CI 中强制：

- 全库无显式 `EFFECT_DENY`（未命中即隐式拒绝）
- `platform` / `project_permission` 资源只能住在 `platform/` 命名空间
- 平台层不 import、也不定义派生角色
- 项目层的授权规则不得直接授给身份角色（`user` / `system_admin` / `admin`）
- `importDerivedRoles` 与 `derivedRoles` 引用的角色都必须有定义

解析语义由 `tests/test_cerbos_policy_parser.py` 守护：项目隔离、平台命名空间
全局可见、无 parentRoles 并集、矩阵去重。

### 明令禁止

**策略文件侧**：不出现具体 user_id；不出现硬编码授权列表；不写显式 DENY；
不与 DB 做双向同步。变更方式 = 改文件 → Git Review → 部署 → 自动生效。

**DB 侧**：不存 `permissions`；不存同步状态；不存派生角色激活条件；
不存角色→动作映射。变更方式 = API 写入 → 实时生效。

**API 层**：不在 Cerbos 决策之外做二次权限判断；不混用策略解析结果与授权记录；
不把用户授权数据缓存超过请求生命周期（撤权即时生效）。
