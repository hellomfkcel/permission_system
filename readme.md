# 权限管理系统 (Permission System)

集中式授权服务。业务系统不在本地实现权限判定，通过 HTTP 接口向本系统请求判定结果、检索预过滤条件和可见性投影；权限的授予、回收、封禁与策略维护统一在本系统的管理台完成。

设计依据：`docs/外部系统设计.md`、`docs/外部系统实施方案.md`、`docs/权限管理系统架构设计.md`、`docs/tenant_design.md`、`docs/manage_role_design.md`。

## 1. 系统职责边界

| 能力 | 归属 | 说明 |
|------|------|------|
| 用户、组、顶层角色、SSO | Keycloak | 本系统只读同步到 `user_cache`，不写回 |
| 策略规则求值 | Cerbos PDP | 规则以 YAML 描述，本系统不复制规则逻辑 |
| ACL、角色绑定、限制、资源镜像 | 权限服务后端 | 权威存储，PostgreSQL |
| 判定编排、投影计算、事件发布 | 权限服务后端 | 组装 Cerbos 入参并映射判定结果 |
| 授权管理界面 | 管理台前端 | 授予、回收、封禁、策略、审计、模拟 |
| 权限拦截与执行 | 接入方业务系统 | 消费判定结果，本系统不代替执行 |

接入方不实现本地判定逻辑，也不直接调用 Cerbos PDP。所有授权数据的读写经过本服务。

## 2. 架构

### 2.1 组件与调用关系

```
   Keycloak (IdP)                     Cerbos PDP
   用户/组/角色/JWT 签发               策略 YAML 求值
        │                                  ▲
        │ 只读同步 (15 min)                 │ /api/check/resources
        │ OIDC 密码授权                     │
        ▼                                  │
┌───────────────────────────────────────────────────────────┐
│                   权限服务后端 (FastAPI)                   │
│                                                           │
│  决策面   POST /v1/check  /v1/check/batch  /v1/filter      │
│  投影面   GET  /v1/prefilter    POST /v1/visibility        │
│  上下文   POST /v1/context                                 │
│  生命周期 POST /v1/resources/{register,link,unlink,retire} │
│  管理面   /api/v1/{acl,roles,restrictions,resources,       │
│                    tenants,projects,audit,policies}        │
│                                                           │
│  PostgreSQL: ACL / 角色绑定 / 限制 / 资源镜像 / 变更日志    │
└───────────────────────────────────────────────────────────┘
        │                    │                     ▲
        │ Pub/Sub + Stream   │ Bearer JWT          │ X-Api-Key + X-Client-Id
        ▼                    ▼                     │
     Redis              管理台前端            接入方业务系统
  visibility_changed    (Next.js 14)         (perm-service-client)
```

### 2.2 三层分工与拆分理由

| 层 | 承担 | 不承担 | 拆分理由 |
|----|------|--------|----------|
| Keycloak | 身份、凭据、组织结构、JWT 签发 | 资源级授权 | 身份数据与授权数据变更频率、责任人不同；IdP 的组织模型不适合表达资源粒度的授权 |
| 权限服务 | 授权数据存储、判定编排、投影计算 | 规则表达 | ACL 条目高频变更且需要事务一致性与查询索引，不适合写入策略文件 |
| Cerbos PDP | 规则求值 | 授权数据持久化 | Cerbos 无状态、策略热加载，规则以声明式 YAML 表达，变更不需重启或改代码 |

判定时，权限服务把 ACL 与角色绑定解析成 `principal.attr.granted_actions`（形如 `{kb-id: ["read","write"]}`），把资源属性解析成 `resource.attr`，再交给 Cerbos 求值。规则只依据属性判断，不感知数据来源。

### 2.3 部署拓扑与端口

| 服务 | 容器内端口 | 宿主机端口 | 编排文件 |
|------|-----------|-----------|----------|
| permission-service | 8080 | 18080 | `docker-compose.yml` |
| admin-console | 3000 | 3002 | `docker-compose.yml` |
| perm-postgres | 5432 | 25433 | `docker-compose.yml` |
| perm-redis | 6379 | 16380 | `docker-compose.yml` |
| keycloak | 8080 | 8080 | `docker-compose.keycloak.yml` |
| cerbos | 3592 / 3593 | 13592 / 13593 | 外部编排，配置见 `cerbos/.cerbos.yaml` |

