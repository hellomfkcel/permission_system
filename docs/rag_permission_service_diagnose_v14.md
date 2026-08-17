# 权限平台 角色语义 / 数据来源 / 项目隔离 诊断与整改报告 v14

> **诊断日期**：2026-08-17
> **诊断范围**：权限平台自身（Cerbos 策略结构、权限服务后端、管理台前端、数据库 schema）
> **诊断方法**：代码走查 + 策略解析器离线复现 + 数据库约束比对；判定链路的真实联调结论沿用 v13
> **整改分支**：`claude/permission-system-role-diagnosis-m87gt4`
> **设计依据**：
> - `docs/permission_model_v2.md`（本轮产出的目标模型，整改的唯一依据）
> - `docs/外部系统设计.md`
> - `docs/权限管理系统架构设计.md`
> - `docs/manage_role_design.md`（其中"角色权限双写"部分已被 v2 取代）

---

## 〇、报告导读

本轮诊断分两批，处理结果一并记录在此：

| 批次 | 内容 | 提交 |
|------|------|------|
| 第一批 | 角色语义混淆、平台/项目未解耦、策略与 DB 数据交集（问题 1–8） | `945e00b` |
| 第二批 | 项目隔离专项扫描（F1–F10） | `5f5e5d4` |

第一批的**目标模型**写在 `docs/permission_model_v2.md`，本文只记录"发现了什么、
怎么处理的、验证到什么程度"。两者不重复，改动细节以 v2 文档为准。

---

## 一、第一批：角色语义与数据来源

### 1.1 核心症结

系统里存在两类本质不同的角色，却用同一套"父/子继承"的 UI 呈现：

| 角色类型 | 来源 | 真实语义 |
|---|---|---|
| 身份角色 | Keycloak | 平台入场资格，本身不持项目权限 |
| 派生角色 | Cerbos | 真正的权限持有者，`parentRoles` 只是"被哪个身份角色**激活**" |

`parentRoles` 是激活条件，不是权限继承。把两者混同，就出现了"子角色继承了 user，
权限却只有 4 个，而 user 有 14 个"这类看起来自相矛盾的展示。其余现象都是这个
误解叠加作用域计算错误的衍生结果。

### 1.2 问题清单与处理结果

| # | 问题 | 根因 | 处理 |
|---|------|------|------|
| 1 | 无策略项目显示内置角色 + rag 数据 | 平台级角色的有效权限按**全局并集**算，未按查询项目过滤 | 动作改按 `(角色, 命名空间)` 二元索引，查询时只合并该项目 + 全局命名空间 |
| 2 | 角色卡权限数与详情不一致 | 角色卡取全局矩阵，详情按项目过滤，两个作用域 | 三处展示统一走 `/definitions?project_id=`，同源同作用域 |
| 3 | 子角色"继承"父角色却只有 2 个权限 | 把激活关系当继承关系 | 取消 `parentRoles` 权限并集；UI 改称"激活角色"，并标注激活方式 |
| 4 | 自定义角色看起来无用 | 同上：父角色默认 `user` 且 `user` 显示为全权 | `user` 项目层权限归零后，自定义角色成为真正的新权限组合 |
| 5 | 双重角色（派生+父）的并集断裂 | 纯父角色算并集、双重角色只算自身，两套语义 | 统一为"只算策略直接授予的动作"，不存在第二套算法 |
| 6 | 矩阵出现重复条目 | 同一角色既在 `roles` 字段出现、又被引作 `parentRoles`，被列成两条 | 矩阵按角色名去重，附 `kind` 区分派生/身份 |
| 7 | DB 角色名与策略角色名脱节 | `platform_auditor` 等在策略中不存在 | 平台角色写进 `platform.yaml`；清理策略中已不存在的角色定义 |
| 8 | `user`/`system_admin` 混入平台权限 | 平台策略挂在项目目录且 import 项目派生角色 | 平台层独立为 `policies/platform/` 命名空间，不 import 任何项目派生角色 |

### 1.3 用户提出的五点架构问题

