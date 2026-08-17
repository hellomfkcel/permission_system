# 权限平台 模块访问控制 系统级联调诊断报告 v16

> **诊断日期**：2026-08-17
> **诊断方法**：**真实栈联调**（无 mock、无硬编码、不 skip 调用链）
> **诊断范围**：12 个管理台功能模块的访问控制、设计原则符合性
> **前序报告**：v14（角色语义/隔离）、v15（死代码）
> **设计依据**：`docs/permission_model_v2.md` + 用户给出的 0.1/0.2 原则与模块分类

---

## 〇、联调环境（真实,非 mock）

按用户"不 mock、不硬编码、不 skip 环节"的要求,搭建了完整真实栈:

| 组件 | 版本/配置 | 状态 |
|---|---|---|
| PostgreSQL | 16, UTF8, 端口 25433 | 17 个 Alembic 迁移全部跑通（含 v14 新增的 f1a2b3c4d5e6 / a7b8c9d0e1f2 / b8c9d0e1f2a3） |
| Redis | 端口 16380, 带密码 | ✓ |
| Cerbos | 0.38.1, disk 驱动指向真实策略目录 | **11 个策略全部编译通过** |
| permission-service | uvicorn, 端口 18080 | `/readyz` = ready（DB+Redis+Cerbos 全通） |

**凭据**：用服务的 RSA 私钥签发真实 RS256 JWT —— 与 dev-login 在 Keycloak 验密后
签发的是同一种凭据,不是 mock。所有调用走真实 HTTP,经完整
`JWT → parse_principal → platform_authorizer → Cerbos → 响应` 链路。

**两种身份**：
- 平台管理员：`roles=[system_admin, user]`
- 项目管理员：`palice`，仅 `roles=[user]`，经真实 API 加为 rag-v14 的
  `project_members`（role=project_admin）

---

## 一、结论速览

| 维度 | 结果 |
|---|---|
| 模块功能连通性 | ✅ 平台管理员访问全部 13 个模块端点均 200，无 5xx |
| 端到端 ABAC 判定 | ✅ grant → `/v1/check` → allow 全链路正常 |
| 审计项目打标（F9） | ✅ ROLE_BOUND 带 project_id，TenantCreated 为平台级 |
| 设计原则 0.1/0.2 | ✅ 无 DENY / 无 permissions 列 / 无 policy_synced 存储 / 无 Keycloak 角色副本 |
| **模块访问控制** | ❌ **发现 2 个高危 + 1 个中危 + 2 个低危** |

用户对模块的分类要求与实测不符,存在**权限提升**与**项目管理员无法履职**两类问题。

---

## 二、高危发现（实证）

### G1 · tenant_mgmt 无平台守卫 → 权限提升 🔴

**要求**：租户管理只有平台级能看到。

**实测**：项目管理员 palice（非平台管理员）直接调 `POST /api/v1/tenants` **成功创建了平台级租户**：

```
POST /api/v1/tenants  (Bearer=palice, roles=[user])
→ 201 {"id":"palice-tenant","created_by":"user:palice",...}
平台管理员查列表 → ['palice-tenant']   # 确认落库
```

**根因**：`api/tenant_routes.py` 全部端点只有 `get_current_admin`，
**无 `require_platform_permission("tenant_mgmt", ...)`**。任何通过管理员认证的用户
（含项目成员）都能列举、创建、修改、删除租户。前端侧边栏隐藏了入口，
但后端 API 完全敞开 —— 隐藏 ≠ 鉴权。

**影响**：项目级管理员可越权操作平台级资源。这是真实的横向/纵向越权。

---

### G2 · 项目管理员看不到 5/7 个应见模块，且全部只读 🔴

**要求**：资源管理、角色管理、权限管理、封禁管理、策略管理、审计日志、策略模拟
（7 个）—— 项目可见。

**实测**：palice（project_admin）的 `GET /api/v1/auth/me/access` 返回：

```
is_platform_admin: False   project_ids: ['rag-v14']
可访问功能:
  dashboard      [platform:read]
  resource_mgmt  [platform:read]
  role_mgmt      [platform:read]
  user_mgmt      [platform:read]
```

对照要求：

| 模块 | 要求项目可见 | 实测 | 缺口 |
|---|---|---|---|
| resource_mgmt | ✓ | read | 只读 |
| role_mgmt | ✓ | read | 只读 |
| permission_mgmt | ✓ | **DENY** | ✗ 完全不可见 |
| restriction_mgmt | ✓ | **DENY** | ✗ |
| policy_mgmt | ✓ | **DENY** | ✗ |
| audit_mgmt | ✓ | **DENY** | ✗ |
| playground | ✓ | **DENY** | ✗ |

执行层实测一致确认（palice 令牌）：

```
role_mgmt 写 POST /roles/definitions      → 403  (只有 read,不能建角色)
permission_mgmt GET /acl                  → 403
audit_mgmt GET /audit                     → 403
playground POST /simulate                 → 403
```

**根因**：
1. `platform.yaml` 的 `project_member_baseline` 规则把可见模块**硬编码**为
   `[dashboard, resource_mgmt, user_mgmt, role_mgmt]` 且只授 `platform:read`。
2. `platform_authorizer._project_member_role` 对**所有** project_members 一律返回
   合成角色 `project_member`，**不区分 project_admin 与普通成员** ——
   `project_members.role` 字段（palice 是 project_admin）在平台鉴权层被丢弃。

