# 权限管理平台 (Permission Platform)

一个独立的、多项目、多租户的**授权中台**。集中管理"谁能对什么资源做什么"，为多个业务系统
统一提供权限判定、资源可见性投影与授权管理。业务系统只作为消费方，自身不做授权判定。

- 决策引擎：**Cerbos**（策略即代码，PDP 判定）
- 身份源：**Keycloak**（用户/组/JWT）
- 后端：**FastAPI**（Python 3.11）+ PostgreSQL + Redis
- 管理台：**Next.js**（admin-console）

---

## 架构一览

```
业务系统 ──X-Api-Key + X-Client-Id──▶  /v1/*   ┐
                                              ├─▶ 权限服务(FastAPI) ──▶ Cerbos PDP（判定）
管理台   ──Bearer JWT──────────────▶ /api/v1/* ┘        │
                                                        ├─ PostgreSQL：授权事实（绑定/ACL/封禁）
                                                        ├─ Redis：变更事件 + 缓存
              Keycloak(IdP) ──JWT 校签 / 定时同步──▶     └─ cerbos/policies/*.yaml：授权规则
```

**单一数据源**：规则写在策略文件、事实存在数据库、决策由 Cerbos 做，三者互不重叠。

**两类入口**：
- `/v1/*`：外部系统调用，用 API Key + Client-Id 鉴权（中间件解析出所属项目）。
- `/api/v1/*`：管理台调用，用 Bearer JWT 鉴权。

**项目是隔离单元**：每个项目有独立的策略目录、授权数据（按 `project_id` 隔离）与管理台可见范围。

---

## 快速开始

### 依赖

Python 3.11 · PostgreSQL 16 · Redis 7 · Cerbos PDP · Keycloak 24+ · 一对 RS256 JWT 公私钥。

### 开发模式

```bash
# 1. 基础设施 + Keycloak IdP（Cerbos 亦含于其中，挂载本仓库策略目录）
docker compose up -d perm-postgres perm-redis cerbos keycloak

# 2. 权限服务
cd permission-service
pip install -r requirements.txt
cp .env.example .env          # 按需修改（见下方“配置”）
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 18080 --reload

# 3. 管理台
cd admin-console && npm install && npm run dev            # http://localhost:3002
```

接口文档：`http://localhost:18080/docs`

### 容器部署

```bash
docker compose up -d perm-postgres perm-redis cerbos
docker compose up -d permission-service admin-console
```

生产部署时 `permission-service` 设 `PRODUCTION=true`，凭据通过 Docker secrets 注入；
`admin-console` 的 `NEXT_PUBLIC_*` 在构建期嵌入，需用 `EXTERNAL_HOST` 指定宿主机可达地址。

### 端口

| 服务 | 宿主端口 |
| --- | --- |
| permission-service | 18080 |
| admin-console | 3002 |
| postgres / redis | 25433 / 16380 |
| cerbos (HTTP/gRPC) | 13592 / 13593 |
| keycloak | 8080 |

首次启动若 `projects` 表为空，会自动建 `rag-v14` 内置项目承载 `SERVICE_API_KEY` 等历史接入，
可在管理台删除或改造。

---

## 使用

### 外部系统鉴权 API（`/v1`）

| 端点 | 用途 |
| --- | --- |
| `POST /v1/check`、`/v1/check/batch` | 单条 / 批量权限判定，返回 `allow/deny/indeterminate` + `decision_id` |
| `POST /v1/filter`、`GET /v1/prefilter`、`POST /v1/visibility` | 检索型项目的可见性投影 |
| `POST /v1/resources/{register,link,unlink,retire}`、`PATCH /v1/resources/...` | 资源生命周期镜像 |
| `POST /v1/context` | 把 JWT 打包为带 audience 的 `ctx_token`（异步任务传递身份）|

请求头 `X-Api-Key` + `X-Client-Id` + `X-Request-Id`。推荐用 SDK：`pip install ./perm-service-client`，
`PermissionClient` 自动注入头部并透传 `traceparent`。

```python
from perm_service_client import PermissionClient
c = PermissionClient(base_url="http://host:18080", api_key="psk_…", client_id="retrieval")
print(c.check("kb:read", "kb", "kb-1")["decision"])   # "allow" / "deny"
```

### 管理 API（`/api/v1`，需 Bearer JWT）

| 前缀 | 功能 |
| --- | --- |
| `/api/v1/projects` `/tenants` | 项目 / 租户 CRUD、成员、client_id、API Key、audience |
| `/api/v1/roles` | 角色定义 CRUD、角色绑定/解绑、权限矩阵 |
| `/api/v1/acl` | ACL 授予 / 回收 / 批量 / 有效权限计算 |
| `/api/v1/restrictions` | 封禁（型一主体 / 型二资源）添加、解除、查询 |
| `/api/v1/policies` | 策略文件读写、上传、校验、版本历史 / diff |
| `/api/v1/audit` `/api/v1/simulate` `/api/v1/events/replay` | 审计查询、策略模拟、事件重放 |
| `/api/v1/auth` | 登录、token 校验/刷新、用户与组、平台访问权限、统计 |