| 提出的问题 | 处理 |
|---|---|
| 项目策略文件写得有问题 | rag-v14 重写为 ABAC + ACL 三路并行，权限只授派生角色，全库无显式 DENY |
| 权限平台存在逻辑问题 | 解析器、角色端点、判定链路组装逻辑全部重写（见 1.2 与 §三） |
| `platform.yaml` 为何放在具体项目中 | 移到全局唯一的 `policies/platform/`，不随项目增减 |
| 平台权限与项目权限未解耦 | 三层模型：平台层认 Keycloak 角色、项目权限数据层认 `granted_actions[project_id]`、项目业务层认派生角色 |
| 数据不一致，策略与 DB 有交集 | 删除 `role_definitions.permissions` 列；ACL 复用 `acl_entries` 不另建表；平台权限判定改由 Cerbos 单一路径 |

### 1.4 落实"设计底线"

> 策略文件描述"结构"，DB 记录"事实"，Cerbos 做"决策"。
> 没有一类数据同时存在于两个地方，没有第二个判断路径，没有需要手动同步的状态。

| 底线 | 违反项 | 处理 |
|---|---|---|
| 无重复数据 | `role_definitions.permissions` 是策略文件的副本 | 迁移 `f1a2b3c4d5e6` 删列，权限实时从策略解析 |
| 无第二判断路径 | `auth_routes` 手写了一份"角色 → 平台功能"映射，与 `platform.yaml` 并存 | 删除手写映射，统一由 Cerbos 判定（`services/platform_authorizer.py`），Cerbos 不可达时 fail-closed 503 |
| 无手动同步状态 | 策略写入后把权限同步进 DB | 同步降级为"建档"（名称、激活角色、项目归属），不写权限 |

### 1.5 顺带修复的判定链路缺陷

这三处都是"展示与判定脱节"排查过程中发现的真实判定错误：

| 位置 | 缺陷 | 后果 |
|---|---|---|
| `/v1/check/batch` | `granted_actions` 以 `resource_id` 为键，策略按 `kb_id` 查找 | 批量判定与单条判定结果可以不一致 |
| `/v1/filter` | 把单篇文档的 `doc:retrieve` 映射成整个 KB 的 `read` | 一条文档级授权放大成 KB 级检索可见性 |
| 策略模拟器 | 把 `{principal: [actions]}` 当 `granted_actions` 传给 Cerbos | 键是主体不是资源作用域，派生角色永远匹配不上 |

三处已统一走 `resolve_granted_actions()` + `get_resource_acl()`，与 `/v1/check` 同源。

### 1.6 与参考方案的两处偏离

整改整体按用户给出的参考方案执行，以下两处经权衡后未照搬，理由记录在案：

**（1）未新建 `resource_acl` 表。**
参考方案建议新建 `resource_acl(resource_kind, resource_id, principal_type,
principal_id, actions[])`。但既有 `acl_entries` 的 `(principal, resource_type,
resource_id, action)` 已经完整表达同一件事，再建一张表就是让同一类事实存在于
两处 —— 与本轮的目标相反。ACL 路直接读 `acl_entries`：`role:` 前缀进
`role_acl`，其余进 `acl`。

**（2）`platform.yaml` 保留了按功能委托的授权读取。**
参考方案要求平台层"不读任何授权记录"。但按功能委托（把单个管理模块授权给非
管理员）是在用的能力，删掉是实打实的功能损失。折中做法是用 `roles: ["user"]` +
条件表达，与 Layer 2 的委托规则同一写法，仍不 import 任何项目派生角色，
满足解耦目标。

---

## 二、第二批：项目隔离专项扫描

### 2.1 两条决定性前提

扫描结论都建立在这两条事实上：

1. **不同项目允许使用相同的资源 ID。** 迁移 `d4f1a7c9b2e3` 把
   `resource_registry` 的唯一约束改成了 `(project_id, resource_type, resource_id)`，
   这是明确的设计选择。因此任何按裸 `resource_id` / `doc_id` / `kb_id` 匹配的
   查询，不带项目条件就会命中别的项目的数据。
2. **Cerbos 的模块 ID 是全局的。** 资源策略按 `(resource, version, scope)`、
   派生角色集合按 `name` 唯一，**跨整个策略根目录**。`policies/{project}/`
   只是文件组织方式，不构成命名空间。

### 2.2 缺陷清单与处理结果

#### 🔴 高危

