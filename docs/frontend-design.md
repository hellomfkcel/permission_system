# RAG v14 前端设计方案

> 基于后端现状分析 + 用户初稿设计 + 缺口诊断
> 原则：优先复用已有后端能力，新增端点只做薄封装（SQL 查询 + Pydantic 序列化）

---

## 〇、认证与租户 — 前端路由保护的最外层

### 架构定位

设计文档 §0.3 明确：认证 = "IdP（Keycloak 或企业既有）"，租户 = "JWT `tenant` claim"。本系统不内置用户管理/密码存储/SSO——认证是外部的。但前端必须处理以下流程，否则永远停留在"dev-user / tenant-dev" 的伪登录状态。

### 登录→租户→进入系统 全流程

```
用户访问 / → 检查 localStorage 是否有 token
  ├── 无 token → 跳转 /login
  │     ├── 开发模式：输入用户名 + 选择租户 → 后端签发 dev JWT
  │     └── 生产模式：跳转 IdP (Keycloak) → 回调 /auth/callback → 解析 JWT
  │
  └── 有 token → 检查 token 是否过期
        ├── 已过期 → 清除 token → 跳转 /login
        └── 未过期 → 进入系统
              └── 首次登录或切换租户 → /select-tenant 页面（多租户用户）
```

### 登录页 `/login`

```
┌──────────────────────────────────────────────┐
│                                              │
│         🔐 RAG v14 企业知识库平台              │
│                                              │
│    ┌──────────────────────────────────┐      │
│    │  开发模式                          │      │
│    │  ┌──────────────────────────┐    │      │
│    │  │ 用户名:  [______________] │    │      │
│    │  │ 租户:    [tenant-dev  ▼] │    │      │
│    │  │ 角色:    [普通用户   ▼]  │    │      │
│    │  └──────────────────────────┘    │      │
│    │  [ 登录 ]                         │      │
│    └──────────────────────────────────┘      │
│                                              │
│    ┌──────────────────────────────────┐      │
│    │  生产模式 (SSO)                    │      │
│    │  [ 通过 Keycloak 登录 ]            │      │
│    └──────────────────────────────────┘      │
│                                              │
└──────────────────────────────────────────────┘
```

**开发模式行为**：
1. 调用 `POST /api/v1/auth/dev-login` `{username: "admin", tenant: "tenant-dev", role: "system_admin"}` → 后端签发 JWT（RS256 自签）
2. 前端存 `access_token` 到 localStorage + 存 `expires_at`
3. 跳转到 `/select-tenant` 或直接进入系统

**生产模式行为**：
1. 前端重定向到 IdP 登录页
2. IdP 回调 `/auth/callback?code=xxx` → 后端 `POST /api/v1/auth/token` 换取 `access_token` + `refresh_token`
3. 存 token + 跳转

### 会话过期与自动退出

| 配置项 | 开发模式 | 生产模式 | 来源 |
|--------|---------|---------|------|
| access_token 有效期 | 1 小时（默认） | JWT `exp` 字段 | JWT claim |
| 自动刷新 | ❌ 开发模式不刷新 | ✅ `refresh_token` 换新 token（过期前 5 分钟） | OAuth2 refresh |
| 强制退出 | 过期立即跳 `/login` | 同上 | — |
| 退出按钮 | Header 右侧 "退出登录" → 清除 token → `/login` | 同左 | — |

**前端实现**：Axios interceptor 拦截 401 → 清除 token → 跳转 `/login`。Zustand store 记录 `lastActivity`，每 30 秒检查，若超 1 小时无操作则清 token 跳登录。

### 登录后如果用户有多个租户 → `/select-tenant`

```
┌──────────────────────────────────────────────┐
│  请选择要进入的租户                            │
│                                              │
│  ┌──────────────────────────────────────┐    │
│  │ 🏢 开发测试租户          (tenant-dev) │    │
│  │    3 个知识库, 142 个文档              │    │
│  │                              [进入]  │    │
│  ├──────────────────────────────────────┤    │
│  │ 🏢 生产租户               (prod-01)  │    │
│  │    8 个知识库, 1,240 个文档           │    │
│  │                              [进入]  │    │
│  └──────────────────────────────────────┘    │
│                                              │
│  [ 退出登录 ]                                 │
└──────────────────────────────────────────────┘
```

### 全局 Header 更新 — 加入用户信息

