# 权限服务系统级联调测试报告 v17

> 测试日期：2026-08-17
> 环境：真实全栈（PostgreSQL + Redis + Cerbos PDP + FastAPI service），不 mock、不硬编码、
> 不为跑通绕过架构。所有判定都走真实链路（Cerbos 判定 + DB 授权数据 + 中间件准入）。
> 承接 v16（G1–G4 修复）。本轮在联调中新发现并修复越权缺陷 **G5 / G6 / G7 / G8**，
> 并针对其共性根因加了一道结构性静态门禁（第十节）。

## 一、测试环境与夹具

| 组件 | 端点 | 状态 |
| --- | --- | --- |
| permission-service | 127.0.0.1:18080 | ready |
| Cerbos PDP | 127.0.0.1:13592（disk 驱动，watchForChanges） | SERVING |
| PostgreSQL | 127.0.0.1:25433（UTF8） | ok |
| Redis | 127.0.0.1:16380 | PONG |

两个项目用于跨项目隔离对照：

- `rag-v14`：ABAC + ACL 三路模型（kb / document / rag_roles）。
- `demo2`：静态角色模型（oa_roles）。

四个测试主体（真实 RS256 JWT，等价于 dev-login/Keycloak 后签发）：

| 主体 | 身份 | 来源 |
| --- | --- | --- |
| admin | system_admin | realm 角色 |
| palice | project_admin @ rag-v14 | project_members |
| pviewer | project_viewer @ rag-v14 | project_members |
| bob | project_admin @ demo2 | project_members |

模块分类（沿用 0.1/0.2 设计约定）：平台专属 = project_mgmt / tenant_mgmt / settings；
项目内 7 模块 = resource_mgmt / role_mgmt / permission_mgmt / restriction_mgmt /
policy_mgmt / audit_mgmt / playground；dashboard 为登录首页只读。

---

## 二、测试 1 —— system_admin 全模块管理 + 策略上传越权防护

### 2.1 模块可达性

`GET /api/v1/auth/me/access` 返回全部 12 功能读写；逐个模块入口 GET 均 200：

project_mgmt / tenant_mgmt / settings / user_mgmt / dashboard /
resource_mgmt / role_mgmt / permission_mgmt / restriction_mgmt /
policy_mgmt / audit_mgmt —— 11/11 = 200。

### 2.2 策略上传越权防护（本测试的关键问题）

即便是 system_admin（合法的平台写权限），也不能借"项目级策略上传"把项目级写权限
兑换成平台层准入。防护由 `_assert_policy_layer` + `_assert_no_module_id_conflict`
承担，与"谁在上传"无关：

| 尝试 | 期望 | 实测 |
| --- | --- | --- |
| A) 把 `platform` 资源策略上传进 rag-v14 目录 | 拒 | **422** platform-layer resource may only be defined under policies/platform/ |
| B) 把 `project_permission` 资源上传进 rag-v14 目录 | 拒 | **422** 同上 |
| C) 在 demo2 目录里重声明 rag-v14 已有的 `kb` 模块 | 拒 | **409** Cerbos module IDs are global; edit that file instead |
| D) 在 demo2 目录里重声明 `rag_roles` 派生角色集合名 | 拒 | **409** pick a project-scoped name |
| E) 正常上传一个项目内唯一命名的资源策略到 rag-v14 | 允许 | **200** created；删除 200 |

结论：目录不是命名空间，Cerbos 模块 ID 全局唯一——平台层资源与跨项目撞名都被前置
拦截；合法的项目内策略操作不受影响。**测试 1 通过。**

---

## 三、测试 2 —— project_admin 项目管理 + 跨项目/平台越权

### 3.1 模块分级（承接 G2）

palice `me/access`：项目 7 模块读写 + dashboard 只读，**无** project_mgmt /
tenant_mgmt / settings / user_mgmt 的授权（features 目录仍列全量作为标签字典，
授权以 permissions 为准）。

### 3.2 本轮新发现缺陷

#### G5：读端点显式 project_id 缺少范围校验（跨项目读取泄漏）

**现象**：palice（rag-v14 管理员）对 demo2 的读端点返回 200 而非 403：

