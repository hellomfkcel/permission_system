# 权限服务穷举式越权与功能验证报告 v18

> 测试日期：2026-08-17
> 环境：真实全栈（PostgreSQL + Redis + Cerbos PDP + FastAPI service），不 mock、不硬编码、
> 不为跑通绕过架构。承接 v17（G5–G8 + 静态门禁）。
> 方法：**穷举** (persona × endpoint × project) 授权矩阵 + 各角色真实功能操作。
> 本轮发现并修复一处深层越权 **G9（跨项目角色升级）**，另记录一处设计观察（平台 ACL 委托对非成员失效）。

## 一、被测主体（覆盖单角色 / 多角色 / ACL 委托）

| 主体 | 身份 | 用途 |
| --- | --- | --- |
| admin | system_admin（平台级） | 平台全权基线 |
| palice | project_admin @ rag-v14 | 单项目管理员 |
| pviewer | project_viewer @ rag-v14 | 单项目只读成员 |
| bob | project_admin @ demo2 | 单项目管理员（跨项目对照）|
| **carol** | project_admin @ rag-v14 **+** project_viewer @ demo2 | **多角色**（跨项目不同角色）|
| **dave** | project_viewer @ demo2 **+** 平台 ACL 委托 audit_mgmt:read | **成员 + ACL 委托** |
| **erin** | 仅 平台 ACL 委托 audit_mgmt:read（无任何项目成员） | **纯 ACL 委托** |

## 二、方法：穷举授权矩阵

`scratchpad/matrix.py` 枚举 7 主体 × 2 项目 × ~20 端点（平台专属模块 + 项目 7 模块的读与写探针
+ provisioning + 成员管理），共 **280** 个 (主体,端点,项目) 组合。每个组合按真实 HTTP 判定：

- DENIED = 401/403/400（400 为"非平台管理员须限定项目"的拒绝口径，无数据）
- ALLOWED = 其它（进入了处理逻辑）

再用一个独立的期望模型（按主体在**该项目**的角色 + 平台身份 + ACL 委托推导应否放行）比对，
分类 **ESCALATION**（本应拒却放行）与 **WRONGFUL-DENY**（本应放行却拒）。

## 三、G9 —— 跨项目角色升级（本轮核心发现）

### 3.1 现象

首轮矩阵 carol 在 **demo2** 上出现 11 处 ESCALATION：她在 demo2 只是 **project_viewer**，却能

```
GET  /audit?project_id=demo2            -> 200   （只读成员本不该看审计）
GET  /roles/permissions?project_id=demo2-> 200
GET  /acl?project_id=demo2              -> 200
GET  /restrictions?project_id=demo2     -> 200
GET  /policies?project_id=demo2         -> 200
POST /restrictions/add   (demo2)        -> 200   （真实写入一条封禁！）
POST /events/replay      (demo2)        -> 200
POST /simulate           (demo2)        -> 200
```

对照：纯 viewer 的 pviewer 在自己项目做同样操作全部 403。差别只在于 carol 在**另一个**项目
（rag-v14）是管理员。

### 3.2 根因

模块准入 `require_platform_permission(module, action)` 的权限映射来自
`resolve_platform_permissions` → `_project_member_roles`，后者把成员角色**跨所有项目取并集**：
carol 在 rag-v14 是 project_admin → 合成出 `project_admin` 角色 → platform.yaml 授予她全部 7 模块
读写。随后端点的项目范围校验（`assert_project_scope` / `can_access`）只验证她**是不是** demo2 的
成员（是，viewer），**不验证她在 demo2 的角色**。两道校验合起来仍挡不住"在 A 项目是管理员、
去 B 项目当管理员用"。

即：**模块准入用的是跨项目角色并集，项目范围只查成员资格，二者不按"该项目里的角色"合成。**

### 3.3 修复

让模块准入判定收口到"这个请求针对的那个项目"：

1. `_project_member_roles(db, user_id, scope_project_id=None)` 新增 `scope_project_id`：指定时只按
   该项目的成员角色解析（不再并集）。
2. `resolve_platform_permissions(..., scope_project_id=None)` 透传。
3. `require_platform_permission` 的依赖读取本次请求要操作的 project_id（`_target_project_id`：优先
   query，其次 JSON / 表单 body），以 `scope_project_id` 传入判定。

侧边栏 `me/access` 不传 scope_project_id，仍按并集显示（carol 能看到 audit 模块入口，因为她在
rag-v14 确是管理员）；但对具体项目的请求，按**该项目**的角色判定。平台级身份（system_admin/
platform_admin，走 jwt/绑定角色）与平台 ACL 委托（granted_actions，平台级）不受影响。