Cerbos PDP 不在本仓库的 compose 中启动，需以 `cerbos/policies` 作为 `/policies` 挂载点独立运行。`cerbos/.cerbos.yaml` 开启了 `watchForChanges`，策略文件写入后自动重载。

## 3. 技术选型

| 组件 | 选型 | 选择理由 |
|------|------|----------|
| Web 框架 | FastAPI + uvicorn | 判定链路是 IO 密集型（数据库查询串接 Cerbos HTTP 调用），需要 async；Pydantic 模型同时承担请求校验与 OpenAPI 契约生成，接入方可直接读 `/docs` |
| 策略引擎 | Cerbos PDP | 规则与代码分离，策略以 YAML 声明并支持热加载；派生角色（derived roles）机制可用条件表达式把外部注入的属性映射为角色，避免在服务端硬编码角色判断 |
| 数据库 | PostgreSQL | ACL 授予与变更日志必须在同一事务内提交（Outbox 模式）；`global_permission_version` 使用 sequence 提供全局单调版本号；`JSONB` 承载角色权限列表与变更详情 |
| 缓存/事件 | Redis | 同时提供 Pub/Sub（低延迟通知）与 Stream（持久化、消费组、断点续消费），一个组件覆盖两种投递语义 |
| 身份源 | Keycloak | 提供 OIDC、组、Realm 角色与 Admin API；本系统只需消费 JWT 与只读同步，不重复实现用户体系 |
| ORM / 迁移 | SQLAlchemy 2.0 (asyncio) + Alembic | 与 asyncpg 配合支持异步会话；Alembic 保证表结构随代码版本演进 |
| 限流 | slowapi | 按端点差异化配置 req/s，投影面高频端点与管理面写端点限额不同 |
| 日志 | structlog | 判定链路需要结构化字段（request_id、client_id、decision）以便检索 |
| 链路追踪 | OpenTelemetry | FastAPI 自动埋点加 Cerbos 调用手动 span，可定位判定耗时落在数据库查询还是 PDP 往返 |
| 管理台 | Next.js 14 (App Router) + Tailwind + Zustand + Axios | 页面以表格与表单为主，App Router 的路由分段与中间件直接承担登录保护；Zustand 状态量小，不引入额外数据层 |
| SDK | httpx（同步客户端） | 接入方多为同步 Web 后端；SDK 零业务依赖，仅封装 HTTP 契约与失败兜底 |

## 4. 权限模型

### 4.1 主体展开

`services/jwt_parser.py` 从 JWT claims 展开主体列表，后续所有 ACL 与限制查询以该列表作为 `IN` 条件：

| 来源 claim | 展开结果 |
|-----------|---------|
| `preferred_username`，回退 `sub` | `user:{id}` |
| `groups` 或 `group` | `group:{name}` |
| `realm_access.roles`，回退 `roles` | `role:{name}` |

`tenant` 或 `tenant_id` claim 决定租户，参与全部 ACL 与限制查询的过滤条件。

### 4.2 资源与动词目录

动词目录的权威定义在 `app/role_actions_config.py`：

| 资源类型 | 动作 |
|---------|------|
| `kb` | `kb:read`、`kb:write`、`kb:manage`、`kb:grant` |
| `document` | `doc:view`、`doc:download`、`doc:retrieve`、`doc:unmount`、`doc:purge`、`doc:share` |
| `platform` | `platform:read`、`platform:write` |

`doc:retrieve` 用于检索链路，由 `/v1/filter` 判定。

### 4.3 授权来源

| 来源 | 表 | 语义 |
|------|-----|------|
| ACL 条目 | `acl_entries` | 主体对具体资源的具体动作，支持 `expires_at` 过期与 `revoked` 撤销 |
| 角色绑定 | `role_bindings` | 主体持有派生角色，可无范围（全部资源）、限定资源类型加 ID，或限定到 KB（对该 KB 下文档生效） |
| 限制 | `restrictions` | 型一 `subject_ban`：主体级封禁；型二 `resource_restriction`：主体对指定资源的禁止 |

角色绑定在判定时展开成动作集合，展开映射由 `services/cerbos_policy_parser.py` 从 Cerbos YAML 解析并缓存，策略文件是角色到动作映射的唯一权威源。策略写入或角色定义变更时调用 `invalidate_role_actions_cache()` 失效缓存。

### 4.4 派生角色

