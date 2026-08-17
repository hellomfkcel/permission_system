# 权限平台 死代码扫描诊断报告 v15

> **诊断日期**：2026-08-17
> **诊断范围**：`permission-service/`、`admin-console/`、`perm-service-client/`、`scripts/`、`cerbos/policies/`
> **诊断方法**：AST 全量交叉引用 + 路由/消费方比对 + 人工语义判定
> **状态**：**A 组已执行删除并验证通过**（见 §七）；B / D 组待确认。
> **前序报告**：`docs/rag_permission_service_diagnose_v14.md`（角色语义 / 数据来源 / 项目隔离）

---

## 〇、扫描口径与一次自我修正

### 0.1 规模

| 项 | 数量 |
|---|---|
| Python 文件 | 81 |
| 模块级定义（函数 / 类 / 常量） | 681 |
| FastAPI 路由处理器 | 81 |
| pytest 测试函数 | 76 |
| Alembic 迁移函数 | 34 |
| 前端 TS/TSX 文件 | 42 |

### 0.2 判定规则

死代码扫描最大的风险不是漏报，是**误报**——把框架按约定加载的符号当成无人调用。
本次扫描的排除规则：

| 类别 | 为什么不是死代码 |
|---|---|
| 带 `@router.*` / `@app.*` / `@limiter.*` / `@pytest.fixture` 等装饰器的函数 | 由框架注册，代码里本就无人调用 |
| 迁移文件里的 `upgrade` / `downgrade` | Alembic 按文件名加载 |
| `test_*` 函数 | pytest 按命名约定收集 |
| Next.js `app/**/page.tsx`、`layout.tsx` 的默认导出 | App Router 按文件路径加载 |
| Pydantic 请求模型 | 只作为类型注解出现，引用计数天然为 1 |

引用统计除 `ast.Name` / `ast.Attribute` 外，**还纳入字符串字面量与全部非 py 文本**
（md / yaml / ts / tsx / sh / json），以覆盖 `getattr`、路由字符串、配置名这类动态引用。
判定倾向保守：任何一处出现即不报。

### 0.3 扫描器的两次错误（记录在案，避免重复踩）

第一版与第二版扫描器都给出了错误结论，修正过程本身值得记录：

| 版本 | 错误 | 后果 | 修正 |
|---|---|---|---|
| v1 | 阈值统一取 `refs <= 1` | 169 个"疑似"，绝大多数是路由处理器与 Pydantic 注解类，结论不可用 | 排除框架注册点，阈值改 `refs == 0` |
| v2 | 只收集 `ast.Assign`，漏掉 `ast.AnnAssign` | **所有带类型注解的模块级常量对扫描器不可见**（`PLATFORM_ROLE_NAMES: set[str] = {...}` 这类全部漏报） | 补 `AnnAssign` |
| v3 | 补上 `AnnAssign` 后仍未报出 | 注解赋值的**定义点自身就是一个 `ast.Name` 节点**，被计成了一次引用 | 按定义类型分阈值：函数 / 类 `refs == 0`，变量 `refs <= 1` |

**教训**：静默返回空结果的扫描器比不扫描更危险。任何"全部通过"的结论，都应先用
一个已知的死符号验证扫描器本身能报出来。

---

## 一、可以直接删除（A 组）— 已执行

证据充分、无外部可达性、删除无行为影响。**已全部删除，验证结果见 §七。**