| 编号 | 缺陷 | 后果 | 处理 |
|---|---|---|---|
| F1 | 项目级自定义角色可对平台层资源生成策略 | 生成物与 `platform/platform.yaml` 同模块 ID；且派生角色条件恒为真、父角色为 `user` —— 要么 Cerbos 拒绝加载导致平台准入整体失效，要么所有登录用户获得平台权限 | `resolve_resource_types` 改用 `resources_owned_by(project)`，只认本项目自有资源类型 |
| F2 | 派生角色集合名 `custom_roles` 全局硬编码 | 两个项目各有一个自定义角色即产生两个同名集合，`importDerivedRoles` 解析到哪份不确定 | 集合名改为 `custom_roles_{project}`；写入时自动改写同项目内引用旧名的遗留文件 |
| F3 | `mount_registry` 无 `project_id`，唯一约束全局 | link 幂等误判、跨项目建不了同名组合、retire 级联误伤、visibility 误判 unmounted、prefilter 反查串项目 | 加列 + 唯一约束改 `(project_id, doc_id, kb_id)` + 五处查询加项目条件 |
| F4 | 平台资源的 ACL 读取不按项目过滤 | 带项目的 `platform` 授权被平台判定路径读到，项目级写权限升级为平台级 | 读取端加 `project_id IS NULL`；写入端按资源所在命名空间校验层级 |

#### 🟡 中危

| 编号 | 缺陷 | 处理 |
|---|---|---|
| F5 | `/api/v1/policies*` 全系列不做项目范围校验，且列表无差别返回全部项目策略 | 六个端点（列表 / 写入 / 上传 / 删除 / 版本 / diff）加命名空间访问控制；平台层与策略根目录仅平台管理员可写；列表按可见范围过滤 |
| F6 | `/acl/effective` 的角色绑定展开不按项目、且用全局角色→动作映射 | 绑定按条目所属项目过滤，映射按同一项目解析 |

#### 🟢 低危

| 编号 | 缺陷 | 处理 |
|---|---|---|
| F7 | 解析器按角色名单键，同名派生角色跨项目互相覆盖激活条件与归属 | 改 `(角色, 命名空间)` 索引（此项是第一批引入的缺陷） |
| F8 | `_binding_counts` 不按项目统计，同名角色绑定数相加 | 按项目统计，平台级绑定一并计入 |
| F9 | `permission_changes` 无项目列，四类事件根本没写 `project_id` | `write_change_log` 的 `project_id` 改为**必填参数**，在唯一入口统一写入 |
| F10 | 模块 ID 全局唯一但无人校验 | CI 增加重复检查；策略写入/上传端点冲突直接 409 |

#### 扫描中额外发现（不在原编号内）

| 缺陷 | 处理 |
|---|---|
| 生命周期端点完全信任请求体的 `project_id` —— 持 A 项目凭据可把资源登记进 B 项目 | 项目以凭据为准（`lifecycle._caller_project`），请求体只能复述，不一致 403 |
| 管理台授权对话框写死项目兜底值，平台功能授权因此被挂到该项目名下 | 去掉兜底值；平台层资源不带 `project_id`，项目层资源要求先切换到目标项目 |

### 2.3 确认正确、未作改动的部分

- **运行时判定链路**：`/v1/*` 的 `project_id` 来自 API key 与 client_id 的双向
  校验（`client_validator.py`），跨项目混用返回 403 `project_mismatch`。
- `acl_entries` / `role_bindings` / `resource_registry` / `role_definitions`
  的唯一约束已按项目隔离（`d4f1a7c9b2e3`）。
- `acl_resolver` 全部查询接受并应用 `project_id`。
- `/v1/resources` 生命周期列表按调用方项目过滤，且拒绝无过滤条件的枚举。
- 管理面的角色绑定、ACL、封禁、资源列表均走 `scope.filter_condition()`。

---

## 三、整改产物索引

### 3.1 策略文件