`cerbos/policies/rag-v14/derived_roles/rag_roles.yaml` 定义四个派生角色。条件表达式在 `granted_actions` 中按键查找：资源类型为 `kb` 时用 `resource.id`，否则用 `resource.attr.kb_id`。

| 派生角色 | 父角色 | 成立条件 |
|---------|--------|---------|
| `kb_reader` | `user` | `granted_actions[key]` 含 `read` |
| `kb_writer` | `user` | `granted_actions[key]` 含 `write` |
| `kb_admin` | `user` | `granted_actions[key]` 含 `manage` |
| `admin` | `system_admin` | 无条件成立 |

因为文档的派生角色按 `kb_id` 取值，`/v1/check` 在资源类型为 `document` 时以 `channel.kb` 作为 `granted_actions` 的键，而非文档 ID。

### 4.5 准入矩阵

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

矩阵以 `cerbos/policies/rag-v14/resource_policies/*.yaml` 为准，修改策略文件即修改矩阵。

### 4.6 租户与项目

两者是正交的隔离维度。

| 维度 | 表 | 隔离对象 | 使用位置 |
|------|-----|---------|---------|
| 租户 `tenant_id` | `tenants`、`tenant_memberships` | 业务数据归属 | ACL、限制、资源镜像的查询条件，来自 JWT claim |
| 项目 `project_id` | `projects`、`project_clients`、`project_api_keys`、`project_audiences`、`project_members` | 接入方系统 | 服务间认证、client_id 准入、ctx_token audience 白名单、管理员可见范围 |

一个项目可以承载多个租户；一个管理员通过 `project_members` 获得对某项目数据的管理权，`platform_admin` 不受项目范围限制。

## 5. 核心功能

### 5.1 决策面

| 端点 | 调用方 | 行为 |
|------|-------|------|
| `POST /v1/check` | 业务后端 | 单条判定，返回 `allow` / `deny` / `indeterminate` 与 `decision_id` |
| `POST /v1/check/batch` | 业务后端 | 批量判定，单次 Cerbos 往返，逐资源独立决策 |
| `POST /v1/filter` | 检索链路 | 对候选项批量执行 `doc:retrieve` 判定，返回 `allowed` / `denied` |

三态映射：Cerbos `EFFECT_ALLOW` 映射 `allow`，`EFFECT_DENY` 映射 `deny`，其余取值映射 `indeterminate`。`decision_id` 取 Cerbos 响应的 `cerbosCallId`，用于事后追溯。

失败处理：`/v1/check/batch` 与 `/v1/filter` 在 Cerbos 调用异常时整批返回 `deny`；`/v1/check` 向上抛出异常，由调用方按 fail-closed 处理。

### 5.2 投影面

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

### 5.3 上下文令牌

`POST /v1/context` 把 JWT 打包为带 audience 与过期时间的 `ctx_token`，格式为 `ctx.{header}.{payload}.{signature}`，签名算法 HMAC-SHA256。用于异步任务携带主体身份而不在任务参数中传递 JWT 原文。

约束：`ttl_s` 上限 600 秒；`audience` 必须已注册在 `project_audiences`，否则返回 400；`/v1/check`、`/v1/filter`、`/v1/prefilter` 的 `credential` 参数接受 `ctx_token`，由 `jwt_parser` 验签、验过期后取出内层 JWT 继续解析。

签名密钥取 `CTX_TOKEN_SECRET`；未配置时回退为 Redis URL 的 SHA-256，仅用于开发环境。

### 5.4 生命周期端口

供接入方在资源写路径同步调用，维护本系统的资源镜像。

| 端点 | 效果 |
|------|------|
| `POST /v1/resources/register` | 写入 `resource_registry` |
| `POST /v1/resources/link` | 写入 `mount_registry` |
| `POST /v1/resources/unlink` | 置 `mount_registry.unlinked = true` |
| `POST /v1/resources/retire` | 置 `resource_registry.retired = true`，并级联把相关挂载置为 `unlinked` |
| `PATCH /v1/resources/{type}/{id}` | 更新 `is_enabled`、`allow_download` 运营属性 |

`idempotency_key` 必填，格式 `{facade}-{tenant}-{resource_id}[-{kb_id}]-v{n}`。校验拒绝十进制时间戳与 `ts=` / `timestamp=` 形式的非确定性值；资源 ID 本身为 UUID 时允许，因为同一资源产生同一键值。格式不合法返回 422。

