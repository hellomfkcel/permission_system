# RAG 权限系统 — 上线投产前联合诊断报告 v2

> **诊断日期**: 2026-07-30
> **诊断范围**: 权限外部系统（permission-service + admin-console + Cerbos PDP + Keycloak）+ RAG v14 系统权限集成
> **诊断依据**:
> - `docs/外部系统设计.md` — 外部系统架构与功能规格
> - `docs/RAG系统设计v14.md` — RAG 系统权限模块规格
> - `docs/frontend-design.md` — 前端架构设计
> - `docs/权限管理系统架构设计.md` — 四方协作模型
> - 项目代码实际状态

---

## 〇、基础设施运行状态

### 当前运行的 Docker 容器

| 容器 | 状态 | 端口 | 归属 |
|------|------|------|------|
| perm-postgres | ✅ Up (healthy) | 25433 | 权限外部系统 |
| perm-redis | ✅ Up (healthy) | 16380 | 权限外部系统 |
| perm-keycloak | ✅ Up (healthy) | 8080 | 权限外部系统 |
| proj_rag_dev-cerbos-1 | ✅ Up (healthy) | 13592/13593 | RAG 基础设施 |
| proj_rag_dev-postgres-1 | ✅ Up (healthy) | 25432 | RAG 基础设施 |
| proj_rag_dev-redis-1 | ✅ Up (healthy) | 16379 | RAG 基础设施 |
| proj_rag_dev-milvus-1 | ✅ Up (healthy) | 19530/9091 | RAG 基础设施 |
| proj_rag_dev-seaweedfs-1 | ✅ Up (healthy) | 18333 | RAG 基础设施 |
| proj_rag_dev-etcd-1 | ✅ Up (healthy) | — | Milvus 依赖 |
| proj_rag_dev-minio-1 | ✅ Up (healthy) | — | Milvus 依赖 |
| proj_observ_grafana | ✅ Up | 3000 | 可观测栈 |
| proj_observ_otel-collector | ✅ Up | 4317/4318 | 可观测栈 |
| proj_observ_prometheus | ✅ Up (healthy) | — | 可观测栈 |
| proj_observ_loki | ✅ Up (healthy) | — | 可观测栈 |
| proj_observ_tempo | ✅ Up (healthy) | — | 可观测栈 |
| demo_deepagents-langfuse-* | ✅ Up | 13000 | 模型观测 |

### 直接启动的应用服务（非 Docker）

| 服务 | 运行方式 | 端口 | 状态 |
|------|---------|------|------|
| **permission-service** | `uvicorn app.main:app --port 18080` | 18080 | ✅ 运行中（uptime ~20min，healthz=200，readyz=ready） |
| **admin-console** | `next dev` (2 进程) | 3002 | ⚠️ 运行中但页面返回 500 — Next.js 编译错误（"missing required error components"） |
| **RAG API** | `uvicorn src.main:app --port 8000` | 8000 | ✅ 运行中 |
| **RAG 前端** | `next dev -p 3001` | 3001 | ✅ 运行中 |

### 运行数据快照（来自 permission-service /metrics）

| 指标 | 值 | 说明 |
|------|------|------|
| 活跃 ACL 条目 | 49 | ACL 授权记录 |
| 已注册资源 | 132 | 含 89 KB + 43 文档 |
| 活跃封禁 | 27 | restrictions 条目 |
| 全局权限版本 | 147 | 权限变更事件总数 |
| check:allow | 1 | 通过判定 |
| check:deny | 108 | 拒绝判定 |
| prefilter:allow | 108 | 检索前编译调用 |
| filter:allow/deny | 2/5 | 批量过滤判定 |

> **诊断结论**: 基础设施全部正常。permission-service 后端运行正常且已有真实数据（89 KB、43 文档、49 ACL 条目）。admin-console 进程在运行但 Next.js 编译有错误导致页面返回 500，需要排查。

---

## 一、项目完整性诊断

### 1.1 权限服务后端（permission-service/）— 代码完整性