```
┌──────────────────────────────────────────────────────────────┐
│  🏠 RAG v14  [📚 KB选择器 ▼]          🏢 tenant-dev  👤 张三  ⚙️ 设置  🚪 退出 │
│                                          ↑ 当前租户    ↑ 用户名            ↑ 退出 │
└──────────────────────────────────────────────────────────────┘
```

- 租户名右侧下拉箭头 → 切换租户（多租户用户）
- 用户名 → 无交互（仅展示）
- 退出按钮 → `logout()` → 清除 localStorage → `/login`

---

## 一、全局组件：KB 选择器 — 必须放在最顶层

KB 选择器是上传和对话的共同入口，不是某个页面内部组件。放在应用 Header 或全局导航中。

```
┌─────────────────────────────────────────────────────┐
│  🏠 RAG v14    [📚 KB选择器 ▼]         ⚙️ 设置     │  ← 全局 Header
│                 已选: 开发测试KB ×                    │
├─────────────────────────────────────────────────────┤
│                                                     │
│   <当前页面内容>                                      │
│                                                     │
└─────────────────────────────────────────────────────┘
```

**行为**：
- 下拉列表只显示用户有 `kb:read` 权限的 KB（后端 prefilter 已实现）
- 支持多选（对话场景选多个 KB 做交叉检索）
- 选中状态持久化到 URL query params（`?kb_ids=xxx,yyy`）或 Zustand store
- 上传页面自动继承当前选中的 KB

**对应后端**：需新增 `GET /api/v1/knowledge-bases`（thin wrapper：`SELECT * FROM knowledge_bases` → 对每个 KB 调 Cerbos check kb:read → 返回有权限的子集）

---

## 二、页面结构（你的初稿四页保留，调整后）

```
RAG v14 前端
├── 🔐 /login                    → 登录页（开发模式 / SSO 生产模式）
├── 🏢 /select-tenant            → 租户选择页（仅多租户用户）
│
├── 🏠 全局 Header（KB选择器 + 租户名 + 用户名 + 退出）
│
├── 📂 知识库管理页  /kb
│   ├── 左侧目录树                → 两种类型（kb_bound 自动 + manual 手动）
│   ├── 上传区                    → Dropzone + Progress
│   ├── 文档列表                  → DataTable（多选 + 批量操作 + 状态轮询 + 解析触发）
│   └── 文档预览/Chunk            → Sheet（右侧滑出）
│
├── 💬 对话页  /chat
│   ├── 新建/选择会话    → 左侧会话列表
│   ├── 消息流 + 流式输出 → SSE + useChat
│   ├── 引用卡片         → HoverCard 显示 chunk 原文
│   └── 输入框           → Textarea + 发送
│
├── ⚙️ 设置页  /settings
│   ├── 模型选择         → Select（从 DB model_registry 读）
│   ├── 检索参数         → Slider（top_k / oversample / strict）
│   └── Prompt 模板选择  → Select（v1/v2）
│
└── 📊 Dashboard  /dashboard
    ├── 使用统计         → Recharts（从 audit_logs 聚合）
    └── 检索质量         → RAGAS 指标可视化
```

---

## 三、各页面的数据流、后端端点、前端状态

### 3.0 KB 生命周期管理 — 创建、重命名、删除

KB 是系统的顶层资源。用户进入知识库管理页时如果还没有 KB，应该能直接创建。

```
创建流程:
  点击 [+ 新建知识库] → Dialog 弹出
  → 填写 name(必填) + description(可选)
  → 选择切分策略（sentence/word/passage，默认 sentence）
  → POST /api/v1/knowledge-bases → 返回 kb_id
  → resource_registry 自动注册(kb, kb_id)
  → chunking_configs 自动插入默认配置
  → retrieval_configs 自动插入默认配置
  → kb_bound 类型目录自动创建
  → 全局 KB 选择器自动刷新列表

重命名:
  KB 名称右侧铅笔图标 → 点击触发内联编辑
  → PATCH /api/v1/knowledge-bases/{id} (body: {name: "新名称"})
  → 关闭编辑模式 → KB 选择器自动刷新

删除:
  KB 名称右侧删除图标 → 确认 Dialog
  → DELETE /api/v1/knowledge-bases/{id}
  → 前置：KB 下无活跃文档挂载（否则返回 409）
  → 成功后 retire_resource(kb, kb_id) + 级联清理关联配置 + 删除 kb_bound 目录
```