```
GET /resources?project_id=demo2        -> 200
GET /roles/definitions?project_id=demo2 -> 200
GET /restrictions?project_id=demo2      -> 200
GET /audit?project_id=demo2             -> 200
（仅 policies 因走 _assert_namespace_access 正确 403）
```

**根因**：`require_platform_permission("<module>")` 只回答"能不能进这个模块"，
回答不了"能不能看这个项目"。列表/详情端点在**显式传入 project_id**时，直接把它落进
过滤条件而不校验范围——`elif` 分支里的 scope 过滤只在不传 project_id 时才生效。
写端点（bind / grant / restriction / role-def create）早已用 `scope.can_access`
挡住，读端点系统性遗漏。

**修复**：在 `api/auth_routes.py` 新增共享校验 `assert_project_scope(scope, project_id)`，
对每个显式接收 project_id 的读端点补齐：

- `resource_routes`：list_resources、transfer_ownership、get_resource_owners
- `role_definitions_routes`：list_role_definitions、get_role_definition、get_permissions_matrix
- `role_routes`：list_bindings
- `restriction_routes`：list_restrictions
- `acl_routes`：list_acl
- `audit_routes`：list_audit、simulate

对不传 project_id 的矩阵/模拟端点：平台管理员看全部，非平台管理员必须显式限定项目
（否则等于跨项目取数），返回 400。

#### G6：平台层 ACL grant 未校验平台管理员（项目管理员自授平台权限，完全越权）

**现象**（最严重）：palice 直接调用

```
POST /api/v1/acl/grant
{ resource_type: "platform", resource_id: "tenant_mgmt", action: "platform:write", ... }
-> 200
```

随后 palice 的 `me/access` 多出 `tenant_mgmt: [read, write]`，
`GET /tenants` 从 403 变 200，`POST /tenants` 返回 **201**——项目管理员越权成了
平台租户管理员。

**根因**：`_validate_grant_scope` 的 `layer == "platform"` 分支只校验了"project_id 必须
为空"和"动作已声明"，**没有校验调用者是平台管理员**。平台层 ACL 的判定路径按
`project_id IS NULL` 读取（`platform_authorizer._platform_feature_grants`），因此一条
项目管理员签发的平台 ACL 会被平台判定直接采纳。而 `require_platform_permission
("permission_mgmt","write")` 每个项目管理员都持有（用于管自己项目的 ACL），于是成了
越权入口。

**修复**：平台层分支加 `if not scope.is_platform_admin: raise 403`。平台层 ACL 是把平台
功能委托给非管理员，只有平台管理员能签发；批量端点 `batch_grant_acl` 共用同一校验，
一并覆盖。

### 3.3 修复后复测

| 用例 | 修复前 | 修复后 |
| --- | --- | --- |
| palice 读 rag-v14 全部 7 模块 | 200 | 200（不受影响）|
| palice 读 demo2 resources/roles/restrictions/audit/acl | **200 泄漏** | **403** |
| palice 读 demo2 policies | 403 | 403 |
| palice 矩阵/模拟不传 project_id | 全项目 | **400**（须限定项目）|
| palice `POST /acl/grant` platform 资源 | **200 越权** | **403** |
| palice `me/access` 是否含 tenant_mgmt | **是（越权后）** | 否 |
| palice `GET/POST /tenants` | **200/201** | **403/403** |
| bob 读 demo2（自有项目）| 200 | 200 |
| bob 读 rag-v14（跨项目）| — | **403** |
| admin 读任意项目 + 平台层 ACL grant | 200 | 200（正向对照不受影响）|

其它越权路径（本轮一并核实，均 403/拒）：
palice 把平台资源策略写进自己项目（422）、上传到别的项目（403）、写 platform.yaml 根
目录（403）、在 demo2 建角色/绑角色/授 ACL（403）、把自己加进 demo2 成员（403）。
palice 在**自己项目** rag-v14 内建角色（201）属正常职责，非越权。

**测试 2 通过**（含 G5 / G6 修复与复测）。

---

## 四、测试 3 —— 普通成员（project_viewer）只读 + 越权检查

pviewer `me/access`：dashboard / resource_mgmt / role_mgmt 三项**只读**，无写、无其它
模块（对应 platform.yaml 的 `project_member_readonly` 规则）。