| 路径 | 变更 |
|---|---|
| `cerbos/policies/platform/resource_policies/platform.yaml` | 新增 — 平台功能模块入口，只认 Keycloak 角色与平台授权记录 |
| `cerbos/policies/platform/resource_policies/project_permission.yaml` | 新增 — 项目权限数据操作，`granted_actions` 键统一为 `project_id` |
| `cerbos/policies/resource_policies/platform.yaml` | 删除 — 已移入平台命名空间 |
| `cerbos/policies/derived_roles/platform_roles.yaml` | 删除 — 平台层不再定义派生角色 |
| `cerbos/policies/rag-v14/derived_roles/rag_roles.yaml` | 重写 — 增加 `project_admin` / `platform_admin_role` / `acl_user` / `acl_role` |
| `cerbos/policies/rag-v14/resource_policies/{kb,document}.yaml` | 重写 — 权限只授派生角色 + ACL 路规则 |

### 3.2 后端

| 模块 | 变更 |
|---|---|
| `services/cerbos_policy_parser.py` | 重写 — 命名空间隔离、角色分类与激活方式、去除 parentRoles 并集、去重、资源层级判定 |
| `services/platform_authorizer.py` | 新增 — 平台权限的唯一判定路径 |
| `services/role_policy_writer.py` | 重写 — 项目级集合名、拒绝平台层资源、拒绝无项目归属的角色 |
| `services/acl_resolver.py` | 新增 `resolve_granted_actions` / `get_resource_acl`；挂载反查加项目条件 |
| `services/event_publisher.py` | `write_change_log` / `publish_visibility_changed` 的 `project_id` 改为必填 |
| `api/decision.py` | 三个端点统一组装链路，注入 `resource.attr.acl` / `role_acl` |
| `api/lifecycle.py` | 项目以凭据为准；挂载查询全部按项目 |
| `api/acl_routes.py` | 新增授权层级校验与资源 ACL 只读投影；effective 按项目展开 |
| `api/audit_routes.py` | 策略端点命名空间访问控制、层级对齐、模块 ID 冲突检查 |
| `api/auth_routes.py` | 删除手写平台权限映射，改由 Cerbos 判定 |
| `api/role_definitions_routes.py` | 权限按查询作用域解析；不再写 DB 权限列；绑定计数按项目 |
| `models/{role_definition,mount}.py` | 删除 `permissions` 列；`mount_registry` 加 `project_id` |

### 3.3 数据库迁移

| 版本 | 内容 | 注意 |
|---|---|---|
| `f1a2b3c4d5e6` | 删除 `role_definitions.permissions`；清理策略中已不存在的角色；平台角色描述与 parentRoles 归位 | 降级只能重建空列 |
| `a7b8c9d0e1f2` | `mount_registry` 加 `project_id` + 项目内唯一约束 | 三轮回填；无法归属的行移入 `mount_registry_unattributed` 备查后删除 |
| `b8c9d0e1f2a3` | `platform` / `project_permission` 的授权记录归一为平台级 | 先删冗余再置 NULL，避免撞唯一索引 |

**升级顺序**：先 `alembic upgrade head`，再部署服务
（代码已不读 `permissions` 列，且依赖 `mount_registry.project_id`）。

### 3.4 管理台

- 角色页：矩阵与角色卡同作用域取数；"父角色"改称"激活角色"；标注激活方式
  （持有身份角色即激活 / 需授权记录 / 资源 ACL 动态）；矩阵单元格区分
  ✅ 策略直授、🔑 需授权记录、🔒 ACL 动态。
- 角色详情页：按当前项目作用域解析权限，与列表页同源。
- 创建角色：必须选择所属项目（平台模式下新增项目下拉）。
- 授权对话框：去掉写死的项目兜底值，平台层资源不带项目。

### 3.5 测试

| 文件 | 覆盖 |
|---|---|
| `tests/test_cerbos_policy_parser.py` | 项目隔离、平台命名空间全局可见、无 parentRoles 并集、矩阵去重、同名角色跨项目不串味（11 例） |
| `tests/test_role_policy_writer.py` | 平台层资源不可作为项目角色目标、集合名按项目隔离、角色必须归属项目、失败不留半成品（9 例） |
| `tests/test_policy_conventions.py` | 对真实策略目录强制结构约定，含模块 ID / 集合名重复检查（10 例） |
| `tests/test_joint_contract.py` J-21 | 文档级 ACL 精确生效且不放大成 KB 级 |

---

## 四、行为变更（对接方须知）

