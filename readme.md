# 权限管理平台 (Permission Platform)

面向多个业务系统的集中式授权平台。接入方不在本地实现权限判定，通过 HTTP 接口请求判定结果；权限的授予、回收、封禁、角色与策略维护统一在平台管理台完成。

平台以项目（project）为接入单元。每个接入的业务系统注册为一个项目，拥有独立的策略命名空间、客户端标识、API Key、异步任务 audience 和管理员成员列表。资源类型与动作由项目自己的 Cerbos 策略定义，平台不预设业务模型。

仓库中的 `rag-v14` 是首个接入项目，也是平台早期的耦合来源；`demo2`（OA 场景）、`demo3`（通用文档场景）是后续接入的项目，用于验证平台对异构业务模型的支持。文档中标注为“检索型项目专用”或“rag-v14 参考实现”的部分不适用于全部项目。

设计依据：`docs/外部系统设计.md`、`docs/外部系统实施方案.md`、`docs/权限管理系统架构设计.md`、`docs/tenant_design.md`、`docs/manage_role_design.md`、`docs/cerbos-policy-gray-release.md`。

权限模型以 `docs/permission_model_v2.md` 为准（三层授权 + 单一数据来源），其诊断与整改过程记录在 `docs/rag_permission_service_diagnose_v14.md`，死代码扫描结论记录在 `docs/rag_permission_service_diagnose_v15.md`（A 组已清理，B / D 组待确认）。该模型取代了 `docs/manage_role_design.md` 中"角色权限双写"的部分，历史诊断报告 v1–v13 中关于策略目录布局与角色权限来源的描述同样以 v2 为准。

## 1. 职责边界

| 能力 | 归属 | 说明 |
|------|------|------|
| 用户、组、顶层角色、SSO | Keycloak | 平台只读同步到 `user_cache`，不写回 |
| 策略规则表达与求值 | Cerbos PDP | 规则以 YAML 声明，平台不复制规则逻辑 |
| ACL、角色绑定、限制、资源镜像、项目注册 | 权限服务后端 | 权威存储，PostgreSQL |
| 判定编排、事件发布、投影计算 | 权限服务后端 | 组装 Cerbos 入参并映射判定结果 |
| 授权管理界面 | 管理台前端 | 项目、租户、授权、封禁、角色、策略、审计、模拟 |
| 权限拦截与执行 | 接入方业务系统 | 消费判定结果，平台不代替执行 |

接入方不直接调用 Cerbos PDP，也不直接读写平台数据库。所有授权数据的读写经过平台 API。

## 2. 架构

### 2.1 组件与调用关系

```
   Keycloak (IdP)                        Cerbos PDP
   用户/组/角色/JWT 签发                  单实例，加载全部项目策略
        │                                      ▲
        │ 只读同步 (15 min)                     │ POST /api/check/resources
        │ OIDC 密码授权                         │
        ▼                                      │
┌────────────────────────────────────────────────────────────────┐
│                    权限服务后端 (FastAPI)                       │
│                                                                │
│  决策面    POST /v1/check   /v1/check/batch                     │
│  上下文    POST /v1/context                                     │
│  生命周期  POST /v1/resources/{register,link,unlink,retire}     │
│  投影面    GET /v1/prefilter  POST /v1/visibility  POST /v1/filter │
│            （检索型项目专用）                                    │
│  管理面    /api/v1/{projects,tenants,acl,roles,restrictions,    │
│                     resources,policies,audit,simulate,auth}     │
│                                                                │
│  PostgreSQL: 项目注册 / ACL / 角色绑定 / 角色定义 / 限制 /       │
│              资源镜像 / 租户 / 变更日志 / 用户缓存               │
└────────────────────────────────────────────────────────────────┘
        │                    │                        ▲
        │ Pub/Sub + Stream   │ Bearer JWT             │ X-Api-Key + X-Client-Id
        ▼                    ▼                        │
     Redis              管理台前端              各接入项目的业务系统
  visibility_changed    (Next.js 14)           (perm-service-client)
```

### 2.2 组件分工

| 组件 | 承担 | 拆分理由 |
|------|------|----------|
| Keycloak | 身份、凭据、组织结构、JWT 签发 | 身份数据与授权数据的变更频率、责任人不同；IdP 的组织模型不表达资源粒度授权 |
| 权限服务 | 授权数据存储、判定编排、项目与租户管理，以及策略文件的解析 | 授权记录高频变更且需要事务一致性与索引，不适合写入策略文件 |
| Cerbos PDP | 规则求值 | 策略与代码分离、热加载；新项目接入只需新增 YAML，不改服务端代码 |

策略文件不是 Cerbos 独占的输入。权限服务同样读取并解析同一批 YAML，机制见 §2.3。

### 2.3 策略文件的两个消费者

`cerbos/policies/` 下的文件被两方读取：

| 消费者 | 读取方式 | 用途 |
|--------|---------|------|
| Cerbos PDP | disk driver 从 `/policies` 递归加载，`watchForChanges` 热重载 | 规则求值 |
| 权限服务 `services/cerbos_policy_parser.py` | 遍历策略树，收集 `derived_roles/` 与 `resource_policies/` 下的文件，合并 `rules[].roles` 与 `rules[].derivedRoles` 两个字段的 `actions` | 产出策略索引：角色到动作、资源类型到动作两组映射 |

权限服务需要这份索引的原因：属性注入式策略（§4.3）要求服务端在调用 PDP 前先算出主体拥有哪些动作并注入 `principal.attr.granted_actions`，而“角色对应哪些动作”这层信息写在策略里。早期实现是在服务端维护一份 `ROLE_ACTIONS_MAP` 硬编码字典，与策略文件手工同步；当前实现删除了该副本，改为运行时解析策略文件，以策略为唯一权威源。代价是权限服务必须能读到策略目录，与 PDP 共享同一份文件（路径由 `CERBOS_POLICIES_DIR` 指定，容器部署中两者挂载同一卷）。

索引的命名空间按文件路径推导：

| 路径形态 | 归属 | 可见范围 |
|---------|------|---------|
| `{root}/{project_id}/{derived_roles,resource_policies}/*.yaml` | 项目级 | 仅该项目 |
| `{root}/{derived_roles,resource_policies}/*.yaml` | 无项目归属 | 全部项目 |

平台功能策略与管理台创建的平台级自定义角色落在后者。

索引按策略目录指纹（文件路径、修改时间、大小）缓存，指纹探测有最小间隔以避免高频判定路径上的 stat 开销，并设 TTL 上限强制重新探测。策略写入、策略删除、角色定义增删四类操作额外主动失效本进程缓存。指纹机制使得其他进程写入的策略与绕过 API 直接编辑的文件都能被自动感知，无需重启。

角色定义在两处存储，各自承担不同职责：

| 存储 | 内容 | 地位 |
|------|------|------|
| `role_definitions` 表 | 名称、描述、父角色、`project_id`、`is_system` | 档案信息 |
| Cerbos YAML | 派生角色定义与资源规则 | 权限的唯一权威源 |

写路径先写策略文件再提交数据库，数据库失败时还原文件；删除路径先清理文件再删记录，失败时同样还原。管理台展示与判定链路的权限取值统一来自策略索引，角色接口另返回 `policy_synced` 标识表中存在但策略中缺失的角色。

### 2.4 项目作为隔离单元

| 维度 | 载体 | 作用 |
|------|------|------|
| 策略命名空间 | `cerbos/policies/{project_id}/` | 派生角色与资源策略按项目分目录，版本归档在同目录 `.versions/` |
| 服务间凭据 | `project_api_keys` | 每项目独立 API Key，SHA-256 存储，可吊销 |
| 客户端标识 | `project_clients` | 业务系统的调用方身份（如 `interactive-backend`、`retrieval`、`ingest`） |
| 异步 audience | `project_audiences` | ctx_token 的合法目标服务白名单 |
| 管理员范围 | `project_members` | 决定管理员在管理台能看到与操作哪些项目的数据 |
| 授权数据归属 | `acl_entries`、`role_bindings`、`role_definitions`、`restrictions`、`resource_registry` 的 `project_id` | 管理面查询按项目过滤 |