| 类别 | 用例 | 实测 |
| --- | --- | --- |
| 可读模块（rag-v14）| resources、roles/definitions | 200 |
| 不可见模块 | permission / restriction / acl / policies / audit | 403 |
| 写/越权 | 建角色、绑角色、授 ACL、授平台 ACL、加封禁、模拟、上传策略 | 全部 **403** |
| 跨项目 | demo2 resources / roles | 403 |

**测试 3 通过**：只读成员无任何写面与越权能力。

---

## 五、测试 4 —— 策略模拟器（playground）作用域隔离

模拟器走**真实判定链路**（resolve_granted_actions + get_resource_acl +
check_subject_ban + Cerbos），非读原始 YAML。经 G5 收口后按项目隔离：

| 主体 | 用例 | 实测 |
| --- | --- | --- |
| palice | 模拟 rag-v14（自有）| **200**，decision=allow，matched_rules 命中 kb.yaml，cerbos_verdict=EFFECT_ALLOW |
| palice | 模拟 demo2（跨项目）| **403** No access to project 'demo2' |
| palice | 模拟不传 project_id | **400** 须限定项目 |
| bob | 模拟 rag-v14（跨项目）| **403** |
| admin | 模拟任意/不限项目 | **200** |

**测试 4 通过**：项目管理员只能在自己项目内用本项目策略做演示，看不到其它项目/平台。

---

## 六、测试 5 —— /api 与 /v1 接口联调（含外部系统鉴权）

### 6.1 管理台 /api（Bearer JWT）

前四项测试全程以 curl + Bearer token 验证，等价于前端调用路径——`/api/v1/*` 管理端点
可经前端与 curl 调用。

### 6.2 外部系统 /v1（X-Api-Key + X-Client-Id）

外部系统（如 rag-v14）经 `ClientIdValidationMiddleware` 用 API Key（`project_api_keys`
表）+ Client-Id（`project_clients` 表）解析出 project_id，判定期按此隔离。

| 用例 | 实测 |
| --- | --- |
| `POST /v1/check` 有效 key + 已注册 client | **200** 真实 Cerbos 判决 |
| `POST /v1/check` 无 API Key | **401** |
| `POST /v1/check` 错误 API Key | **401** |
| `POST /v1/check` 未注册 Client-Id | **403** |
| `POST /v1/context` 铸 ctx_token | **200** |
| `GET /v1/resources`（生命周期查询）| **200** |

### 6.3 端到端授权闭环（/api 授权 → DB → /v1 判定 → Cerbos）

1. alice 对 kb-1 无授权：`/v1/check` → **deny**
2. admin 经 `/api/v1/acl/grant` 授 alice `kb:read`@kb-1（rag-v14）→ 200
3. 同一 `/v1/check` → **allow**（判决翻转，证明全链路真实生效）
4. 用 rag-v14 的 key 配 demo2 的 client → **403**（key 与 client 项目不匹配）
5. 清理：revoke → 200

**测试 5 通过**：/api 与 /v1 双通道可用，外部鉴权与项目隔离在判定层成立。

---

## 七、修复清单与回归

### 7.1 代码改动

| 缺陷 | 文件 | 改动 |
| --- | --- | --- |
| G5 | `api/auth_routes.py` | 新增 `assert_project_scope(scope, project_id)` 共享校验 |
| G5 | `api/resource_routes.py` | list / transfer_ownership / get_resource_owners 补范围校验 |
| G5 | `api/role_definitions_routes.py` | list / detail / permissions-matrix 补校验（矩阵加平台守卫）|
| G5 | `api/role_routes.py` | list_bindings 补校验 |
| G5 | `api/restriction_routes.py` | list_restrictions 补校验 |
| G5 | `api/acl_routes.py` | list_acl 补校验 |
| G5 | `api/audit_routes.py` | list_audit、simulate 补校验 |
| G6 | `api/acl_routes.py` | `_validate_grant_scope` 平台层分支加 `is_platform_admin` 校验 |
| G7 | `api/audit_routes.py` | 事件重放按项目范围收口；抽出 `_audit_project_conditions` 共享给审计查询 |

### 7.2 回归确认