调用顺序要求：先调本服务成功，再提交本地事务；调用失败则回滚本地事务，避免镜像与业务数据不一致。

### 5.5 管理面

| 前缀 | 功能 |
|------|------|
| `/api/v1/acl` | 授予、回收、批量授予、CSV 导入、列表、有效权限计算 |
| `/api/v1/roles` | 角色绑定与解绑、绑定查询、角色定义 CRUD、权限矩阵查询 |
| `/api/v1/restrictions` | 型一与型二限制的添加、解除、查询 |
| `/api/v1/resources` | 资源列表、所有权查询、所有权转移 |
| `/api/v1/tenants` | 租户 CRUD、成员管理、按用户反查租户 |
| `/api/v1/projects` | 项目 CRUD、client_id、API Key、audience、成员、SDK 接入配置 |
| `/api/v1/audit` | 变更历史查询 |
| `/api/v1/simulate` | 策略模拟，走真实 ACL 与封禁查询链路 |
| `/api/v1/policies` | 策略文件读写、上传、删除、校验、版本历史、版本 diff、部署状态 |
| `/api/v1/events/replay` | 从指定版本重放变更事件 |
| `/api/v1/auth` | 登录、token 校验与刷新、Keycloak 同步触发、用户与组查询、平台访问权限、统计 |

创建自定义角色时，除写入 `role_definitions` 表外，同时生成 Cerbos YAML（`derived_roles/custom_roles.yaml` 与 `resource_policies/custom_{kb,doc}_{name}.yaml`），使角色在判定中生效。YAML 写入失败不回滚角色创建，记录告警，可由管理员手工补齐。

### 5.6 事件与全局版本号

授权数据变更采用 Outbox 模式：

1. 在业务事务内调用 `write_change_log()`，执行 `nextval('global_permission_version')` 并插入 `permission_changes`；
2. `db.commit()` 原子提交业务变更与变更日志；
3. 提交后调用 `publish_event()` 双通道发布。

| 通道 | Key | 语义 |
|------|-----|------|
| Pub/Sub | `visibility_changed` | 实时通知，订阅方离线则丢失 |
| Stream | `visibility_changed_stream` | 持久化，`MAXLEN ~100000` 近似裁剪，订阅方用消费组断点续消费 |

两个通道独立失败互不阻塞。Redis 完全不可用时事件已落在 `permission_changes` 表，可通过重放或对账恢复。全局版本号同时用于 `prefilter.policy_version` 与 `visibility.version`。

### 5.7 身份接入与同步

| 场景 | 机制 |
|------|------|
| 管理台登录（非生产） | `POST /api/v1/auth/dev-login` 用 Keycloak 密码授权校验凭据，再由本服务签发 RS256 JWT；`PRODUCTION=true` 时该端点返回 501 |
| 生产登录 | Keycloak OIDC，管理台 `/auth/callback` 处理回调 |
| JWT 校签 | 本地公钥 `JWT_PUBLIC_KEY_PATH`，算法取 `JWT_ALGORITHM`；`JWT_ALLOWED_ISSUERS` 非空时校验 issuer 白名单 |
| 用户与组同步 | 后台任务在启动 30 秒后首次同步，此后每 15 分钟一次，写入 `user_cache`；同步失败仅记录告警并继续下一轮 |

### 5.8 多项目隔离与准入

`app/client_validator.py` 中间件对每个请求执行：

1. 公开路径（`/healthz`、`/readyz`、`/metrics`、`/docs`、`/openapi.json`、`/redoc`）与 `OPTIONS` 预检直接放行；
2. `/v1/` 前缀端点校验 `X-Api-Key`，按 SHA-256 匹配 `project_api_keys` 中未吊销的记录，匹配失败返回 401；
3. `/api/v1/` 前缀端点跳过 client_id 校验，改由 `get_current_admin` 依赖校验 Bearer token；
4. 其余端点校验 `X-Client-Id` 是否注册在 `project_clients`，未注册返回 403；
5. API Key 与 client_id 解析出的项目不一致时返回 403 `project_mismatch`。

client_id、API Key、audience 三张注册表均带 60 秒内存缓存，变更时由对应路由主动失效。

### 5.9 平台功能权限