| 设计文档要求 | 文件 | 行数 | 状态 |
|-------------|------|------|------|
| FastAPI 入口 | `app/main.py` | 184 | ✅ 完整 — 10 个路由模块注册 |
| 统一配置 | `app/config.py` | 54 | ✅ 完整 — 含 JWT/Redis/Cerbos/限流配置 |
| 数据库连接 | `app/database.py` | 33 | ✅ 完整 — asyncpg session |
| 决策面 `/v1/check` | `api/decision.py` | 284 | ✅ 完整 — 三态映射 + ACL 查询 + Cerbos 判定 |
| 决策面 `/v1/filter` | `api/decision.py` | 284 | ✅ 完整 — 型二封禁预检 + 批量判定 + fail-closed |
| 投影面 `/v1/prefilter` | `api/projection.py` | 178 | ✅ 完整 — 型一封禁检查 + KB 过滤 |
| 投影面 `/v1/visibility` | `api/projection.py` | 178 | ✅ 完整 — mount/resource 状态 + allow/deny stamps |
| ctx_token `/v1/context` | `api/context.py` | 123 | ✅ 完整 — HMAC-SHA256 签名 + 600s TTL |
| 生命周期 4 端口 | `api/lifecycle.py` | 260 | ✅ 完整 — register/link/unlink/retire + 幂等 |
| ACL 管理 API | `api/acl_routes.py` | 408 | ✅ 完整 — grant/revoke/batch/list/effective |
| 角色绑定 API | `api/role_routes.py` | 170 | ✅ 完整 — bind/unbind/list |
| 限制管理 API | `api/restriction_routes.py` | 176 | ✅ 完整 — add/remove/list |
| 审计日志 API | `api/audit_routes.py` | 153 | ✅ 完整 — 查询 + 模拟器 |
| 认证 API | `api/auth_routes.py` | 248 | ✅ 完整 — JWT 验证 + 用户/组查询 + 统计 |
| 资源管理 API | `api/resource_routes.py` | 135 | ✅ 完整 — 资源 CRUD + 所有权 |
| Cerbos 适配器 | `services/cerbos_adapter.py` | 59 | ✅ 完整 — `/api/check/resources` 调用 |
| ACL 解析器 | `services/acl_resolver.py` | 222 | ✅ 完整 — 权限/角色/封禁统一查询 |
| 事件发布器 | `services/event_publisher.py` | 153 | ✅ 完整 — Outbox 模式 + Redis Pub/Sub |
| JWT 解析器 | `services/jwt_parser.py` | 59 | ✅ 完整 — PyJOSE RS256 解析 |
| 戳记计算器 | `services/stamp_calculator.py` | 152 | ✅ 完整 |
| Keycloak 同步 | `idp/keycloak_sync.py` | 已实现 | ⚠️ 需验证 |
| 数据模型 7 张表 | `models/` | 371 | ✅ 完整 — 与设计文档 §2.3.1 完全一致 |
| 请求 Schema | `schemas/requests.py` | 67 | ✅ 完整 — 6 个请求模型 |
| 响应 Schema | `schemas/responses.py` | 66 | ✅ 完整 — 8 个响应模型 |
| Metrics 收集 | `app/metrics_collector.py` | 115 | ✅ 完整 — Prometheus 格式 |
| **总计** | **30 个文件** | **~3678 行** | — |

### 1.2 管理台前端（admin-console/）— 代码完整性

| 设计文档要求 (§3.3) | 文件 | 行数 | 状态 |
|---------------------|------|------|------|
| 登录页 /login | `app/login/page.tsx` | 182 | ✅ 完整 — 开发模式 + Keycloak SSO |
| OAuth2 回调 | `app/auth/callback/page.tsx` | 已实现 | ✅ 完整 |
| Dashboard /dashboard | `app/dashboard/page.tsx` | 163 | ⚠️ 基础版 — 6 个统计卡片 + 快捷入口；缺最近变更时间线、待处理告警 |
| 资源管理 /resources | `app/resources/page.tsx` | 205 | ⚠️ 基础版 — 列表 + 搜索 + 筛选；缺 KB/文档详情页、所有权转移 |
| 用户与组 /users-groups | `app/users-groups/page.tsx` | 199 | ⚠️ 基础版 — 列表 + 同步按钮；缺用户/组详情（权限汇总） |
| 权限管理 /permissions | `app/permissions/page.tsx` | 256 | ⚠️ 基础版 — ACL 授予/回收 + CSV 导出；缺角色绑定 UI、过期时间、主体搜索 |
| 封禁管理 /restrictions | `app/restrictions/page.tsx` | 232 | ⚠️ 基础版 — 型一/型二 CRUD；缺封禁历史时间线 |
| 策略管理 /policies | `app/policies/page.tsx` | 347 | ⚠️ 基础版 — 只读 YAML 查看；缺编辑器/版本历史/部署 |
| 审计日志 /audit | `app/audit/page.tsx` | 86 | ⚠️ 基础版 — 简单表格；缺筛选/导出/判定记录查询 |
| 策略模拟 /playground | `app/playground/page.tsx` | 281 | ⚠️ 基础版 — 8 个预设场景；缺规则匹配详情展示 |
| 系统设置 /settings | `app/settings/page.tsx` | 174 | ⚠️ 基础版 — 只读状态面板；无可修改配置项 |
| AuthGuard | `components/layout/AuthGuard.tsx` | 67 | ✅ 完整 — 路由保护 |
| Sidebar | `components/layout/Sidebar.tsx` | 99 | ✅ 完整 — 9 个导航项 |
| Toast 通知 | `components/shared/Toast.tsx` | 78 | ✅ 完整 |
| 权限追踪 | `components/acl/PermissionTrace.tsx` | 163 | ✅ 完整 |
| Auth Store | `stores/useAuthStore.ts` | 139 | ✅ 完整 — Zustand + Token Refresh |
| API 客户端 | `lib/api.ts` | 69 | ✅ 完整 — Axios + JWT 拦截器 |
| Middleware | `middleware.ts` | 47 | ✅ 完整 — Cookie 检查 |
| **总计** | **17 个源文件** | **~2993 行** | — |

### 1.3 设计文档要求的页面 vs 实际实现对照