| # | 位置 | 依据 | 量 |
|---|---|---|---|
| A1 | `admin-console/lib/constants.ts` — 整个文件 | **0 个 importer**。且内容与现架构冲突：硬编码 rag-v14 的资源类型与 10 个动作，`DERIVED_ROLES` 中仍列已删除的 `admin`。文件头自称"单一权威源"，但该职责已由 `/api/v1/auth/config` 从 Cerbos 策略动态解析接管 | 87 行 |
| A2 | `components/shared/Toast.tsx` 的 `globalToast()` 与 `_globalShowToast` | 0 个 importer。同文件 L77 已设置 `window.__globalToast`，`lib/api.ts:81` 用的是这条。两套全局 Toast 机制并存，此为被取代的一套 | 24 行 |
| A3 | `app/platform_features.py: PLATFORM_ROLE_NAMES` | 全仓库 0 引用。内容已过时——平台角色的权威定义现在在 `platform.yaml` | 6 行 |
| A4 | `app/platform_features.py: PLATFORM_FEATURE_PATHS` | 0 引用。注释称"供前端使用"，但无任何端点暴露它，侧边栏路径由前端自行维护 | 15 行 |
| A5 | `api/acl_routes.py:23-24` 导入 `VALID_ACTIONS`、`VALID_RESOURCE_TYPES` | 只导入不使用；实际调用的是 `get_valid_actions(project_id)` | 2 行 |
| A6 | `api/decision.py:31` 导入 `record_authz_obligation_unknown` | 只导入不调用 | — |
| A7 | `api/auth_routes.py:1137 filter_by_project_scope` | 0 调用。是 `get_admin_project_ids` 的薄包装，5 个真实调用点全部直接调后者 | 15 行 |
| A8 | `services/platform_authorizer.py:191 check_platform_permission` | 0 调用。**v14 整改时引入**，实际调用方全部走 `resolve_platform_permissions` | 13 行 |
| A9 | `app/role_actions_config.py: PROJECT_PERMISSION_KINDS` | 0 引用。**v14 整改时引入**，原意给前端展示项目权限数据类别，未接上 | 9 行 |
| A10 | `schemas/tenant_requests.py:63 ListTenantsParams` | 0 引用；对应端点使用独立 Query 参数 | 7 行 |
| A11 | `app/observability.py:90 start_cerbos_span` | 0 调用。`services/cerbos_adapter.py:68` 自行用 `_TRACER.start_span()` 实现了同一职责 | 21 行 |
| A12 | `tests/test_joint_contract.py:42 CERBOS_URL` | 0 引用 | 1 行 |

> A8、A9 是上一轮（v14）整改时新引入的死代码，一并列出，不做区别对待。

---

## 二、建议删除，但需先确认无接入方调用（B 组）

这两条是**活的 HTTP 端点**。静态分析能证明"仓库内无调用方"，不能证明"外部无调用方"。

| # | 端点 | 依据 | 风险 |
|---|---|---|---|
| B1 | `GET /v1/resources/{type}/{id}/owners`（`api/lifecycle.py:614`） | 与 `api/resource_routes.py:179` 的 `/api/v1/...` 版本功能重复；`readme.md §268-273` 与 `外部系统设计.md` 列出的 `/v1` 契约中没有这条；其 docstring 自称"管理台用于展示"并引用 §2.4.4，而 §2.4.4 记的是 `/api/v1` 那条；签名中 `request: Request = None` 的默认值也不符合 `/v1` 端点的写法 | 需向 RAG 侧确认无调用 |
| B2 | `GET /api/v1/tenants/by-user/{user_id}`（`api/tenant_routes.py`） | 前端、SDK、测试、脚本 0 调用；`docs/tenant_design.md` 未记载 | 需确认无外部管理脚本调用 |

---

## 三、静态上"无引用"但不可删除（C 组，已排除的误报）

记录在此，避免后续扫描重复报出：

| 类别 | 数量 | 说明 |
|---|---|---|
| FastAPI 路由处理器 | 81 | 装饰器注册 |
| pytest 测试函数 | 76 | 命名约定收集 |
| Alembic `upgrade`/`downgrade` | 34 | 按文件名加载 |
| Pydantic 请求模型 | — | 仅作类型注解 |
| Next.js `page.tsx` / `layout.tsx` 导出 | 19 | App Router 按路径加载 |

单独说明的三项：

| 项 | 为什么保留 |
|---|---|
| `GET /api/v1/resources/{type}/{id}/owners` | 前端确实未调用，但 `外部系统设计.md:479` **已列入契约清单**，属"已声明未接入"，不是死代码 |
| `_LEGACY_DERIVED_SET` / `_retarget_legacy_imports` / `remove_role_policies` 的根目录清理分支 | 为存量部署保留的迁移路径。仓库内没有触发样本（无 `custom_roles.yaml`），不代表线上没有 |
| `rag_roles.yaml` 中 `platform_admin_role` 的 `admin` parentRole | 存量角色绑定兼容，删除会让这些绑定在判定期静默失效 |

---

## 四、其它维度：已确认干净