| 前端操作 | HTTP 方法 + 路径 | 权限 | 后端现状 |
|---------|-----------------|------|---------|
| 创建 KB | `POST /api/v1/knowledge-bases` | `kb:manage`（system_admin 或 tenant admin） | ❌ 需新增—`INSERT INTO knowledge_bases` + `register_resource` + seed chunking/retrieval configs + 创建 kb_bound 目录 |
| 重命名 KB | `PATCH /api/v1/knowledge-bases/{id}` | `kb:manage` | ❌ 需新增—`UPDATE knowledge_bases SET name=$1` |
| 删除 KB | `DELETE /api/v1/knowledge-bases/{id}` | `kb:manage` | ❌ 需新增—级联清理 + `retire_resource` |
| 更新 KB 切分配置 | `PATCH /api/v1/knowledge-bases/{id}/chunking-config` | `kb:write` | ❌ 需新增—更新或新增 chunking_configs 版本 |

### 3.1 知识库管理页

```
状态流转:
  KB选择器(全局) ──选KB──→ 左侧目录树 + 右侧文档列表
  无KB时 → 空状态页 + [创建第一个知识库] 按钮
```

**页面布局（参照 RAGFlow 两栏结构）**：

```
┌──────────────────────────────────────────────────────────────┐
│  📂 知识库管理                                               │
├────────────┬─────────────────────────────────────────────────┤
│            │  [上传文件] [新建文件夹] [批量删除] [批量解析]      │
│  📁 目录树  │                                                 │
│            │  ┌─────────────────────────────────────────────┐ │
│  📄 KB绑定  │  │ ☐ 文件名       大小   状态    时间   操作    │ │
│    (自动)   │  │ ☐ xxx.md      12KB   ✅完成  2h前  👁📥🗑  │ │
│            │  │ ☐ yyy.pdf     5MB    ⏳处理中 1h前  👁📥🗑  │ │
│  📁 手动目录 │  │ ☐ zzz.txt     1KB    ❌失败  30m前 👁📥🗑🔄│ │
│    项目A    │  └─────────────────────────────────────────────┘ │
│    项目B    │                                                 │
│            │  状态图例: ✅已完成 ⏳队列中 🔄处理中 ❌失败 🛑停用 │
│  [+ 新建]  │                                                 │
├────────────┴─────────────────────────────────────────────────┤
│  当前目录: 全部文档 (42 个文件)   已选: 3 个                   │
└──────────────────────────────────────────────────────────────┘
```

**关键交互**：

| 交互 | 触发方式 | 行为 |
|------|---------|------|
| 多选文件 | DataTable 第一列 checkbox | 顶部出现批量操作栏 |
| 批量删除 | 选中 ≥1 文件后点 [批量删除] | 确认 Dialog → 逐文件调 `DELETE /documents/{id}/kb/{kb_id}` → 某文件权限不足单独失败 |
| 批量解析 | 选中 ≥1 未解析文件后点 [批量解析] | 逐文件调 `trigger_parse`，前端轮询状态变绿 |
| 单文件重新解析 | 状态为 ❌失败 或 ✅已完成 的文件行右侧 🔄 按钮 | `trigger_parse(mount_id)` — 适用于切分配置变更后重新解析 |
| 文件移动 | 拖拽文件到左侧目录树的目标文件夹 | `PATCH /api/v1/documents/{id}/directory` {directory_id} |
| 文件重命名 | 文件名右侧铅笔图标 | `PATCH /api/v1/documents/{id}` {filename} — 仅改显示名，不改存储路径 |
| 下载 | 📥 图标 | `GET /api/v1/documents/{id}/download` → 签名 URL（需 `doc:download` 权限） |

**解析触发（§13.4.2 按需而非自动）**：

上传文件后 `auto_parse` 默认 `false`——文件出现在列表中但状态为 `not_parsed`。用户必须在文件行右侧点击 🔄 "解析" 按钮，或勾选多个未解析文件后点 [批量解析]，此时才真正触发 `trigger_parse → DocumentMounted 事件 → B-INGEST 摄入 Pipeline`。这样用户可以在解析前确认切分配置无误。

**解析状态轮询**：