`role_definitions.project_id` 为 `NULL` 表示平台级角色，全部项目共享；非空表示仅该项目可见。

### 2.5 部署拓扑与端口

| 服务 | 容器内端口 | 宿主机端口 | 编排文件 |
|------|-----------|-----------|----------|
| permission-service | 8080 | 18080 | `docker-compose.yml` |
| admin-console | 3000 | 3002 | `docker-compose.yml` |
| perm-postgres | 5432 | 25433 | `docker-compose.yml` |
| perm-redis | 6379 | 16380 | `docker-compose.yml` |
| keycloak | 8080 | 8080 | `docker-compose.keycloak.yml` |
| cerbos | 3592 / 3593 | 13592 / 13593 | `docker-compose.yml` |

Cerbos 与权限服务挂载同一份 `./cerbos/policies`，两者对策略文件的视图一致。`.cerbos.yaml` 开启 `watchForChanges`，策略写入后 PDP 自动重载，管理台的策略部署不需要重启；权限服务侧由指纹探测感知（§2.3）。

## 3. 技术选型

| 组件 | 选型 | 选择理由 |
|------|------|----------|
| Web 框架 | FastAPI + uvicorn | 判定链路是 IO 密集型（数据库查询串接 Cerbos HTTP 调用），需要 async；Pydantic 模型同时承担请求校验与 OpenAPI 契约生成，接入方可直接读 `/docs` |
| 策略引擎 | Cerbos PDP | 规则与代码分离，YAML 热加载，新项目接入不改服务端代码；同时支持两种授权模型（见 §4.3），可覆盖属性驱动与静态角色驱动两类业务 |
| 数据库 | PostgreSQL | 授权变更与变更日志必须同事务提交（Outbox 模式）；`global_permission_version` 用 sequence 提供全局单调版本号；`JSONB` 承载角色权限列表与变更详情 |
| 缓存与事件 | Redis | 同时提供 Pub/Sub（低延迟通知）与 Stream（持久化、消费组、断点续消费），一个组件覆盖两种投递语义 |
| 身份源 | Keycloak | 提供 OIDC、组、Realm 角色与 Admin API；平台只需消费 JWT 与只读同步，不重复实现用户体系 |
| ORM 与迁移 | SQLAlchemy 2.0 (asyncio) + Alembic | 与 asyncpg 配合支持异步会话；Alembic 保证表结构随代码版本演进 |
| 限流 | slowapi | 按端点差异化配置 req/s，高频投影端点与管理面写端点限额不同 |
| 日志 | structlog | 判定与准入链路需要结构化字段（request_id、client_id、project_id、decision）以便检索 |
| 链路追踪 | OpenTelemetry | FastAPI 自动埋点加 Cerbos 调用手动 span，可定位耗时落在数据库查询还是 PDP 往返 |
| 管理台 | Next.js 14 (App Router) + Tailwind + Zustand + Axios | 页面以表格与表单为主，App Router 中间件直接承担登录保护；Zustand 承载项目切换与平台权限两处全局状态，不引入额外数据层 |
| 接入 SDK | httpx 同步客户端 | 接入方多为同步 Web 后端；SDK 零业务依赖，只封装 HTTP 契约与失败兜底，新项目改三个初始化参数即可接入 |

## 4. 授权模型

### 4.1 主体展开

`services/jwt_parser.py` 从 JWT claims 展开主体列表，后续全部 ACL、角色绑定与限制查询以该列表作为 `IN` 条件。该规则与项目无关。

| 来源 claim | 展开结果 |
|-----------|---------|
| `preferred_username`，回退 `sub` | `user:{id}` |
| `groups` 或 `group` | `group:{name}` |
| `realm_access.roles`，回退 `roles` | `role:{name}` |

`tenant` 或 `tenant_id` claim 决定租户，参与 ACL 与限制查询的过滤条件。

### 4.2 资源类型与动作由项目定义

`/v1/check` 与 `/v1/check/batch` 的 `resource.type` 与 `action` 是任意字符串，直接作为 Cerbos 的 `resource.kind` 与 action 传递。平台侧不校验其取值，有效集合由项目的资源策略决定。

`app/role_actions_config.py:get_resource_actions()` 从策略索引（§2.3）按 `resourcePolicy.resource` 分组取动作，作为管理台下拉选项、配置接口与 ACL 的 CSV 导入校验的数据源。`platform` 类型始终包含。

现有项目的资源类型示例：

| 项目 | 资源类型 | 动作示例 |
|------|---------|---------|
| `rag-v14` | `kb`、`document`、`platform` | `kb:read`、`kb:write`、`kb:manage`、`kb:grant`、`doc:view`、`doc:download`、`doc:retrieve`、`doc:unmount`、`doc:purge`、`doc:share` |
| `demo2` | `oa_leave_request`、`oa_expense_report`、`oa_employee_record`、`oa_performance_review`、`oa_announcement`、`oa_department_info` | `create`、`read:own`、`read:department`、`read:all`、`update:own`、`update:department`、`update:all`、`approve`、`reject`、`cancel`、`delete`、`view_salary` |
| `demo3` | `demo3_document` | `read`、`create`、`update`、`delete` |

`app/role_actions_config.py:VALID_ACTIONS` 中的 kb 与 doc 动作是硬编码补充集，仅在 ACL 的 CSV 导入端点作为校验白名单，不限制 `/v1/check` 与 `/api/v1/acl/grant`。

### 4.3 两种授权模型

平台支持两种把主体映射到权限的方式，选择哪一种取决于项目策略的写法。

| 模型 | 策略写法 | 授权数据位置 | 判定时的注入方式 | 适用场景 |
|------|---------|------------|----------------|---------|
| 属性注入式 | 规则用 `derivedRoles`，派生角色条件读 `request.principal.attr.granted_actions` | 平台的 `acl_entries` 与 `role_bindings` | 服务端解析出 `{资源键: [动作后缀]}` 注入 `principal.attr.granted_actions` | 授权按资源实例变化，需要在管理台随时授予与回收 |
| 静态角色式 | 规则用 `roles`，直接匹配主体的静态角色 | Keycloak Realm 角色（进入 JWT） | 服务端把 JWT 角色加上默认的 `user` 写入 `principal.roles` | 权限按岗位固定，授权粒度到角色而非资源实例 |

`rag-v14` 采用属性注入式，`demo2` 与 `demo3` 采用静态角色式。两者可在同一 PDP 内共存。

两条约束：

- `principal.roles` 只来自 JWT，平台的 `role_bindings` 不会写入 `principal.roles`。静态角色式项目的角色授予必须在 Keycloak 完成，在管理台创建角色绑定对这类策略不产生效果。
- 规则的 `roles` 字段匹配主体静态角色，`derivedRoles` 字段匹配派生角色。在 `derived_roles/*.yaml` 中定义的角色只有被规则的 `derivedRoles` 字段引用时才参与判定；若规则写的是 `roles`，同名派生角色定义不生效，主体必须真实持有该名称的静态角色。`demo2` 与 `demo3` 的策略即属此类。

### 4.4 授权数据来源

| 来源 | 表 | 语义 |
|------|-----|------|
| ACL 条目 | `acl_entries` | 主体对具体资源的具体动作，支持 `expires_at` 过期与 `revoked` 撤销 |
| 角色绑定 | `role_bindings` | 主体持有角色，可无范围（全部资源）、精确到资源类型加 ID，或限定到某容器资源 |
| 限制 | `restrictions` | 型一 `subject_ban`：主体级封禁，判定直接 deny 且投影返回 suspended；型二 `resource_restriction`：主体对指定资源的禁止 |