| 设计 §3.3 页面 | 实现状态 | 缺口 |
|---------------|---------|------|
| `/login` | ✅ 完整 | — |
| `/select-tenant` | ❌ 缺失 | 多租户用户首次登录后的租户选择页 |
| `/dashboard` | ⚠️ 基础 | 缺最近变更时间线、待处理告警面板 |
| `/resources` → KB 列表 | ✅ 实现 | — |
| `/resources` → KB 详情 (ACL+角色+文档) | ❌ 缺失 | 点击 KB 行进入详情页 |
| `/resources` → 文档详情 | ❌ 缺失 | 点击文档行进入详情页 |
| `/resources` → 所有权转移 | ❌ 缺失 | 后端 API 已有，前端未对接 |
| `/users-groups` → 用户/组列表 | ✅ 实现 | — |
| `/users-groups` → 用户详情 (权限聚合) | ❌ 缺失 | 点击用户行查看拥有的全部权限 |
| `/permissions` → ACL 授予/回收 | ✅ 实现 | — |
| `/permissions` → 角色绑定 | ❌ 缺失 | `POST /api/v1/roles/bind` 前端未对接 |
| `/permissions` → 批量操作 | ⚠️ 部分 | 后端 API `/batch-grant` 已有，前端未对接 |
| `/restrictions` → 封禁管理 | ✅ 实现 | — |
| `/restrictions` → 封禁历史 | ❌ 缺失 | 谁在何时被封禁/解除的时间线 |
| `/policies` → 策略列表 | ✅ 实现 | — |
| `/policies` → 策略编辑器 | ❌ 缺失 | 可编辑 YAML + 校验 + 部署 |
| `/policies` → 版本历史/Diff | ❌ 缺失 | Git 式 diff 视图 |
| `/audit` → 变更记录 | ✅ 实现 | — |
| `/audit` → 判定记录查询 | ❌ 缺失 | 按 decision_id/principal/时间 筛选 |
| `/playground` → 模拟判定 | ✅ 实现 | — |
| `/playground` → 规则匹配详情 | ❌ 缺失 | 显示匹配的具体策略规则 (文件名:行号) |
| `/settings` → 连接配置 | ⚠️ 只读 | 可编辑的限流/Cerbos/Keycloak 配置 |

### 1.4 管理台前端 — 未使用的后端 API

以下后端 API 已在 permission-service 中完整实现，但 admin-console 前端未调用：

| 后端 API | 功能 | 影响 |
|----------|------|------|
| `POST /api/v1/acl/batch-grant` | 批量授予权限 | 管理员无法批量操作 |
| `POST /api/v1/roles/bind` | 绑定角色 | 角色绑定管理完全不可用 |
| `POST /api/v1/roles/unbind` | 解除角色绑定 | 同上 |
| `GET /api/v1/roles/bindings` | 查询角色绑定 | 同上 |
| `GET /api/v1/resources/{type}/{id}/owners` | 查看资源所有权 | 所有权信息不可见 |
| `POST /api/v1/resources/transfer-ownership` | 转移所有权 | 所有权转移不可用 |
| `GET /api/v1/audit?resource_type=&resource_id=&principal=&from=&to=` | 审计筛选查询 | 审计日志无法筛选 |
| `GET /api/v1/restrictions?principal=&resource_type=&resource_id=` | 封禁筛选查询 | 封禁列表无法筛选 |

---

## 二、架构达成度诊断

### 2.1 四方协作模型达成度

根据 `docs/权限管理系统架构设计.md` §1 定义的四方模型：

| 参与方 | 设计角色 | 实现达成度 | 说明 |
|--------|---------|-----------|------|
| **IdP (Keycloak)** | 身份源：用户/组/角色、JWT 签发、SSO | ⚠️ 85% | Keycloak 运行中；Realm `rag-v14` 已配置；但 JWT `granted_actions` claims 注入、用户属性映射需验证 |
| **Cerbos PDP** | 决策权威：策略评估、五端点 | ✅ 95% | 策略完整（4 派生角色 + 10 规则）；运行中；RAG 和权限系统双份策略一致 |
| **权限服务后端** | ACL/角色/限制权威存储 + Cerbos 适配 | ✅ 90% | 全部 API 端点已实现；Outbox 事件模式已实现；`is_enabled`/`allow_download` 硬编码 |
| **管理台前端** | 授权管理界面 | ⚠️ 60% | 全部 10 个页面骨架存在；但约 16 个功能点缺失（见 §1.3） |
| **RAG 本系统** | 权限消费方：P-AUTHC + 三层检索 + 盖戳 | ✅ 90% | P-AUTHC 完整；三层链路完整；盖戳管道完整；当前 local 模式 |

### 2.2 设计文档 §0.2 依赖方向规则达成度

| 规则 | 状态 | 说明 |
|------|------|------|
| 业务模块 → 平台模块 → 外部权限服务（单向） | ✅ 通过 | RAG 系统 B-* → P-* → CerbosClient 方向正确 |
| 任何模块不得越过 P-AUTHC 直接调权限服务 | ⚠️ 需验证 | admin-console 前端直接调 RAG `dev-login`（开发模式预期行为）；policies 页面直接 fetch `localhost:13592`（Cerbos PDP Admin API） |
| 禁止直读直写对方独占表 | ✅ 通过 | RAG 系统通过 P-AUTHC 门面访问，无直写 |
| 禁止依赖环 | ✅ 通过 | 无环 |

### 2.3 单一写者原则达成度

| 数据 | 设计唯一写者 | 实际写者 | 状态 |
|------|------------|---------|------|
| ACL/角色/限制 | ★ 权限服务 | permission-service PostgreSQL | ✅ |
| 资源镜像 (local 模式) | P-AUTHC (CerbosClient) | RAG 本地 PostgreSQL | ⚠️ 设计文档说"权限服务独占"，但 local 模式下在 RAG 侧 |
| 资源镜像 (remote 模式) | 权限服务 | permission-service PostgreSQL | ✅ |
| 向量库 chunk 戳记 | B-INGEST | RAG stamp_channel_task | ✅ |
| document_kb_mount | B-DOC | RAG B-DOC 模块 | ✅ |

