# 权限数据落地与一致性审计报告 v19

> 日期：2026-08-17　环境：真实全栈（PG + Redis + Cerbos + FastAPI），穷举验证、不 mock。
> 目标：梳理"角色/绑定/ACL/封禁"数据究竟落在哪、是否合理、是否混乱。
> 结论：落地口径与"单一数据源"设计完全吻合；发现并修复一处注册表对账缺口 **D1**。

## 一、设计基线（0.1/0.2）

- "角色能做什么" → **策略文件**（Cerbos YAML），是唯一权威。
- "谁被授了什么" → **数据库事实表**（绑定 / ACL / 封禁）。
- "某次请求放不放行" → **Cerbos** 运行时判定。
- 策略文件不含 user_id / 不写 DENY / 不记同步态；DB 不存 role.permissions / 不记 policy_synced。

## 二、逐类落地核验（真实创建 + 落点观测）

在 rag-v14 上真实创建每类数据，观测 DB 行与策略文件变化：

| 操作 | 数据落在哪 | 是否触碰策略文件 | 评价 |
| --- | --- | --- | --- |
| **建角色** auditrole（perms=kb:read/doc:download） | 策略文件：`derived_roles/custom_roles.yaml`（集合名 `custom_roles_rag_v14`）+ `resource_policies/custom_kb_auditrole.yaml`、`custom_document_auditrole.yaml`；DB `role_definitions` 仅存档案（name/描述/parent_keycloak_roles/project_id）| ✅ 写策略（这是权威）| **合理**：权限只进策略文件，DB 不含 permissions 列 |
| **角色绑定** user:auditee→auditrole | DB `role_bindings` 一行（principal/role/project_id）| ❌ 策略文件 mtime 全未变 | **合理**：绑定是事实，只落 DB |
| **ACL 授权** user:aclee kb:read@kb-77 | DB `acl_entries` 一行 | ❌ 未变 | **合理**：授权是事实，只落 DB |
| **封禁** subject_ban user:banned | DB `restrictions` 一行（+ `permission_changes` 审计 outbox）| ❌ 未变 | **合理**：封禁是事实，只落 DB |

关键点：**建角色写策略文件、其余三类只写 DB，二者互不交叉**。`role_definitions` 表结构确认
无 `permissions`、无 `policy_synced` 列；角色的 permissions/policy_synced 是**读取时**从策略文件解析
出来回填到响应里的投影，不落库。事务性：建角色先写文件后写库，写库失败回滚文件（无分叉）。

策略文件内容也符合约定 —— `custom_kb_auditrole.yaml` 是纯规则（`derivedRoles:[auditrole]` →
`actions:[kb:read]` → `EFFECT_ALLOW`），无 user_id、无 DENY。

## 三、封禁逻辑核验（端到端，走真实判定链）

用 `/api/v1/simulate`（与 `/v1/check` 同一判定链）验证型一封禁 subject_ban：

```
1) 无授权            → deny（资源策略未命中）
2) 授 kb:read@kb-88   → allow
3) 加 subject_ban     → deny（matched: "型一封禁: user 已被封禁 suspended"）  ← 封禁硬覆盖授权
4) demo2 同名主体      → 不受 rag-v14 封禁影响（封禁按 project_id 收口）
```

判定链：`decision.py` 在调用 Cerbos **之前**先查 `check_subject_ban`，命中即直接返回 deny——封禁是
"先于一切授权的硬拒绝"，覆盖 ACL / 角色。型二 `resource_restriction` 走 `check_resource_restriction`
产出资源级黑名单，用于可见性/过滤端点。两者都按 (tenant, project, principal/resource) 收口。**逻辑合理**。

- 观察（非缺陷，措辞）：型一命中时 matched_rules 文案写作"全局封禁（suspended）"，实际是**项目内**
  封禁；建议把"全局"改为"本项目"以免误解。

## 四、数据卫生扫描

- 全部 `role_definitions` 的 `policy_synced` 修复后为 True（DB 档案与策略文件不分叉）。
- 孤儿引用扫描：`acl_entries / role_bindings / restrictions / role_definitions` 中指向"非活跃项目"的
  活跃行 **均为 0**。
- 平台级角色（platform_admin / platform_auditor / platform_viewer / project_admin，project_id=NULL）
  是**单行**、在每个项目的列表里都显示（列表按 `project_id==P OR IS NULL` 取并集）——非重复落库。

## 五、D1 缺陷 —— 落盘种子项目的角色缺席 DB 注册表（已修复）

### 现象

demo2 的业务角色 `oa_employee / oa_manager / oa_hr / oa_ceo` 定义在策略文件 `oa_roles.yaml`，在
**权限矩阵**（`/roles/permissions`，从策略解析）里可见；但 DB `role_definitions` 中 demo2 **一行都没有**，
于是**角色管理页**（`/roles/definitions`，DB 驱动）看不到 demo2 的任何业务角色 —— 两个视图分叉。

### 根因

`role_definitions` 注册表只由 `_sync_policy_roles_to_db(project_id)` 填充，而它**只在策略写入/上传
API 里被调用**。demo2 的 `oa_roles.yaml` 是**直接落盘的种子**（未走上传接口），因此从未同步。rag-v14
的角色因经 API/bootstrap 建立而在表中。启动时没有对存量策略文件做对账。

### 影响

纯管理/展示层不一致（角色管理页对 demo2 角色失明、拿不到绑定计数），**不影响判定**——Cerbos 始终
以策略文件为权威。属"数据混乱"里的注册表滞后，非授权漏洞。

### 修复

`app/main.py` 启动生命周期在 bootstrap 之后新增 `_reconcile_policy_roles()`：遍历全部活跃项目，逐个
调用 `_sync_policy_roles_to_db` 做幂等对账（只补角色档案：名称/激活角色/归属，**不写权限、不改策略**）。

### 复测

重启后 demo2 `role_definitions` 出现 oa_ceo/oa_employee/oa_hr/oa_manager；角色管理页与权限矩阵
两个视图对 demo2、rag-v14 **完全一致**。确认 `role_definitions` 仍无 permissions/policy_synced 列
（对账只补档案，DB 保持"只存事实"）。

## 六、结论

四类数据落地口径清晰、互不交叉，与单一数据源设计一致：**角色规则进策略文件，绑定/ACL/封禁进 DB，
判定在 Cerbos**。封禁为先于授权的硬拒绝、按项目收口，逻辑合理。数据卫生无孤儿、无重复落库。唯一
缺口 D1（落盘种子项目角色未进注册表，致管理页与矩阵分叉）已用启动对账修复并复测。一处措辞建议
（型一封禁文案"全局"→"本项目"）记录待改，非缺陷。

修复清单：

| 项 | 文件 | 改动 |
| --- | --- | --- |
| D1 | `app/main.py` | 新增 `_reconcile_policy_roles()`，启动时对全部活跃项目幂等对账策略角色→注册表 |

回归：离线静态测试 24 项全过；`compileall` 通过；本轮全部探针数据（角色/绑定/ACL/封禁/策略文件）
经真实 API 清理，策略目录与 DB 回到基线。