角色绑定在判定时展开为动作集合，展开映射来自策略文件的解析结果，机制与缓存行为见 §2.3。

### 4.5 租户与项目

两者是正交的隔离维度。

| 维度 | 表 | 隔离对象 | 取值来源 |
|------|-----|---------|---------|
| 租户 `tenant_id` | `tenants`、`tenant_memberships` | 业务数据归属 | JWT claim，参与授权数据查询条件 |
| 项目 `project_id` | `projects` 及四张关联表 | 接入的业务系统 | 服务间认证解析，或管理面请求显式传入 |

一个项目可承载多个租户。管理员通过 `project_members` 获得对某项目数据的管理权，`platform_admin` 不受项目范围限制。

### 4.6 平台自身的功能权限

管理台的功能访问由 `platform` 资源类型控制，功能目录定义在 `app/platform_features.py`，共 12 项：`dashboard`、`project_mgmt`、`tenant_mgmt`、`resource_mgmt`、`user_mgmt`、`role_mgmt`、`permission_mgmt`、`restriction_mgmt`、`policy_mgmt`、`audit_mgmt`、`playground`、`settings`。动作为 `platform:read` 与 `platform:write`，后者隐含前者。

解析顺序（`_get_platform_permissions`）：

1. JWT 角色含 `platform_admin`、`system_admin` 或 `admin`：全部功能的读写权限，不查库；
2. 查 `platform` 类型的 ACL 条目，`resource_id` 即功能 ID；
3. 查 `project_id IS NULL` 的平台角色绑定：`platform_admin` 给全部读写，`platform_viewer` 给全部只读，`platform_auditor` 给 `audit_mgmt`、`playground`、`dashboard` 只读；
4. 以上均无结果但用户在 `project_members` 中：给 `dashboard`、`resource_mgmt`、`user_mgmt`、`role_mgmt` 只读。

三个平台角色由迁移 `a1b2c3d4e5f6` 种子化。管理台登录后调用 `GET /api/v1/auth/me/access` 获取 `features`、`permissions`、`project_ids`，据此渲染侧边栏与读写态；后端由 `require_platform_permission(feature_id, action)` 依赖执行同一判断。

### 4.7 参考实现：rag-v14 项目

以下模型只属于 `rag-v14` 项目，不是平台的固有语义。

派生角色定义在 `cerbos/policies/rag-v14/derived_roles/rag_roles.yaml`。条件表达式按键查找 `granted_actions`：资源类型为 `kb` 时用 `resource.id`，否则用 `resource.attr.kb_id`。因此 `/v1/check` 在资源类型为 `document` 时以 `channel.kb` 作为 `granted_actions` 的键。

| 派生角色 | 父角色 | 成立条件 |
|---------|--------|---------|
| `kb_reader` | `user` | `granted_actions[key]` 含 `read` |
| `kb_writer` | `user` | `granted_actions[key]` 含 `write` |
| `kb_admin` | `user` | `granted_actions[key]` 含 `manage` |
| `admin` | `system_admin` | 无条件成立 |

准入矩阵：

| 动作 | kb_reader | kb_writer | kb_admin | admin | 资源条件 |
|------|:---------:|:---------:|:--------:|:-----:|---------|
| `kb:read` | 是 | 是 | 是 | 是 | `retired == false` |
| `kb:write` | | 是 | 是 | 是 | `retired == false` |
| `kb:manage` | | | 是 | 是 | |
| `kb:grant` | | | | 是 | |
| `doc:view` | 是 | 是 | 是 | 是 | `is_enabled == true`、`retired == false` |
| `doc:download` | 是 | 是 | 是 | 是 | 加 `allow_download == true` |
| `doc:retrieve` | 是 | 是 | 是 | 是 | `is_enabled == true`、`retired == false` |
| `doc:unmount` | | 是 | 是 | 是 | `retired == false` |
| `doc:purge` | | | 是 | 是 | `retired == false` |
| `doc:share` | | | | 是 | |

## 5. 平台能力

### 5.1 决策面（全部项目适用）

| 端点 | 行为 |
|------|------|
| `POST /v1/check` | 单条判定，返回 `allow` / `deny` / `indeterminate` 与 `decision_id` |
| `POST /v1/check/batch` | 批量判定，单次 Cerbos 往返，逐资源独立决策，单批上限 200 条 |

三态映射：Cerbos `EFFECT_ALLOW` 映射 `allow`，`EFFECT_DENY` 映射 `deny`，其余取值映射 `indeterminate`。`decision_id` 取 Cerbos 响应的 `cerbosCallId`，用于事后追溯。

失败处理：`/v1/check/batch` 在 Cerbos 调用异常时整批返回 `deny` 且 `decision_id` 为 `error`；`/v1/check` 向上抛出异常，由调用方按 fail-closed 处理。

### 5.2 资源生命周期（全部项目适用）

供接入方在资源写路径同步调用，维护平台侧资源镜像。资源属性（`retired`、`is_enabled`、`allow_download`、`owner`、`tenant_id`）会在判定时作为 `resource.attr` 传给 Cerbos，策略可直接引用。

| 端点 | 效果 |
|------|------|
| `POST /v1/resources/register` | 写入 `resource_registry`，带 `project_id` |
| `POST /v1/resources/link` | 写入 `mount_registry`（容器与成员的二元挂载） |
| `POST /v1/resources/unlink` | 置 `mount_registry.unlinked = true` |
| `POST /v1/resources/retire` | 置 `resource_registry.retired = true`，并级联把相关挂载置为 `unlinked` |
| `PATCH /v1/resources/{type}/{id}` | 更新 `is_enabled`、`allow_download` |
| `GET /v1/resources` | 按类型、租户或资源 ID 查询，至少需一个过滤条件 |

`idempotency_key` 必填，格式 `{facade}-{tenant}-{resource_id}[-{kb_id}]-v{n}`。校验拒绝十进制时间戳与 `ts=` / `timestamp=` 形式的非确定性值；资源 ID 本身为 UUID 时允许，因为同一资源产生同一键值。格式不合法返回 422。

调用顺序要求：先调平台成功，再提交本地事务；调用失败则回滚本地事务，避免镜像与业务数据不一致。

`link` 与 `unlink` 的数据模型固定为 `doc_id` 与 `kb_id` 两列，非文档挂载场景的项目不使用这两个端点。

### 5.3 上下文令牌（全部项目适用）

`POST /v1/context` 把 JWT 打包为带 audience 与过期时间的 `ctx_token`，格式 `ctx.{header}.{payload}.{signature}`，签名算法 HMAC-SHA256。用于异步任务携带主体身份而不在任务参数中传递 JWT 原文。

约束：`ttl_s` 上限 600 秒；`audience` 必须已注册在该平台的 `project_audiences`，否则返回 400；`/v1/check`、`/v1/filter`、`/v1/prefilter` 的 `credential` 参数接受 `ctx_token`，由 `jwt_parser` 验签、验过期后取出内层 JWT 继续解析。

签名密钥取 `CTX_TOKEN_SECRET`；未配置时回退为 Redis URL 的 SHA-256，仅用于开发环境。

### 5.4 投影面（检索型项目专用）

三个端点为检索链路设计，数据模型固定为知识库与文档的通道结构，非检索项目不使用。

`POST /v1/filter` 对候选项批量执行 `doc:retrieve` 判定，单批上限 200 条，异常时整批 denied。结果不允许缓存。

`GET /v1/prefilter` 返回主体在当前租户下可访问的 KB 列表，供检索链路在查询向量库前注入过滤条件。