- 最终四主体访问矩阵（见下）与设计完全一致：admin 全通；palice 仅 rag-v14；
  pviewer 仅 rag-v14 只读子集；bob 仅 demo2。
- 平台管理员访问不受任一修复影响（正向对照全部保留）。
- 全部测试产生的临时数据（越权 ACL、越权租户、测试策略文件、测试 API Key、探针角色）
  均经真实 API 清理；策略目录无残留。
- 后端 `py_compile` 全部通过。

最终访问矩阵（HTTP 状态码）：

```
端点(除注明外均 rag-v14)               admin  palice  pviewer  bob
GET /tenants        [平台]              200    403     403      403
GET /projects                          200    403     403      403
GET /resources?rag-v14                 200    200     200      403
GET /roles/definitions?rag-v14         200    200     200      403
GET /roles/permissions?rag-v14         200    200     403      403
GET /acl?rag-v14                       200    200     403      403
GET /policies?rag-v14                  200    200     403      403
GET /audit?rag-v14                     200    200     403      403
GET /resources?demo2  [跨项目]         200    403     403      200
GET /roles/definitions?demo2 [跨项目]  200    403     403      200
GET /policies?demo2   [跨项目]         200    403     403      200
GET /roles/permissions（不传 pid）     200    400     403      400
```

---

## 八、结论

五个测试场景全部通过。系统满足：单一数据源三层授权、项目严格隔离、无跨项目/平台数据
泄漏、各身份无越权路径、/api 与 /v1 双通道及外部系统鉴权正常。

本轮联调新暴露并修复三处越权缺陷：**G5**（读端点显式 project_id 缺范围校验，导致跨项目
读取泄漏）、**G6**（平台层 ACL grant 未校验平台管理员，导致项目管理员可自授平台权限、
完全越权到租户/项目管理）、**G7**（事件重放缺项目范围收口）。三者均属"模块级准入通过、
但项目级/层级校验缺失"的同一类问题，修复后判定链路在模块准入之外补齐了项目范围与层级
两道关口。

---

## 九、追加排查 —— G7 事件重放（/v1/events/replay）跨项目问题

v17 初版把重放列为"遗留观察待定"，本节承接排查并完成修复。

### 9.1 缺陷确认（真实栈复现）

`POST /api/v1/events/replay`（audit_mgmt:write）按**全局版本号**重放 `permission_changes`，
原实现对事件流无任何项目过滤。两条泄漏路径：

- **读**（dry_run，默认）：响应回带 `change_detail`（含 principal、resource、project_id）。
  播种一条 demo2 的 `RESTRICTION_ADDED`（principal=user:mallory）后，palice（rag-v14
  管理员）`replay from_version=1` 实测能看到该 demo2 事件：

  ```
  total: 11 | by project: {'rag-v14': 4, 'demo2': 1, None: 4, '<none>': 2}
  demo2 events VISIBLE to palice: 1  (v11 RESTRICTION_ADDED principal=user:mallory)
  ```

- **写**（dry_run=false）：把匹配到的事件重新打进共享的 Redis `visibility_changed`
  频道。项目管理员据此可republish其它项目的事件，波及别项目的 RAG 订阅方。

根因与 G5 同类：`require_platform_permission("audit_mgmt","write")` 只校验模块准入，
项目管理员均持有该权限；重放端点未叠加项目范围过滤。

### 9.2 修复

project_id 存于 `change_detail` JSONB（与审计查询同源）。抽出共享条件构造器
`_audit_project_conditions(scope, project_id)`，统一三段范围语义并加固空项目集：

- 显式 project_id → `assert_project_scope` 校验后按该项目过滤；
- 平台管理员不传 → 不过滤（全部项目）；
- 非平台管理员不传 → 限定到自己的项目集合；无项目归属返回 `false()`（一条不可见）。

未带 project_id 的事件（平台层 / 早期未归档）对非平台管理员一律不可见。审计查询
`list_audit_entries` 与重放 `replay_events` 共用该构造器（顺带修掉审计查询里
空项目集会漏过滤的隐患）。重放请求体新增可选 `project_id`。

重放语义仍是项目内 补消费（RAG 侧订阅断开后按本项目版本流补发），只是范围收口到
调用者的项目——不改为平台专属，与"audit_mgmt 是项目模块"的定位一致。