```
not_parsed ──trigger_parse──→ queued ──worker pickup──→ processing ──success──→ completed
                                  │                          │
                                  └──────────────────────────┴── failure ──→ failed
```

前端 TanStack Query 每 3 秒 `GET /api/v1/documents/{id}` 轮询 `parse_status`，状态变化时自动更新 Badge 颜色：
- `not_parsed` — gray
- `queued` — blue (pulsing)
- `processing` — yellow (pulsing + progress spinner)
- `completed` — green
- `failed` — red (hover 显示 failure_reason tooltip)

**目录树**（§13.5 两种类型）：

| 目录类型 | 前端展示 | 操作 |
|---------|---------|------|
| `kb_bound` | 📄 图标，名称 = KB 名称，斜体灰色 | 不可删除、不可重命名。内容 = 该 KB 下所有挂载文档的聚合视图 |
| `manual` | 📁 图标，名称可自定义 | 可删除、可重命名、可拖入文档。内容来自 `document_directory_entry` 表 |

目录树的 CRUD：

| 操作 | 端点 | 权限 |
|------|------|------|
| 列出目录 | `GET /api/v1/knowledge-bases/{kb_id}/directories` | `kb:read` |
| 创建手动目录 | `POST /api/v1/directories` {name, parent_id, kb_id} | `kb:write` |
| 重命名目录 | `PATCH /api/v1/directories/{id}` | `kb:write` |
| 删除空目录 | `DELETE /api/v1/directories/{id}` | `kb:write` |
| 移动文档到目录 | `POST /api/v1/directories/{id}/documents` {document_id} | `doc:view` + `kb:write` |
| 从目录移除文档 | `DELETE /api/v1/directories/{id}/documents/{doc_id}` | `kb:write` |

### 3.2 知识库管理页 — 组件树更新

```tsx
<KnowledgeBasePage>
  <div className="flex h-full">
    {/* 左侧目录树 */}
    <DirectorySidebar className="w-64">
      <DirectoryTree>
        <KBBoundDirectory name="开发测试KB" docCount={42} icon="📄" />
        <ManualDirectory name="项目A" docCount={15} icon="📁" 
          onRename={handleRenameDir} onDelete={handleDeleteDir} />
        <ManualDirectory name="项目B" docCount={8} icon="📁" />
      </DirectoryTree>
      <CreateDirectoryButton onClick={openCreateDirDialog} />
    </DirectorySidebar>

    {/* 右侧主区域 */}
    <div className="flex-1">
      {/* 工具栏 */}
      <DocumentToolbar>
        <UploadButton kbId={selectedKB} />           ← 上传文件
        <CreateDirectoryButton />                     ← 新建文件夹
        <BatchDeleteButton disabled={selectedCount===0} />  ← 批量删除
        <BatchParseButton disabled={selectedCount===0} />   ← 批量解析
        <BatchMoveButton disabled={selectedCount===0} />    ← 批量移动到目录
      </DocumentToolbar>

      {/* 文档表格 */}
      <DocumentTable 
        selectable          // ← checkbox 多选
        onSelectionChange={setSelected}
      >
        <columns: ☐ | 文件名(可编辑) | 大小 | 状态Badge(轮询) | 上传时间 | 操作 />
        <row.operations>
          <Button "预览" onClick={openSheet} />
          <Button "下载" onClick={download} />          ← 需 doc:download 权限
          <Button "解析" onClick={triggerParse} />      ← 需 kb:write 权限
          <Button "停用/启用" onClick={toggleEnabled} /> ← 需 kb:write 权限
          <Button "删除" onClick={confirmDelete} />     ← 需 doc:unmount 权限
        </row.operations>
      </DocumentTable>

      {/* 文档预览 Sheet */}
      <DocumentSheet docId={selectedDoc}>
        ...
      </DocumentSheet>
    </div>
  </div>
</KnowledgeBasePage>
```

### 3.3 对话页 — 核心交互链

```
状态流转:
  1. 新建会话: POST /conversations → 返回 conversation_id
  2. 用户在 KB选择器中选绑定 KB(s)
  3. 用户输入问题 → POST /conversations/query (body: {question, kb_ids, conversation_id})
     → 后端返回 session_id + answer + chunk_ids (同步)
  4. SSE 频道实时显示 typing effect
     GET /conversations/{id}/stream → events: retrieved/token/done
```

