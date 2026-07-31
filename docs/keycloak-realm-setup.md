# Keycloak Realm 配置指南

> 设计依据：docs/外部系统设计.md §4.1 + docs/权限管理系统架构设计.md §一 四方协作模型。

## 一、Realm 创建

```
Realm Name: rag-v14
Enabled: true
```

## 二、Clients

### 2.1 rag-frontend（RAG 前端 → 用户 SSO 登录）

| 配置项 | 值 |
|--------|-----|
| Client ID | `rag-frontend` |
| Client Protocol | `openid-connect` |
| Access Type | `public` |
| Standard Flow Enabled | `ON` |
| Valid Redirect URIs | `http://192.168.1.127:3001/*` |
| Web Origins | `http://192.168.1.127:3001` |

### 2.2 admin-console（管理台 → 管理员 SSO 登录）

| 配置项 | 值 |
|--------|-----|
| Client ID | `admin-console` |
| Client Protocol | `openid-connect` |
| Access Type | `confidential` |
| Standard Flow Enabled | `ON` |
| Valid Redirect URIs | `http://192.168.1.127:3002/*` |
| Web Origins | `http://192.168.1.127:3002` |

### 2.3 permission-service（权限服务后端 → 服务间认证）

| 配置项 | 值 |
|--------|-----|
| Client ID | `permission-service` |
| Client Protocol | `openid-connect` |
| Access Type | `confidential` |
| Service Accounts Enabled | `ON` |
| Standard Flow Enabled | `OFF` |

## 三、Realm Roles

| 角色 | 说明 |
|------|------|
| `system_admin` | 超级管理员 — 无条件 allow 所有动作 |
| `user` | 普通用户 — 默认角色，通过 granted_actions 分级授权 |

## 四、Groups（可选）

| 组 | 路径 |
|----|------|
| Engineering | `/engineering` |
| Product | `/product` |
| Admin | `/admin` |

## 五、Client Scopes / Mappers

### JWT Claims 配置

在 `rag-frontend` 和 `admin-console` Client 的 Mappers 中配置以下 claims 映射：

| Claim | 来源 | 说明 |
|-------|------|------|
| `sub` | 用户 ID | Keycloak 内置 |
| `tenant` | 用户属性 `tenant_id` | 需添加 User Attribute Mapper |
| `realm_access.roles` | Realm Roles | Keycloak 内置 |
| `groups` | 组成员 | 需添加 Group Membership Mapper |
| `email` | 用户邮箱 | Keycloak 内置 |
| `preferred_username` | 用户名 | Keycloak 内置 |

### 添加 Mapper 步骤

1. 进入 Client → Mappers → Create
2. **tenant Mapper**:
   - Name: `tenant`
   - Mapper Type: `User Attribute`
   - User Attribute: `tenant_id`
   - Token Claim Name: `tenant`
   - Claim JSON Type: `String`
3. **groups Mapper**:
   - Name: `groups`
   - Mapper Type: `Group Membership`
   - Token Claim Name: `groups`

## 六、环境变量配置

### RAG v14 (.env)

```bash
# Keycloak
KEYCLOAK_URL=http://192.168.1.127:8080
KEYCLOAK_REALM=rag-v14
KEYCLOAK_CLIENT_ID=rag-frontend
```

### Admin Console (环境变量)

```bash
NEXT_PUBLIC_KEYCLOAK_URL=http://192.168.1.127:8080
NEXT_PUBLIC_KEYCLOAK_REALM=rag-v14
NEXT_PUBLIC_KEYCLOAK_CLIENT_ID=admin-console
```

### Permission Service (.env)

```bash
KEYCLOAK_SERVER_URL=http://192.168.1.127:8080
KEYCLOAK_REALM=rag-v14
KEYCLOAK_CLIENT_ID=permission-service
KEYCLOAK_CLIENT_SECRET=<从 Keycloak Credentials 获取>
```

## 七、验证清单

- [ ] 用户可以通过 `rag-frontend` 登录到 RAG 前端
- [ ] 管理员可以通过 `admin-console` 登录到管理台
- [ ] 管理台可以从 Keycloak 同步用户列表（`POST /api/v1/auth/sync/users`）
- [ ] JWT 包含正确的 `tenant`、`roles`、`groups` claims
- [ ] 用户退出后 token 失效，需要重新登录