管理台自身的功能访问由 `platform` 资源类型控制，功能目录定义在 `app/platform_features.py`（12 项，如 `permission_mgmt`、`policy_mgmt`、`audit_mgmt`）。管理台登录后调用 `GET /api/v1/auth/me/access` 获取 `features`、`permissions`、`project_ids`，据此决定侧边栏项与只读或可写状态。后端侧由 `require_platform_permission(feature_id, action)` 依赖执行同一判断，`platform:write` 隐含 `platform:read`。

平台角色 `platform_admin`、`platform_viewer`、`platform_auditor` 由迁移 `a1b2c3d4e5f6` 种子化。

### 5.10 策略管理

策略文件按项目分目录存放：`cerbos/policies/{project_id}/{derived_roles,resource_policies}/*.yaml`，历史版本归档在同项目下的 `.versions/`。管理面提供读写、校验、版本历史与 diff，灰度发布流程见 `docs/cerbos-policy-gray-release.md`。

服务首次启动时会把旧的扁平目录（`policies/derived_roles`、`policies/resource_policies`）迁移到 `policies/rag-v14/` 下，`rag-v14` 目录已存在则跳过。

## 6. 关键处理流程

### 6.1 单条判定

```
POST /v1/check
 ├─ 中间件：X-Api-Key → project_id，X-Client-Id → project_id，两者必须一致
 ├─ parse_principal(credential)：ctx. 前缀先解包，再 RS256 校签，展开 principals
 ├─ resolve_granted_actions_by_principal()
 │    ├─ 查 acl_entries：未撤销且未过期
 │    └─ 展开 role_bindings：按无范围 / 精确匹配 / KB 范围三种情形判定是否适用
 ├─ check_subject_ban()：命中则直接返回 deny + reasons=["subject_banned"]，不调用 Cerbos
 ├─ 组装 granted_actions：动作去前缀取后缀；键为 resource.id，document 类型取 channel.kb
 ├─ get_resource_attr()：retired / owner / tenant_id / is_enabled / allow_download
 │    └─ 资源未注册时返回 retired=false、is_enabled=true、allow_download=true
 ├─ CerbosAdapter.check_resources()：5xx 与网络错误重试 2 次（200ms、400ms），4xx 不重试
 └─ 三态映射，decision_id 取 cerbosCallId
```

`get_resource_attr` 对未注册资源显式返回 `retired=false`，因为 Cerbos CEL 表达式 `retired == false` 在属性缺失时求值为假，会导致全部规则判 deny。

### 6.2 检索前编译

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

### 6.3 逐条复核

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
| `resource_registry` | 资源镜像 | `project_id`、`resource_type`、`resource_id`、`name`、`tenant_id`、`owner`、`retired`、`is_enabled`、`allow_download` |
| `mount_registry` | 文档与 KB 的挂载关系 | `doc_id`、`kb_id`、`unlinked` |
| `acl_entries` | 权限授予记录 | `principal`、`resource_type`、`resource_id`、`action`、`granted_by`、`expires_at`、`revoked` |
| `role_bindings` | 角色绑定 | `principal`、`role`、可选 `resource_type` 与 `resource_id`、`revoked` |
| `role_definitions` | 角色定义 | `name`、`parent_keycloak_roles`、`permissions`、`is_system`、`project_id` |
| `restrictions` | 封禁与限制 | `restriction_type`、`principal`、`resource_type`、`resource_id`、`removed` |
| `permission_changes` | 变更日志 | `event_type`、`kb_id`、`change_detail`、`version` |
| `tenants`、`tenant_memberships` | 租户与成员 | `status`、`role`、`revoked` |
| `projects`、`project_clients`、`project_api_keys`、`project_audiences`、`project_members` | 项目与接入注册 | `key_hash`、`key_prefix`、`audience`、`role` |
| `user_cache` | Keycloak 只读缓存 | `user_id`、`username`、`roles`、`groups`、`enabled`、`last_synced_at` |
| `global_permission_version` | 序列，非表 | 全局单调版本号 |

## 8. 认证与准入

| 端点组 | 认证方式 | 必需请求头 |
|--------|---------|-----------|
| `/v1/*` | 服务间预共享密钥 | `X-Api-Key`、`X-Client-Id`，决策面另需 `X-Request-Id` |
| `/api/v1/*` | 管理员 Bearer token | `Authorization: Bearer <JWT>` |
| `/healthz`、`/readyz`、`/metrics`、`/docs` | 无 | 无 |