| 前端操作 | HTTP 方法 + 路径 | 后端现状 |
|---------|-----------------|---------|
| 列出历史会话 | `GET /api/v1/conversations` | ❌ 需新增—`SELECT FROM conversations WHERE tenant_id=$1 AND user_id=$2` |
| 新建会话 | `POST /api/v1/conversations` | ❌ 需新增—`INSERT INTO conversations`，无 KB 绑定时为 `bound_kb_ids=[]` |
| 发起查询 | `POST /api/v1/conversations/query` | ✅ 已有 |
| SSE 流式接收 | `GET /api/v1/conversations/{id}/stream` | ✅ 已有 |
| 删除会话 | `DELETE /api/v1/conversations/{id}` | ❌ 需新增—`DELETE FROM conversations WHERE id=$1` |

**⚠️ 引用卡片的阻塞点**：当前 `retrieved` 流事件只传 `chunk_ids: [...]`，不传原文。引用卡片显示原文需要**改后端流事件**——在 `retrieved` 事件中多传 `chunks: [{id, content: 前200字}]`。这是 5 行代码的改动。

```tsx
<ChatPage>
  <Sidebar>
    <NewChatButton />
    <ConversationList>
      <ConversationItem key={id} title={firstQuestion} onClick={switchConv} />
    </ConversationList>
  </Sidebar>
  
  <MainChatArea>
    <MessageList>
      <Message role="user" content={question} />
      <Message role="assistant">
        <StreamingText content={answer} />  ← SSE driven
        <SourcesBar>
          <SourceCard chunk={chunk} />  ← HoverCard 显示原文
        </SourcesBar>
      </Message>
    </MessageList>
    
    <InputBar kbIds={selectedKBs} onSubmit={sendQuery} />
  </MainChatArea>
</ChatPage>
```

### 3.4 设置页

| 前端操作 | HTTP 方法 + 路径 | 后端现状 |
|---------|-----------------|---------|
| 列出可用模型 | `GET /api/v1/models` | ❌ 需新增—`SELECT FROM model_registry` |
| 获取当前检索配置 | `GET /api/v1/configs/retrieval?kb_id=X` | ❌ 需新增—`resolve_retrieval_config` → JSON |
| 更新检索配置 | `PATCH /api/v1/configs/retrieval` | ❌ 需新增—`UPSERT retrieval_configs` |
| 获取 Prompt 模板列表 | `GET /api/v1/prompts` | ❌ 需新增—`SELECT FROM prompt_templates` |
| 设置默认 Prompt 版本 | `PATCH /api/v1/prompts/{id}/activate` | ❌ 需新增—`UPDATE prompt_templates SET is_active=true` |

### 3.5 文档搜索、筛选与排序

KB 内文档数量可能很大（数百~数万），DataTable 仅靠浏览器排序不够。前端应在表格上方提供搜索栏 + 状态过滤器。

```
┌──────────────────────────────────────────────────────────────┐
│  🔍 [搜索文件名...]  [状态: 全部 ▼]  [排序: 上传时间 ▼]     │
└──────────────────────────────────────────────────────────────┘
```

| 操作 | 实现 |
|------|------|
| 搜索 | 前端 debounce 300ms → `GET /api/v1/knowledge-bases/{kb_id}/documents?search=xxx`，后端 `WHERE filename ILIKE '%xxx%'` |
| 状态筛选 | Dropdown: 全部 / 已完成 / 处理中 / 队列中 / 失败 / 未解析 / 已停用 |
| 排序 | 按上传时间 / 文件名 / 文件大小 升序/降序，后端 `ORDER BY` |

**对应后端**：在已有的 `GET /api/v1/knowledge-bases/{kb_id}/documents` 上加 query params `?search=&status=&sort_by=&order=`

### 3.6 降级与错误状态处理

设计文档 §25.3 规定了两种必须可区分的"无结果"状态。前端不能把它们混为一谈。

| 场景 | HTTP 状态 | error_code | 前端展示 |
|------|----------|-----------|---------|
| 检索到文档但无权查看 | 200 | `retrieve:insufficient_evidence` | "未找到足够信息"（普通文本，无特殊 UI） |
| 权限服务不可达（熔断器打开） | 503 | `auth:authz_unavailable` | "服务暂时不可用，请稍后重试"（⚠️ 警告样式 + 重试按钮） |
| 向量库不可达 | 503 | `retrieve:vector_store_unavailable` | "检索服务暂时不可用"（⚠️ 警告 + 自动重试倒计时） |
| 网络错误/超时 | — | — | Toast 通知 + "请求失败，点击重试" |
| LLM 生成超时 | — | `chat:stream_timeout` | SSE 流中 `error` 事件 → "生成超时，请重试" |