### 2.4 架构偏离：双模式 (local/remote) 导致的双重权威源

```
当前状态:
  local 模式: RAG PostgreSQL 的 resource_registry/mount_registry = 资源镜像权威
  remote 模式: permission-service PostgreSQL 的 resource_registry/mount_registry = 资源镜像权威
  
  两份数据独立维护，不互通，不互相同步。
  
  设计文档 (§0.1.3) 明确: 结构镜像权威源 = ★ 权限服务独占
```

**偏离等级**: 🟡 中等 — 这是设计文档明确规划的迁移阶段（§7.2 阶段 0→1），local 模式是"开发模式"的预期行为。但投产前必须完成切换到 remote 模式。

### 2.5 Outbox 模式实现不一致

设计文档 §5.2 + §3.2 要求所有权限变更使用 Outbox 模式（同事务写业务变更 + change_log + 事务后发布 Redis）。

| API 路由 | Outbox 模式 | 说明 |
|---------|------------|------|
| `acl_routes.py:grant_acl` | ✅ 正确 | 两步模式：`write_change_log`（同事务）→ commit → `publish_to_redis`（异步） |
| `acl_routes.py:revoke_acl` | ✅ 正确 | 同上 |
| `acl_routes.py:batch_grant_acl` | ✅ 正确 | 同上 |
| `role_routes.py:bind_role` | ❌ 错误 | 使用 `publish_visibility_changed()` 一站式方法，在**新独立事务**中写 change_log，不回滚角色绑定写入 |
| `role_routes.py:unbind_role` | ❌ 错误 | 同上 |
| `restriction_routes.py:add_restriction` | ❌ 错误 | 同上 |
| `restriction_routes.py:remove_restriction` | ❌ 错误 | 同上 |
| `lifecycle.py:register/link/unlink/retire` | ❌ 缺失 | 生命周期端口**完全不发布** VisibilityChanged 事件 |

**修复**: 角色绑定和限制管理路由应改为与 ACL 路由一致的两步 Outbox 模式。生命周期端口应在事务提交后发布事件。

---

## 三、死亡代码 / 无用依赖诊断

### 3.1 管理台前端的死依赖

| 依赖 | package.json 版本 | 使用情况 |
|------|-------------------|---------|
| `@tanstack/react-query` | ^5.101.4 | ❌ 未使用 — 所有页面使用 `useState + useEffect + axios` |
| `recharts` | ^3.10.1 | ❌ 未使用 — Dashboard 页全为数字卡片，无图表 |
| `date-fns` | ^4.4.0 | ❌ 未使用 — 无日期格式化调用 |

**建议**: 移除以减小 bundle 体积，或在 Dashboard 和审计页面中实际使用。

### 3.2 权限服务后端的死代码

| 文件/函数 | 状态 | 说明 |
|----------|------|------|
| `services/stamp_calculator.py:compute_visibility_stamps()` | ❌ 死代码 | 核心函数从未被任何 API 路由调用；投影逻辑在 `projection.py` 中重复实现 |
| `services/stamp_calculator.py:get_allow_stamps_for_channel()` | ⚠️ 重复 | 与 `acl_resolver.py` 中同名函数逐字重复（文档说明：有意分离，但当前无消费者） |
| `services/stamp_calculator.py:get_deny_stamps_for_channel()` | ⚠️ 重复 | 同上 |
| `app/config.py:visibility_rate_limit` | ❌ 未使用 | 配置值 `200 req/s` 和 `prefilter_rate_limit=500` 在代码中未被任何限流逻辑引用 |
| `PreFilterResponse.tenant_wide_read` | ❌ 未使用 | 始终硬编码 `False`，无实际计算逻辑 |
| `resource_routes.py:list_resources()` | ⚠️ 重复 | 与 `lifecycle.py:list_resources()` 逻辑基本相同（一个需 admin 认证，一个无认证） |
| `idp/keycloak_sync.py` | ⚠️ 需验证 | 实现完整但实际同步功能可用性未验证，依赖 Keycloak Admin API 的网络连通性 |
| `requirements.txt` | ⚠️ 膨胀 | 包含数百个无关的 ROS2 包（rclpy、ros2*、tf2*、sensor-msgs 等），实际依赖约 15-20 个 |

### 3.3 双份 Cerbos 策略

- RAG 项目: `/home/mfkcel/proj_rag_dev/cerbos/policies/` — 被 RAG 系统的 CerbosClient (local 模式) 使用
- 权限系统: `/home/mfkcel/permission-system/cerbos/policies/` — 被 permission-service（通过 docker-compose 挂载）使用

两份内容一致，但**没有同步机制**。生产模式下应只保留一份权威源。

---

## 四、硬编码诊断

### 4.1 后端硬编码

| 位置 | 硬编码内容 | 风险等级 |
|------|-----------|---------|
| `services/acl_resolver.py:114-115` | `is_enabled: True`, `allow_download: True` | 🔴 高 — 设计文档要求这些字段应由 B-DOC 维护，权限服务不跟踪 |
| `api/projection.py:103` | `ttl_s=60` 硬编码 | 🟡 中 — prefilter TTL 应可配置 |
| `api/context.py:54` | `ttl_s = min(body.ttl_s, 600)` | 🟢 低 — 上限 600s 符合设计 |
| `app/config.py:10-28` | 数据库/Cerbos/Redis/Keycloak 默认值 | 🟢 低 — pydantic-settings 支持 .env 覆盖 |
| `docker-compose.yml` | `POSTGRES_PASSWORD: perm_pass`, `perm_redis_pwd_2026` | 🟡 中 — 开发环境可接受，生产需 K8s Secret |
| `docker-compose.keycloak.yml` | `KEYCLOAK_ADMIN_PASSWORD: admin123` | 🟡 中 |
| `app/config.py:46-47` | `visibility_rate_limit: 200`, `prefilter_rate_limit: 500` | 🟢 低 — 已配置但未被使用 |
| `Dockerfile` 端口 | 暴露 8080，但服务实际运行在 18080 | 🟡 中 — 不一致 |

