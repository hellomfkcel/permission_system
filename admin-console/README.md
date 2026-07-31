# 权限管理台前端 (Admin Console)

RAG v14 权限系统的管理台 Web 应用。

**设计依据**：
- `docs/外部系统设计.md` §3 管理台前端设计
- `docs/权限管理系统架构设计.md` §5 管理台功能规划

## 技术栈

- **框架**: Next.js 14 (App Router)
- **组件库**: Tailwind CSS + 自定义组件
- **图标**: Lucide React
- **状态管理**: Zustand
- **HTTP 客户端**: Axios
- **图表**: Recharts
- **语言**: TypeScript

## 快速开始

```bash
# 安装依赖
npm install

# 开发模式（http://localhost:3002）
npm run dev

# 生产构建
npm run build

# 生产启动
npm start
```

## 环境变量

创建 `.env.local`（开发模式）或通过 Docker 环境变量注入（生产模式）:

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `NEXT_PUBLIC_PERMISSION_SERVICE_URL` | 权限服务后端地址 | `http://localhost:18080` |
| `NEXT_PUBLIC_KEYCLOAK_URL` | Keycloak IdP 地址 | `http://localhost:8080` |
| `NEXT_PUBLIC_KEYCLOAK_REALM` | Keycloak Realm | `rag-v14` |
| `NEXT_PUBLIC_KEYCLOAK_CLIENT_ID` | Keycloak Client ID | `admin-console` |
| `NEXT_PUBLIC_RAG_SYSTEM_URL` | RAG 系统入口 | `http://localhost:3000` |

## 页面结构

| 路由 | 页面 | 说明 |
|------|------|------|
| `/login` | 登录页 | 开发模式登录 / Keycloak SSO |
| `/dashboard` | 仪表盘 | 权限概览统计 |
| `/resources` | 资源管理 | KB/文档列表 |
| `/resources/kb/[id]` | KB 授权管理 | RAG 系统跳转目标 |
| `/resources/document/[id]` | 文档授权管理 | 文档级 ACL 管理 |
| `/users-groups` | 用户与组 | Keycloak 同步列表 |
| `/users-groups/user/[id]` | 用户详情 | 用户权限汇总 |
| `/users-groups/group/[id]` | 组详情 | 组权限汇总 + 溯源 |
| `/permissions` | 权限管理 | ACL 授予/回收 + 角色绑定 |
| `/restrictions` | 封禁管理 | 型一/型二限制管理 |
| `/policies` | 策略管理 | Cerbos YAML 编辑 |
| `/audit` | 审计日志 | 变更历史查询 |
| `/playground` | 策略模拟器 | 沙箱权限判定 |
| `/settings` | 设置 | 服务状态 + RAG 系统链接 |

## 目录结构

```
admin-console/
├── app/                    # Next.js App Router 页面
│   ├── login/              # 登录页
│   ├── auth/callback/      # Keycloak OAuth 回调
│   ├── dashboard/          # 仪表盘
│   ├── resources/          # 资源管理（含 kb/[id], document/[id]）
│   ├── users-groups/       # 用户与组（含 user/[id], group/[id]）
│   ├── permissions/        # 权限管理
│   ├── restrictions/       # 封禁管理
│   ├── policies/           # 策略管理
│   ├── audit/              # 审计日志
│   ├── playground/         # 策略模拟器
│   ├── settings/           # 设置
│   ├── loading.tsx         # 全局 Loading 骨架
│   ├── error.tsx           # 全局错误边界
│   └── not-found.tsx       # 全局 404 页面
├── components/
│   ├── acl/                # ACL 相关组件
│   ├── layout/             # 布局组件
│   └── shared/             # Toast 通知 + 确认对话框
├── lib/
│   ├── api.ts              # Axios 实例 + 拦截器
│   └── constants.ts        # 动词目录、角色、标签（单一权威源）
├── stores/
│   └── useAuthStore.ts     # 认证状态 (Zustand)
└── Dockerfile              # 多阶段 standalone 构建
```

## Docker 部署

```bash
# 从项目根目录
docker compose up -d admin-console
```

浏览器通过 `localhost:3002` 访问。生产环境配置 `EXTERNAL_HOST` 环境变量。