**全局错误处理**：
- Axios response interceptor：401 → 跳 `/login`；403 → Toast "权限不足"；500 → "服务器内部错误"
- React Error Boundary 包裹每个页面，捕获未预期崩溃 → 显示 "页面出错了，[刷新]"

**DataTable Loading/Empty/Error 三态**：
- **Loading**：Skeleton 行（占位闪烁）
- **Empty**：空状态插图 + "暂无文档，点击上传第一个"
- **Error**：红色提示 + "加载失败，[重试]"

### 3.7 管理台跳转入口

设计文档 §13.4 明确："授予/回收权限不在本系统——管理台直连权限服务；本系统 UI 至多提供跳转入口"。

前端需要在以下位置提供**跳转到管理台**的链接（生产环境配置 `ADMIN_CONSOLE_URL`）：

| 位置 | 触发方式 | 说明 |
|------|---------|------|
| 设置页底部 | 卡片 "🔗 权限管理" → 跳转管理台 | "文档授权、角色绑定、访问控制请前往管理台操作" |
| KB 管理页 Header | KB 名称旁的 "👥" 图标 | 跳转到该 KB 的授权页面 |
| 无权限提示 | 403 错误页面 | "您没有此操作的权限。如需申请权限，请前往 [管理台]" |

### 3.8 文件上传区域

```
┌──────────────────────────────────────────────────────┐
│                                                      │
│           📁 拖拽文件到此处，或点击选择文件             │
│                                                      │
│           支持格式: .txt .md .pdf .docx .html .csv     │
│           单个文件最大: 50MB                           │
│                                                      │
│                      [选择文件]                        │
│                                                      │
└──────────────────────────────────────────────────────┘
```

| 交互 | 行为 |
|------|------|
| 拖拽上传 | shadcn/ui Dropzone 组件，高亮拖入区域 |
| 文件格式校验 | 前端检测扩展名 + MIME type，不支持的格式弹出 Toast "不支持的格式" |
| 大小校验 | 超过 50MB 立即拒绝，不上传 |
| 上传进度 | Progress bar（axios `onUploadProgress`） |
| 上传完成 | Toast "上传成功" + DataTable 自动刷新 |
| 上传失败 | Toast "上传失败: [原因]" + 保留文件在队列中供重试 |

### 3.9 设置页补充

除了已列的模型选择/检索参数/Prompt 模板，设置页还需要：

| 项目 | 说明 | 对应后端 |
|------|------|---------|
| **Prompt 模板选择** | 下拉选择 prompt_id + version（如 default v1 / default v2） | `GET /api/v1/prompts` + 当前 active 标记 |
| **Preview 面板** | 选中模板后显示渲染预览（"基于以下文档...问题：{{ query }}"） | 纯前端 Jinja2 近似渲染 |
| **Langfuse 跳转** | "📊 模型观测 → Langfuse" | `LANGFUSE_HOST` 环境变量 |

### 3.10 Dashboard

| 前端操作 | HTTP 方法 + 路径 | 后端现状 |
|---------|-----------------|---------|
| 使用统计 | `GET /api/v1/stats/usage?days=30` | ❌ 需新增—`SELECT count(*), date_trunc FROM audit_logs GROUP BY` |
| 检索质量趋势 | `GET /api/v1/stats/quality` | ❌ 需新增—读取 `metrics/baseline.json` + RAGAS 历史 |
| **日均查询量** | 同上 | 折线图 (Recharts LineChart) |
| **Top 活跃 KB** | `GET /api/v1/stats/top-kbs` | `SELECT kb_id, count(*) FROM conversation_turns GROUP BY` |
| **文档处理统计** | `GET /api/v1/stats/documents` | `SELECT parse_status, count(*) FROM ingest_executions GROUP BY` |

---

## 四、新增 REST 端点汇总 — 33 个薄封装

全部是已有 Python 函数的薄包装，不需要新业务逻辑：