| 字段 | 含义 |
|------|------|
| `kbs` | 允许访问的活跃 KB ID 列表 |
| `excluded_kbs` | 命中型二封禁而被排除的 KB |
| `tenant_wide_read` | 主体是否持有 `role:system_admin` 或 `role:admin` |
| `policy_version` | `v` 加全局版本号 |
| `ttl_s` / `expires_at` | 请求内缓存时长，固定 60 秒 |

主体命中型一封禁时改为返回 `{"suspended": true, "reason": "subject_banned"}`。

`POST /v1/visibility` 返回 `(doc_id, kb_id)` 通道的可见性戳记，供摄入管道写入向量库的 chunk payload。该端点无主体入参，返回的是资源侧投影。

| 字段 | 计算来源 |
|------|---------|
| `allow_stamps` | 文档级 ACL（`doc:retrieve`、`doc:view`）、KB 级 ACL（`kb:read`、`kb:write`、`kb:manage`、`kb:grant`）、命中该 KB 或全租户的角色绑定，三源去重聚合 |
| `deny_stamps` | 该文档与该 KB 上的型二封禁主体 |
| `version` | 全局权限版本号，供接入方做版本单调性检查 |
| `unmounted` | 挂载已解除，或文档、KB 任一已退役 |

戳记只包含原始主体（`user:` / `group:` / `role:` 前缀），不展开组成员，避免组成员变动导致全量重算。

### 5.5 管理面

| 前缀 | 功能 |
|------|------|
| `/api/v1/projects` | 项目 CRUD、client_id、API Key、audience、成员、SDK 接入配置 |
| `/api/v1/tenants` | 租户 CRUD、成员管理、按用户反查租户 |
| `/api/v1/acl` | 授予、回收、批量授予、CSV 导入、列表、有效权限计算 |
| `/api/v1/roles` | 角色绑定与解绑、绑定查询、角色定义 CRUD、角色权限矩阵 |
| `/api/v1/restrictions` | 型一与型二限制的添加、解除、查询 |
| `/api/v1/resources` | 资源列表、所有权查询、所有权转移 |
| `/api/v1/policies` | 策略文件读写、上传、删除、校验、版本历史、版本 diff、部署状态 |
| `/api/v1/audit` | 变更历史查询 |
| `/api/v1/simulate` | 策略模拟，走真实 ACL 与封禁查询链路 |
| `/api/v1/events/replay` | 从指定版本重放变更事件 |
| `/api/v1/auth` | 登录、token 校验与刷新、Keycloak 同步触发、用户与组查询、平台访问权限、运行时配置、统计、变更时间线 |

写操作统一经 `get_current_admin` 校验 Bearer token，经 `get_project_scope` 校验项目范围，经 `require_platform_permission` 校验平台功能权限。`granted_by` 一律取自 token 中的管理员身份，忽略请求体中的同名字段。

角色定义的增删改会同时生成或清理 Cerbos 策略文件，落在角色所属的命名空间内：项目级角色写 `cerbos/policies/{project_id}/`，平台级角色写策略根目录。派生角色合并进该命名空间的 `derived_roles/custom_roles.yaml`，资源规则按资源类型分别写 `resource_policies/custom_{resource_type}_{name}.yaml`。

动作到资源类型的对应关系取自策略索引：某动作出现在哪些资源策略的 `rules` 中就属于哪些资源类型。存在无法归属的动作时请求返回 422，不生成引用未定义资源的规则。文件写入与数据库提交互为回滚条件，写入顺序与失败处理见 §2.3。

### 5.6 事件与全局版本号

授权数据变更采用 Outbox 模式：

1. 在业务事务内调用 `write_change_log()`，执行 `nextval('global_permission_version')` 并插入 `permission_changes`；
2. `db.commit()` 原子提交业务变更与变更日志；
3. 提交后调用 `publish_event()` 双通道发布。

| 通道 | Key | 语义 |
|------|-----|------|
| Pub/Sub | `visibility_changed` | 实时通知，订阅方离线则丢失 |
| Stream | `visibility_changed_stream` | 持久化，`MAXLEN ~100000` 近似裁剪，订阅方用消费组断点续消费 |

两个通道独立失败互不阻塞。Redis 完全不可用时事件已落在 `permission_changes` 表，可通过 `/api/v1/events/replay` 或对账恢复。全局版本号同时用于 `prefilter.policy_version` 与 `visibility.version`。事件的 `change_detail` 中带 `project_id`，订阅方据此过滤本项目事件。

### 5.7 身份接入与同步

| 场景 | 机制 |
|------|------|
| 管理台登录（非生产） | `POST /api/v1/auth/dev-login` 用 Keycloak 密码授权校验凭据，再由平台签发 RS256 JWT；`PRODUCTION=true` 时该端点返回 501 |
| 生产登录 | Keycloak OIDC，管理台 `/auth/callback` 处理回调 |
| JWT 校签 | 本地公钥 `JWT_PUBLIC_KEY_PATH`，算法取 `JWT_ALGORITHM`；`JWT_ALLOWED_ISSUERS` 非空时校验 issuer 白名单 |
| 用户与组同步 | 后台任务在启动 30 秒后首次同步，此后每 15 分钟一次，写入 `user_cache`；失败仅记录告警并继续下一轮 |

全部项目共用同一个 Keycloak realm 与同一份 JWT 校签公钥。

### 5.8 策略管理

策略按项目分目录存放：`cerbos/policies/{project_id}/{derived_roles,resource_policies}/*.yaml`，历史版本归档在同项目下的 `.versions/`，文件名为时间戳加内容哈希。管理面提供读写、上传、校验、版本历史、版本 diff 与部署状态查询，灰度发布流程见 `docs/cerbos-policy-gray-release.md`。

`cerbos/policies/platform/` 是保留的平台层命名空间，只放 `platform.yaml`（功能模块入口）与 `project_permission.yaml`（项目权限数据操作），全局唯一、不随项目增减，对所有项目可见。项目目录里不得出现平台层资源，平台层也不 import 任何项目的派生角色 —— 这两条约定由 `tests/test_policy_conventions.py` 在 CI 中强制。完整模型见 `docs/permission_model_v2.md`。

## 6. 关键处理流程

### 6.1 单条判定

```
POST /v1/check
 ├─ 中间件：X-Api-Key → project_id，X-Client-Id → project_id，两者必须一致
 ├─ parse_principal(credential)：ctx. 前缀先解包，再 RS256 校签，展开 principals
 ├─ resolve_granted_actions_by_principal()
 │    ├─ 查 acl_entries：未撤销且未过期
 │    └─ 展开 role_bindings：按无范围 / 精确匹配 / 容器范围三种情形判定是否适用，
 │       角色到动作的映射从 Cerbos YAML 解析
 ├─ check_subject_ban()：命中则直接返回 deny + reasons=["subject_banned"]，不调用 Cerbos
 ├─ 组装 granted_actions：动作去前缀取后缀；键为 resource.id，
 │  资源类型为 document 时取 channel.kb
 ├─ get_resource_attr()：retired / owner / tenant_id / is_enabled / allow_download
 │    └─ 资源未注册时返回 retired=false、is_enabled=true、allow_download=true
 ├─ 组装 principal.roles：JWT 角色加默认 user
 ├─ CerbosAdapter.check_resources()：5xx 与网络错误重试 2 次（200ms、400ms），4xx 不重试
 └─ 三态映射，decision_id 取 cerbosCallId
```

`get_resource_attr` 对未注册资源显式返回 `retired=false`，因为 Cerbos CEL 表达式 `retired == false` 在属性缺失时求值为假，会导致全部规则判 deny。

对静态角色式项目，上述链路中 `granted_actions` 的计算结果不被策略引用，判定由 `principal.roles` 与规则的 `roles` 字段匹配决定。

### 6.2 检索前编译（检索型项目）