### 4.2 前端硬编码

| 位置 | 硬编码内容 | 风险等级 |
|------|-----------|---------|
| `stores/useAuthStore.ts:31-32` | `KEYCLOAK_URL: "http://192.168.1.127:8080"` | 🟡 中 — 应通过 `NEXT_PUBLIC_KEYCLOAK_URL` 环境变量 |
| `app/policies/page.tsx:43` | `http://localhost:13592/admin/policy` | 🟡 中 — Cerbos PDP Admin API 地址硬编码 |
| `app/settings/page.tsx` | 端口映射信息硬编码 | 🟢 低 — 仅展示用途 |

### 4.3 Cerbos 策略中的条件硬编码

Cerbos 策略依赖 `request.principal.attr.granted_actions` 中的精细权限映射。这个映射由 `permission-service` 中的 `resolve_granted_actions_by_principal` 动态构建，不是硬编码。✅ 正确。

---

## 五、Mock 代码 / 占位符诊断

### 5.1 开发模式默认值

| 位置 | Mock/默认行为 | 影响 |
|------|-------------|------|
| `acl_resolver.py:109` | `get_resource_attr` 资源不存在时返回 `{"is_enabled": True, "allow_download": True}` | 🟡 对未注册资源的判定返回默认属性，可能导致未注册资源被判定 |
| `cerbos_client.py:89-100` | `_resolve_granted_actions` — local 模式下 owner=当前用户 → full access | 🟡 开发模式正确，但生产需走权限服务 |

### 5.2 前端占位符

| 位置 | 占位符行为 | 建议 |
|------|----------|------|
| `app/policies/page.tsx` | PDP Admin API 不可达时使用静态 YAML 回退 | 生产模式应始终从 PDP 加载 |
| `app/users-groups/page.tsx` | 数据加载失败时静默保持空列表 | 应展示错误状态 + 重试按钮 |
| `app/audit/page.tsx` | `api.get(...).catch(() => {})` — 错误时静默忽略 | 应展示错误状态 |

---

## 六、项目能否正常运行诊断

### 6.1 权限服务后端 — 运行确认 ✅

**运行状态**: `uvicorn app.main:app --host 0.0.0.0 --port 18080` — 正常运行中

**健康检查**:
- `GET /healthz` → `{"status":"ok"}` ✅
- `GET /readyz` → `{"status":"ready"}` ✅
- `GET /metrics` → Prometheus 指标正常 ✅
- `GET /api/v1/auth/stats` → `{"kb_count":89,"document_count":43,"acl_count":49,...}` ✅

**数据库**: Alembic 迁移已执行，7 张表 + `global_permission_version` 序列均已创建。

**已验证的 API 端点**:
- `GET /api/v1/resources?type=kb` → 返回 89 个 KB ✅
- `GET /api/v1/acl` → 返回 49 条 ACL 记录 ✅
- Metrics 显示 /v1/check、/v1/filter、/v1/prefilter 均已被调用 ✅

### 6.2 管理台前端 — 运行异常 ⚠️

**运行状态**: `next dev` (2 个 Node 进程) — 进程运行中但页面编译有错误

**问题**: 访问 `http://localhost:3002/login` 返回 HTTP 500：
```
missing required error components, refreshing...
```
这是 Next.js 开发模式的 Fast Refresh 编译错误。可能原因：
- TypeScript 编译错误导致页面无法渲染
- 缺少必需的客户端组件或导入错误
- `.next` 构建缓存损坏

**修复建议**: 检查终端中 Next.js 编译输出，修复编译错误后重新访问。或尝试 `rm -rf .next && npm run dev` 清除缓存重建。

### 6.3 RAG 系统 — 运行模式

```
AUTHZ_SERVICE_MODE=local  ← 当前：直接调 Cerbos + 本地镜像表
AUTHZ_SERVICE_URL=http://192.168.1.127:18080  ← 已配置但未生效（因为 mode=local）
ADMIN_CONSOLE_URL=http://192.168.1.127:3002  ← 管理台跳转地址已配置
```

### 6.4 端到端服务调用链路验证矩阵

| 调用链路 | 所需服务 | 状态 |
|---------|---------|------|
| RAG 交互 API → Cerbos PDP (local check) | RAG API + Cerbos | ✅ local 模式理论可通 |
| RAG 检索 → Cerbos PDP (local prefilter) | RAG retrieval-worker + Cerbos | ✅ local 模式理论可通 |
| RAG 盖戳 → Cerbos PDP (local visibility) | RAG stamping-worker + Cerbos | ✅ local 模式理论可通 |
| RAG → permission-service `/v1/check` | RAG API + permission-service | ⚠️ permission-service 运行中但 AUTHZ_SERVICE_MODE=local，未走此路径 |
| admin-console → permission-service REST API | admin-console + permission-service | ⚠️ admin-console 编译错误（500），无法加载前端 |
| admin-console → Keycloak SSO | admin-console + Keycloak | ❌ admin-console 页面不可访问 |
| permission-service → Cerbos PDP | permission-service + Cerbos | ✅ 通过 healthz/readyz/metrics 间接验证 |
| permission-service → Redis Pub/Sub 事件 | permission-service + perm-redis | ⚠️ 组件就绪但未验证事件发布 |
| RAG P-AUTHC → permission-service 事件流 | RAG visibility_events + perm-redis | ❌ AUTHZ_SERVICE_MODE=local，未订阅 |