管理员身份由 `get_current_admin` 解析：JWT 角色含 `system_admin`、`admin` 或 `platform_admin` 直接通过；否则回退查询 `project_members`（先按 `user_id`，未命中再经 `user_cache.username` 换取 Keycloak UUID 重查），仍无记录返回 403。

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
| 生产安全检查 | 每次启动 | 详见 §10.2 |
| 硬编码注册表迁移 | `projects` 表为空 | 建立 `rag-v14` 项目，写入 client_id `interactive-backend` / `retrieval` / `ingest`，audience `retrieval-worker` / `ingestion-worker` / `stamping-worker`，并把 `SERVICE_API_KEY` 哈希写入 `project_api_keys` |
| 策略目录迁移 | `policies/rag-v14/` 不存在且存在扁平目录 | 移动 `derived_roles/`、`resource_policies/`、`.versions/` |
| Keycloak 同步任务 | 每次启动 | 30 秒后首次执行，之后每 15 分钟一轮 |

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
| `KEYCLOAK_SERVER_URL` / `KEYCLOAK_REALM` / `KEYCLOAK_CLIENT_ID` | `http://localhost:8080` / `rag-v14` / `permission-service` | IdP 接入 |
| `KEYCLOAK_CLIENT_SECRET` / `_FILE` | 空 | 文件存在时优先于明文值 |
| `KEYCLOAK_ADMIN_USERNAME` / `_PASSWORD`（含 `_FILE`） | 空 | service account 不可用时的同步回退凭据 |
| `JWT_PUBLIC_KEY_PATH` | `./config/jwt_public.pem` | 校签公钥，缺失则启动失败 |
| `JWT_PRIVATE_KEY_PATH` | `./config/jwt_private.pem` | dev-login 签发用 |
| `JWT_ALGORITHM` / `JWT_EXPIRE_SECONDS` | `RS256` / `3600` | |
| `JWT_ALLOWED_ISSUERS` | 空 | 逗号分隔白名单，空表示不校验 |
| `DEV_LOGIN_PASSWORD` | `dev_password_2026` | 开发登录口令 |
| `SERVICE_API_KEY` / `_FILE` | 空 | 首次启动写入 `rag-v14` 项目的 API Key |
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

## 11. 业务系统接入

### 11.1 接入步骤

1. 在管理台 `/projects` 创建项目，登记 client_id、签发 API Key（仅签发时显示一次）、登记异步任务需要的 audience；
2. 通过 `GET /api/v1/projects/{id}/sdk-config` 获取 `base_url`、`client_ids`、`audiences` 与调用示例；
3. 业务系统安装 SDK：源码在 `perm-service-client/`，本地安装用 `pip install ./perm-service-client`，`sdk-config` 返回的示例命令 `pip install perm-service-client` 需要该包已发布到可用的索引源；
4. 在资源写路径接入生命周期端口，保证 `resource_registry` 与 `mount_registry` 与业务数据一致；
5. 在读路径接入决策面与投影面。

### 11.2 SDK

`perm_service_client.PermissionClient` 封装全部对外契约，自动注入 `X-Request-Id`、`X-Client-Id`、`X-Api-Key`，并在存在 OpenTelemetry span 时透传 `traceparent`。

```python
from perm_service_client import PermissionClient

client = PermissionClient(
    base_url="http://permission-service:18080",
    api_key="<project api key>",
    client_id="interactive-backend",
)

# 单条判定
result = client.check(credential=jwt, action="kb:read",
                      resource_type="kb", resource_id="kb-1")
if result["decision"] != "allow":
    raise PermissionError

# 检索前编译
prefilter = client.get_prefilter(credential=jwt)
if prefilter.get("suspended"):
    return []

# 检索后逐条复核
kept = client.filter_items(credential=jwt, items=[("document", doc_id), ...])

# 资源生命周期
client.register_resource(resource_type="kb", resource_id="kb-1",
                         owner="user:alice", tenant_id="tenant-dev",
                         project_id="rag-v14")
```

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

页面清单与目录结构见 `admin-console/README.md`。认证流程：登录后 token 存入 `localStorage.admin_token` 并写入 `admin_session` cookie；`middleware.ts` 在服务端按 cookie 拦截未登录访问，`AuthGuard` 在客户端二次校验；Axios 请求拦截器注入 `Authorization` 头、检查 token 过期（提前 5 分钟输出告警，已过期则清理并跳转登录），并在选定具体项目时自动附加 `project_id` 查询参数，选择“平台管理”模式（`__all__`）时不附加。