| 维度 | 结果 |
|---|---|
| 未使用导入 | 仅 A5 / A6 两处 |
| 从未被 import 的模块 | 0 |
| 零调用的类方法 | 0 |
| 模型字段在 `models/` 之外零引用 | 0 |
| 前端孤立组件文件 | 0（`components/` 下全部被引用） |
| `perm-service-client` SDK | 非死代码，`readme.md §599` 记录为对外交付物 |
| `cerbos/policies/**/.versions/` 归档 | 7 个快照文件，是策略版本历史，非死代码 |

---

## 五、需要决策的灰色地带（D 组）

### 策略根命名空间 `UNSCOPED_PROJECT`

v14 整改把平台策略移入 `cerbos/policies/platform/` 后，原来的
`cerbos/policies/derived_roles/` 与 `cerbos/policies/resource_policies/` 已被删除，
**仓库内已无任何文件落在根命名空间**。但代码仍全面支持它：

- `cerbos_policy_parser` 把它视为 `_GLOBAL_NAMESPACES` 之一 → **对所有项目全局可见**
- `audit_routes._assert_namespace_access` 允许平台管理员往那里写
- `audit_routes._target_namespace` 在 `project_id=None` 时返回它
- `role_policy_writer.remove_role_policies` 仍会清理那里的遗留条目

v2 模型下它已无合法用途：自定义角色必须归属项目，平台层在 `platform/`。
问题在于它**既是死代码，又是残余风险面**——任何文件落进那两个目录都会对所有项目生效。

三个可选处置：

| 方案 | 说明 | 代价 |
|---|---|---|
| 1. 保留现状 | 读写路径都留着 | 全局命名空间写入通道一直敞着 |
| 2. **只读兼容**（倾向此项） | 解析器继续读（保住存量部署），写入端点拒绝 `project_id=None` 的写入 | 需要确认没有运维流程依赖往根目录写 |
| 3. 彻底移除 | 解析器不再识别根命名空间 | 需先确认所有部署的根目录均为空 |

判断依据不足以单方面决定：无法从仓库得知线上部署的根目录是否还有策略文件。

---

## 六、执行建议

| 组 | 处置 | 前置条件 |
|---|---|---|
| A（12 项） | **已执行**，见 §七 | — |
| B（2 个端点） | 暂缓 | 向 RAG 侧与运维确认无调用后再删 |
| C | 不动 | — |
| D | 待决策 | 需线上根目录策略文件的实际情况 |

---

## 七、A 组执行结果

### 7.1 变更量

```
11 files changed, 12 insertions(+), 205 deletions(-)
```

净减 193 行。新增的 12 行全部是替代注释——原地说明"这类知识现在归谁维护"，
避免同一份清单被重新加回来：

| 原符号 | 留下的说明 |
|---|---|
| `PLATFORM_ROLE_NAMES` / `PLATFORM_FEATURE_PATHS` | 平台角色的权威定义在 `platform.yaml`；功能→路径映射由管理台自行决定，后端不暴露 |
| `PROJECT_PERMISSION_KINDS` | 权限数据类别是 `project_permission.yaml` 中的 `resource.id`，取值由策略文件决定 |
| `start_cerbos_span` | Cerbos 的 span 由 `cerbos_adapter.py` 自行创建 |

### 7.2 验证

| 项 | 结果 |
|---|---|
| 后端 `python -m compileall`（api/app/services/models/schemas/tests/migrations） | 通过 |
| 9 个被删符号的残留引用 grep | 全部为 0 |
| 死代码扫描器重跑 | A 组与 B 组均为空 |
| 策略解析器测试 | 11/11 通过 |
| 策略生成器测试 | 9/9 通过 |
| 策略约定测试 | 10/10 通过 |
| 管理台 `tsc --noEmit` | 仅剩 `PolicySimulator.tsx` 一处改动前既有报错，与删除前一致 |

### 7.3 未覆盖的验证

删除的都是零引用符号，理论上无运行时影响；但以下仍未在真实环境验证：

- 服务实际启动（本环境无 PostgreSQL / Cerbos / Redis）
- 管理台构建与页面渲染（`npm run build` 未执行）
- 联合契约测试 J-1…J-21

其中 `admin-console/lib/constants.ts` 整文件删除的风险最需要复核：扫描确认 0 个
importer，`tsc --noEmit` 也通过，但如果存在运行期动态导入（本仓库未发现），
静态检查覆盖不到。