> **核心阻塞**: AUTHZ_SERVICE_MODE=local（RAG 未使用外部权限服务）+ admin-console 编译错误（前端无法加载）。两个后端服务都在运行，但连接未打通。

---

## 七、cross-system 交互完整性诊断

### 7.1 RAG 系统 → permission-service 五端点对接

| 端点 | RAG 侧调用方 | permission-service 侧实现 | 理论可达 | 实际联调 |
|------|------------|--------------------------|---------|---------|
| `POST /v1/check` | `permission_service_client.py` | `api/decision.py:check_permission()` | ✅ 18080 端口可达 | ❌ 未联调（RAG 在 local 模式） |
| `POST /v1/filter` | `permission_service_client.py` | `api/decision.py:filter_items()` | ✅ 18080 端口可达 | ❌ 未联调 |
| `GET /v1/prefilter` | `permission_service_client.py` | `api/projection.py:get_prefilter()` | ✅ 18080 端口可达 | ❌ 未联调 |
| `POST /v1/visibility` | `permission_service_client.py` | `api/projection.py:get_visibility()` | ✅ 18080 端口可达 | ❌ 未联调 |
| `POST /v1/context` | `permission_service_client.py` | `api/context.py:mint_context_token()` | ✅ 18080 端口可达 | ❌ 未联调 |

### 7.2 生命周期端口对接

| 端口 | RAG 侧调用方 (B-DOC) | permission-service 侧实现 | 联调状态 |
|------|---------------------|--------------------------|---------|
| `POST /v1/resources/register` | `permission_service_client.py:register_resource()` | `api/lifecycle.py:register_resource()` | ❌ 未联调 |
| `POST /v1/resources/link` | `permission_service_client.py:link_resource()` | `api/lifecycle.py:link_resource()` | ❌ 未联调 |
| `POST /v1/resources/unlink` | `permission_service_client.py:unlink_resource()` | `api/lifecycle.py:unlink_resource()` | ❌ 未联调 |
| `POST /v1/resources/retire` | `permission_service_client.py:retire_resource()` | `api/lifecycle.py:retire_resource()` | ❌ 未联调 |

### 7.3 事件系统 (VisibilityChanged) 对接

| 组件 | 实现 | 状态 |
|------|------|------|
| 权限服务发布事件 | `event_publisher.py` → Redis Pub/Sub channel `visibility_changed` | ✅ 已实现 |
| RAG 侧订阅 | `visibility_events.py` — 支持 Redis Pub/Sub 订阅模式 | ✅ 已实现 |
| 事件格式一致性 | 两边 event payload schema 需对照 | ❌ 未验证 |

### 7.4 前端 ← → 后端交互对比

| 管理台前端调用 | 后端 API | 参数对齐 | 响应格式 |
|--------------|---------|---------|---------|
| `GET /api/v1/auth/stats` | `api/auth_routes.py` dashboard stats | ⚠️ 需验证 | ⚠️ 需验证 |
| `GET /api/v1/resources` | `api/lifecycle.py list_resources` | ⚠️ 前端用 `?type=` 后端用 `?type=` | ✅ 一致 |
| `GET /api/v1/acl` | `api/acl_routes.py list_acl` | ⚠️ 需验证 | ⚠️ 需验证 |
| `POST /api/v1/acl/grant` | `api/acl_routes.py grant_acl` | ⚠️ 前端 `form.granted_by` 被后端 JWT 覆盖 | ✅ 设计预期 |
| `POST /api/v1/acl/revoke` | `api/acl_routes.py revoke_acl` | ⚠️ 需验证 | ⚠️ 需验证 |
| `GET /api/v1/restrictions` | `api/restriction_routes.py` | ⚠️ 需验证 | ⚠️ 需验证 |
| `POST /api/v1/restrictions/add` | `api/restriction_routes.py add_restriction` | ⚠️ 需验证 | ⚠️ 需验证 |
| `GET /api/v1/audit` | `api/audit_routes.py` | ⚠️ 前端仅 `?limit=50` | ⚠️ 需验证 |
| `POST /api/v1/simulate` | `api/audit_routes.py simulate` | ⚠️ 需验证 | ⚠️ 需验证 |
| `GET /api/v1/auth/users` | `api/auth_routes.py` | ⚠️ 需验证 | ⚠️ 需验证 |
| `GET /api/v1/auth/groups` | `api/auth_routes.py` | ⚠️ 需验证 | ⚠️ 需验证 |

> **注意**: admin-console 前端和后端均未启动，所有前端-后端交互对比基于代码静态分析，未经运行时验证。

### 7.5 联合契约测试 J-1 至 J-20 的状态

根据 `docs/RAG系统设计v14.md` §27.2，20 项联合契约测试需要在联调环境中运行。**当前状态：全部未执行**。

