# Cerbos 策略灰度发布流程

> **设计依据**：docs/外部系统设计.md §3.3 /policies 策略管理页面 + docs/RAG系统设计v14.md §27.3 发布工程
> 
> **适用场景**：修改 Cerbos PDP 策略（派生角色/资源策略），需要通过灰度发布确保不破坏现有权限判定。

---

## 一、策略版本管理机制

### 1.1 存储结构

```
cerbos/policies/
├── derived_roles/
│   └── rag_roles.yaml              ← 当前生效版本
├── resource_policies/
│   ├── kb.yaml                     ← 当前生效版本
│   └── document.yaml               ← 当前生效版本
└── .versions/                        ← 历史版本（自动归档）
    └── resource_policies/
        ├── 20260730T234231Z_*.yaml
        └── 20260731T012348Z_*.yaml
```

### 1.2 版本历史 API

```bash
# 查看策略版本历史
GET /api/v1/policies/{path}/versions

# 查看两个版本的 diff
GET /api/v1/policies/{path}/diff?v1=20260730T234231Z&v2=current

# 部署新版本（自动归档旧版本）
PUT /api/v1/policies/{path}
```

---

## 二、灰度发布流程

### 阶段 1：沙箱验证（Playground）

在管理台的 Playground 页面（`/playground`）中模拟新策略的判定结果。

**操作步骤**：
1. 打开管理台 → Playground
2. 选择场景预设或手动输入 Principal / Action / Resource
3. 点击"执行模拟"，观察判定结果
4. 验证关键场景：
   - 管理员 (`system_admin`) 全部操作 → allow
   - 普通用户无授权 → deny
   - 有 ACL 授权的用户 → allow
   - 型一封禁用户 → deny (suspended)
   - 型二封禁资源 → deny (filter pre-deny)

### 阶段 2：单 KB 试运行

选择 1 个低风险的测试 KB，先部署新策略到 Cerbos PDP。

**操作步骤**：
1. 通过管理台 PUT `/api/v1/policies/{path}` 部署新策略
2. Cerbos PDP 自动加载新策略（无需重启）
3. 在测试 KB 上执行完整操作序列：
   - 文档上传 → 解析 → 检索 → 下载
   - 授予权限 → 生效验证
   - 撤销权限 → 即时拒
4. 监控指标：
   - `authz_decision_total{endpoint=check,decision=indeterminate}` 应为 0
   - `authz_call_failed_total` 不应增加
   - `authz_obligation_unknown_total` 应为 0

**验证通过标准**：
- 所有判定结果与旧策略一致（或仅预期的差异）
- 无 indeterminate 判定
- 无 obligation 未知告警

### 阶段 3：多 KB 扩展（如适用）

如果 Cerbos PDP 支持按策略版本路由（高级特性），可以逐步扩大灰度范围：
1. 将新策略绑定到更多 KB
2. 监控检索质量和权限判定一致性
3. 确认无异常后继续扩展

如果 Cerbos PDP 不支持策略版本路由（当前版本），跳过此阶段，直接进入全量发布。

### 阶段 4：全量发布

**操作步骤**：
1. 确认阶段 2 验证通过
2. 通过管理台 PUT 部署新策略，覆盖所有 KB
3. 监控以下指标 30 分钟：
   - `authz_decision_total` 三态分布无异常变化
   - `authz_call_failed_total` 无增长
   - `permission_service_global_version` 正常递增
4. 执行联合契约测试 J-1~J-20 中的权限判定相关项

### 阶段 5：回滚（如需要）

如果在全量发布后发现异常：

**操作步骤**：
1. 通过管理台 PUT 部署上一个版本（从版本历史中选择）
2. 确认 Cerbos PDP 重新加载旧策略
3. 验证判定恢复正常
4. 记录异常现象，分析根因后重新发布

```bash
# 查看版本历史
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:18080/api/v1/policies/resource_policies/kb.yaml/versions"

# 获取旧版本内容
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:18080/api/v1/policies/resource_policies/kb.yaml/diff?v1=20260730T234231Z&v2=current"

# 部署回滚（PUT 旧版本 YAML 内容）
```

---

## 三、策略变更检查清单

### 变更前

- [ ] 在 Playground 中模拟新策略的 10+ 场景
- [ ] 对比新旧策略的判定差异（`/diff` API）
- [ ] 确认变更不影响废除动词（`doc:write`/`acl:update`/`doc:delete`）
- [ ] 确认新策略的 derivedRoles 层级正确
- [ ] 准备回滚方案（确认旧版本文件可获取）

### 变更中

- [ ] 部署新策略到 Cerbos PDP
- [ ] 验证 `authz_decision_total{decision=indeterminate}` = 0
- [ ] 验证 `authz_call_failed_total` 无增长
- [ ] 执行关键场景的 /v1/check 验证
- [ ] 执行 prefilter + filter 链路验证

### 变更后（30 分钟观察期）

- [ ] `authz_decision_total` 三态分布正常
- [ ] `permission_service_global_version` 正常递增
- [ ] Grafana 中无新增告警
- [ ] 用户反馈无异常权限问题

---

## 四、策略变更的安全约束

| 约束 | 说明 |
|------|------|
| **禁止删除派生角色** | 删除已使用的派生角色会导致现有用户无法通过权限判定 |
| **禁止放宽敏感操作** | `kb:grant`/`doc:share`/`doc:purge` 的准入条件只能收紧不能放宽 |
| **必须保留 admin 无条件 allow** | `admin` 派生角色的 `expr: "true"` 不可修改 |
| **资源条件只能加不能减** | 如为 `doc:download` 增加 `allow_download` 条件是安全的；移除 `retired` 检查是不安全的 |
| **派生角色条件保持确定性** | `granted_actions` 检查逻辑不可引入非确定性因素 |

---

## 五、管理台操作界面

策略管理页面（`/policies`）提供：

1. **策略文件列表**：显示所有策略文件（当前版本 + 历史版本）
2. **YAML 内容浏览**：语法高亮的源码查看
3. **版本历史**：Git 式 diff 视图，对比任意两个版本
4. **策略部署**：编辑 YAML 后推送，自动版本归档
5. **删除策略**：删除过期的历史版本

所有操作均通过 `Authorization: Bearer <token>` 认证，确保只有管理员可修改策略。