### 9.3 修复后复测

| 主体 | 用例 | 实测 |
| --- | --- | --- |
| palice | replay dry_run（不传 pid）| 仅 rag-v14 4 条，**无 demo2/未归属事件** |
| palice | replay dry_run=false（真实 republish）| replayed=4，全部 rag-v14 |
| palice | replay 显式 project_id=demo2 | **403** |
| bob | replay dry_run | 仅 demo2 1 条 |
| pviewer | replay（无 audit_mgmt:write）| **403** |
| admin | replay dry_run | 全部 11 条（含 demo2 + 未归属）|
| admin | replay 显式 project_id=demo2 | 仅 demo2 1 条 |

回归：审计查询 `GET /audit` 经共享构造器重构后范围不变（palice 仅 rag-v14、bob 仅
demo2、admin 全量、palice 显式 demo2→403）。`py_compile` 通过；测试播种数据经真实 API
清理。**G7 修复完成，重放不再存在跨项目问题。**

---

## 十、根因追问 —— "模块级准入通过、项目级校验缺失"是什么问题，能不能系统修

用户在 G5–G7 后追问：这类问题到底是什么性质，联调为什么发现不了、修不彻底？本节回答，
并把"逐个打补丁"升级为"结构性门禁 + 穷举审计"。

### 10.1 问题的性质：两个正交维度，强制力不对称

授权有两个正交维度，每个数据端点都要同时过：

1. **模块准入**（能不能进这个模块）—— 由 `require_platform_permission(feature, action)` 等
   **强制声明式依赖**把守，写在函数签名里，不写就没有认证，**忘不掉**。
2. **项目/层级范围**（能不能操作这个项目、这一层的数据）—— 长期靠每个端点**手写内联校验**
   （`scope.can_access` / JSONB 过滤 / 命名空间校验），散落各处，**极易漏写**。

G5–G8 都是同一个结构性缺陷的症状：**维度 1 有单一强制点，维度 2 没有**。写端点当初记得手写、
读端点忘了，就成了"模块级准入通过、但项目级校验缺失"。这不是几个编码 bug，是架构层面
缺一个统一强制点。

### 10.2 为什么联调不足以修这一类

联调**能**发现（本轮发现了四个），但结构上**不足以**根治：

- 测试只能证明**被测路径上有 bug**，证明不了**未测路径上没有 bug**。
- 空间是组合爆炸：端点 N × 角色 M × 项目 K ×（显式 project_id / 隐式 / 路径 / body / 资源反查）。
  抽样命中几个，剩下取决于恰好测了哪些组合。
- 逐个打补丁 = 打地鼠。正确做法是**穷举 + 把"易漏"变成"漏了就红"**。

### 10.3 G8 —— 穷举审计立刻暴露的一大片（联调完全没碰到）

静态穷举所有路由后，`project_routes.py` 一整片项目基础设施端点只挂 `get_current_admin`
（而 `get_current_admin` 对**任意项目的任意成员**都放行），既无平台守卫也无项目范围校验：

| 端点 | 原守卫 | 真实栈复现（palice=rag-v14 管理员 打 demo2）|
| --- | --- | --- |
| POST `/{pid}/api-keys` | get_current_admin | **201 铸出 demo2 的 API Key** |
| GET `/{pid}/api-keys` | get_current_admin | 200 列出 demo2 全部 key |
| POST/DELETE `/{pid}/clients` | get_current_admin | 201/204 改 demo2 的 client 注册 |
| POST/DELETE `/{pid}/audiences` | get_current_admin | 201/204 改 demo2 的 audience |
| DELETE `/{pid}/members/{uid}` | require_project_member（缺 admin 判定）| 只读成员即可删他人 |

其中**铸 API Key 是最严重的**：API Key 是 `/v1` 外部鉴权凭证。实测 palice 用铸出的 demo2 key
配注入的 demo2 client 调 `POST /v1/check`，返回 200 真实判决 —— 即以 demo2 身份进入判定链路，
**跨项目完整攻陷外部鉴权面**。连**只读成员 pviewer** 也能给 rag-v14 铸 key（201）。

联调（测试 1–5）完全没碰这片端点，因此一个都没发现；穷举审计一次全暴露。