**影响**：项目管理员无法管理自己项目的权限/策略/封禁,无法查看本项目审计,
无法用策略模拟器。与"项目可见这 7 个模块"的设计直接冲突。这不是隐藏,是功能缺失。

---

## 三、中危发现

### G3 · 可见性与执行不一致 —— GET /policies 🟡

**实测**：palice 的 me/access **无 policy_mgmt**（G2），但

```
GET /api/v1/policies?project_id=rag-v14  (Bearer=palice)  → 200
```

**根因**：`list_policy_files`（`api/audit_routes.py`）只有 `get_current_admin` +
`get_project_scope`，**无 `require_platform_permission("policy_mgmt", "platform:read")`**。
同模块的写/上传/删除端点都有该守卫,唯独列表读端点漏了。

**影响**：侧边栏说项目管理员看不到策略管理,但 API 能读到本项目策略全文 ——
可见性契约（me/access）与执行契约（端点守卫）不一致。属"自相矛盾"一类。

> 注：修 G2 后若项目管理员本应能读 policy_mgmt,则此端点也应补 `platform:read`
> 守卫使两者一致,而不是保持"无守卫"。

---

## 四、低危发现

### G4 · settings/config 无平台守卫 🟢

`GET /api/v1/auth/config`（对应 settings 页面）只有 `get_current_admin`,
palice 可读（200）。settings 设计为平台专属。该端点只返回只读运行时配置
（端口、限流值、资源类型标签等）,不含敏感数据,故列低危。仍应补
`require_platform_permission("settings", "platform:read")` 以与分类一致。

### G5 · 单一来源潜在重复 —— parent_keycloak_roles 🟢

`role_definitions.parent_keycloak_roles`（DB）与策略文件的 `parentRoles` 是**同一事实
两处存**。自定义角色创建时 `write_role_policies` 写策略文件、`RoleDefinition` 行又写
一份 parent_keycloak_roles。

按原则 0.1（每类数据只有一个出处）,"派生角色由哪个身份角色激活"属于策略结构,
应只在策略文件。当前展示层 `describe_role` 已优先取策略文件、DB 仅作 fallback,
故影响有限,但双写本身违反单一来源。建议 DB 不再存该列,展示统一从策略解析。

---

## 五、符合设计原则的部分（实测确认）

| 原则 | 实测 |
|---|---|
| 策略文件无 DENY | ✓ 全库 grep 无 EFFECT_DENY |
| 策略文件无 user_id/授权记录 | ✓ 仅注释中有 `user:alice` 示例 |
| DB 无 role_definitions.permissions 列 | ✓ 实际列：id/name/description/parent_keycloak_roles/is_system/project_id/created_at/updated_at |
| 无 policy_synced 存储 | ✓ 仅为计算响应字段,不入库 |
| 模型无 Keycloak 用户角色副本 | ✓ users/role_definitions 均不存 realm_access |
| 平台权限单一判定路径 | ✓ me/access 全部经 Cerbos platform.yaml,无第二套 if 分支 |
| 端到端 ABAC | ✓ grant kb:read → /v1/check → allow |
| 审计项目打标 | ✓ ROLE_BOUND.change_detail.project_id = rag-v14 |

---

## 六、修复建议

| 编号 | 修复 | 是否需设计确认 |
|---|---|---|
| G1 | tenant_routes 全部端点加 `require_platform_permission("tenant_mgmt", read/write)` | 否,明确 |
| G3 | `GET /policies` 加 `require_platform_permission("policy_mgmt", "platform:read")` | 否,明确 |
| G4 | `GET /auth/config` 加 `require_platform_permission("settings", "platform:read")` | 否,明确 |
| G2 | ① `_project_member_role` 按 `project_members.role` 区分 `project_admin` / `project_member`；② `platform.yaml` 授 project_admin 这 7 个模块（read+write），project_member 授只读子集 | **是** —— 需确认 project_admin 与普通成员各自的模块集与读写 |
| G5 | DB 移除 `role_definitions.parent_keycloak_roles`,展示统一从策略解析 | 是 —— 涉及 schema 变更 |

**G2 的关键设计问题**（需用户拍板）：
- 项目管理员（project_admin）应拿这 7 个模块的读+写吗？
- 普通项目成员（project_members.role=member）应拿什么？（只读子集？还是不可见？）
- 平台层的 feature 权限是"能进这类模块",具体项目由数据层 `project_id` 过滤 ——
  所以给 project_admin `policy_mgmt:write` 不会让他改别的项目,写操作仍被
  `_assert_namespace_access` 限定在自己项目。这个模型是成立的。

---

## 七、未覆盖

- 前端页面渲染未测（无 `npm run build`/浏览器）；本报告只测 API 层访问控制。
- 多项目隔离在决策层已测通（palice 只见 rag-v14），但未构造第二项目做交叉验证。
- 真实 Keycloak 未接入（用等价的 RS256 JWT 直签,不影响鉴权链路结论）。

---

> **结论**：模块本身功能正常、设计原则 0.1/0.2 基本落实、端到端判定链路健康。
> 但**模块访问控制与用户给出的分类要求存在实质偏差**：tenant_mgmt 越权可写（G1）、
> 项目管理员缺 5 个应见模块且无写权（G2）、policies 读端点可见性与执行不一致（G3）。
> G1/G3/G4 是明确的守卫缺口可直接修；G2 的修复需先确认 project_admin 与普通成员的
> 模块与读写边界。