| 变更 | 影响 |
|---|---|
| `doc:download` 提升到 `kb_writer` 及以上 | 只有 `kb_reader` 授权的用户不再能下载；需要下载就授 `write`，或用文档级 ACL 单独放行 |
| `/v1/filter` 不再把文档级授权放大成 KB 级 `read` | "只授了一篇文档却能检索整个 KB"的情况被收紧 |
| 平台权限改由 Cerbos 判定 | Cerbos 不可达时管理台返回 503，而不是按 JWT 角色放行 |
| `role_definitions.permissions` 列删除 | 直接读该列的外部查询改调 `/api/v1/roles/permissions` |
| 生命周期端点校验项目 | 请求体 `project_id` 与凭据不一致返回 403 |
| 自定义角色必须选项目、不能授平台动作 | `POST /roles/definitions` 的 `project_id` 变必填；勾选 `platform:*` 返回 422 |
| 平台功能授权必须是平台级 | 带 `project_id` 的 `platform` 授权返回 422 |
| 策略管理端点收紧 | 项目级管理员不能再读写其他项目与平台层的策略 |
| 派生角色 `admin` → `platform_admin_role` | 存量以 `admin` 名义的绑定仍生效（parentRoles 保留了 `admin`） |

---

## 五、验证情况

### 5.1 已验证

| 项 | 结果 |
|---|---|
| 策略解析器单元测试 | 11/11 通过 |
| 策略生成器隔离测试 | 9/9 通过 |
| 策略约定测试（对真实策略目录） | 10/10 通过 |
| 后端全量 `python -m compileall` | 通过 |
| 变更日志调用点 AST 静态扫描 | 全部调用点均带 `project_id`，无遗漏 |
| 管理台 `tsc --noEmit` | 仅剩 `PolicySimulator.tsx` 一处改动前既有报错 |

### 5.2 未验证 —— 上线前必须补

> 整改环境无 PostgreSQL / Cerbos PDP / 运行中的服务，以下均未实际执行：

| 项 | 建议做法 |
|---|---|
| 三个数据库迁移 | 在测试库执行 `alembic upgrade head`，重点核对 `a7b8c9d0e1f2` 的三轮回填结果与 `mount_registry_unattributed` 的行数 |
| 新 CEL 表达式的 Cerbos 编译 | `cerbos compile` 走一遍 `cerbos/policies/` |
| 联合契约测试（含新增 J-21） | 按 v13 §十四 的环境要求执行全量 J-1…J-21 |
| 平台权限 fail-closed 行为 | 停掉 Cerbos，确认管理台返回 503 而非放行 |
| 自定义角色端到端 | 在两个项目各建一个同名自定义角色，确认策略文件集合名不同、Cerbos 正常加载 |

---

## 六、遗留事项

| 事项 | 说明 |
|---|---|
| 本轮引入的死代码 | 整改中新增但未接上调用方的两个符号（`check_platform_permission`、`PROJECT_PERMISSION_KINDS`）已在 `docs/rag_permission_service_diagnose_v15.md` A 组记录并**已清理** |
| demo2 / demo3 的静态角色式策略 | 这两个演示项目用 `roles:` 字段消费平台侧角色绑定，未按 v2 的派生角色模型改写。功能正常，但与 rag-v14 的写法不统一，后续如要统一需同步调整其角色绑定数据 |
| `mount_registry_unattributed` | 迁移产生的备查表，确认无用后可手工删除 |
| 历史诊断文档 | v1–v13 中关于策略目录布局、角色权限来源的描述已被本轮取代，未逐篇回改；以 `docs/permission_model_v2.md` 为准 |
| `manage_role_design.md` | 其中"角色权限双写"一节已失效，v2 文档已声明取代关系 |

---

> **结论**：角色语义混淆与数据双源两个根因已消除，权限映射收敛到 Cerbos 策略文件
> 单一来源，平台权限判定收敛到 Cerbos 单一路径。项目隔离补齐了策略生成、挂载关系、
> 授权层级、策略管理四条此前缺失的战线，并以 CI 测试固化约定。
>
> 代码层面的整改已完成，但**全部数据库迁移与 Cerbos 策略编译尚未在真实环境验证**，
> 上线前须按 §5.2 逐项补做。