## 13. 可观测性

| 出口 | 内容 |
|------|------|
| `GET /metrics` | Prometheus 文本格式。内存计数器涵盖判定结果分布、调用失败、事件发布、Keycloak 同步；gauge 由数据库实时查询，包含活跃 ACL 数、活跃资源数、活跃限制数、变更日志总数、全局版本号 |
| OTel traces | FastAPI 自动埋点，加 `cerbos.check_resources` 手动 span，属性含 `request_id`、`resource_count`、`attempts`、`elapsed_ms`、`call_id` |
| 结构化日志 | structlog，关键事件包括 `client_id_rejected`、`api_key_rejected`、`project_mismatch`、`keycloak_sync_completed`、`security_config_warning` |
| `GET /healthz`、`/readyz` | 存活与就绪探针 |

## 14. 测试

```bash
cd permission-service
python -m pytest tests/test_api.py -v            # 端点行为，24 项
python -m pytest tests/test_joint_contract.py -v # 联合契约，J-1 至 J-20
```

两个套件都以 HTTP 方式打真实服务，运行前需启动 permission-service（默认 `http://localhost:18080`，可用 `TEST_BASE_URL` 覆盖）、PostgreSQL、Redis 与 Cerbos PDP。`tests/utils.py` 中的 `JWT_PRIVATE_KEY_PATH` 为绝对路径常量，在其他机器上运行需要改为本机私钥路径。

联合契约覆盖的场景包括：文档级授权反查检索可见性、型一与型二封禁生效、戳记不展开成员、KB 粒度授权传播、retire 级联清理、未注册资源判 deny、prefilter 接受 ctx_token、filter 批量上限、decision_id 可追溯、超时 fail-closed、版本号严格单调、事件持久化。

辅助脚本：

| 脚本 | 用途 |
|------|------|
| `scripts/cleanup_test_data.py` | 按测试资源命名前缀调用 retire 端点清理残留数据，支持 `--dry-run` |
| `scripts/verify_cerbos_policy_sync.sh` | 比对两侧 Cerbos 策略文件差异，路径为脚本内硬编码常量，使用前需修改 |

## 15. 目录结构

```
permission-service/          权限服务后端 (FastAPI)
├── app/                     配置、数据库、限流、中间件、可观测性、平台功能目录
├── api/                     路由：decision / projection / context / lifecycle
│                            + acl / role / role_definitions / restriction
│                            + resource / tenant / project / audit / auth
├── services/                jwt_parser、acl_resolver、cerbos_adapter、
│                            cerbos_policy_parser、stamp_calculator、event_publisher
├── models/                  SQLAlchemy 模型
├── schemas/                 Pydantic 请求与响应模型
├── idp/keycloak_sync.py     Keycloak 用户与组同步
├── migrations/              Alembic 迁移
└── tests/                   端点测试与联合契约测试

admin-console/               管理台前端 (Next.js 14)
perm-service-client/         Python SDK
cerbos/policies/{project}/   Cerbos 策略，按项目分命名空间
docs/                        设计文档与诊断记录
scripts/                     运维脚本
docker-compose.yml           权限服务、管理台、PostgreSQL、Redis
docker-compose.keycloak.yml  Keycloak
```

## 16. 已知约束

| 约束 | 影响 |
|------|------|
| `tests/utils.py`、`tests/conftest.py`、`scripts/verify_cerbos_policy_sync.sh`、`scripts/cleanup_test_data.py` 含绝对路径常量 | 换机运行需修改 |
| `docker-compose.yml` 的 `secrets` 段引用固定宿主机路径 | 部署前需按实际环境改写 |
| Cerbos PDP 与 Keycloak 不在主 compose 中 | 需独立启动并保证策略目录挂载一致 |
| `/v1/filter` 结果永久禁止缓存，`/v1/check` 默认不缓存 | 判定链路无缓存层，容量按 §8 限流值规划 |
| 自定义角色的 Cerbos YAML 写入失败不回滚角色创建 | 需检查 `cerbos_yaml_write_failed` 告警并手工补齐策略 |
| `prefilter` 的型二封禁检查逐 KB 查询 | KB 数量大时该端点查询次数随之线性增长 |