```
GET /v1/prefilter
 ├─ 型一封禁 → {suspended: true}
 ├─ 主体含 role:system_admin 或 role:admin → 返回该租户全部未退役 KB
 ├─ 否则：KB 级 ACL 直查
 │        加 文档级 ACL（doc:view/doc:download/doc:retrieve）经 mount_registry 反查所属 KB
 ├─ 过滤 retired=true
 ├─ 逐 KB 检查型二封禁 → excluded_kbs，并从 kbs 中剔除
 └─ 读 global_permission_version 作为 policy_version
```

文档级授权反查 KB 的分支保证只被授予单篇文档的用户仍能在检索中命中该文档所在的 KB。

### 6.3 逐条复核（检索型项目）

```
POST /v1/filter
 ├─ 逐项查型二封禁，命中者直接进 denied，不发往 Cerbos
 ├─ 按 kb_id 聚合权限：kb:read 取后缀 read；doc:retrieve 与 doc:view 映射为该 kb_id 上的 read
 ├─ 其余项组装为 actions=["doc:retrieve"] 的 Cerbos 资源，附 kb_id 与 tenant_id
 ├─ 单次 Cerbos 批量往返
 └─ 异常 → allowed 为空，全部项进 denied，decision_id="error"
```

## 7. 数据模型

| 表 | 用途 | 关键字段 |
|----|------|---------|
| `projects` | 接入项目 | `id`、`name`、`status` |
| `project_clients` | 客户端标识注册 | `project_id`、`client_id` |
| `project_api_keys` | 服务间密钥 | `key_hash`、`key_prefix`、`revoked`、`expires_at` |
| `project_audiences` | ctx_token audience 白名单 | `project_id`、`audience` |
| `project_members` | 项目管理员 | `user_id`、`role` |
| `acl_entries` | 权限授予记录 | `project_id`、`principal`、`resource_type`、`resource_id`、`action`、`granted_by`、`expires_at`、`revoked` |
| `role_bindings` | 角色绑定 | `project_id`、`principal`、`role`、可选 `resource_type` 与 `resource_id`、`revoked` |
| `role_definitions` | 角色定义 | `project_id`（NULL 为平台级）、`name`、`parent_keycloak_roles`、`permissions`、`is_system` |
| `restrictions` | 封禁与限制 | `restriction_type`、`principal`、`resource_type`、`resource_id`、`removed` |
| `resource_registry` | 资源镜像 | `project_id`、`resource_type`、`resource_id`、`name`、`tenant_id`、`owner`、`retired`、`is_enabled`、`allow_download` |
| `mount_registry` | 容器与成员挂载 | `doc_id`、`kb_id`、`unlinked` |
| `tenants`、`tenant_memberships` | 租户与成员 | `status`、`role`、`revoked` |
| `permission_changes` | 变更日志 | `event_type`、`kb_id`、`change_detail`、`version` |
| `user_cache` | Keycloak 只读缓存 | `user_id`、`username`、`roles`、`groups`、`enabled`、`last_synced_at` |
| `global_permission_version` | 序列，非表 | 全局单调版本号 |

## 8. 认证与准入

| 端点组 | 认证方式 | 必需请求头 |
|--------|---------|-----------|
| `/v1/*` | 项目预共享密钥 | `X-Api-Key`、`X-Client-Id`，决策面另需 `X-Request-Id` |
| `/api/v1/*` | 管理员 Bearer token | `Authorization: Bearer <JWT>` |
| `/healthz`、`/readyz`、`/metrics`、`/docs` | 无 | 无 |

`app/client_validator.py` 中间件对每个请求执行：

1. 公开路径与 `OPTIONS` 预检直接放行；
2. `/v1/` 前缀端点按 SHA-256 匹配 `project_api_keys` 中未吊销的记录，失败返回 401；
3. `/api/v1/` 前缀端点跳过 client_id 校验，改由 `get_current_admin` 校验 Bearer token；
4. 其余端点校验 `X-Client-Id` 是否注册在 `project_clients`，未注册返回 403；
5. API Key 与 client_id 解析出的项目不一致时返回 403 `project_mismatch`，防止跨项目混用凭据；
6. 解析出的 `project_id` 注入 `request.state`。

client_id、API Key、audience 三张注册表均带 60 秒内存缓存，变更时由对应路由主动失效。

管理员身份解析：JWT 角色含 `system_admin`、`admin` 或 `platform_admin` 直接通过；否则回退查询 `project_members`（先按 `user_id`，未命中再经 `user_cache.username` 换取 Keycloak UUID 重查），仍无记录返回 403。

限流按端点配置：

| 端点 | 默认限额 | 配置项 |
|------|---------|--------|
| `/v1/check` | 1000 req/s | `CHECK_RATE_LIMIT` |
| `/v1/check/batch` | 500 req/s | `CHECK_BATCH_RATE_LIMIT` |
| `/v1/filter` | 500 req/s | `FILTER_RATE_LIMIT` |
| `/v1/prefilter` | 500 req/s | `PREFILTER_RATE_LIMIT` |
| `/v1/visibility` | 200 req/s | `VISIBILITY_RATE_LIMIT` |

## 9. 部署与运行

### 9.1 前置依赖

| 依赖 | 要求 |
|------|------|
| Python | 3.11 |
| PostgreSQL | 16 |
| Redis | 7 |
| Cerbos PDP | 以 `cerbos/policies` 为 `/policies` 挂载点运行，HTTP 端口 13592 |
| Keycloak | 24 或以上，realm 配置见 `docs/keycloak-realm-setup.md` |
| JWT 密钥对 | RS256 公私钥；公钥文件缺失时服务启动失败 |

### 9.2 开发模式

```bash
# 基础设施
docker compose up -d perm-postgres perm-redis
docker compose -f docker-compose.keycloak.yml up -d

# 权限服务后端
cd permission-service
pip install -r requirements.txt
cp .env.example .env          # 按 §10 填写
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 18080 --reload

# 管理台前端
cd admin-console
npm install
npm run dev                   # http://localhost:3002
```

接口文档在 `http://localhost:18080/docs`。

### 9.3 容器部署

```bash
docker compose up -d perm-postgres perm-redis
docker compose up -d permission-service admin-console
```

`docker-compose.yml` 中 `permission-service` 设置 `PRODUCTION=true`，凭据通过 Docker secrets 注入，`secrets` 段的宿主机路径需按实际环境修改。`admin-console` 的 `NEXT_PUBLIC_*` 变量在构建期嵌入浏览器代码，浏览器无法解析 Docker 服务名，需通过 `EXTERNAL_HOST` 指定宿主机可达地址。

### 9.4 数据库迁移

```bash
cd permission-service
alembic upgrade head                      # 应用全部迁移
alembic revision --autogenerate -m "..."  # 生成新迁移
```

`alembic.ini` 中的 `sqlalchemy.url` 为开发默认值，生产环境通过 `DATABASE_URL_SYNC` 覆盖。

### 9.5 首次启动的自动行为

| 动作 | 触发条件 | 说明 |
|------|---------|------|
| 生产安全检查 | 每次启动 | 见 §10.2 |
| 内置项目建档 | `projects` 表为空 | 建立 `rag-v14` 项目，写入 client_id `interactive-backend` / `retrieval` / `ingest`，audience `retrieval-worker` / `ingestion-worker` / `stamping-worker`，把 `SERVICE_API_KEY` 哈希写入 `project_api_keys`，并把 `admin` 加为项目成员 |
| 策略目录迁移 | `policies/rag-v14/` 不存在且存在扁平目录 | 移动 `derived_roles/`、`resource_policies/`、`.versions/` |
| Keycloak 同步任务 | 每次启动 | 30 秒后首次执行，之后每 15 分钟一轮 |

内置项目建档是平台从单项目形态演进而来的兼容逻辑。全新部署可在建档后通过管理台删除或改造该项目。

## 10. 配置

### 10.1 环境变量