| # | 端点 | 方法 | 参数 | 权限 | 对应后端函数/表 | 优先级 |
|---|------|------|------|------|---------------|--------|
| **认证与租户** | | | | | | |
| 1 | `/api/v1/auth/dev-login` | POST | {username, tenant, role} | — | 自签 JWT (RS256) | 🔴 P0—开发模式登录 |
| 2 | `/api/v1/auth/token` | POST | {code} (OAuth2) | — | IdP code → JWT | 🟡 P1—生产模式登录 |
| 3 | `/api/v1/auth/refresh` | POST | {refresh_token} | — | refresh_token → 新 JWT | 🟡 P1—token 自动刷新 |
| **租户** | | | | | | |
| 4 | `/api/v1/tenants` | GET | user_id | — | 从 JWT claims 或 `knowledge_bases` 表反查用户所属租户 | 🔴 P0—多租户选择 |
| 5 | `/api/v1/tenants/{id}/stats` | GET | tenant_id | — | KB 数量、文档总数 | 🟡 P1—租户选择页展示 |
| **KB 管理** | | | | | | |
| 6 | `/api/v1/knowledge-bases` | GET | tenant_id | `kb:read` | `knowledge_bases` 表 + Cerbos check | 🔴 P0 |
| 7 | `/api/v1/knowledge-bases` | POST | {name, description, chunking_strategy} | `kb:manage` | INSERT + register_resource + seed configs + 创建 kb_bound 目录 | 🔴 P0 |
| 8 | `/api/v1/knowledge-bases/{id}` | PATCH | {name, description} | `kb:manage` | `UPDATE knowledge_bases` | 🟡 P1 |
| 9 | `/api/v1/knowledge-bases/{id}` | DELETE | — | `kb:manage` | 级联清理 + retire_resource + 删 kb_bound 目录 | 🟡 P1 |
| 10 | `/api/v1/knowledge-bases/{id}/chunking-config` | PATCH | {strategy, split_length, split_overlap} | `kb:write` | 更新/新增 chunking_configs 版本 | 🟡 P1 |
| **目录管理** | | | | | | |
| 11 | `/api/v1/knowledge-bases/{kb_id}/directories` | GET | kb_id | `kb:read` | `directories` 表 + `document_directory_entry` 计数 | 🔴 P0 |
| 12 | `/api/v1/directories` | POST | {name, parent_id, kb_id} | `kb:write` | `INSERT INTO directories` (type=manual) | 🟡 P1 |
| 13 | `/api/v1/directories/{id}` | PATCH | {name} | `kb:write` | `UPDATE directories SET name=$1` (仅 manual 类型) | 🟡 P1 |
| 14 | `/api/v1/directories/{id}` | DELETE | — | `kb:write` | 前置: 目录为空 | 🟡 P1 |
| 15 | `/api/v1/directories/{dir_id}/documents` | POST | {document_id} | `doc:view`+`kb:write` | `INSERT INTO document_directory_entry` | 🟡 P1 |
| 16 | `/api/v1/directories/{dir_id}/documents/{doc_id}` | DELETE | — | `kb:write` | `DELETE FROM document_directory_entry` | 🟡 P1 |
| **文档管理** | | | | | | |
| 17 | `/api/v1/knowledge-bases/{kb_id}/documents` | GET | kb_id, directory_id(optional) | `kb:read` | `document_kb_mounts JOIN documents` | 🔴 P0 |
| 18 | `/api/v1/documents/{doc_id}` | GET | doc_id | `doc:view` | `documents` 表 | 🟡 P1 |
| 19 | `/api/v1/documents/{doc_id}` | PATCH | {filename} | `kb:write` | `UPDATE documents SET filename=$1` | 🟡 P1 |
| 20 | `/api/v1/documents/{doc_id}/chunks` | GET | doc_id | `doc:view` | Milvus query by document_id, limit 100 | 🟡 P1 |
| 21 | `/api/v1/documents/{doc_id}/content` | GET | doc_id | `doc:view` | P-STORE.get() → 返回文本 | 🟡 P1 |
| 22 | `/api/v1/documents/{doc_id}/download` | GET | doc_id | `doc:download` | P-STORE.generate_presigned_url() → 302 重定向 | 🟡 P1 |
| 23 | `/api/v1/documents/upload` | POST | file + kb_id + auto_parse | `kb:write` | `submit_ingest_task` | ✅ 已有 |
| 24 | `/api/v1/documents/{doc_id}/kb/{kb_id}` | DELETE | purge | `doc:unmount`/`doc:purge` | `delete_document_from_kb` | ✅ 已有 |
| 25 | `/api/v1/documents/{doc_id}/kb/{kb_id}` | PATCH | is_enabled | `kb:write` | `UPDATE document_kb_mounts` | 🟡 P1 |
| 26 | `/api/v1/documents/{doc_id}/trigger-parse` | POST | mount_id | `kb:write` | `trigger_parse(mount_id)` | 🔴 P0—解析触发 |
| **批量操作** | | | | | | |
| 27 | `/api/v1/documents/batch/delete` | POST | [{doc_id, kb_id}] | 逐资源独立 check | `check_batch` → 逐个 `delete_document_from_kb` | 🟡 P1 |
| 28 | `/api/v1/documents/batch/parse` | POST | [{mount_id}] | 逐资源独立 check | `check_batch` → 逐个 `trigger_parse` | 🟡 P1 |
| **会话** | | | | | | |
| 29 | `/api/v1/conversations` | GET | tenant_id | `kb:read` | `conversations` 表 | 🔴 P0 |
| 30 | `/api/v1/conversations` | POST | {kb_ids} | `kb:read` | `INSERT INTO conversations` | 🔴 P0 |
| 31 | `/api/v1/conversations/{id}` | DELETE | — | — | `DELETE FROM conversations` | 🟡 P1 |
| **设置** | | | | | | |
| 32 | `/api/v1/models` | GET | — | — | `model_registry` 表 | 🔴 P0 |
| 33 | `/api/v1/configs/retrieval` | GET/PATCH | kb_id | `kb:read`/`kb:write` | `resolve_retrieval_config` / `UPSERT retrieval_configs` | 🟡 P1 |
| 34 | `/api/v1/prompts` | GET | — | — | `prompt_templates` 表 | 🟡 P1 |