### 3.4 修复后复测

carol demo2 的 11 处 ESCALATION 全部转为 403；她在 rag-v14（管理员）一切照常。最终全矩阵：

```
TOTAL: 280 | OK: 280 | ESCALATIONS: 0 | WRONGFUL-DENIALS: 0
```

覆盖：单角色（palice/pviewer/bob）、多角色（carol）、ACL 委托（dave）、纯 ACL（erin）。

### 3.5 残留误报核销（均经真实请求确认非越权）

初次矩阵还有 13 条被标记，逐条查实为分类器口径问题，非真实越权：

| 标记 | 真相 |
| --- | --- |
| `/auth/users -> 400`（非平台主体，不传 pid）| G 修复的"须限定项目"拒绝口径，无数据；带自己项目 pid→200，跨项目→403 |
| `/auth/config -> 200`（非平台主体）| G4 软门：返回 200 外壳但敏感字段脱敏（palice 实测 port=0/cerbos=''/keycloak=''）|
| `/policies/upload -> 422`（跨项目）| 我发的 multipart 缺字段，校验先于授权触发；补全后 palice→demo2、bob→rag-v14、carol→demo2 均 **403** |

分类器据此校正（400 视为 DENIED、config 记为软门、策略写改用 PUT+JSON）后即 0 违规。

## 四、功能穷举（各角色在允许范围内能真正完成操作）

`scratchpad/func.py` 对每个角色做**真实操作**（非仅看非-403），确认 2xx、无 5xx / 逻辑失败：

- **system_admin**：平台专属 3 模块（项目 list/create 201、租户 list/create 201、系统设置 200、
  用户与组 200、概览 200）+ 项目 7 模块读全 200 + 策略模拟 200。
- **project_admin(palice) @rag-v14**：7 模块读全 200；每模块一次真实写全部成功 —— 建角色 201、
  授 ACL 200、加封禁 200、写策略 200、自助签发 API Key 201、事件重放 200。
- **project_viewer(pviewer)**：概览/资源/角色 view 全 200。

三类角色都能在各自允许范围内正常访问与管理，无功能缺陷。

## 五、设计观察：平台 ACL 委托对"纯非成员"失效（erin）

`erin` 只有一条平台 ACL 委托（audit_mgmt:read），无任何项目成员身份。实测
`GET /auth/me/access` → **403 "Access denied. 需要管理员角色或项目成员"**：她被 `get_current_admin`
挡在门外，platform.yaml 里为委托预留的 `delegated_feature_read`（`roles:["user"]`）规则根本走不到。

含义：**给一个非成员、非管理员的用户单独授平台 ACL，目前是"哑"操作 —— 他仍进不了管理台。**
`dave`（既是 demo2 成员、又有该委托）则正常：`me/access` 里多出 audit_mgmt(ro)，委托对已进门的
成员有效。

这是 fail-closed（erin 得到的是更少而非更多，无安全风险），但属一处**逻辑/设计不一致**，两种收敛
方向供选择：
- (a) 若委托本就只用于"给已有成员加一个平台模块"，则应在授权 UI/接口侧禁止给非成员签发平台
  ACL，并从 platform.yaml 移除对纯委托的暗示，避免"授了但没用"的误解；
- (b) 若平台模块委托应独立于项目成员身份成立，则 `get_current_admin` 应接纳"持有有效平台 ACL
  委托"的用户入场。

本轮未擅自改动（属放宽入场、需明确设计意图），记录待定。

## 六、修复清单与回归

| 项 | 文件 | 改动 |
| --- | --- | --- |
| G9 | `services/platform_authorizer.py` | `_project_member_roles` / `resolve_platform_permissions` 加 `scope_project_id`，项目请求按该项目角色解析 |
| G9 | `api/auth_routes.py` | 新增 `_target_project_id`（query/body 提取）；`require_platform_permission` 以 scope_project_id 判定 |

回归：静态门禁 3 项 + 离线测试合计 **24 项全过**；`compileall` 通过；全部测试数据（探针项目/租户/
角色/绑定/ACL/封禁/策略/API Key）经真实 API 清理，策略目录与项目/租户回到原始状态。

## 七、结论

穷举 280 组 (主体×端点×项目)：**0 越权、0 误拒**。单角色各守其位；多角色 carol 修复前可把 A 项目
的管理员权限带去 B 项目（G9），修复后严格按"该项目里的角色"判定；ACL 委托对已进门成员正常
叠加。功能面各角色均能在允许范围内完成真实读写。一处设计观察（纯 ACL 委托对非成员失效）记录
待定，非安全问题。