配置类为 `app/config.py:Settings`，读取 `.env` 与进程环境变量，模板见 `permission-service/.env.example`。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DATABASE_URL` | `postgresql+asyncpg://perm_user:perm_pass@localhost:25433/permission_db` | 异步驱动连接串 |
| `DATABASE_URL_SYNC` | 同上，psycopg2 驱动 | Alembic 使用 |
| `CERBOS_PDP_URL` | `http://localhost:13592` | PDP 地址 |
| `CERBOS_POLICIES_DIR` | 空 | 空时取仓库内 `cerbos/policies` |
| `REDIS_URL` | `redis://localhost:16380/0` | 事件通道 |
| `REDIS_PASSWORD` | 空 | 非空时覆盖 URL 中的密码段 |
| `KEYCLOAK_SERVER_URL` / `KEYCLOAK_REALM` / `KEYCLOAK_CLIENT_ID` | `http://localhost:8080` / `rag-v14` / `permission-service` | IdP 接入，realm 名为历史默认值 |
| `KEYCLOAK_CLIENT_SECRET` / `_FILE` | 空 | 文件存在时优先于明文值 |
| `KEYCLOAK_ADMIN_USERNAME` / `_PASSWORD`（含 `_FILE`） | 空 | service account 不可用时的同步回退凭据 |
| `JWT_PUBLIC_KEY_PATH` | `./config/jwt_public.pem` | 校签公钥，缺失则启动失败 |
| `JWT_PRIVATE_KEY_PATH` | `./config/jwt_private.pem` | dev-login 签发用 |
| `JWT_ALGORITHM` / `JWT_EXPIRE_SECONDS` | `RS256` / `3600` | |
| `JWT_ALLOWED_ISSUERS` | 空 | 逗号分隔白名单，空表示不校验 |
| `DEV_LOGIN_PASSWORD` | `dev_password_2026` | 开发登录口令 |
| `SERVICE_API_KEY` / `_FILE` | 空 | 首次启动写入内置项目的 API Key |
| `CTX_TOKEN_SECRET` / `_FILE` | 空 | ctx_token 签名密钥 |
| `HOST` / `PORT` / `LOG_LEVEL` | `0.0.0.0` / `18080` / `INFO` | |
| `ALLOWED_ORIGINS` | `http://192.168.1.127:3002,http://localhost:3002` | CORS 来源，逗号分隔 |
| `CHECK_RATE_LIMIT` 等 5 项 | 见 §8 | 端点限流 |
| `TLS_ENABLED` / `TLS_CERT_FILE` / `TLS_KEY_FILE` | `false` / 空 / 空 | |
| `PRODUCTION` | `false` | 生产模式开关 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` | OTel Collector |
| `OTEL_SERVICE_NAME` | `permission-service` | span 服务名 |

以 `_FILE` 结尾的变量指向 Docker secrets 或 K8s Secret 的挂载路径，文件存在时其内容覆盖同名明文变量。

### 10.2 生产模式检查

`validate_production_secrets()` 在启动时执行。JWT 公钥缺失在任何环境下都抛出 `RuntimeError` 阻止启动。以下项在 `PRODUCTION=false` 时记录告警，在 `PRODUCTION=true` 时阻止启动：

- `CTX_TOKEN_SECRET` 未配置（会回退为 Redis URL 哈希）；
- `DATABASE_URL` 仍含默认凭据 `perm_user:perm_pass`；
- Redis 使用无密码的默认 URL。

`PRODUCTION=true` 且未启用 TLS、或未配置 `SERVICE_API_KEY` 时记录告警但不阻止启动。

## 11. 接入新项目

### 11.1 步骤

1. **建项目**：管理台 `/projects` 创建项目，填写项目 ID（同时作为策略目录名）、名称，登记 client_id 与 audience，签发 API Key。API Key 仅在签发时明文显示一次，之后只保留哈希与前缀。
2. **写策略**：在 `cerbos/policies/{project_id}/` 下建 `derived_roles/` 与 `resource_policies/`，定义本项目的资源类型与动作。资源类型名在全平台唯一，建议加项目前缀。策略可通过管理台 `/policies` 上传与校验，PDP 自动重载。
3. **选授权模型**：按 §4.3 决定用属性注入式还是静态角色式。属性注入式需要在派生角色条件中读 `request.principal.attr.granted_actions`；静态角色式需要在 Keycloak 建对应 Realm 角色。
4. **建角色与授权**：管理台 `/roles` 创建项目级角色定义，`/permissions` 授予 ACL 或绑定角色，`/restrictions` 配置封禁。
5. **加管理员**：在项目详情中添加 `project_members`，使该管理员在项目模式下可见本项目数据。
6. **接入 SDK**：读 `GET /api/v1/projects/{id}/sdk-config` 获取 `base_url`、`client_ids`、`audiences` 与调用示例，在业务系统中初始化客户端。
7. **接入生命周期端口**：在资源写路径同步调用 `register` 与 `retire`，使策略可以引用 `retired`、`owner` 等资源属性。
8. **验证**：管理台 `/playground` 用真实 ACL 链路模拟判定，确认规则命中符合预期。

### 11.2 SDK

`perm_service_client.PermissionClient` 封装对外契约，自动注入 `X-Request-Id`、`X-Client-Id`、`X-Api-Key`，并在存在 OpenTelemetry span 时透传 `traceparent`。源码在 `perm-service-client/`，本地安装用 `pip install ./perm-service-client`。

```python
from perm_service_client import PermissionClient

client = PermissionClient(
    base_url="http://permission-service:18080",
    api_key="<项目 API Key>",
    client_id="<项目已注册的 client_id>",
)

# 单条判定：action 与 resource_type 取自本项目策略
result = client.check(credential=jwt, action="approve",
                      resource_type="oa_leave_request", resource_id="req-1024")
if result["decision"] != "allow":
    raise PermissionError

# 批量判定
decisions = client.check_batch(
    credential=jwt, action="read",
    resources=[{"type": "demo3_document", "id": doc_id} for doc_id in ids],
)

# 资源生命周期
client.register_resource(resource_type="oa_leave_request", resource_id="req-1024",
                         owner="user:alice", tenant_id="tenant-hq",
                         project_id="demo2")