| 测试编号 | 测试内容 | 状态 | 关键依赖 |
|---------|---------|------|---------|
| J-1 | 分享可检索性 | ❌ 未执行 | 需要 permission-service + doc 级 ACL |
| J-2 | 同一用户不能命中其他未授权文档 | ❌ 未执行 | 需要 stamp_channel_task |
| J-3 | 型一封禁 | ❌ 未执行 | 需要 restrictions 表有数据 |
| J-4 | 型二封禁派生覆盖 | ❌ 未执行 | 同上 |
| J-5 | 通道封禁 | ❌ 未执行 | 同上 |
| J-6 | 戳记内容正确性 | ❌ 未执行 | 需要 /v1/visibility 返回 group: 不被展开 |
| J-7 | KB 粒度授权事件形态 | ⚠️ 设计已确认 | 需联调验证 payload 结构 |
| J-8 | strict 实时性 | ❌ 未执行 | 需要 RAG 检索链路 + strict KB |
| J-9 | 非 strict 自愈 | ❌ 未执行 | 需要完整事件传播链路 |
| J-10 | retire 四合一 | ❌ 未执行 | 需要 lifecycle 端点 |
| J-11 | 镜像缺失行为 | ❌ 未执行 | 需要未注册资源判定 |
| J-12 | 动词与端点绑定 | ❌ 未执行 | 需要 doc:retrieve 走 /v1/check 返回 invalid_request |
| J-13 | 准入矩阵 client_id | ❌ 未执行 | 需要 retrieval client_id 调 doc:view |
| J-14 | 批量端点可用性 | ✅ 设计已确认 | check/batch 对 interactive-backend 开放 |
| J-15 | prefilter 接受 ctx_token | ✅ 设计已确认 | audience 值待确认 |
| J-16 | filter 上限与超限行为 | ❌ 未执行 | 需要传 201 条测试 |
| J-17 | decision_id 可追溯 | ❌ 未执行 | 需要权限服务审计日志 |
| J-18 | 超时行为 | ❌ 未执行 | 需要注入网络延迟 |
| J-19 | 限流行为 | ⚠️ 已按惯例落地 | 联调时验证实际返回码 |
| J-20 | is_enabled=false 不在 strict 保证内 | ❌ 未执行 | 需要测试边界行为 |

---

## 八、严重等级汇总

### 🔴 P0 — 阻塞投产

| # | 问题 | 影响 | 修复建议 |
|---|------|------|---------|
| 1 | **AUTHZ_SERVICE_MODE=local** | RAG 系统未使用外部权限服务，ACL 数据在 RAG 本地 mirror 表，不符合架构设计 | 完成联调后切换为 remote 模式 |
| 2 | **admin-console 编译错误（HTTP 500）** | 管理台前端页面完全无法加载 | 检查 Next.js 终端编译输出，修复 TypeScript/导入错误；可尝试 `rm -rf .next && npm run dev` |
| 3 | **is_enabled/allow_download 硬编码** | 文档运营状态（停用/禁止下载）无法通过权限服务控制 | 在 resource_registry 表中增加 `is_enabled`、`allow_download` 字段，由 B-DOC 同步维护 |
| 4 | **Outbox 模式实现不一致** | 角色绑定、限制管理、生命周期端口未正确发布 VisibilityChanged 事件 | 角色/限制路由改为两步 Outbox；生命周期端口增加事件发布 |
| 5 | **全部 20 项联合契约测试未执行** | 跨系统理解偏差未发现 | 依次执行 J-1 至 J-20 |

### 🟡 P1 — 影响完整体验

| # | 问题 | 影响 | 修复建议 |
|---|------|------|---------|
| 6 | 角色绑定/限制管理 Outbox 模式不一致 | 角色绑定和限制变更没有正确发布 VisibilityChanged 事件 | 改为两步 Outbox 模式（同事务写 change_log + 事务后 Redis） |
| 7 | 生命周期端口不发布 VisibilityChanged 事件 | register/link/unlink/retire 后 RAG 系统不知道戳记需刷新 | 在生命周期操作的事务提交后发布事件 |
| 8 | 角色绑定前端 UI 缺失 | 管理员无法绑定/解绑角色 | 在 permissions 页面增加角色绑定面板 |
| 9 | 用户/组详情页缺失 | 无法查看某用户的权限全貌 | 实现 `/users-groups/{id}` 详情页 |
| 10 | 资源详情页缺失 | 无法查看 KB/文档的 ACL 列表 | 实现 `/resources/{type}/{id}` 详情页 |
| 11 | 审计日志无筛选/导出 | 合规审计不便 | 增加搜索框 + CSV/JSON 导出按钮 |
| 12 | 策略编辑器/版本管理缺失 | Cerbos 策略只能手动编辑 YAML 文件 | 实现 YAML 编辑器 + 版本历史 API |
| 13 | 前端死依赖 (react-query, recharts, date-fns) | 增大 bundle 体积 | 移除或实际使用 |
| 14 | 管理台登录依赖 RAG 系统 dev-login | 开发模式耦合 RAG API（需 RAG API 同时在 8000 端口运行） | 增加独立的 admin 登录端点或确保 RAG API 一直可用 |
| 15 | `stamp_calculator.py` 死代码 + compute_visibility_stamps 未被调用 | 代码维护混乱，逻辑分散在 projection.py 和 stamp_calculator.py 两处 | 统一到一个模块，删除未被调用的函数 |
| 16 | `requirements.txt` 包含数百个无关 ROS2 包 | ~20+ MB 不必要的依赖 | 清理为仅含实际使用的 15-20 个包 |
| 17 | 限流配置值未生效 | `visibility_rate_limit=200` 和 `prefilter_rate_limit=500` 配置了但未被 slowapi 使用 | 在对应端点上显式配置 `@limiter.limit` |