**P0 (11 个) — 立即需要**: 认证、租户列表、KB 列表、创建 KB、目录树、文档列表、上传、解析触发、会话列表/新建、模型列表

**P1 (23 个) — 完整体验需要**: 目录 CRUD、文档 move/rename、批量操作、配置管理、chunk 查看、token refresh

---

## 五、前端技术栈建议

| 层 | 选型 | 理由 |
|----|------|------|
| 框架 | Next.js 14 (App Router) | SSR 不是刚需但路由约定好，`/api` 代理到 FastAPI 方便 |
| 组件库 | shadcn/ui | 你初稿已选 ✅ |
| AI SDK | Vercel AI SDK `useChat` | 原生消费 SSE `text/event-stream` |
| 状态管理 | Zustand | 比 Redux 轻，KB 选择器 / 会话列表两个 store 够用 |
| 数据获取 | TanStack Query | 自动 refetch + 缓存 + 轮询（文档状态轮询） |
| 图表 | Recharts (Dashboard) | 你初稿已选 ✅ |
| 部署 | 静态导出 `next export` → Nginx 反代 → FastAPI:8000 | 前后端分离，同域部署 |

---

## 六、实施顺序

**不要从首页开始** — 按依赖顺序来：

1. **认证 + 登录页**（后端：`POST /api/v1/auth/dev-login` 签发 JWT + 前端：`/login` 页面 + Axios interceptor + session timer）
   - 没有它，所有后续页面都依赖"当前用户是谁、属于哪个租户"
2. **租户选择页**（后端：`GET /api/v1/tenants` + 前端：`/select-tenant`）
3. **新增 8 个核心 P0 REST 端点**（KB 列表+创建、目录树、文档列表、解析触发、会话列表/新建、模型列表）
4. **全局 Header**（KB 选择器 + 租户名 + 用户名 + 退出按钮 — 需要步骤 1-3 的端点全到位）
5. **对话页骨架**（`useChat` + SSE + 引用卡片 + 修复流事件加 chunk 原文）
6. **知识库管理页**（上传 + 列表 + 目录树 + KB 创建/重命名/删除 + 批量操作）（需要 P0+P1 端点全到位）
7. **设置页**（模型选择 + 参数调节 — 只读模式即可）
8. **Dashboard**（最后一步，P2，纯查询聚合）

按这个顺序，步骤 1-4 完成就能跑通"选 KB → 提问 → 流式回答 → 看引用"的完整核心链路。