**修复**（按 0.1/0.2 分类，`project_mgmt` 属平台专属）：

- provisioning **变更**（建/删 client、签发/吊销 API Key、建/删 audience）→ `require_platform_admin`
  （project_mgmt:write）。签发凭证是平台级 provisioning，项目成员不得自签，杜绝为别项目铸凭证。
- provisioning **读取**（列 API Key/audience）→ `require_project_member`，限本项目、不返回明文 key。
- `remove_project_member` 补上与 `add_project_member` 同口径的 `project_admin` 判定。

复测：palice 打 demo2 全部 403；pviewer 建 key/改 client 403；palice 给**本项目**铸 key 也 403
（provisioning 平台专属）；项目成员可读本项目 key 列表（200）；平台管理员照常 provisioning（201）。

### 10.4 结构性门禁：`tests/test_authz_scope_guard.py`

把"项目级校验易漏"变成"漏了就 CI 红"。纯静态源码解析（不起服务、不连库），穷举所有路由
处理函数，两道检查：

1. **零强制**：凡触及项目级数据（带 project_id 形参，或引用带 project_id 列的模型）的端点，
   必须出现至少一个范围/权属强制标记（assert_project_scope / require_project_member /
   require_platform_admin / _caller_project / …）。→ 抓 G7/G8 那种整段没守卫的形态。
2. **显式 project_id 未校验**：凡签名里出现调用方直接提交的 `project_id`（Query/Form），必须出现
   **主动**校验标记（assert_project_scope / can_access / _assert_namespace_access / …），而不能只有
   `is_platform_admin`/`filter_condition` 这类被动过滤。→ 抓 G5/G6 那种"某分支有范围逻辑、但显式
   project_id 分支绕过"的形态。

豁免走显式 `ALLOWLIST`，每条注明理由（平台专属 / 服务鉴权 / 无状态 / 公开），并有第三个测试
防止 ALLOWLIST 残留失效条目。**新增端点漏了项目校验，这三个测试直接红。**

**门禁当场又抓出一个联调与人工穷举都漏掉的 G5 实例**：`acl_routes.py::get_effective_permissions`
——`if project_id:` 分支直接落过滤、未 `assert_project_scope`，palice 可读 demo2 的有效权限。
已修复并复测（palice demo2→403、本项目→200、admin→200）。这正是结构性门禁优于人工审查的证明：
人工"穷举"仍漏了一个，静态门禁没漏。

### 10.5 本轮范围收口总账（真实栈四主体，HTTP 状态码）

```
端点(除注明外均 rag-v14)                     admin  palice  pviewer  bob(demo2)
GET  acl/effective?rag-v14                    200    200     403      403
GET  acl/effective?demo2   [跨项目]           200    403     403      200
GET  auth/stats?demo2      [跨项目]           200    403     403      200
GET  auth/users?demo2      [跨项目]           200    403     403      200
GET  auth/recent-changes?demo2 [跨项目]       200    403     403      200
GET  projects/demo2/api-keys [跨项目]         200    403     403      200
POST projects/demo2/api-keys [铸凭证]         201    403     403      403
POST projects/rag-v14/api-keys [本项目铸凭证] 201    403     403      403
```

### 10.6 结论

"模块级准入通过、项目级校验缺失"是一个**横切关注点缺乏统一强制点**的架构问题，不是若干孤立
bug。联调只能抽样发现症状（G5/G6/G7），穷举静态审计才暴露出最严重的一片（G8 铸凭证跨项目攻陷），
而**结构性门禁**把这一类从"靠记性"变成"漏了就红"，并当场再抓出一个人工漏掉的实例
（get_effective_permissions）。

三道防线各司其职：Cerbos 判定管模块准入（维度 1），`assert_project_scope`/`require_*` 管项目与
层级（维度 2），`test_authz_scope_guard.py` 管"维度 2 别再漏写"。离线静态测试合计 24 项全过。

遗留设计点（待用户确认，非安全阻断）：provisioning（API Key/client/audience）现按 `project_mgmt`
平台专属处理，项目管理员不能自助签发本项目凭证，需平台管理员代办。若要放开项目管理员自助，
可把这几个变更端点从 `require_platform_admin` 调整为"项目管理员 + 本项目范围"，属设计取舍。