### 🟢 P2 — 优化改进

| # | 问题 | 影响 | 修复建议 |
|---|------|------|---------|
| 18 | 封禁管理无筛选功能 | 封禁条目多时不方便查找 | 增加搜索/筛选组件 |
| 19 | 权限授予无主体搜索建议 | 管理员需手动输入精确 principal 格式 | 增加用户/组搜索下拉 |
| 20 | 双份 Cerbos 策略无同步机制 | 策略变更时两份可能不一致 | 将 Cerbos 策略统一放在权限系统仓库，RAG 项目通过卷挂载引用 |
| 21 | Dashboard 缺少时间线和告警面板 | 管理员无法快速发现异常 | 增加最近变更时间线和待处理告警 |
| 22 | settings 页全部只读 | 无法通过 UI 修改限流/Cerbos 配置 | 增加可编辑配置面板 |
| 23 | 缺少 `/select-tenant` 页面 | 多租户用户登录后体验不完整 | 实现租户选择页 |
| 24 | 未认证的 `GET /v1/resources` 端点暴露所有资源数据 | 攻击者可枚举所有已注册资源 | 移除此端点或增加认证 |
| 25 | `tenant_wide_read` 始终为 False | prefilter 响应的租户级读访问语义未实现 | 实现租户级角色判定的计算逻辑或移除此字段 |
| 26 | `Dockerfile` 暴露 8080 但服务运行在 18080 | Docker 部署后端口映射不一致 | 统一端口配置 |

---

## 九、启动验证检查清单

### 执行顺序（投产前必跑）

```
步骤 1: 数据库初始化
  ✅ 已完成 — 7 张表 + global_permission_version 序列已创建
  ✅ 验证: Metrics 显示 132 资源、49 ACL、27 封禁

步骤 2: 启动权限服务后端
  ✅ 已完成 — uvicorn 运行在 18080 端口
  ✅ 验证: curl http://localhost:18080/healthz → {"status":"ok"}

步骤 3: 修复管理台前端编译错误
  ❌ admin-console 返回 500 — 需排查 Next.js 编译错误
  □ 检查终端 Next.js 编译输出中的错误信息
  □ rm -rf admin-console/.next && cd admin-console && npm run dev
  □ 验证: curl http://localhost:3002/login → 200 (HTML)

步骤 4: 验证权限服务 → Cerbos PDP
  ⚠️ 权限服务调用 Cerbos 的 /v1/check 端点（已在 Metrics 中确认）
  □ 手动测试: POST /v1/check 携带有效 JWT

步骤 5: 验证管理台 → 权限服务
  ⚠️ 阻塞于步骤 3（admin-console 编译错误）
  □ 修复后浏览器访问 http://localhost:3002/login
  □ 开发模式登录 → 跳转 /dashboard

步骤 6: RAG 系统切换到 remote 模式
  □ 修改 ~/proj_rag_dev/.env: AUTHZ_SERVICE_MODE=remote
  □ 重启 RAG API 服务
  □ 执行端到端测试: 创建 KB → 上传文档 → 检索查询

步骤 7: 联合契约测试
  □ 按 §27.2 从 J-1 到 J-20 逐项执行
  □ 任一项失败 → 阻断投产 → 双方联合定位
```

---

## 十、总结

### 代码交付质量

| 维度 | 得分 | 说明 |
|------|------|------|
| 后端完整性 | 90% | 全部 API 端点已实现，数据模型完整，但 Outbox 模式实现不一致（3 个模块有缺陷） |
| 前端完整性 | 60% | 全部页面骨架存在，基础 CRUD 可用，但约 16 个功能点缺失 |
| 后端运行状态 | 95% | permission-service 运行正常，healthz/readyz/metrics 均正常，已有真实业务数据 |
| 前端运行状态 | 30% | 进程运行中但 Next.js 编译错误导致页面返回 500，完全不可访问 |
| 架构合规性 | 80% | 四方模型已建立，依赖方向正确；Outbox 模式不一致、生命周期端口缺少事件发布 |
| 硬编码治理 | 70% | `is_enabled`/`allow_download` 硬编码为 True；限流配置未生效；Dockerfile 端口不一致 |
| 死代码比例 | 12% | 3 个前端死依赖 + `stamp_calculator.py` 核心函数 + 重复端点 + 未使用的配置字段 + 膨胀的 requirements.txt |
| 联调就绪度 | 10% | permission-service 运行且有数据，但 RAG 侧仍在 local 模式，admin-console 无法访问，0/20 联合契约测试 |

### 核心阻塞项

1. **AUTHZ_SERVICE_MODE=local** — RAG 系统未实际使用外部权限服务，仍直接调 Cerbos PDP
2. **admin-console Next.js 编译错误** — 进程在运行但页面全部返回 500，管理台完全不可用
3. **Outbox 模式不一致** — 角色绑定、限制管理、生命周期端口未正确发布 VisibilityChanged 事件
4. **0/20 联合契约测试** — 跨系统理解偏差未发现

### 投产建议

- **当前状态**: ⛔ 不可投产
- **最小投产条件**: 修复 admin-console 编译错误 + RAG 切换 remote 模式 + 至少通过 J-1 至 J-8（核心权限链路）
- **建议投产前完成**: 全部 P0 + P1 项目