```

`register_resource` 等生命周期方法的 `project_id` 参数默认值为 `rag-v14`，新项目必须显式传入。

### 11.3 失败语义

SDK 内置 fail-closed 兜底，异常时的返回值如下，接入方据此拒绝而非放行：

| 方法 | 异常时返回 |
|------|-----------|
| `check` | `{"decision": "deny", "reasons": ["perm_service_unavailable"]}` |
| `check_batch` | 空字典 |
| `filter_items` | 空列表 |
| `get_prefilter` | `{"suspended": True}` |
| `get_visibility` | `{"unmounted": True}` |
| `mint_ctx_token`、生命周期端口 | 抛出异常，由调用方回滚本地事务 |

`check_batch` 与 `filter_items` 的单批上限为 200 条。`/v1/filter` 的结果不允许缓存。

## 12. 管理台

页面清单与目录结构见 `admin-console/README.md`。

界面分两种模式，由侧边栏顶部的项目选择器切换，选择结果存在 `localStorage.admin_current_project`：

| 模式 | 取值 | 行为 |
|------|------|------|
| 平台管理 | `__all__` | 显示全部项目的聚合数据，`/projects` 等平台级页面仅在此模式可见 |
| 项目模式 | 具体项目 ID | Axios 请求拦截器自动附加 `project_id` 查询参数，各页面只展示该项目数据 |

认证流程：登录后 token 存入 `localStorage.admin_token` 并写入 `admin_session` cookie；`middleware.ts` 在服务端按 cookie 拦截未登录访问，`AuthGuard` 在客户端二次校验；请求拦截器注入 `Authorization` 头并检查 token 过期，提前 5 分钟输出告警，已过期则清理凭据并跳转登录。侧边栏项由 `usePlatformPermissions` 依据 `GET /api/v1/auth/me/access` 的返回值过滤。

## 13. 可观测性

| 出口 | 内容 |
|------|------|
| `GET /metrics` | Prometheus 文本格式。内存计数器涵盖判定结果分布、调用失败、事件发布、Keycloak 同步；gauge 由数据库实时查询，包含活跃 ACL 数、活跃资源数、活跃限制数、变更日志总数、全局版本号 |
| OTel traces | FastAPI 自动埋点，加 `cerbos.check_resources` 手动 span，属性含 `request_id`、`resource_count`、`attempts`、`elapsed_ms`、`call_id` |
| 结构化日志 | structlog，关键事件包括 `client_id_rejected`、`api_key_rejected`、`project_mismatch`、`keycloak_sync_completed`、`security_config_warning` |
| `GET /healthz`、`/readyz` | 存活与就绪探针 |

指标当前不带 `project_id` 维度，跨项目的调用量需从日志或 trace 侧区分。

## 14. 测试

```bash
cd permission-service
python -m pytest tests/test_api.py -v            # 端点行为，24 项
python -m pytest tests/test_joint_contract.py -v # 联合契约，J-1 至 J-20
```

两个套件都以 HTTP 方式打真实服务，运行前需启动 permission-service（默认 `http://localhost:18080`，可用 `TEST_BASE_URL` 覆盖）、PostgreSQL、Redis 与 Cerbos PDP。`tests/utils.py` 中的 `JWT_PRIVATE_KEY_PATH` 为绝对路径常量，在其他机器上运行需要改为本机私钥路径。

联合契约测试以 `rag-v14` 的模型编写，覆盖文档级授权反查检索可见性、型一与型二封禁生效、戳记不展开成员、KB 粒度授权传播、retire 级联清理、未注册资源判 deny、prefilter 接受 ctx_token、filter 批量上限、decision_id 可追溯、超时 fail-closed、版本号严格单调、事件持久化。其他项目当前没有对应的契约套件。

辅助脚本：

| 脚本 | 用途 |
|------|------|
| `scripts/cleanup_test_data.py` | 按测试资源命名前缀调用 retire 端点清理残留数据，支持 `--dry-run` |
| `scripts/verify_cerbos_policy_sync.sh` | 比对两侧 Cerbos 策略文件差异，路径为脚本内硬编码常量，使用前需修改 |

## 15. 目录结构

```
permission-service/          权限服务后端 (FastAPI)
├── app/                     配置、数据库、限流、准入中间件、可观测性、平台功能目录
├── api/                     路由：decision / context / lifecycle / projection
│                            + project / tenant / acl / role / role_definitions
│                            + restriction / resource / audit / auth
├── services/                jwt_parser、acl_resolver、cerbos_adapter、
│                            cerbos_policy_parser、stamp_calculator、event_publisher
├── models/                  SQLAlchemy 模型
├── schemas/                 Pydantic 请求与响应模型
├── idp/keycloak_sync.py     Keycloak 用户与组同步
├── migrations/              Alembic 迁移
└── tests/                   端点测试与联合契约测试

admin-console/               管理台前端 (Next.js 14)
perm-service-client/         通用接入 SDK
cerbos/policies/
├── platform/                平台层，全局唯一、不随项目增减
│   └── resource_policies/   platform.yaml（功能模块入口）
│                            project_permission.yaml（项目权限数据操作）
├── rag-v14/                 首个接入项目：知识库与文档模型，属性注入式
├── demo2/                   OA 场景项目：请假、报销、绩效等，静态角色式
└── demo3/                   通用文档项目，静态角色式
docs/                        设计文档与诊断记录
scripts/                     运维脚本
docker-compose.yml           权限服务、管理台、PostgreSQL、Redis
docker-compose.keycloak.yml  Keycloak
```

## 16. 已知约束

本节只记录当前仍然存在的约束。此前记录的角色定义一致性缺口、项目隔离缺口与环境硬编码已在代码中修复，修复内容见 §17。

### 16.1 平台级约束

| 约束 | 影响 |
|------|------|
| Cerbos 单 PDP 单策略根，全部项目策略同时加载 | 资源类型名（`resourcePolicy.resource`）与派生角色名在全平台唯一，需靠项目前缀避免冲突（`demo2` 用 `oa_`、`demo3` 用 `demo3_`） |
| `mount_registry` 无 `project_id` 列 | 挂载关系按 `(doc_id, kb_id)` 全局唯一，跨项目复用相同 ID 对会冲突 |
| `principal.roles` 的 JWT 来源部分不可由平台授予 | 平台角色绑定已注入 `principal.roles`（§17），但 JWT 中的静态角色仍只能在 Keycloak 侧变更 |
| 全部项目共用一个 Keycloak realm 与一份 JWT 公钥 | 无法按项目隔离身份源 |
| 判定链路无缓存层 | 容量按 §8 限流值规划；`/v1/filter` 结果永久禁止缓存 |

### 16.2 RAG 耦合残留

以下属于对外 API 契约，改动会破坏已接入的检索型系统，需版本化迁移，未在本次范围内处理。

| 位置 | 现状 |
|------|------|
| `/v1/prefilter`、`/v1/visibility`、`/v1/filter` | 字段固定为 `kbs`、`doc_id`、`channel.kb`、`allow_stamps`，仅检索型项目可用 |
| `/v1/resources/link`、`unlink` 与 `mount_registry` | 挂载模型固定为 `doc_id` 与 `kb_id` 两列 |
| `/v1/check` 对 `resource.type == "document"` 以 `channel.kb` 作 `granted_actions` 键 | 为 `rag_roles.yaml` 的派生角色表达式服务，其他项目若使用 `document` 类型需注意此特例 |
| `app/role_actions_config.py:VALID_ACTIONS` | 保留 kb 与 doc 十个动作，用于补全策略未声明的同族动作；校验路径已改为按项目策略动态解析（§17） |
| `tests/test_joint_contract.py` | 全部用例基于知识库与文档模型，其他项目没有对应的契约套件 |

### 16.3 测试与环境

| 约束 | 影响 |
|------|------|
| 两个测试套件需要 Cerbos PDP 与 Redis 在位 | 判定类用例在缺少 PDP 时无法通过 |
| `tests/test_api.py` 中部分用例未携带 `X-Api-Key` 与 `Authorization` | 这些用例早于多项目改造，仍会返回 401；本次只补齐了必填的 `project_id` |
| 首次启动引导默认建立 `rag-v14` 项目 | 全新部署可设 `BOOTSTRAP_PROJECT_ENABLED=false` 跳过 |

## 17. 已修复项

以下为针对 §16 早期版本所列问题的代码修改，按主题归类。

### 17.1 角色定义的数据一致性

| 问题 | 处理 |
|------|------|
| 自定义角色写入策略根目录，而解析器按项目子目录扫描，导致角色对权限服务不可见 | 解析器改为遍历整个策略树并按路径推导命名空间，`{root}/{project}/…` 为项目级、`{root}/…` 为无项目归属；写入端按 `role_definitions.project_id` 落到对应目录 |
| 表与策略文件双写无法互相回滚 | 改为先写策略文件再提交数据库，`PolicyTransaction` 保留被改写文件的原始内容，数据库失败时还原文件；删除路径同构 |
| 删除角色时 YAML 清理必定失败 | 原实现引用未导入的 `_os` / `_re`，每次抛 `NameError` 并被调用方吞掉；清理逻辑重写在 `services/role_policy_writer.py` |
| 角色权限无法修改 | 新增 `PUT /api/v1/roles/definitions/{name}`；内置角色拒绝改写，其策略条件由手工维护 |
| 解析缓存无 TTL，跨进程与带外改文件不自愈 | 改为按策略目录指纹（路径 + mtime + 大小）缓存，探测有最小间隔并设 TTL 上限，其他进程写入与直接编辑文件均可感知 |
| 管理台展示与判定取值不同源 | 两者统一走策略索引；角色接口新增 `policy_synced` 字段，标识表中存在但策略中缺失的角色 |
| 生成的资源规则文件名按 kb / doc 前缀硬编码 | 改为按策略索引把动作反查到资源类型；无法归属的动作在创建时返回 422，不再生成引用未定义资源的规则 |