写操作统一经 `get_current_admin`（Bearer）+ 项目范围校验 + 平台功能权限校验；`granted_by` 取自
token，忽略请求体同名字段。

### 管理台

浏览器访问 `http://localhost:3002`。平台管理员可管全部模块；项目管理员登录后管理本项目的
资源 / 角色 / 权限 / 封禁 / 策略 / 审计 / 模拟；项目查看者只读。

---

## 配置

配置类 `permission-service/app/config.py:Settings`，读 `.env` 与环境变量。常用项：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` / `DATABASE_URL_SYNC` | 本地 pg | 异步 / Alembic 连接串 |
| `CERBOS_PDP_URL` | `http://localhost:13592` | PDP 地址 |
| `REDIS_URL` / `REDIS_PASSWORD` | 本地 redis | 事件通道 |
| `KEYCLOAK_SERVER_URL` / `_REALM` / `_CLIENT_ID` | 本地 | IdP 接入 |
| `JWT_PUBLIC_KEY_PATH` / `JWT_PRIVATE_KEY_PATH` | `./config/*.pem` | 校签公钥（缺失则启动失败）/ dev-login 签发 |
| `SERVICE_API_KEY` / `CTX_TOKEN_SECRET` | 空 | 内置项目 API Key / ctx_token 密钥 |
| `PRODUCTION` | `false` | 生产模式（开启后对默认凭据等做启动阻断）|
| `LOG_FORMAT` | 空 | 设 `json` 输出结构化日志（供 Loki）|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` | OTel Collector |

以 `_FILE` 结尾的变量指向 Secret 挂载路径，存在时覆盖同名明文。完整清单见 `app/config.py:Settings`。

---

## 接入新项目（简要）

1. 管理台建项目（项目 ID = 策略目录名），登记 client_id / audience，签发 API Key。
2. 在 `cerbos/policies/{project_id}/` 写策略，定义本项目资源类型与动作（类型名加项目前缀）。
3. 建角色定义、授 ACL 或绑角色、配封禁；用 `/playground` 走真实链路验证。
4. 读 `GET /api/v1/projects/{id}/sdk-config` 接入 SDK；资源写路径接生命周期端口。

---

## 测试

```bash
cd permission-service
python -m pytest tests/ -q
```

其中 `tests/test_policy_conventions.py` 与 `tests/test_authz_scope_guard.py` 是**静态门禁**：前者
强制策略命名空间约定，后者强制"每个触及项目数据的端点都有范围校验"——新增端点若漏校验，
CI 直接失败。

---

## 目录结构

```
permission-service/     后端（api/ app/ services/ models/ schemas/ idp/ migrations/ tests/）
admin-console/          管理台前端（Next.js）
perm-service-client/    Python SDK
cerbos/policies/        Cerbos 策略（platform/ + 各项目目录）
docker-compose*.yml     编排
```

---

## 生产部署与初始账号

### 生产部署

```bash
KC_START_MODE=start bash scripts/deploy.sh    # 生产（外部 postgres + realm 自动导入）
BUILD=1 bash scripts/deploy.sh                # 强制重建镜像
```

`deploy.sh`：生成强口令/密钥 → `init_secrets`（9 个 secret，含从 RAG 同步 JWT 密钥）→
起基础设施 + Keycloak → **kcadm 幂等导入 realm/client/roles/mappers** → alembic 迁移 →
起 permission-service + admin-console + **permission-nginx**（独立入口）。

**访问入口**：
| 入口 | 地址 |
|------|------|
| 权限管理台 | `http://192.168.1.127:18081`（permission-nginx，独立于 RAG） |
| Keycloak 管理台 | `http://192.168.1.127:18081/admin` |
| RAG 前端（SSO） | `https://192.168.1.127/login` |

### 初始账号

| 账号 | 密码 | 角色 | 说明 |
|------|------|------|------|
| `admin` | `Admin@44545780` | `system_admin` | 权限平台 + RAG 超管（rag-v14 realm 业务账号） |
| `testuser` | `testpass123456` | `user` | 普通只读，联调/演示用 |
| Keycloak master | `admin` | master 管理员 | 口令在 `permission-service/config/keycloak_admin_password` |

### 新用户创建

1. Keycloak 管理台（`http://192.168.1.127:18081/admin`，master admin 登录）→ 切 realm `rag-v14`
   → Users → Add user → 设密码 → Role Mapper 勾 `system_admin`/`user` → Attributes 加 `tenant_id`。
2. 管理台 → **👥 用户与组 → 从 Keycloak 同步**。
3. 给 `user:<用户名>` 授 RAG 角色绑定（`kb_reader`/`kb_writer`）。

> ⚠️ 项目管理员会授予项目内全部知识库写/管理权限，给 RAG 只读用户请用角色绑定，勿用项目管理员。
> 详细：`docs/ops/权限系统上线运维手册.md` §2.2 与 §9（部署问题实录）。