### 17.2 项目隔离

| 问题 | 处理 |
|------|------|
| 判定期 ACL、角色绑定、限制、资源属性查询不按项目过滤 | `services/acl_resolver.py` 全部查询接受 `project_id`，由 `/v1/*` 从准入中间件解析出的 `request.state.project_id` 传入 |
| `resource_registry` 唯一键不含 `project_id`，跨项目同名资源互相覆盖 | 唯一键改为 `(project_id, resource_type, resource_id)`，生命周期端点按项目定位资源 |
| `acl_entries`、`role_bindings` 唯一键不含 `project_id` | 改为项目内唯一；平台级授权（`project_id IS NULL`）用部分唯一索引单独表达 |
| `role_definitions.name` 全表唯一 | 改为项目内唯一，平台级角色全平台唯一 |
| 平台角色绑定对静态角色式项目不生效 | 适用的角色绑定并入 Cerbos `principal.roles`，匹配 `roles` 字段的策略可消费平台侧绑定 |
| `system_admin` 通过认证与平台权限检查后被项目范围拒绝 | `get_project_scope` 与 `get_admin_project_ids` 改为与 `get_current_admin`、`require_platform_permission` 同口径处理 `platform_admin` / `system_admin` / `admin` |
| 指标无项目维度 | `authz_decision_total`、`authz_call_failed_total` 增加 `project` 标签 |
| 自定义角色可对平台层资源生成策略，生成物与平台策略同 Cerbos 模块 ID | 只允许对本项目自有资源类型生成规则；自定义角色必须归属项目 |
| 自定义角色的派生角色集合名全项目共用 `custom_roles` | 改为 `custom_roles_{project}`，写入时自动改写引用旧名的遗留文件 |
| `mount_registry` 无 `project_id`，唯一约束全局 | 加列 + 唯一约束改 `(project_id, doc_id, kb_id)`，link/unlink/retire/visibility/prefilter 全部按项目过滤 |
| 生命周期端点信任请求体的 `project_id` | 项目以凭据（API key + client_id）为准，请求体只能复述，不一致返回 403 |
| `platform` 资源的授权记录可挂在项目下并升级为平台权限 | 授权层级按资源所在的策略命名空间校验，平台层授权必须 `project_id IS NULL` |
| `/api/v1/policies*` 不校验项目范围 | 加命名空间访问控制：平台层与策略根目录仅平台管理员可写；列表按可见范围过滤；模块 ID 冲突 409 |
| 审计事件的项目标记靠各调用方自觉写入 | `write_change_log` 的 `project_id` 改为必填参数，在唯一入口统一写进 `change_detail` |

### 17.3 解耦

| 问题 | 处理 |
|------|------|
| 平台功能策略放在 `rag-v14` 目录且引用该项目的派生角色 | 移到全局唯一的 `policies/platform/` 命名空间，只认 Keycloak 角色与平台授权记录，不 import 任何项目派生角色（见 `docs/permission_model_v2.md`） |
| 平台功能准入在策略文件与 `auth_routes` 中各写一套 | 删除后端手写映射，统一由 Cerbos 判定（`services/platform_authorizer.py`），Cerbos 不可达时 fail-closed |
| `role_definitions.permissions` 是策略文件的副本 | 删列（迁移 `f1a2b3c4d5e6`），角色权限只从 Cerbos 策略解析 |
| `parentRoles` 被当作权限继承展示 | 取消 parentRoles 权限并集，管理台改称"激活角色"并标注激活方式 |
| CSV 导入按 kb / doc 硬编码校验动作与资源类型 | 改为按目标项目的策略动态解析合法取值 |
| 首次启动固定建档 `rag-v14` 并迁移策略目录 | 建档改为可配置（`BOOTSTRAP_*`）；策略目录迁移逻辑删除，该目录现用于承载平台级策略 |
| `KEYCLOAK_REALM` 默认值为 `rag-v14` | 改为 `permission-platform`，既有部署通过环境变量保留原值 |

### 17.4 迁移链与依赖

| 问题 | 处理 |
|------|------|
| `alembic upgrade head` 在空库必定失败 | `dddcf4c5164c` 未创建 `role_definitions.permissions`，而 `a1b2c3d4e5f6` 写入该列；补建列。种子数据向 `NOT NULL` 的 `project_id` 写 NULL，改为放开这两列的非空约束以表达平台级语义；`project_members` 种子改为仅在项目存在时插入 |
| `requirements.txt` 缺 `python-multipart` | CSV 导入与策略上传端点在注册时即抛错，服务无法启动；补入该依赖与显式的 `PyYAML` |

### 17.5 准入与可观测性

| 问题 | 处理 |
|------|------|
| 未注册的 `X-Client-Id` 返回 500 而非 403 | `client_validator.py` 引用了未导入的 `HTTPException`，且中间件中抛异常不会被转成 403；改为直接返回 `JSONResponse` |
| `/metrics` 标签值未加引号，Prometheus 无法解析 | 按文本格式输出 `label="value"` 并转义 |

### 17.6 环境硬编码

| 问题 | 处理 |
|------|------|
| 测试私钥路径写死为特定机器的绝对路径 | 改为默认取仓库内 `config/jwt_private.pem`，可用 `TEST_JWT_PRIVATE_KEY_PATH` 覆盖 |
| `scripts/verify_cerbos_policy_sync.sh` 写死两侧目录 | 改为命令行参数或环境变量传入，支持按项目命名空间比对 |
| `scripts/cleanup_test_data.py` 写死服务地址与数据库连接串 | 改为读环境变量 |
| `docker-compose.yml` 的 secrets 指向固定宿主机路径 | 改为 `${SECRETS_DIR:-./permission-service/config}` |
| Cerbos 不在主编排中，策略目录需手工保持一致 | 纳入 `docker-compose.yml`，与权限服务共享 `./cerbos/policies` 挂载 |

### 17.7 验证方式

| 验证项 | 方式 | 结果 |
|--------|------|------|
| 迁移链 | 空库执行 `alembic upgrade head` | 13 个版本全部通过 |
| 唯一约束 | 直接 SQL 插入跨项目同名资源与 ACL，再插入项目内重复行 | 跨项目通过，项目内被 `uq_acl_project_scoped` 拒绝 |
| 判定期项目隔离 | 真实数据库下调用 `resolve_granted_actions_by_principal`、`resolve_bound_roles`、`check_subject_ban` | 三者均按项目返回不同结果 |
| 策略索引 | 对比新旧解析器在同一策略树上的输出 | 新解析器额外索引到根目录下的自定义角色 |
| 缓存自愈 | 带外改写策略文件后再次查询角色动作 | 无需重启即反映新内容 |
| 角色写入与回滚 | 临时策略树上执行创建、更新、回滚、删除 | 未定义动作被拒且无残留文件；回滚后角色不可见 |
| 准入返回码 | 对 `/v1/check` 分别缺 API Key、错误 client_id、缺 client_id | 401 / 403 / 403 |
| 服务启动 | 空库首次启动 | 引导建档成功，83 个端点注册 |
| 回归对比 | 同环境下对改造前后运行 `tests/test_api.py` | 改造前 2 通过（14 例因硬编码路径报错），改造后 9 通过；判定类用例需 Cerbos PDP，本环境不具备 |
