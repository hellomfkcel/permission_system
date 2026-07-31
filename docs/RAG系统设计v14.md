# RAG 系统设计 v14（企业级多知识库方案 · 权限外置 + Haystack 框架版）

> **核心定位**：本文档是完整独立的设计规格，无需参考 v13 即可阅读和开发。
>
> **与 v13 的唯一结构性变更**：以 Haystack 2.x 替换 LlamaIndex 作为算法框架底座。权限设计、模块边界、单一写者原则、事件契约、fail-closed 纪律、联合契约测试——一条不变。
>
> **Haystack 替本系统扛下的横切平台功能：**
>
> | 原需自研 | Haystack 原生替代 | 减负 |
> |---------|-----------------|------|
> | 管道编排引擎 | `Pipeline` 类 + YAML 序列化 + DAG 执行 | ⭐⭐⭐⭐⭐ |
> | 组件协议/防腐层类型约束 | `@component` 装饰器 + I/O 类型注解 | ⭐⭐⭐⭐ |
> | OTel 埋点（节点级） | 内置 OpenTelemetry tracing，节点即 span | ⭐⭐⭐⭐ |
> | 向量库/模型适配器 | 官方 Integrations（Milvus、BGE-M3、LiteLLM） | ⭐⭐⭐⭐⭐ |
> | MetadataFilter 标准化 | `MetadataFilter` + `FilterPolicy` 标准接口 | ⭐⭐⭐ |
>
> **Haystack 未覆盖、本系统仍需自建：** P-AUTHC 权限适配层、盖戳管道、写路径结构镜像、Celery 三队列异步调度（Haystack Pipeline 是同步的，需包装）、所有平台模块。
>
> **三条红线：**
> 1. **权限绝不内化**：六条件过滤由 P-AUTHC 编译为 `MetadataFilter` 注入 Retriever，Component `run()` 内部零权限逻辑。
> 2. **异步队列不降级**：Haystack Pipeline 只在 worker 进程内同步执行，API 进程禁止调 `pipeline.run()`。
> 3. **契约测试先于集成**：四个能力契约（切分/融合/层级合并/合成）先写测试，Haystack 实现跑通后才进集成。

## 阅读指引

- **要开发某个模块**：读「第 0 章模块注册表 + 第一部分共享契约 + 该模块章节 + 你依赖的接口签名」即可开工。
- **要做变更评审**：用 §0.2 依赖方向规则做客观判定。
- **★ 要动权限相关任何东西**：读「§6 P-AUTHC 全章 + §6A 五端点调用契约 + §14.5 盖戳管道 + §15.1/§15.2 三层检索链路 + §20 场景一」。
- **凡本文提及"Haystack Component"**：均指以 `@component` 装饰器声明、具有显式 `run()` 方法和类型安全 I/O 的 Haystack 2.x 原生组件。凡提及"Pipeline"均指 `haystack.Pipeline`（DAG 结构，可 YAML 序列化）。
- **契约类型标注**（贯穿全文）：`【REST】` 系统对外 HTTP 端点；`【内部接口】` 模块间进程内调用契约；`【事件】` 异步协作契约，一律经 Outbox 可靠投递；`【数据·独占】` 某模块独占的表；`【外部契约】` 与权限服务之间的调用约定，变更需双方会签。

---

# 第 0 章：模块注册表与全局规则

## 0.1 模块注册表

系统划分为**平台能力模块**（横切、被依赖）与**业务模块**（纵向、依赖平台）。每个模块声明：职责（做什么/明确不做什么）、提供的接口、依赖的接口、独占数据、发布/消费事件。

### 0.1.1 平台能力模块

| 模块 | 职责（做什么 / 不做什么） | 提供的接口 | 独占数据 | 发布/消费事件 |
|-----|------------------------|-----------|---------|-------------|
| **P-AUTHC 权限消费** | 做：JWT 透传与 ctx 构建、调用外部权限服务五端点并做三态映射、路由级拦截、ctx_token 铸造代理、在写路径同步调用生命周期端口、prefilter 请求内缓存。不做：**不存任何权益数据（无 acl/role_binding 表）、不写任何策略（无 Rego/OPA）、不做任何判定**（含"顺手的 `if owner then pass`"短路） | `build_context`、`require_permission`、`check`、`check_batch`、`filter_items`、`get_prefilter`、`mint_ctx_token`、`register_resource`/`link_resource`/`unlink_resource`/`retire_resource` | 无独占权益数据（仅 prefilter 请求内缓存，进程内不落盘） | 发布：`AUTH_ALLOW`/`AUTH_DENY`（经审计）。消费：`VisibilityChanged`（权限服务事件流，转交 B-INGEST 盖戳） |
| **P-AUDIT 审计** | 做：接收审计事件、按风险分级落库、归档、记录 decision_id 串联。不做：不理解业务语义 | `emit_audit_event`、`emit_audit_event_txn` | `audit_log` 表 | 消费：所有业务模块审计调用 |
| **P-OBS 可观测** | 做：Trace 自动埋点（**Haystack Pipeline 节点自动产生 span，无需额外埋点**）、结构化日志、Metric 采集，统一经 OTel Collector 单一出口，Grafana 单一查询入口。不做：不评判检索质量、不承担模型观测、不做审计 | OTel 自动插桩（含 Haystack 内置 tracing）、`logger`、Metric 门面 | 统一可观测栈 | 无领域事件 |
| **P-TASK 任务基础设施** | 做：异步任务提交、可靠投递、结果回传、健康检查、Outbox relay 投递。**Haystack Pipeline 的同步执行在此模块内包装为 Celery 任务**（`run_pipeline_async`）。不做：不持有业务逻辑 | `submit_task`、`subscribe_stream`、`/healthz`、`/readyz`、`outbox_relay`、**`run_pipeline_async`** | Celery/Redis | 无领域事件 |
| **P-STORE 对象存储** | 做：文件读写、签名 URL、删除。不做：不理解文件语义 | `StorageBackend.put/get/generate_presigned_url/delete` | 对象存储后端（SeaweedFS S3 网关） | 无领域事件 |
| **P-MODEL 模型与 Prompt 注册** | 做：模型别名解析、连接配置、Prompt 版本池、**经 Haystack Integration（`LiteLLMGenerator`、`SentenceTransformersDocumentEmbedder`、`SentenceTransformersRanker`）统一调用**、模型可观测性（Langfuse）。不做：不决定哪个 KB 用哪个模型 | `resolve_model`、`invoke_llm`（→Haystack Generator）、`invoke_embedding`（→Haystack Embedder）、`invoke_rerank`（→Haystack Ranker）、`resolve_prompt`（→Haystack PromptBuilder 模板） | `model_registry`、`prompt_template`、`prompt_binding`、Langfuse 后端 | 无领域事件 |
| **P-CONFIG 配置** | 做：检索参数级联解析、切分配置版本化、特性开关。切分配置新增 `haystack_strategy` 字段（对应 Haystack `DocumentSplitter.split_by`）。不做：不执行检索/切分 | `resolve_retrieval_config`、`resolve_chunking_config`、`feature_flag` | `retrieval_config`（含 `strict`、`haystack_pipeline_name`）、`chunking_config`（含 `haystack_strategy`） | 无领域事件 |

### 0.1.2 业务模块

| 模块 | 职责（做什么 / 不做什么） | 提供的接口 | 依赖的接口 | 独占数据 | 发布/消费事件 |
|-----|------------------------|-----------|-----------|---------|-------------|
| **B-DOC 文档与目录管理** | 做：文档物理登记与去重、挂载关系、目录树、下载签名与只读渲染、触发解析、**在写路径同步调用权限服务生命周期端口（register/link/unlink/retire）**。不做：不执行解析、不写执行状态、不碰 chunk、**不碰任何权益数据**、不提供授权界面 | `submit_ingest_task`、`trigger_parse`、`delete_document_from_kb`、目录接口、批量接口 | P-STORE、P-AUTHC、P-AUDIT、P-CONFIG、B-INGEST | `document`、`document_kb_mount`、`directory`、`document_directory_entry`、`outbox`（B-DOC 分区） | 发布：`DocumentMounted`、`DocumentUnmounted`、`MountEnabledChanged`。消费：无 |
| **B-INGEST 摄入管线** | 做：解析、**切分（委托 Haystack `DocumentSplitter` 系列）**、嵌入（委托 **Haystack `SentenceTransformersDocumentEmbedder`**，经 P-MODEL）、写向量库（**Haystack `MilvusDocumentStore`**）、执行状态机、幂等与失败隔离、**盖戳管道**。不做：不拥有挂载关系、不做权限决策、**不计算戳记内容（只搬运）**、**不在 Haystack Component 内部写权限逻辑** | `ingest_document_task`、`retry_mount`、`has_execution`、`get_parse_status`、`stamp_channel_task` | P-TASK（含 `run_pipeline_async`）、P-MODEL、P-CONFIG、P-AUTHC、P-AUDIT、B-RETRIEVE、P-STORE | `ingest_execution`、`chunks` | 发布：`DocumentParsed`、`DocumentParseFailed`。消费：`DocumentMounted`、`DocumentUnmounted`、`MountEnabledChanged`、`VisibilityChanged`（经 P-AUTHC 转交） |
| **B-RETRIEVE 检索** | 做：**Haystack 查询 Pipeline 管理**、prefilter 编译注入（层 1）、过采样与补检索（层 2）、strict 库逐条复核（层 3）、**混合检索（`MilvusEmbeddingRetriever` + 稀疏 Retriever + `DocumentJoiner` RRF）**、层级合并、**rerank（`SentenceTransformersRanker`）**。不做：不做生成编排、不拥有对话状态、**不做权限判定**、**不做事后过滤**、**不在 Haystack Component 内部写权限逻辑** | `retrieve` | P-AUTHC（prefilter + filter_items）、P-MODEL、P-CONFIG | 向量库集合（chunk 向量 + payload） | 发布：`CHUNK_FILTERED`（经审计）。消费：无 |
| **B-CHAT 对话编排** | 做：对话/轮次存储、检索任务分发、流式回传、**生成合成（委托 Haystack 查询 Pipeline 生成节点，`PromptBuilder` + `LocalGenerator`，经 P-MODEL 防腐）**、参数快照、拒答型降级文案。不做：不做底层检索计算、不做权限决策 | `POST /conversations/{id}/query`、`retrieve_and_generate_task` | B-RETRIEVE、P-MODEL、P-CONFIG、P-TASK、P-AUTHC、P-AUDIT | `conversation`、`conversation_turn` | 发布：`KB_QUERY`（经审计）。消费：无 |

### 0.1.3 单一写者对照

| 数据 | 唯一写者 | 其他模块如何获取 |
|-----|---------|----------------|
| `document_kb_mount` 关系 + is_enabled | **B-DOC 独占** | 经 B-DOC 接口或其事件 |
| 解析执行状态（`ingest_execution`） | **B-INGEST 独占** | 经 B-INGEST 接口或事件 |
| `chunks` | **B-INGEST 独占** | 检索侧读向量库 payload |
| 向量库 chunk payload 的 `allow_stamps`/`deny_stamps`/`vis_version` | **B-INGEST 独占（盖戳管道是唯一写者）** | 内容唯一来源是权限服务 `/v1/visibility`；B-INGEST 只做"取回来、写下去"，**不加工、不推导、不补全** |
| 权益数据（acl/role_binding/restriction） | **★ 权限服务独占，不在本系统** | 决策经五端点；**禁止绕过接口直查权益表** |
| 结构镜像（resource_registry/mount_mirror） | **★ 权限服务独占** | 由 B-DOC 写路径同步调 register/link/unlink/retire 维护 |
| `audit_log` | **P-AUDIT 独占** | 只写不读；查询走 P-AUDIT 接口 |
| `retrieval_config`/`chunking_config` | **P-CONFIG 独占** | 经 `resolve_*` 门面 |
| `conversation`/`conversation_turn` | **B-CHAT 独占** | 经 B-CHAT 接口 |
| `model_registry`/`prompt_*` | **P-MODEL 独占** | 经 `resolve_*`/`invoke_*` 门面 |

## 0.2 全局规则

### 0.2.1 依赖方向规则

```
业务模块（B-*） ──单向依赖──▶ 平台模块（P-*） ──单向依赖──▶ 外部权限服务
业务模块之间：只允许「内部接口调用」或「事件」，禁止直读直写对方独占表
平台模块之间：互不依赖对方业务态
禁止任何依赖环
★ 任何模块不得越过 P-AUTHC 直接调用权限服务 HTTP 端点
★ 任何模块不得在 Haystack Component 内部写权限判断逻辑
  （Component 的 run() 方法不得出现 check/filter/prefilter 调用；
   六条件过滤以 MetadataFilter 对象形式在 Component 外部注入）
```

### 0.2.2 单一写者原则

每张表有且仅有一个模块可写。展开必须"到主体为止"，不得继续展开成员关系（`allow_stamps` 展开到 `group:eng` 就停，不再展开成该组当前 user 列表）。答不上来权威源的派生数据不允许存在。

### 0.2.3 横切能力门面化红线（四条）

1. **禁止业务模块 import 任何可观测后端 SDK**——一律经 P-OBS 门面。
2. **禁止业务模块自行拼装 Collector/后端地址**。
3. **禁止业务模块自行拼装权限服务地址、自行构造 Envelope、自行解释 reasons**——权限服务的 base URL、`x-client-id`、超时值、重试策略、三态映射，只在 P-AUTHC 一处存在。
4. **（v14 新增）禁止在 Haystack Component 内部旁路 P-MODEL 门面调用模型**——所有 Generator/Embedder/Ranker 实例封装在 P-MODEL 内部，以防腐层对外暴露，B-INGEST/B-RETRIEVE/B-CHAT 不得直接 import Haystack 模型类。

### 0.2.4 框架使用原则

编排、状态机、数据模型自持；算法性组件（切分/层级合并/融合/合成）委托 **Haystack 2.x**，经 `@component` 防腐层引入，**Haystack 类型不出现在任何模块接口签名中**。

沉淀为长期资产的是四个能力契约：
- 切分契约：`split(document) → list[Document]`
- 融合契约：`fuse(dense_results, sparse_results) → list[Document]`
- 层级合并契约：`merge_levels(leaf_chunks) → list[Document]`
- 合成契约：`synthesize(query, chunks) → Answer`

权限"采购"外部服务，同样经防腐层（P-AUTHC）引入，权限服务的 HTTP 细节不出现在任何业务模块签名中。

## 0.3 组件选型

| 关注点 | 组件 | 归属 | 角色 |
|-------|-----|------|-----|
| 认证 | IdP（Keycloak 或企业既有） | 外部 | 签发 JWT，管理 User/Group/Role |
| **授权判定** | **外部权限服务** | **外部** | **五端点决策面 + 投影面 + 管理面；本系统零判定** |
| 权限消费适配 | 自持（P-AUTHC） | P-AUTHC | 唯一出口，封装五端点调用与三态映射 |
| 向量库 | **Milvus**（通过 Haystack `MilvusDocumentStore` Integration） | B-RETRIEVE | chunk 向量 + payload（含双戳） |
| 关系库 | PostgreSQL | 各模块 | 业务库/审计/各 outbox（无 ACL 库） |
| 对象存储 | SeaweedFS（S3 网关） | P-STORE | 原文件 |
| 任务队列 | Celery + Redis | P-TASK | 摄入/检索/盖戳三类队列 |
| **算法框架** | **Haystack 2.x**（Pipeline + @component 协议） | B-INGEST/B-RETRIEVE/B-CHAT | 经防腐层的切分/合并/融合/合成；类型不出签名 |
| **切分** | Haystack `DocumentSplitter`（word/sentence/passage）、`SemanticDocumentSplitter`、自定义层级 Component | B-INGEST（经防腐） | 替代 LlamaIndex Splitter 系列 |
| **嵌入** | Haystack `SentenceTransformersDocumentEmbedder`/`TextEmbedder`（封装在 P-MODEL） | B-INGEST/B-RETRIEVE | BGE-M3 稠密 + 稀疏双路 |
| **重排序** | Haystack `SentenceTransformersRanker`（封装在 P-MODEL） | B-RETRIEVE | 替代 LlamaIndex Reranker |
| **生成合成** | Haystack `LiteLLMGenerator` + `PromptBuilder`（封装在 P-MODEL） | B-CHAT（经防腐） | 替代 LlamaIndex synthesizer |
| **检索编排** | Haystack Pipeline（摄入 Pipeline + 查询 Pipeline，YAML 版本化） | B-INGEST/B-RETRIEVE | 替代 LlamaIndex QueryEngine |
| 模型网关 | LiteLLM（经 Haystack `LiteLLMGenerator` Integration） | P-MODEL | 统一调用与 callback |
| 可观测 | OTel Collector + Tempo/Loki/Prometheus + Grafana | P-OBS | 通用线（**Haystack 内置 tracing，节点即 span**） |
| 模型观测 | Langfuse | P-MODEL | 模型线 |

## 0.4 框架可替换性定位

**Haystack 地位等同 VectorStore 抽象下的 Milvus——当前实现，可随时替换。** Haystack 的 `@component` 协议是语言层面约束（类型注解 + `run()` 签名），替换单个 Component 不影响 Pipeline 其余节点，可替换性比 LlamaIndex 更强。

**权限服务的定位不同**：它是有双向契约的外部系统，P-AUTHC 防腐层的目的是把契约变更的影响面收敛到一个模块，而非"将来好换掉权限服务"。

## 0.5 跨章节诊断法（评审必跑项）

**两个提问（每次评审必跑）：**

**问题一：权威源归属**——文中每个被反复引用的概念，其权威定义写在哪一节？答不出章节号（或答"在权限服务那边"而不给出上游契约章节号）→ 不通过。

**问题二：同问多答**——同一个问题，系统里有几处在回答？超过一处必须有人证明它们相等，且证明必须是可执行的契约测试；多处且无证明 → 判定为缺陷。

**表 A · 权威源登记（关键概念）：**

| 概念 | 权威定义 | 谁负责 |
|-----|---------|-------|
| 动词目录（16 个） | 上游契约 §4 | ★ 权限服务 |
| 准入矩阵（client_id → 可调动词） | 上游契约 §5 | ★ 权限服务 |
| 三态与 reasons 注册表 | 上游契约 §8 | ★ 权限服务 |
| 戳记字段与版本单调性 | 上游契约 §11 | 权限服务定义/B-INGEST 执行 |
| 层 1 过滤器五条件 | 上游契约 §12 | 权限服务定义/B-RETRIEVE 执行 |
| 存在性三通道纪律 | 上游契约 §13 | B-RETRIEVE/B-CHAT 执行 |
| `strict`（是否开层 3 复核） | §12.2 | P-CONFIG |
| `is_enabled`/`retrievable` | §13.4.1 | B-DOC 写/B-INGEST 展开 |
| 生命周期端口调用点 | §13.7 | B-DOC |
| 盖戳管道 | §14.5 | B-INGEST |
| 三层检索链路 | §15.1–15.3 | B-RETRIEVE |
| 拒答型降级 | §25.3 | B-CHAT/B-RETRIEVE（须会签） |
| `decision_id`（跨系统审计主键） | 上游契约 §14 | 权限服务发出/P-AUDIT 记录 |
| `VisibilityChanged`（外部事件） | 上游契约 §11 | ★ 权限服务发布/B-INGEST 消费 |
| **Haystack Pipeline YAML 版本** | §14.2/§15.0 | P-CONFIG 存储/B-INGEST·B-RETRIEVE 执行 |


---

# 第一部分：共享契约

跨所有模块通用的"通用语"，任何模块开发者都应先读。

## 1. RequestContext 与最小投影

### 1.1 ctx 的定位

ctx 在中间件构建一次作为请求生命周期内传输载体，各接口只声明其实际消费的**最小投影**，不再无差别接收整个 ctx。

### 1.2 RequestContext 字段契约（构建方 = P-AUTHC）

| 字段 | 类型 | 含义与约束 |
|-----|-----|----------|
| `request_id` | 字符串，必填 | 全链路唯一标识，= OTel trace_id（32 位十六进制），同时作为 Envelope 的 `environment.request_id` |
| `user_id` | 字符串，必填 | 来自 JWT `sub` |
| `tenant_id` | 字符串，必填 | 来自 JWT `tenant`，**仅供本系统业务查询与审计；绝不作为权限判定输入**（权限服务从资源镜像取租户） |
| `credential` | 字符串，必填 | JWT 原文。权限服务唯一接受的主体载体。**必须原样保管、原样透传，不得重签、不得裁剪 claims、不得在日志中打印** |
| `roles` | 列表，默认空 | 从 JWT 提取，仅供业务展示与审计；**不参与任何判定** |
| `groups` | 列表，默认空 | 同上 |
| `principals` | 列表，默认空 | `user:`/`group:`/`role:` 前缀并集。**仅用于层 1 向量库过滤器中与戳记做交集匹配**（§15.1 条件③④） |
| `is_service_account` | 布尔，默认 false | 审计取证区分人工/自动化 |
| `client_ip` | 字符串，可空 | 网关注入，审计取证 |
| `authz_decision_ref` | 字符串，可空 | 权限服务返回的 `decision_id`，跨系统取证的**唯一主键** |
| `risk_level` | 字符串，默认 normal | normal/sensitive；按 action 是否敏感显式赋值 |

> **★ `ctx.tenant_id` 不参与权限判定。** Envelope 里没有 `tenant` 字段，权限服务从 JWT 解析主体身份，从资源镜像获取租户归属。**连传都传不了**——这是最好的防呆。

### 1.3 最小投影窄接口契约

| 窄接口 | 暴露字段 | 谁消费 |
|-------|---------|-------|
| `AccessScope` | `tenant_id` + `principals` + `credential` | B-RETRIEVE（层 3 复核须携带 JWT 原文） |
| `Identity` | `user_id` + `tenant_id` + `roles` + `groups` | 业务编排读取身份 |
| `AuditContext` | 全量 ctx（含 `authz_decision_ref`） | P-AUDIT 门面 |
| `AuthzCallContext` | `credential` + `request_id` | P-AUTHC 内部。刻意不含 tenant_id/principals，从签名层面杜绝"顺手传进去" |

### 1.4 字段避坑记录

`credential` 全链路只在 P-AUTHC 与序列化任务参数（ctx_token 形式）中出现，**禁止写入日志、Trace span attribute、审计 payload**；异步链路须改用 ctx_token（§6.7）；`authz_decision_ref` 从权限服务响应 body 读；`risk_level` 按敏感操作集合显式赋值。

## 2. 统一错误模型

### 2.1 错误信封契约

所有 REST 错误响应统一：`error_code`（`模块前缀:错误类别`）、`message`（不含敏感信息）、`request_id`（=trace_id）、`details`（可空）。

### 2.2 统一错误码表

| error_code | HTTP | 含义 | 抛出方 |
|-----------|------|-----|-------|
| `auth:unauthenticated` | 401 | JWT 无效/过期 | P-AUTHC |
| `auth:forbidden` | 403 | 权限不足（权限服务 `decision=deny`） | P-AUTHC |
| `auth:authz_unavailable` | 503 | 权限服务不可达（外部依赖故障，非用户权限问题） | P-AUTHC |
| `auth:authz_indeterminate` | 503 | 权限服务返回 `indeterminate`；**必须额外触发技术告警**，与 deny 在监控中可区分 | P-AUTHC |
| `auth:obligation_unsupported` | 500 | 响应含本系统不认识的 obligation key → 视同 deny + 告警 | P-AUTHC |
| `auth:resource_unregistered` | 500 | 判定返回 `unknown_resource`——资源未在权限服务登记，**是本系统 bug** | P-AUTHC |
| `doc:not_found` | 404 | 文档/挂载/目录不存在 | B-DOC |
| `doc:duplicate` | 200（标记位） | 幂等命中 | B-DOC |
| `doc:kb_reindexing` | 409 | KB 维护中拒绝摄入 | B-DOC |
| `doc:authz_write_failed` | 502 | 写路径调生命周期端口失败，业务事务已回滚 | B-DOC |
| `retrieve:vector_store_unavailable` | 503 | 向量库不可达 | B-RETRIEVE |
| `retrieve:insufficient_evidence` | 200（业务态） | 补检索后仍不足，返回"未找到足够信息"。**是 200 不是错误**——deny 文案与 not-found 同型，用 4xx 即泄露存在性 | B-RETRIEVE |
| `chat:stream_timeout` | 见流事件 | 流式订阅超时 | B-CHAT |
| `model:not_found` | 404 | model_id 未注册 | P-MODEL |
| `config:not_found` | 404 | 配置作用域不存在 | P-CONFIG |
| `common:validation_error` | 422 | 入参校验失败 | 各模块 |
| `common:internal_error` | 500 | 未预期异常 | 各模块 |

> **检索场景 deny 时，对外文案与"未找到足够信息"完全相同，不能说"您没有权限"**（存在性三通道纪律，§15.6）。

## 3. 事件信封与投递语义

### 3.1 事件信封字段

`event_type`、`event_id`（幂等去重）、`occurred_at`、`trace_id`、`tenant_id`、`payload_schema_version`、`payload`。

### 3.2 投递语义（Outbox + 至少一次 + 幂等消费）

所有本系统领域事件一律经 Outbox 发布：① 同事务写业务变更 + 写 outbox；② relay 投递（P-TASK 承载），至少一次；③ 消费幂等（强制），按 `event_id` 去重。

`VisibilityChanged` 来自权限服务 outbox（外部事件），本系统作为订阅方承担三件事：(a) 按 `event_id` 幂等消费；(b) 版本单调性检查（只覆写 version ≥ 当前值）；(c) 消费失败不 ack、下轮重试，绝不落盘空戳记。

`outbox` 表归各发布方独占（仅 B-DOC 一个分区）。

### 3.3 领域事件目录

| event_type | 发布方 | 典型订阅方 | payload 关键字段 |
|-----------|-------|----------|----------------|
| `DocumentMounted` | B-DOC | B-INGEST | document_id, mount_id, kb_id, chunking_config_version |
| `DocumentUnmounted` | B-DOC | B-INGEST | mount_id, kb_id |
| `MountEnabledChanged` | B-DOC | B-INGEST | mount_id, is_enabled |
| `DocumentParsed` | B-INGEST | P-AUDIT、P-OBS、质量采样 | document_id, mount_id, kb_id, chunk_count |
| `DocumentParseFailed` | B-INGEST | P-AUDIT、告警 | document_id, mount_id, kb_id, reason, retry_count |
| **`VisibilityChanged`** | **★ 外部：权限服务** | **B-INGEST（盖戳管道，经 P-AUTHC 转交）** | resource{type,id}, channel{kb}, version, unmounted, tenant |

### 3.4 对账兜底

**本系统内对账**：定时扫描"B-DOC 有挂载、B-INGEST 无执行记录"的缺口，调 `has_execution(mount_ids)` 求差集，补发 `DocumentMounted`；缺口数递增 `mount_execution_gap` 指标并告警。

**跨系统对账**：
- 结构镜像对账：本系统 `document_kb_mount` 与权限服务镜像差异 → `mirror_gap` 指标；
- 戳记对账：向量库 chunk 戳记与权限服务当前投影差异 → `stamp_drift` 指标；
- 未盖戳的绕路写入 → `orphan_stamp` 指标。

## 4. ID 与命名规范

`request_id` = `trace_id`（全链路复用，并作为 Envelope 的 `environment.request_id`，使四方日志可互跳：Grafana / Langfuse / audit_log / 权限服务审计日志）。

**action 命名：唯一权威是上游契约 §4（16 个动词）**，本系统不得新增、不得改名、不得使用废除动词。废除动词对照：

| 废除动词 | 正确替代 |
|---------|---------|
| `doc:write` | `kb:write`（资源改为 kb） |
| `doc:delete` | `doc:purge`（彻底删除）/ `doc:unmount`（通道类，须带 channel.kb） |
| `acl:update` | `doc:share`/`kb:grant`（准入 client_id 为 admin-console，不经本系统） |

**敏感操作集合**（驱动 `risk_level=sensitive`）：`doc:share`、`kb:grant`、`doc:purge`、`doc:unmount`、`kb:manage`、`doc:download`。

其余：model_id 逻辑别名；版本化对象 append-only；一次性执行结果存解析后快照不存配置表指针。

## 5. 接口规格模板

```
接口名/端点：  所属模块：  接口类型：【REST】/【内部接口】/【事件】/【外部契约】
调用方：  前置条件：  入参（含窄接口投影）：  出参：
错误模型：  幂等语义：  权限要求（动词 + 调用端点两项均须写）：
产生的审计事件：  SLO（如适用）：
```


---

# 第二部分：平台能力模块

## 6. P-AUTHC 权限消费模块

> **本模块是 v14 中与 Haystack 完全无关的模块**——权限外置的所有设计均在此集中，Haystack 框架替换不改变任何权限逻辑。

### 6.0 模块边界

**做**：JWT 本地校签与 ctx 构建；封装权限服务五端点的全部调用（本系统访问权限服务的**唯一出口**）；三态到业务行为的统一映射，含四类 fail-closed；路由级依赖注入拦截；prefilter 请求内缓存；ctx_token 铸造代理；生命周期端口调用封装；订阅权限服务事件流并转交 B-INGEST。

**不做**：不存任何权益数据（无 acl/role_binding 表）；不写任何策略（无 Rego/OPA）；不做任何判定（含 `if doc.uploaded_by == ctx.user_id: return True` 这类"顺手的短路"）；不解析主体属性；不理解 privilege 语义（`kb_reader`/`read_only` 不出现在本系统代码）；不提供授权写入接口；**不缓存决策结果**（`/v1/filter` 永久禁缓存，`/v1/check` 决策缓存默认关闭）。

### 6.1 三层 PEP 职责

| 层 | 位置 | 职责 | 调哪个端点 |
|---|-----|-----|-----------|
| PEP #1 | 网关（Nginx） | 校验 JWT 签名/过期、限流；不做授权 | 无 |
| PEP #2 | 中间件（P-AUTHC） | 构建 ctx、保管 credential、审计起点 | 无 |
| PEP #3a | 路由级（`require_permission`） | 交互端点门禁 | `/v1/check`（`x-client-id: interactive-backend`） |
| PEP #3b | 检索前（B-RETRIEVE） | 编译过滤条件注入向量库（层 1） | `/v1/prefilter`（`x-client-id: retrieval`） |
| PEP #3c | 检索后（B-RETRIEVE，仅 strict 库） | 逐条复核（层 3） | `/v1/filter`（`x-client-id: retrieval`） |
| PEP #3d | 盖戳（B-INGEST） | 取戳记写 payload | `/v1/visibility`（`x-client-id: ingest`） |

> **`client_id` 由 P-AUTHC 按方法硬编码**，不能由业务模块传入（否则业务模块可以伪装成 `admin-console`）。

### 6.2 build_context（中间件）

本地校验 JWT 签名与过期（快速失败）→ 从 claims 构建 ctx → **保管 `credential` 原文** → principals 展开 → 开启 OTel Span（trace_id = request_id）→ 非服务账号 groups 为空打 WARNING。

本地校签与权限服务校验是有意的两处（快速失败优化 vs 权威），**本地更严不更松，通过不代表授权通过**。

### 6.3 check——单条判定

```
签名：check(ctx, action, resource_type, resource_id, channel_kb=None) → Decision
Decision = { decision, decision_id, reasons }
```

- `action` 为 `doc:retrieve` 时**直接抛 `common:internal_error` 并告警**，不发请求（`doc:retrieve` 只能走 `/v1/filter`）
- 超时视同 deny；`decision_id` 必须回填 `ctx.authz_decision_ref`

### 6.3b check_batch——批量判定

```
签名：check_batch(ctx, action, resources: list) → dict[resource_id, Decision]
```

**已确认**：`/v1/check/batch` 对 `interactive-backend` 开放，走单次往返。

实现规格：
- 单批上限保守取 **≤200**（与 `/v1/filter` 对齐，`/v1/check/batch` 自身上限待联调确认，在确认前以 200 为默认分批边界）
- 超出 200 条时分批串行调用，**不并发分批**（防止打宽权限服务连接）
- 逐资源独立决策：一个资源判否不影响其余，**但整批有任一传输失败/超时 → 整批判否**（fail-closed）
- 每条独立 `decision_id`，共享同一 `request_id`
- 分批时各批共享同一 `request_id`，各批内各条独立 `decision_id`

### 6.4 require_permission（路由级依赖注入工厂）

`require_permission(action, resource_type)` → 按 action 赋 risk_level → 调 `check` → 不通过抛错 → 通过注入 ctx。

### 6.5 filter_items——检索后逐条复核

```
签名：filter_items(scope: AccessScope, items: list[(doc_id, kb_id)]) → list[(doc_id, kb_id)]
```

- 调用方：B-RETRIEVE 层 3（仅 `strict=true`）
- `action` 固定 `doc:retrieve`，每条必带 `channel.kb`
- **三条硬纪律**：上限 200 条；失败/超时 → 整批视同 deny；**永久禁止缓存**

### 6.6 get_prefilter——检索前编译

```
签名：get_prefilter(scope: AccessScope) → PreFilter | SUSPENDED
PreFilter = { kbs, excluded_kbs, tenant_wide_read, policy_version, ttl_s, expires_at }
```

- `GET /v1/prefilter?credential=<JWT>` → `suspended=true` 返回 SUSPENDED 哨兵（调用方**直接跳过检索**）
- **请求内缓存**（同一次检索复用），**不跨请求缓存**（非 strict 库无层 3 兜底，陈旧即越权窗口）
- 失败 → **不允许回退到"不加过滤条件查询"**，必须整体拒答

### 6.7 mint_ctx_token——异步主体载体

```
签名：mint_ctx_token(ctx, audience: str, ttl_s: int = 600) → CtxToken
```

- `POST /v1/context`，ttl_s **上限 600 秒**，audience 必须与消费方服务名一致
- **worker 任务参数中绝不序列化 JWT 原文**，改用 ctx_token
- 过期 → 任务失败，不续期、不降级

### 6.8 系统主体与内部调用身份

1. 盖戳管道用 `x-client-id: ingest` 调 `/v1/visibility`——该端点无主体入参，结构上就不需要主体身份。
2. 对账任务同理。
3. 生命周期端口（register/link/unlink/retire）须携带当前用户的 credential（审计链路可追"谁删的"）。

**三条硬约束**：系统身份不可用于任何带主体的判定调用；不参与判定不等于豁免；绝不用于降级放行。

---

## 6A. 【外部契约】权限服务调用规格

### 6A.1 五端点使用总表

| 端点 | 谁调 | 何时调 | `x-client-id` | 失败行为 |
|-----|-----|-------|--------------|---------|
| `POST /v1/check` | P-AUTHC | 交互端点进入时 | `interactive-backend` | 抛 403/503，业务不执行 |
| `POST /v1/filter` | B-RETRIEVE（经 P-AUTHC `filter_items`） | 层 3，仅 strict 库 | `retrieval` | **整批 deny**，静默丢弃 |
| `GET /v1/prefilter` | B-RETRIEVE（经 P-AUTHC `get_prefilter`） | 每次检索前 | `retrieval` | 拒答降级（§25.3） |
| `POST /v1/context` | P-AUTHC（`mint_ctx_token`） | 派发异步任务前 | `interactive-backend` | 任务不派发，返回 503 |
| `POST /v1/visibility` | B-INGEST（盖戳管道） | 消费 `VisibilityChanged` 后/摄入完成后 | `ingest` | **不 ack、不落盘、下轮重试**；429 时读 `Retry-After` 退避后重试 |

**管理面生命周期端口**（本系统只调四个）：

| 门面 | 谁调 | 何时调 |
|-----|-----|-------|
| `register` | B-DOC | 首次创建 document/创建 KB 时 |
| `link` | B-DOC | 建立挂载时 |
| `unlink` | B-DOC | 解除挂载时 |
| `retire` | B-DOC | 彻底删除 document/删除 KB 时 |

**本系统不调的管理面门面**（由管理台直连）：grant_acl、revoke_acl、transfer_ownership、update_role_binding、add_restriction、remove_restriction 等。

### 6A.2 Envelope 构造规格（P-AUTHC 内部唯一实现）

```json
{
  "subject":     { "credential": "<ctx.credential>" },
  "action":      "<动词>",
  "resource":    { "type": "<doc|kb|dir|tenant>", "id": "<资源ID>"
                   [, "channel": { "kb": "<kb-id>" }] },
  "environment": { "request_id": "<ctx.request_id>" }
}
```

**四条构造纪律**：不传 `tenant`/`principals`；不传 `context`（v1 注册表为空）；`environment.request_id` 恒等于 trace_id；通道类动词（`doc:retrieve`、`doc:unmount`）必须带 `channel.kb`。

### 6A.3 层 1 过滤器编译（prefilter → 向量库查询条件）

六条件全部必需，缺一即安全缺口（上游契约 §12 + 本系统追加条件⑥）：

```
must:
  ① tenant_id == 当前租户
  ② kb_id ∈ 候选通道列表
  ③ allow_stamps 包含 scope.principals 中至少一个（MatchAny）
  ⑥ retrievable == true（本系统运营条件，权限服务不管）
must_not:
  ④ deny_stamps 包含 scope.principals 中任何一个（MatchAny）
  ⑤ vis_version 为 null 或不存在（排除未盖戳的绕路写入）
```

**编译失败语义**：`compile_filter` 返回 None（如候选通道为空集）→ **返回空结果集，恒假**，而不是"不加条件查询"。

**v14 实现形态**：编译结果为 Haystack `MetadataFilter` 对象，注入 `MilvusEmbeddingRetriever.filters` 参数（§15.1）。

### 6A.4 prefilter 与业务候选 KB 的交集规则

**最终检索的 KB 集合 = prefilter.kbs ∩ 业务候选 KB。**

两条方向性纪律：prefilter 是上界，业务绑定只能缩小不能扩大；交集为空 → 直接返回"未找到足够信息"，不发任何向量库查询。

### 6A.5 异步链路的主体传递

| 场景 | 主体载体 |
|-----|---------|
| 交互端点（API 进程内完成） | `credential`（JWT 原文） |
| 检索任务（retrieval-worker） | `ctx_token` |
| 盖戳任务（stamping-worker） | 无需主体（`/v1/visibility` 无主体入参） |
| 对账任务 | 无需主体 |

**ctx_token 三条使用纪律**：`ttl_s ≤ 600`；`audience` 必须与消费方服务名一致；**过期即任务失败，不续期、不降级**。

检索任务**不做长退避重试**（用户在线等待，且重试可能撞上 token 过期），失败即返回 error 流事件。

### 6A.6 三态映射与四类 fail-closed

| 收到 | 交互端点行为 | 检索端点行为 | 额外要求 |
|-----|------------|------------|---------|
| `allow` | 执行操作 | 保留该 chunk | **检查 `obligations` 字段；不认识的 key → 视同 deny + 告警** |
| `deny` | 拒绝，文案与 not-found 同型 | **静默丢弃** | reasons 只写业务日志，永不出用户文案 |
| `indeterminate` | 503 `auth:authz_indeterminate` | 同 deny | **必须额外触发技术告警，与 deny 在监控中可区分** |
| 传输失败/超时 | 503 `auth:authz_unavailable` | **整批视同 deny** | 记本地超时事件（无 decision_id），按时间窗对账 |

**四条实现纪律**：reasons 是机器码，永不展示给用户；obligations 必须显式检查（即使 v1 注册表为空）；indeterminate 与 deny 在监控中必须可区分；超时事件无 decision_id，记 request_id + 精确时间戳。

### 6A.7 生命周期端口调用规格（供 B-DOC）

```
register_resource(ctx, type, id, owner_principal) → change_id
link_resource(ctx, doc_id, kb_id)                 → change_id
unlink_resource(ctx, doc_id, kb_id)               → change_id
retire_resource(ctx, type, id)                    → change_id
```

**五条调用纪律**：必须携带 `idempotency-key`（构造规则：`{facade}-{resource_id}-{kb_id?}-{业务事务ID}`，禁止时间戳/随机数/UUID）；`result: "noop"` 是成功不是失败；`register` 的 `owner` 必须是 `user:` 前缀；同 key 不同载荷返回 409 必须告警；调用失败即回滚本地业务事务。

### 6A.8 事件订阅规格（`VisibilityChanged`）

P-AUTHC 订阅权限服务事件流，**转交 B-INGEST 处理**（不在 P-AUTHC 内做盖戳）。与权限服务的所有接触点收敛在 P-AUTHC 一处，包括事件流连接、鉴权、反序列化、schema 版本兼容——B-INGEST 收到的是本系统内部形态的事件对象，不感知外部 schema。

---

## 7. P-AUDIT 审计模块

### 7.0 模块边界

做：接收审计事件、按风险分级落库、归档、**记录跨系统串联主键 `decision_id`**。不做：不理解业务语义、不被业务直接读表。

### 7.1 emit_audit_event（唯一审计写入门面）

```
签名：emit_audit_event(event_type, audit_ctx, action, resource_type, resource_id, allowed, **payload)
```

普通事件 fail-open（写库失败不抛异常、落本地缓冲队列、递增 `audit_write_failed`）。

### 7.2 emit_audit_event_txn（高风险同步写入）

高风险操作同步/优先写入：审计记录与业务变更同一事务，写失败则业务回滚/不签发。

**高风险事件清单**：

| 事件类型 | 触发点 | 风险级别 |
|---------|-------|---------|
| `DOC_DOWNLOAD` | 文件下载，签发 URL 前优先落审计 | 高风险，同事务 |
| `DOC_DELETE` | 删除操作（purge/unmount） | 高风险，同事务 |
| `AUTHZ_WRITE` | 生命周期端口调用（register/link/unlink/retire） | 高风险，同事务 |
| `DOC_VIEW` | 文件只读查看 | 普通，fail-open |
| `DOC_INGEST` | 摄入完成 | 普通，fail-open |
| `KB_QUERY` | 对话查询 | 普通，fail-open |
| `STAMP_APPLIED` | 盖戳完成 | 普通，fail-open（盖戳量大，不可 fail-closed） |
| `CHUNK_FILTERED` | 检索层过滤 | 普通，fail-open |

### 7.3 audit_log 表

字段：id、event_type、request_id（=trace_id）、user_id、tenant_id、client_ip、is_service_account、action、resource_type、resource_id、allowed（NULL=不适用）、**authz_decision_ref（= 权限服务 decision_id）**、risk_level、payload（JSONB）、created_at。

**关键索引**：`(tenant_id, created_at DESC)`；`(request_id)` trace 反查；`(risk_level, created_at DESC)`；`(resource_type, resource_id, created_at DESC)`；`(authz_decision_ref)` **跨系统取证反查**。

**跨系统取证路径**：本系统 audit_log（谁操作了什么）→ authz_decision_ref → 权限服务审计日志（当时的判定输入快照与策略版本）。**保留期须双方对齐（建议 180 天）**，较短的一侧会导致取证链断裂。

**保留归档**：热 0-90 天 PG 主表；温 90 天-1 年按月分区；冷 1 年以上增量导出 S3 + Object Lock。

---

## 8. P-OBS 可观测模块

### 8.0 模块边界

做：Trace 自动埋点（**Haystack Pipeline 节点自动产生 span，span 名 `{pipeline_name}.{component_name}`，无需在防腐层额外埋点**）、结构化日志、Metric 采集，统一经 OTel Collector 单一出口，Grafana 单一查询入口。

不做：不评判检索质量；不承担模型观测（归 P-MODEL/Langfuse）；不做审计（可观测可丢、审计不可丢）。

### 8.1 信号归属：五分法

| 信号 | 回答什么 | 归属 | 失败语义 |
|-----|---------|------|---------|
| Trace | 在哪慢、调用链形状 | P-OBS | fail-open |
| Log | 内部状况、异常上下文 | P-OBS | fail-open |
| Metric | 趋势、告警 | P-OBS | fail-open |
| Model-Obs | 模型语义（prompt/token/成本） | P-MODEL（Langfuse） | fail-open |
| **Audit** | **谁做了什么（含 decision_id）** | **P-AUDIT** | **唯一 fail-closed** |

### 8.2 架构：一个入口，一个出口

应用 → OTel SDK（**Haystack 内置，自动读取 `OTEL_EXPORTER_OTLP_ENDPOINT`**）→ **OTel Collector（唯一入口，4317/4318）** → Tempo/Loki/Prometheus → **Grafana（唯一查询出口，3000）**。

**权限相关 span**：Haystack Component 内部零权限逻辑（§0.2.1 红线），故权限 span（`authz.check`/`authz.filter`/`authz.prefilter`）不出现在 Haystack Pipeline trace 中，由 P-AUTHC 调用代码手动创建，附加到同一 trace_id。两类 span 在 Grafana/Tempo 中可关联查看。

### 8.3 Metric 关键指标（必须有）

| 指标 | 类型 | 用途 |
|-----|-----|-----|
| `authz_call_duration_seconds{endpoint}` | Histogram | 五端点延迟，SLO 监控 |
| `authz_decision_total{endpoint,decision}` | Counter | **allow/deny/indeterminate 三态分开计数**（混淆即故障表现为业务波动） |
| `authz_call_failed_total{endpoint,kind}` | Counter | kind ∈ {timeout, connection, http_error} |
| `authz_obligation_unknown_total` | Counter | 恒应为 0；非零即契约演进信号 |
| `mirror_gap` | Gauge | 结构镜像对账缺口数 |
| `stamp_drift` | Gauge | 戳记漂移数 |
| `orphan_stamp` | Gauge | 缺 vis_version 的绕路写入 chunk 数 |
| `stamp_lag_seconds` | Histogram | VisibilityChanged 发出到戳记落盘延迟 |
| `filtered_rate{layer}` | Histogram | layer ∈ {layer1, layer3}；驱动过采样系数 k' 周期校准 |
| `retrieval_insufficient_total` | Counter | 返回"未找到足够信息"次数（需与 filtered_rate 联合看，单看会误判） |
| `mount_execution_gap` | Gauge | 本系统内挂载-执行记录对账缺口 |

### 8.4 端口暴露策略

宿主机**只映射三个端口**：Grafana 3000、Collector 4317(gRPC)、Collector 4318(HTTP)。其余一律不映射。新增端口暴露必须显式改契约测试。

### 8.5 失败语义对照（必须内化）

| 组件 | 不可达时 | 理由 |
|-----|---------|------|
| 可观测栈（P-OBS/Langfuse） | **fail-open**，业务照常 | 遥测丢失是可接受损失 |
| 审计（P-AUDIT，高风险事件） | **fail-closed**，业务回滚 | 合规义务不可丢 |
| **权限服务（外部）** | **fail-closed**，业务拒绝 | **越权不可逆** |

对照断言（契约测试）：同时注入 Collector 不可达 + 权限服务不可达，断言请求返回 503（权限 fail-closed）**且** `otel_export_failed_total` 递增（可观测 fail-open）——证明两条语义没有互相污染。

---

## 9. P-TASK 任务基础设施模块

### 9.0 模块边界

做：异步任务提交、可靠投递、结果回传、健康检查、Outbox relay 投递。**Haystack Pipeline 的同步 DAG 执行在此模块内包装为 Celery 任务**（`run_pipeline_async`）。不做：不持有业务逻辑。

### 9.1 run_pipeline_async（v14 核心包装）

```python
@celery_app.task(acks_late=True, reject_on_worker_lost=True)
def run_pipeline_task(pipeline_name: str, pipeline_input: dict, task_metadata: dict):
    """
    从 P-CONFIG Pipeline YAML 仓库反序列化 Haystack Pipeline，
    同步执行 pipeline.run()，结果通过 Redis Pub/Sub 回传（检索类）
    或写数据库（摄入类）。
    Pipeline 对象在 worker 内部构造，不跨进程共享状态。
    """
    pipeline = Pipeline.loads(get_pipeline_yaml(pipeline_name, task_metadata["yaml_version"]))
    result = pipeline.run(pipeline_input)
    handle_result(result, task_metadata)
```

**API 进程禁止调 `pipeline.run()`**——计算密集与 I/O 密集抢占同组进程，且无法接入队列限流。

### 9.2 四类队列

| 队列 | 承载的 Haystack Pipeline | 特征 |
|-----|------------------------|------|
| `ingestion_queue` | 摄入 Pipeline（DocumentSplitter → Embedder → MilvusDocumentStore） | 长耗时，可长退避重试 |
| `retrieval_queue` | 查询 Pipeline（TextEmbedder → Retriever → Ranker → PromptBuilder → Generator） | 用户在线等待，**不做长退避重试** |
| `stamping_queue` | 盖戳 Pipeline（VisibilityStampComponent） | 低优先级、可分批、可断点续跑；**与摄入分队列**（大 KB 权限变更产生的数万事件不能把摄入任务挤到队尾） |
| `outbox_relay` | 无 Pipeline（搬运 B-DOC outbox） | 常驻进程 |

### 9.3 流式回传（Redis Pub/Sub）

`query-stream:{task_id}` 频道；事件类型：retrieved / token / done / error；API 侧 SSE 订阅转发，**必须设超时**（30s 无消息发 error 断开）。

### 9.4 健康检查

`/healthz`（进程活着）与 `/readyz`（依赖就绪）。

**★ 权限服务不纳入 `/readyz`**，原因：(a) 权限服务挂了所有副本都不健康，摘掉全部 → 降级预案无法生效；(b) 会引发重启风暴；(c) 探测本身加剧故障。代价：必须配告警（`authz_call_failed_total` 非零即告警），不纳入 readyz 的裁定依赖告警有人响应。

---

## 10. P-STORE 对象存储模块

### 10.0 模块边界

做：文件读写、签名 URL、删除。不做：不理解文件语义、不做去重。

`StorageBackend` 抽象接口：`put` / `get` / `generate_presigned_url` / `delete`。SeaweedFS 单机一体化，S3 网关端口即 endpoint_url。

**签名 URL 的例外**：`doc:view` 路径**不签发指向原文件的 URL**，必须服务端渲染经应用层代理；`doc:download` 路径对有权者仍走签名 URL。

---

## 11. P-MODEL 模型与 Prompt 注册模块

### 11.0 模块边界

做：模型别名解析、连接配置、Prompt 版本池与绑定、**经 Haystack 官方 Integration 统一调用**（`LiteLLMGenerator`、`SentenceTransformersDocumentEmbedder`、`SentenceTransformersRanker`）、模型可观测性（Langfuse 经 LiteLLM callback 或 Haystack Langfuse Integration）。

不做：不决定哪个 KB 用哪个模型；不接管检索；不把模型级 span 灌进通用 Trace 后端。

### 11.1 接口（v14 实现映射）

| 接口 | 实现 | 防腐约束 |
|-----|-----|---------|
| `invoke_llm(model_id, prompt)` | 封装 Haystack `LiteLLMGenerator` | Generator 实例不出 P-MODEL 签名 |
| `invoke_embedding(model_id, texts, mode)` | mode=document → `SentenceTransformersDocumentEmbedder`；mode=query → `SentenceTransformersTextEmbedder` | Embedder 实例不出签名 |
| `invoke_rerank(model_id, query, documents)` | 封装 Haystack `SentenceTransformersRanker` | Ranker 实例不出签名 |
| `resolve_prompt(prompt_id, version)` | 返回 Jinja2 模板字符串，注入 Haystack `PromptBuilder` | PromptBuilder 实例不出签名 |
| `resolve_model(model_id)` | 返回连接配置（base_url、api_key、model_name）给上述接口使用 | — |

**与 P-MODEL 两条红线**：(a) synthesizer 模板必须由 `resolve_prompt` 解析后显式传入 `PromptBuilder`，**禁用框架默认 Prompt**；(b) 所有 Generator/Embedder/Ranker 实例必须经 `invoke_*` 注入，**B-INGEST/B-RETRIEVE/B-CHAT 不得直接 import Haystack 模型类**。

### 11.2 三类模型生命周期差异

- Embedding 换模型需全量重嵌入（走 §22 维护窗口期），同时须更新两个 Pipeline（摄入 + 查询）的 YAML 版本；
- Rerank/LLM 换模型即时生效（无历史数据绑定），更新 Pipeline YAML 版本后重新部署 worker 即可。

### 11.3 Langfuse 集成

LLM/Embedding/Rerank 的 trace、token、成本、Prompt 版本、生成质量归 Langfuse，经 LiteLLM callback 落地。**prompt 原文与生成结果只落 Langfuse，不进 Log/Trace/审计。** Langfuse 密钥经 secret_ref 存 Vault/K8s Secret，不硬编码。

---

## 12. P-CONFIG 配置模块

### 12.0 模块边界

做：检索参数级联解析、切分配置版本化存取、特性开关。不做：不执行检索/切分。

### 12.1 resolve_retrieval_config（四层级联）

级联顺序：`turn → conversation → kb → tenant`，就近覆盖。

**关键字段**：

| 字段 | 说明 |
|-----|-----|
| `top_k` | 最终返回 chunk 数 |
| `retrieval_mode` | vector_only / keyword_only / hybrid |
| `fusion_method` | rrf（推荐）/ weighted_sum（须先归一化） |
| `synthesis_mode` | compact / refine / tree_summarize / no_synthesis |
| `rerank_model_id` | 精排模型 |
| `strict` | 是否开层 3 逐条复核（默认 false，敏感 KB 显式开） |
| `oversample_factor` | 过采样系数（初始 1.5，按 filtered_rate 周期校准） |
| `min_results` | 补检索触发阈值（默认 3） |
| `refetch_max_rounds` | 最大补检索轮数（默认 2） |
| `haystack_pipeline_name` | 查询时使用的 Haystack Pipeline YAML 键名 |

> **★ `strict` 语义**：买的是"权限变更即时性"（撤权/封禁零窗口），不是"一切变更即时"。`is_enabled=false`（文档下线）不在 strict 保证范围内——下线用 `unlink` 或 `is_enabled=false`，紧急撤权用 `add_restriction`（管理台）。

### 12.2 resolve_chunking_config（版本化双形态）

- `resolve_chunking_config(kb_id)` 取当前生效版本（供 trigger_parse 锚定）
- `resolve_chunking_config(kb_id, version)` 按指定版本取（供任务执行与重试）
- append-only，(key, version) 唯一

**haystack_strategy 字段（v14 新增）**：

| haystack_strategy | Haystack Component | 关键参数 |
|------------------|-------------------|---------|
| `word` | `DocumentSplitter(split_by="word")` | `split_length`（≈chunk_size）、`split_overlap` |
| `sentence` | `DocumentSplitter(split_by="sentence")` | `split_length`、`split_overlap`、`language`（中文须显式配置） |
| `passage` | `DocumentSplitter(split_by="passage")` | `split_length`、`split_overlap` |
| `semantic` | `SemanticDocumentSplitter` | `embedding_model`（经 P-MODEL 注入）、`breakpoint_threshold_type` |
| `hierarchical` | 自定义 `HierarchicalDocumentSplitter`（`@component`） | `split_lengths`（各层数组）、层级关系写 `Document.meta["parent_id"]` |

### 12.3 feature_flag

`authz.decision_cache_enabled`（默认 **false**，开启须五条件 + 会签）；`authz.strict_default`（新建 KB 的 strict 默认值，建议 **false**）。


---

# 第三部分：业务模块

## 13. B-DOC 文档与目录管理模块

### 13.0 模块边界

做：文档物理登记与去重、挂载关系、目录树、下载签名与只读渲染查看、触发解析、**在写路径同步调用权限服务生命周期端口**。不做：不执行解析、不写执行状态、不碰 chunk、**不碰任何权益数据**、**不提供授权界面**（授权在管理台）。

### 13.1 文档与知识库是多对多

document 与 kb 是 N:M，解析结果（chunk/向量）跟着"文档在某 KB 下的挂载"走——不同 KB 可能用不同 chunking_config 和 embedding_model。

向量库 chunk payload 的戳记**必须按 (doc_id, kb_id) 粒度维护**，不能按 doc_id 粒度（同一文档在不同 KB 下可见性可能完全不同）。

### 13.2 数据模型

**document 表**：tenant_id、filename、content_fingerprint（SHA-256，与 tenant_id 联合唯一）、storage_path、file_size/mime_type、uploaded_by/created_at。

**document_kb_mount 表**：document_id/kb_id（联合唯一）、is_enabled（默认 true）、mounted_by/mounted_at。

**directory 表**：tenant_id、name、parent_directory_id、directory_type（kb_bound/manual）、bound_kb_id（UNIQUE）、created_by/at；唯一 (tenant_id, parent_directory_id, name)。

**document_directory_entry 表**：document_id + directory_id 联合唯一（仅服务手动目录）。

**outbox 表（B-DOC 分区）**：event_id、event_type、payload、tenant_id、trace_id、status、created_at。

### 13.3 文档摄入入口（登记与解析解耦）

#### 13.3.1 submit_ingest_task（上传入口，只登记不解析）

- 权限：`kb:write`（资源 = kb，走 `/v1/check`）
- 两层去重：层面 1 租户级物理去重（content_fingerprint）；层面 2 挂载级关系去重
- **行为契约（三处新增权限服务调用，全部在事务内）**：
  1. 计算指纹，按 (tenant_id, content_fingerprint) 查 document：已存在复用；不存在写物理存储 + 建 document，**同事务调 `register_resource(ctx, "doc", document_id, owner="user:"+ctx.user_id)`**
  2. 查/建挂载关系，**同事务调 `link_resource(ctx, document_id, kb_id)`**
  3. **只登记，不发 DocumentMounted、不触发解析**
  4. 若 `auto_parse=true`：登记后立即内部调 `trigger_parse(mount_id)`
- 返回：`{document_id, mount_id, parse_status: not_parsed, duplicate}`
- 错误：`auth:forbidden`、`doc:authz_write_failed`

#### 13.3.2 trigger_parse（解析动作，触发摄入）

- 权限：`kb:write`；前置：KB 状态非 reindexing（否则 `doc:kb_reindexing` 409）
- 行为：① 解析去重（经 B-INGEST `get_parse_status`）；② 经 P-CONFIG 取当前切分配置并**锚定 version**；③ 同事务写 outbox 发布 `DocumentMounted`（携带 chunking_config_version）
- **不调任何权限服务写端口**（挂载已在 submit 阶段 link 过）——权限镜像跟随**登记**（结构事实），不跟随**解析**（执行过程）

### 13.4 KB 级文件管理能力

| 能力 | 动词与端点 |
|-----|----------|
| 查看 KB 文件列表 | `kb:read` @ `/v1/check` |
| 手动启用/停用（is_enabled） | `kb:write` @ `/v1/check` |
| 触发解析 | `kb:write` @ `/v1/check` |
| 从 KB 移除 | `doc:unmount`（通道类，带 channel.kb）@ `/v1/check` |
| 彻底删除 | `doc:purge` @ `/v1/check` |
| 只读查看（view） | `doc:view` @ `/v1/check` |
| 下载（download） | `doc:download` @ `/v1/check` |
| 批量操作 | 对应动词 @ `check_batch`，逐资源独立判断、独立审计 |
| 授予/回收权限 | **不在本系统**——管理台直连权限服务；本系统 UI 至多提供跳转入口 |

#### 13.4.1 启用/停用开关语义

停用（is_enabled=false）：chunk 不删除；检索层 1 条件⑥ `retrievable=false` 屏蔽；重启用不需重解析。变更同事务发 `MountEnabledChanged`，B-INGEST 更新向量 payload `retrievable` 字段。

**三条硬纪律**：不得用 `add_restriction` 实现文档下线；不得用 `is_enabled=false` 实现紧急撤权（有事件传播窗口且 strict 层 3 不检查它）；紧急撤权正确路径是 `add_restriction`（管理台）或 `unlink`（本系统）。

#### 13.4.2 解析触发：按需而非自动

上传只创建 document 和挂载，不自动提交解析；用户确认配置后点"解析"才触发；提交时锚定当前切分配置版本。

#### 13.4.3 删除语义（并发安全 + 权限服务同步）

| 操作 | 行为契约 |
|-----|---------|
| **从 KB 移除**（purge=false） | 权限 `doc:unmount`（带 channel.kb）；document 加行锁；锁内：**同步调 `unlink_resource`**（超时 ≤2s，失败即回滚）→ 成功后删挂载 → 同事务发 `DocumentUnmounted` + `AUTHZ_WRITE` 审计；B-INGEST 置 cancelling → 清理 chunk/向量 |
| **彻底删除**（purge=true） | 权限 `doc:purge`；document 加行锁；锁内：删各挂载 → 发 `DocumentUnmounted`（每个）→ 统计剩余挂载 → **计数为 0 才**：**同步调 `retire_resource`**（原子完成：回收全部 acl + restriction + 解挂 + 置 retired）→ 成功后删物理文件 + 删 document 记录 + 同事务写 `DOC_DELETE`/`AUTHZ_WRITE` 审计 |

**调用顺序铁律**：先调权限服务，成功后再提交本地事务。唯一可能的不一致是"权限服务有、本地无"（孤儿镜像，安全且可对账回收）。

#### 13.4.4 批量操作

逐个单独校验权限（经 `check_batch`）、单独执行、单独审计；某文档权限不足则该文档单独失败，不影响其余。批量写路径**每个资源的 idempotency-key 必须各自独立**。

#### 13.4.5 view / download 分权

| 路径 | 权限 | 后端行为 |
|-----|-----|---------|
| view | `doc:view` | 服务端渲染只读视图，不签发原文件 URL |
| download | `doc:download` | 经 P-STORE 签发临时签名 URL |

view 不能用签名 URL——签名 URL 让客户端直接取到原文件字节，等价于下载，绕过 read_only 限制。

### 13.5 目录管理

| 类型 | 契约 |
|-----|-----|
| kb_bound | 创建 KB 时自动创建；内容动态从挂载关系派生；不能直接删除，随 KB 删除 |
| manual | 用户自由新增；内容独立存储（document_directory_entry） |

调整挂载的权限双向检查：移入须同时判 `doc:view` **且** `kb:write`，缺一不可；移出走 `doc:unmount`（通道类动词，已同时表达两侧）。

### 13.6 事件汇总

B-DOC 发布：`DocumentMounted`、`DocumentUnmounted`、`MountEnabledChanged`。B-DOC 消费事件：**无**。

### 13.7 写路径同步维护结构镜像

**所有需要同步调用的触发点**：

| 触发 | 调用 | 失败行为 |
|-----|-----|---------|
| 首次创建 document | `register(doc, document_id, owner)` | 回滚事务 |
| 建立挂载 | `link(document_id, kb_id)` | 回滚事务 |
| 创建 KB | `register(kb, kb_id, owner)` | 回滚事务 |
| 解除挂载 | `unlink(document_id, kb_id)` | 回滚事务 |
| 彻底删除 document | `retire(doc, document_id)` | 回滚事务 |
| 删除 KB | `retire(kb, kb_id)` | 回滚事务 |

**幂等键构造规则**：`{facade}-{tenant}-{resource_id}[-{kb_id}]-{schema_version}`，必须确定性可重算，**禁止时间戳/随机数/UUID**。超时 ≤2s；同一事务内不重试；返回 noop 视为成功；返回 409 告警。

### 13.7b 结构镜像对账

定时任务比对本系统 `document_kb_mount` 与权限服务镜像：方向一（我有它无）→ 补调 `link`，递增 `mirror_gap` 并**告警**（写路径漏调的 bug）；方向二（它有我无）→ 补调 `unlink` 回收孤儿，递增 `mirror_gap`，**记录但不告警**（正常回滚残留）。

---

## 14. B-INGEST 摄入管线模块

### 14.0 模块边界

做：解析、**切分（委托 Haystack `DocumentSplitter` 系列，经防腐层）**、嵌入（委托 Haystack Embedder，经 P-MODEL）、写向量库（Haystack `MilvusDocumentStore`）、执行状态机、幂等与失败隔离、**盖戳管道**。独占 `ingest_execution` 与 `chunks`。

不做：不拥有挂载关系、不做权限决策、**不计算戳记内容（只搬运）**、**不在 Haystack Component 内部写权限逻辑**。

> **"不计算戳记内容"是本模块最重要的边界**：盖戳管道调 `/v1/visibility` 拿到的结果原样写入向量库，**不合并、不推导、不补全**。一旦加工，B-INGEST 就成了第二权威。"只搬运"是这个模块能被排除在权限审计范围之外的全部理由。

### 14.1 设计要求

摄入/重嵌入耗时长、依赖外部服务。**耗时操作必须异步**，HTTP 接口只"提交任务 + 返回任务 ID"。经 P-TASK `run_pipeline_async` 投递到 `ingestion_queue`。

### 14.2 ingest_execution 表

| 字段 | 含义 |
|-----|-----|
| `mount_id` | 主键，对应 B-DOC 挂载 |
| `document_id`/`kb_id` | 冗余便查 |
| `parse_status` | not_parsed/queued/processing/completed/failed/cancelling/removed |
| `chunking_config_version` | 锚定的切分配置版本（可复现） |
| `pipeline_yaml_version` | 本次摄入使用的 Haystack Pipeline YAML 版本（可复现） |
| `execution_epoch` | 单调递增整数，每次（重）提交 +1，栅栏令牌 |
| `failure_reason` | 彻底失败原因 |
| `retry_count` | 默认 0 |
| `updated_at` | 状态更新时间 |

### 14.3 ingest_document_task（worker 任务）

**Haystack 摄入 Pipeline 结构**：

```
DocumentSplitter（按 haystack_strategy 选型）
    └──→ SentenceTransformersDocumentEmbedder（BGE-M3 稠密向量，经 P-MODEL）
          └──→ BGE-M3SparseEmbedder（稀疏向量，自定义 @component，经 P-MODEL）
                └──→ PermissionMetadataEnricher（注入权限字段，自定义 @component）
                      └──→ MilvusDocumentStore（写向量库，payload 含空戳记）
```

**PermissionMetadataEnricher 的防腐约束**（唯一允许触碰权限字段的 Component，但只做写入不做判断）：

```python
@component
class PermissionMetadataEnricher:
    @component.output_types(documents=List[Document])
    def run(self, documents: List[Document], permission_meta: PermissionMeta) -> dict:
        for doc in documents:
            doc.meta.update({
                "allow_stamps": [],       # 空戳记（安全默认值，对任何人不可见）
                "deny_stamps":  [],
                "vis_version":  None,
                "retrievable":  permission_meta.retrievable,
                # 其余权限元数据从 document 记录继承，不做任何计算
            })
        return {"documents": documents}
```

**任务主流程**：状态/epoch 前置检查 → 置 processing → 经 P-STORE 读文件 → 经 P-CONFIG 按锚定 version 取配置 → `run_pipeline_async` 执行 Haystack 摄入 Pipeline → upsert chunks（payload 含空戳记）→ **提交 `stamp_channel_task` 到 `stamping_queue`** → 置 completed。

**两原则**：写操作用 upsert（chunk 以 (mount_id, chunk_index) 唯一约束）；执行前检查是否已终态。

**协作式取消 + 乐观栅栏**：任务在每个关键写点前重读 `execution_epoch` 与 `parse_status`，满足任一即退出——自身 epoch < 当前 epoch（僵尸），或状态 == cancelling。

**重试契约**：最大 3 次，指数退避（30s→60s→120s）；retry 复用锚定 version。

### 14.4 切分委托契约（委托 Haystack）

| haystack_strategy | Haystack Component | 关键参数 |
|------------------|-------------------|---------|
| `word` | `DocumentSplitter(split_by="word")` | `split_length`、`split_overlap`、`split_threshold` |
| `sentence` | `DocumentSplitter(split_by="sentence")` | `split_length`、`split_overlap`、`language`（中文须显式配置，不可依赖默认） |
| `passage` | `DocumentSplitter(split_by="passage")` | `split_length`、`split_overlap` |
| `semantic` | `SemanticDocumentSplitter` | `embedding_model`（经 P-MODEL 注入，不旁路）、`breakpoint_threshold_type`、`buffer_size` |
| `hierarchical` | 自定义 `HierarchicalDocumentSplitter`（`@component`） | `split_lengths`（各层数组）、层级关系写 `Document.meta["parent_id"]` |

**防腐约束**：切分 Component 只在 B-INGEST 摄入 Pipeline 内 import，Haystack 类型不出模块签名。semantic 切分阶段调 Embedding，纳入容量规划，且必须经 P-MODEL（不得在 Component 内直接 import 模型 SDK）。

### 14.5 【核心】盖戳管道（Stamping Pipeline）

> **本节是本系统权限机制的关键运行链路。** 盖戳管道是 chunk 从"对任何人不可见"变为"正确可见"的唯一路径。

#### 14.5.1 向量库 chunk payload 字段契约

| 字段 | 来源 | 用途 | 参与层级 |
|-----|-----|-----|---------|
| `tenant_id` | 事件/document | 租户隔离 | **层 1 条件①** |
| `kb_id` | 事件 | 通道标识 | **层 1 条件②** |
| **`allow_stamps`** | **`/v1/visibility`** | 有权检索该 chunk 的**原始主体**（`user:`/`group:`/`role:` 前缀，禁止展开成员） | **层 1 条件③（MatchAny）** |
| **`deny_stamps`** | **`/v1/visibility`** | 被型二封禁的主体标识列表 | **层 1 条件④（must_not MatchAny）** |
| **`vis_version`** | **`/v1/visibility`** | 单调递增版本号；**缺此字段的 chunk 对任何人不可见** | **层 1 条件⑤** |
| `retrievable` | = is_enabled（`MountEnabledChanged`） | 运营停用屏蔽 | **层 1 条件⑥（本系统追加）** |
| `document_id` | 事件/document | 层 3 复核定位 | 层 3（非过滤条件） |
| `level_name` | chunks 表 | 层级检索匹配 | 层级检索 |

**两条硬约束**：`allow_stamps`/`deny_stamps` 只含原始主体，**禁止展开成员**（架构上展不开——`/v1/visibility` 返回的就是原始主体）；戳记按 **(doc_id, kb_id) 通道粒度**维护，同一文档在不同 KB 下戳记可能完全不同。

#### 14.5.2 三个触发源

| 触发源 | 时机 | 粒度 |
|-------|-----|-----|
| **摄入完成** | `ingest_document_task` 末尾提交盖戳任务 | (doc, kb) 单通道 |
| **`VisibilityChanged` 事件** | 权限服务任何影响可见性的变更 | (doc, kb) 单通道 |
| **对账补偿** | §14.5c 检出漂移或孤儿 | (doc, kb) 单通道 |

三个触发源汇聚到同一个任务实现 `stamp_channel_task(tenant, doc_id, kb_id, expected_version=None)`——不为三种来源写三套代码。

**v14 实现形态**（Haystack 盖戳 Pipeline）：

```
VisibilityStampComponent（自定义 @component）
    └──→ 调 POST /v1/visibility（经 P-AUTHC，不直接调）
          └──→ MilvusDocumentStore（update_policy=UPDATE，仅更新三个戳记字段）
```

#### 14.5.3 盖戳任务行为契约（六条，缺一即缺陷）

```
stamp_channel_task(tenant, doc_id, kb_id, expected_version=None):

1. 调 POST /v1/visibility { tenant, doc_id, channel:{kb} }
   失败/超时 → 不写任何东西、不 ack、抛出让 Celery 重试
   ★ 绝不把失败当成"无权限"写入 allow_stamps=[]（空集 = 所有人看不到，是本管道最危险的单点）

2. 若响应 unmounted == true:
     → 清空该 (doc,kb) 全部 chunk 的戳记：allow_stamps=[], deny_stamps=[], vis_version=null
     → 结束

3. 版本单调性：读该通道 chunk 当前 vis_version
     若 响应.version < 当前值 → 丢弃本次结果，直接结束（防乱序旧事件冲掉新封禁）

4. 分批 upsert 该 (doc,kb) 下全部 chunk 的三个字段（批大小建议 500，批间让渡）

5. 记录游标支持断点续跑；崩溃重投凭覆盖写天然幂等

6. 经审计门面发 STAMP_APPLIED（normal，fail-open——盖戳量大，不可 fail-closed）
```

#### 14.5.4 kb 粒度变更的规模化处理

**已确认**：权限服务以 **KB 粒度聚合**发出 `VisibilityChanged`，不逐文档发出。一次 KB 权限变更（如把 `group:all-staff` 加为 `kb_reader`）只发一条事件，payload 携带 `kb_id`，本系统收到后必须**自己展开**：

```
收到 VisibilityChanged(kb_id=xxx)
  → 展开任务（expand_visibility_changed_task，投递 stamping_queue）
      → 分页查 document_kb_mount，取该 kb_id 下全部 (doc_id, kb_id)
        （分页大小 500，防单次查询过大）
      → 每 (doc_id, kb_id) 对提交一个 stamp_channel_task 到 stamping_queue
        （按批次提交，批间让渡，不一次并发全部提交）
```

**展开与版本单调性**：展开过程中若又来了新的 KB 权限变更，会再次触发展开，两轮展开提交的盖戳任务可能交错执行。这不是问题——盖戳任务本身有版本单调性检查（§14.5.3 第 3 条），旧版本结果会被丢弃。**展开层不需要做版本协调，只需保证盖戳任务幂等提交。**

**★ payload 结构待确认（J-7 子问题）**：上述逻辑基于"payload 只有 kb_id"的假设。若权限服务实际在 payload 中附带了 doc_ids 列表，则可直接用，不需要查本地表，展开成本更低。联调时通过 J-7 确认，按实际 payload 调整，整体架构不变。

**`stamping_queue` 并发上限**：展开任务提交盖戳任务时，worker 并发度必须可配且有上限（建议初始 ≤10），防止集中打满向量库写入吞吐。

**限流退避策略（按行业经验，联调时验证）**：权限服务对 `/v1/visibility` 返回 **429** 时，读取响应头 `Retry-After`（单位：秒）作为等待时长；无此头时按当前退避周期等待。退避序列建议：`1s → 2s → 4s → 8s → 16s`（指数 + 抖动 ±20%），上限 60s，不超过 Celery 任务的 `soft_time_limit`。429 与 5xx 共用同一重试计数器，重试用尽后走 §14.6 的盖戳失败处理（不标 failed，交对账兜底）。

#### 14.5.5 层级 chunk 的戳记一致性

hierarchical 策略下父块与子块同属 chunks 表。盖戳按 (doc, kb) 通道粒度对该通道下全部层级 chunk 统一 upsert——父块**不因"是上层"而被跳过**，与子块同源同新。

### 14.5b 事件消费失败语义

| 失败点 | 行为 |
|-------|-----|
| P-AUTHC 订阅连接断开 | 重连；重连期间的事件由权限服务至少一次投递保证不丢 |
| 转交 B-INGEST 失败 | **不 ack**，下轮重投 |
| `/v1/visibility` 调用失败（5xx / 网络错误） | **不写任何东西、不 ack**，Celery 重试（指数退避 + 抖动） |
| `/v1/visibility` 被限流（**429**） | **不写任何东西、不 ack**，读取 `Retry-After` 响应头决定等待时长；无 `Retry-After` 时退避到当前退避上限；与 5xx 共用同一重试计数器 |
| 向量库 upsert 部分失败 | **不 ack**，凭游标续跑；已写入部分因覆盖写而幂等 |
| 事件重复投递 | 按 event_id 幂等；**重复盖戳是安全的**（覆盖写 + 版本单调性），去重是优化不是正确性保证 |

### 14.5c 戳记对账

定时任务（strict 库高频全量、普通库低频抽样）：

| 检出项 | 判据 | 处理 | 指标 |
|-------|-----|-----|-----|
| **戳记漂移** | 抽样调 `/v1/visibility`，与 chunk 当前戳记不一致 | 重新盖戳 | `stamp_drift` 告警 |
| **孤儿戳记** | chunk 的 `vis_version` 为 null 或字段缺失（说明有人绕过盖戳管道直写） | 补盖戳 | `orphan_stamp` 告警 |
| **版本落后** | chunk `vis_version` < 服务侧 `version` | 重新盖戳 | `stamp_drift` |

### 14.6 失败隔离与死信

**彻底失败**（重试用尽）：标记 failed + 记 failure_reason → 经审计发 `DOC_INGEST_FAILED` → 递增 `ingest_task_dead_letter` 告警。

**盖戳任务失败处理（与摄入不同）**：重试用尽后**不标 failed**，递增 `stamp_task_dead_letter` 告警 + 保持旧戳记，交由 §14.5c 对账自动兜底。盖戳失败对用户不可见，必须是自动修复而非人工干预。

**卸载清理时序**：`DocumentUnmounted` → 置 cancelling → 在途任务下个写点重读发现 cancelling 停止写入并退出 → 确认退出后清理 chunk/向量 → 置 removed。超时兜底：cancelling 停留超阈值（2× 单任务最长耗时）→ 强制清理 + 告警。

**卸载与盖戳的竞态**：盖戳任务调 `/v1/visibility` 会得到 `unmounted: true`（权限服务镜像已因 `unlink` 更新），走清空分支——与本地清理同向，不冲突。两个方向都安全，无需额外同步机制。

### 14.7 has_execution / get_parse_status

```
has_execution(mount_ids: list) → set[mount_id]    # 对账用，批量存在性
get_parse_status(mount_id) → ParseStatus | None   # trigger_parse 去重用，窄投影（不暴露 epoch 等）
```

### 14.8 事件汇总

| 消费事件 | 来源 | 动作 |
|---------|-----|-----|
| `DocumentMounted` | B-DOC | 创建执行记录、提交解析任务 |
| `DocumentUnmounted` | B-DOC | 置 cancelling → 清理 |
| `MountEnabledChanged` | B-DOC | 更新 payload `retrievable` |
| `VisibilityChanged` | 权限服务（经 P-AUTHC） | 提交盖戳任务 |

| 发布事件 | 订阅方 |
|---------|-------|
| `DocumentParsed` | P-AUDIT、P-OBS、质量采样 |
| `DocumentParseFailed` | P-AUDIT、告警 |

---

## 15. B-RETRIEVE 检索模块

### 15.0 模块边界

做：**Haystack 查询 Pipeline 管理**（YAML 版本化、按 KB 粒度选 Pipeline 变体）、prefilter 编译注入（层 1）、过采样与补检索（层 2）、strict 库逐条复核（层 3）、**混合检索（`MilvusEmbeddingRetriever` + 稀疏 Retriever + `DocumentJoiner` RRF）**、层级合并、**rerank（`SentenceTransformersRanker`）**。

不做：不做生成编排、不拥有对话状态、**不做权限判定**、**不做事后过滤**、**不在 Haystack Component 内部写权限逻辑**。

### 15.1 层 1 · prefilter 编译注入

```python
# 每次检索前（不跨请求缓存）
pf = authc.get_prefilter(scope)

if pf is SUSPENDED:
    return []   # 型一封禁，跳过检索，不发向量库查询

kb_candidates = compile_channels(pf) ∩ business_candidates   # §6A.4 交集规则
if not kb_candidates:
    return []   # 候选空集，恒假

for kb_id in kb_candidates:
    flt = compile_filter(pf, scope, kb_id)   # 六条件 → Haystack MetadataFilter 对象
    if flt is None:
        continue
    results += pipeline.run({
        "MilvusEmbeddingRetriever": {"filters": flt, "top_k": k_prime},
        "MilvusBM25Retriever":      {"filters": flt, "top_k": k_prime},  # 两路同一 flt
    })
```

**MetadataFilter 编译（六条件 → Haystack 标准对象）**：

```python
def compile_filter(pf: PreFilter, scope: AccessScope, kb_id: str) -> MetadataFilter:
    conditions = [
        MetadataFilter(field="tenant_id",    operator="==",                value=scope.tenant_id),
        MetadataFilter(field="kb_id",        operator="==",                value=kb_id),
        MetadataFilter(field="allow_stamps", operator="array_contains_any", value=list(scope.principals)),
        MetadataFilter(field="retrievable",  operator="==",                value=True),
    ]
    must_not = [
        MetadataFilter(field="deny_stamps",  operator="array_contains_any", value=list(scope.principals)),
        MetadataFilter(field="vis_version",  operator="is_none",           value=True),
    ]
    return MetadataFilter.from_conditions(must=conditions, must_not=must_not)
```

**★ 权限注入的防腐约束**：`compile_filter()` 在 B-RETRIEVE 的检索入口调用（不在 Component 内部）；`MetadataFilter` 对象作为参数传入 Pipeline；**Component 的 `run()` 方法签名接收 `MetadataFilter` 对象，不接收任何权限原语**（user_id、roles 等）。

#### 15.1.1 事后过滤禁令（三条理由，独立成立）

**不可先查全量再在应用层过滤：**
1. **召回坍塌**——top-k 被无权内容占满后，有权内容排到 k 之外直接丢失（结果错误，不只是无用功）；
2. **计数泄露**——"命中 20 条给你 3 条"暴露了无权内容的存在；
3. **上下文泄露**——无权内容进入 LLM 上下文即算泄露，**与最终答案是否引用它无关**（内容离开向量库进入应用进程内存即泄露）。

### 15.2 候选通道的确定

**最终候选 KB = `prefilter.kbs` ∩ 业务候选**（B-CHAT 静态绑定或动态路由）。

| prefilter 返回 | 编译方式 |
|--------------|---------|
| `kbs: ["kb-a","kb-b"]` | 候选 = 该列表 ∩ 业务绑定 |
| `kbs: "__ALL__"` + `excluded_kbs: [...]` | 候选 = 业务绑定 − excluded_kbs |
| `suspended: true` | **跳过检索**，直接返回空 |

**两条方向性纪律**：prefilter 是上界，业务绑定只能缩小不能扩大；交集为空 → 返回"未找到足够信息"，**不告知原因**。

### 15.3 层 2 · 过采样与补检索

```
k' = k × oversample_factor（初始 1.5，按 filtered_rate 指标周期校准）
结果数 ≥ min_results → 正常生成
结果数 < min_results  → 补检索，收窄业务候选通道，最多 refetch_max_rounds（默认 2）轮
仍不足               → 返回 retrieve:insufficient_evidence（"未找到足够信息"）
```

**铁律：绝不放宽过滤条件或扩展通道超过 prefilter 结果凑数。** 补检索收窄的是业务候选（集中 top-k 预算），不是权限过滤条件。契约测试须断言：补检索各轮使用的 `MetadataFilter` 对象内容完全相同。

### 15.4 层 3 · strict 库逐条复核

```python
if config.strict:
    items = [(c.document_id, c.kb_id) for c in results]
    items = dedup(items)                              # 按 (doc,kb) 去重，复核粒度是通道内文档
    allowed = authc.filter_items(scope, items)        # POST /v1/filter
    results = [c for c in results if (c.document_id, c.kb_id) in allowed]
```

**四条实现要点**：按 (document_id, kb_id) 去重再调；单批 ≤200，超出分批；传输失败/超时 → 整批 deny；**永久禁止缓存**。

拦截数记 `CHUNK_FILTERED(filtered_by=layer3_filter)`，被丢弃条目的 `decision_id` 写业务日志。

#### 15.4.1 一致性保证矩阵

| 场景 | strict=false | strict=true |
|-----|------------|------------|
| 撤权/型二封禁传播中 | 窗口内可能可检索（有界自愈） | **层 3 实时拒，不泄露** |
| 授权传播中 | 可能短暂搜不到（自愈，非安全问题） | 复核实时放行 |
| 型一主体封禁 | prefilter suspended，跳过检索 | 同左 |
| doc retire | 戳记随 unmounted 事件清除 | 层 3 即时拒 |
| **`is_enabled=false`（文档停用）** | **事件传播窗口内仍可检索** | **同左——strict 不覆盖此项** |

> **最后一行必须内化**：strict 买的是"权限变更即时"，不是"一切变更即时"。`is_enabled=false` 有事件传播窗口，即使 strict 库也如此。紧急下线用 `unlink` 或 `retire`（走权限服务），不是 `is_enabled=false`。

### 15.5 层级检索跨层合并

```
签名：retrieve_with_level_merge(scope, kb_id, query_vector, top_k, retrieval_level, synthesis_level) → list[Document]
```

**Haystack 查询 Pipeline 中的层级合并** 以自定义 Component 实现：

```python
@component
class HierarchicalMerger:
    @component.output_types(documents=List[Document], leaf_documents=List[Document])
    def run(self, documents: List[Document], synthesis_level: str,
            vector_store: MilvusDocumentStore) -> dict:
        # 沿 parent_id 回溯取 synthesis_level 祖先块
        # 合并取出的父块仍必须重新施加六条件过滤（在 Component 外部完成）
        ...
```

**合并后父块的六条件复核**：`HierarchicalMerger` 之后必须再次施加 `MetadataFilter`（父块不能因"是经权限校验的 leaf 关联出来的"而跳过）。strict=true 时父块也过层 3 复核，与子块来自同一文档则共享一次 `/v1/filter` 结果。

多命中 leaf 指向同一上层块去重；合并后超上下文窗口时须有明确截断策略。

### 15.6 存在性三通道纪律

| 通道 | 纪律 |
|-----|-----|
| **内容通道** | deny/indeterminate 的 chunk **静默丢弃**，绝不在响应中出现；不进 LLM 上下文、不进流式事件 |
| **计数通道** | **绝不告知用户"另有 N 条无权查看"**——计数本身就是泄露 |
| **文案通道** | 检索 deny 时，对外文案与"未找到足够信息"完全相同，**不能说"您没有权限"** |

**业务日志必须记录**（取证用，不出响应）：decision_id、actual_action、allowed_count、denied_ids。

### 15.7 混合检索与融合

**Haystack 查询 Pipeline 的混合检索节点链**：

```
用户查询
  ├──→ SentenceTransformersTextEmbedder（稠密，经 P-MODEL）
  │         └──→ MilvusEmbeddingRetriever(filters=flt)
  │
  └──→ BGE-M3SparseTextEmbedder（稀疏，自定义 @component）
              └──→ MilvusBM25Retriever(filters=flt)   ★ 同一 flt 对象

两路结果 ──→ DocumentJoiner(join_mode="reciprocal_rank_fusion", k=60)
```

**★ 两路必须传入同一个 `MetadataFilter` 对象**（不允许两路用不同 filter）。若关键词路无法施加相同过滤，则不得启用 hybrid 模式。契约测试须断言：hybrid 模式下两路 Retriever 的 `filters` 参数引用相等（或内容相等）。

**融合方式**：RRF（推荐默认，比较排名不比较分数）；weighted_sum（须先归一化，否则 alpha 无意义）。

### 15.8 rerank

**Haystack `SentenceTransformersRanker`** 封装在 P-MODEL，以自定义 `@component` 包装后注入查询 Pipeline：

```
DocumentJoiner 输出 ──→ SentenceTransformersRanker（只排序不筛选、不引入新 chunk）
```

rerank 不做任何权限过滤（进入 Ranker 的 Document 已全部通过六条件 + 层 3），不得引入新 chunk，不得缓存跨请求重用结果。

---

## 16. B-CHAT 对话编排模块

### 16.0 模块边界

做：对话/轮次存储、检索任务分发、流式回传、**生成合成（委托 Haystack 查询 Pipeline 生成节点，`PromptBuilder` + `LiteLLMGenerator`，经 P-MODEL 防腐）**、参数快照、降级文案。不做：不做底层检索计算、不做权限决策、**不在 Haystack Component 内部写权限判断**。

### 16.1 数据模型

**conversation 表**：tenant_id/user_id、assistant_id、bound_kb_ids（从 assistant 继承，允许会话级覆盖）。

**conversation_turn 表**：conversation_id、turn_index（联合唯一）、user_question（原始）、resolved_query（改写后实际检索 query）、retrieval_params_snapshot（JSONB，**解析后快照即最终值，不是配置表指针**）、retrieved_chunk_ids、trace_id、**authz_decision_ref**、**pipeline_yaml_version**（本轮使用的 Haystack Pipeline 版本，参数快照的一部分）。

> **`bound_kb_ids` 与权限的关系**：绑定时校验 `kb:read`（防止 UI 呈现用户永远搜不出东西的绑定）；**检索时仍以 prefilter 为准**（绑定后权限可能被撤销）。

### 16.2 检索模式与路由

- **路径 A 动态路由**：轻量分类模型每轮语义判断自动选库（不感知权限，必须与 prefilter 取交集）
- **路径 B 静态绑定**：助手配置显式绑定 1-N 个 KB
- **组合**：B 让用户选助手（小集合 KB），A 在小集合内路由

**两条路径都必须与 prefilter 取交集**（§15.2）。

### 16.3 生成合成

**Haystack 查询 Pipeline 生成节点链**：

```
（检索 + rerank 输出的 Document 列表）
  └──→ PromptBuilder（Jinja2 模板，从 P-MODEL.resolve_prompt 取）
          └──→ LiteLLMGenerator（从 P-MODEL.resolve_model 取连接配置）
```

**SynthesisMode 枚举与 Pipeline 变体**：

| SynthesisMode | Haystack Pipeline 变体 |
|--------------|----------------------|
| `compact` | 单 PromptBuilder + 单 Generator |
| `refine` | `IterativeRefinementPipeline`（自定义，逐 chunk 迭代，多次 Generator 调用） |
| `tree_summarize` | `TreeSummarizationPipeline`（自定义，分组摘要后合并） |
| `no_synthesis` | 直接返回 Document 列表，不调 Generator（"只要证据"场景） |

**两条红线**：(a) PromptBuilder 的 `template` 参数必须从 `resolve_prompt` 显式赋值，**禁用框架默认 Prompt**；(b) Generator 实例必须经 `invoke_llm` 注入，**B-CHAT 不得直接 import Haystack 生成类**。

### 16.4 生成层治理与降级文案

**引用校验**：确定性 chunk_id 校验——检查模型声称引用的 chunk_id 是否真在本次检索候选集内。

**复述长度守卫**：LLM 不得逐字复述超出必要长度的原文（否则 `doc:retrieve` 权限被当作 `doc:download` 用）。单 chunk 复述长度上限（建议不超过 chunk 长度的 60%），超限时改为摘要 + 引用提示。在查询 Pipeline 以后置 Component 实现。

> **边界声明**：复述守卫拦不住蓄意的多轮分段索取。它防的是无意的大段泄露，不是蓄意的完整提取。后者的正确对策是"敏感文档不进 RAG"或"敏感 KB 只开 no_synthesis 模式"，属产品决策。

**输出内容安全审查**：Llama Guard（本地）或 OpenAI Moderation。

**降级文案**：权限服务不可达时返回"服务暂时不可用，请稍后重试"，**与"未找到足够信息"可区分**（前者是故障，用户应重试；后者是正常结果，重试无用），**但不区分"权限故障"与"其他故障"**（区分即泄露故障面）。

### 16.5 事件汇总

发布：`KB_QUERY`（经审计门面，payload 含 query_hash/returned_count/conversation_id/turn_index/prompt_key/version/model_id/**authz_decision_ref**）。消费：无。


---

# 第四部分：部署架构——计算与存储分离

## 17. 任务化与主体传递

上传、解析、检索、盖戳统一为任务执行模型：摄入类与盖戳类 fire-and-forget；检索类因用户在线等待采用"提交任务 + 流式回传"。

**为什么检索也任务化**：向量检索、rerank 推理、LLM 生成都是计算密集/长耗时，直接在 API 进程处理会与"接收/维持大量 HTTP 连接"抢占同组进程。且检索负载与摄入负载扩容节奏不同。

**Haystack Pipeline 的序列化与传递**：任务参数中传递 `pipeline_name`（字符串，YAML 仓库中的键名）+ `yaml_version`，worker 侧按名反序列化 Pipeline 执行。不传 Pipeline 对象本身（无法安全序列化），不传 YAML 内容（修改 YAML 后应重启 worker 而非依赖任务参数中的旧 YAML）。

**检索任务与流式回传**：

Worker 侧 `retrieve_and_generate_task(query_payload)`（投递 `retrieval_queue`）：
- 输入：**`ctx_token`（不是 JWT 原文，§6A.5）**、conversation_id、kb_id、question、pipeline_name、yaml_version
- 行为：重建 ctx → 复用 P-CONFIG 级联解析 → 复用 B-RETRIEVE 三层检索 → 执行中把事件实时发布到 Redis Pub/Sub（`query-stream:{task_id}`）→ 完成后写 conversation_turn、经审计发 `KB_QUERY`

流式事件契约：`retrieved`（附 chunk_ids）、`token`（附 content）、`done`、`error`（附 message）。

API 侧 `POST /conversations/{id}/query`：**只分发任务 + 转发流，不做任何检索/生成计算**；**必须设超时**（30s 无消息发 error 断开）。

**主体传递三条纪律**：
1. API 层在派发任务前调 `mint_ctx_token(ctx, audience="retrieval-worker", ttl_s=600)`；铸造失败即返回 503，不派发任务
2. 任务参数中**绝不出现 JWT 原文**
3. ctx_token 过期即任务失败，不续期、不降级；故**检索任务不做长退避重试**

**已确认**：`/v1/prefilter` 接受 ctx_token，worker 侧可以直接用 ctx_token 调 prefilter，不需要 API 层预取。

**★ audience 值待确认（J-15 子问题）**：`mint_ctx_token` 的 `audience` 参数目前写 `"retrieval-worker"`（本系统 worker 进程的服务名），但权限服务在收到 ctx_token 后会校验 audience 是否在其注册的合法值列表内。若 `"retrieval-worker"` 不在列表，调 prefilter 会直接返回 `invalid_request`。**需与权限服务团队确认 `/v1/prefilter` 端点期望的 audience 值，在联调前对齐**，否则 worker 第一次调 prefilter 就会失败。

## 18. 部署服务划分契约

### 18.1 三层划分

**存储层（保存状态，持久化卷）**：

| 组件 | 用途 |
|-----|-----|
| PostgreSQL | 业务库/审计/各 outbox 表（**已无 ACL 库**）；Pipeline YAML 版本表 |
| SeaweedFS | 原文件对象存储（S3 网关） |
| Redis | Celery broker + result backend + 检索流式 Pub/Sub + Outbox relay |
| Milvus | 向量库（chunk 向量 + payload） |

**计算层（无状态，增副本扩容）**：

| 进程 | 承载 | Haystack Pipeline |
|-----|-----|-----------------|
| `api` | HTTP 接口，不执行 Pipeline | — |
| `ingestion-worker` | `ingestion_queue` | 摄入 Pipeline（DocumentSplitter → Embedder → MilvusDocumentStore） |
| `retrieval-worker` | `retrieval_queue` | 查询 Pipeline（TextEmbedder → Retriever → Ranker → PromptBuilder → Generator） |
| `stamping-worker` | `stamping_queue` | 盖戳 Pipeline（VisibilityStampComponent） |
| `outbox_relay` | 搬运 B-DOC outbox | — |

**Worker 进程隔离原则**：摄入、检索、盖戳不共享 worker；Haystack Pipeline 在各 worker 内**独立加载**，不跨进程共享 Pipeline 对象状态。

**外部依赖层（v14 必须在部署图上显式画出）**：

| 外部服务 | 我方依赖形态 | 不可达时 |
|---------|-----------|---------|
| 权限服务（决策面） | 每次交互端点 + 每次检索 + 每次盖戳 | **全面 fail-closed** |
| 权限服务（事件流） | 订阅 `VisibilityChanged` | 戳记停止更新；strict 库仍正确，非 strict 库陈旧扩大 |
| 权限服务（管理面生命周期端口） | 文档/KB 的增删挂载 | 写路径失败，读路径不受影响 |
| IdP（JWT 签发） | 本地校签（公钥缓存） | 已签发 token 有效期内仍可用 |

### 18.2 可观测栈部署契约

独立编排（`observability/docker-compose.yml`），与业务栈分开。**宿主机只映射三个端口**：Grafana 3000、Collector 4317(gRPC)、Collector 4318(HTTP)。

**Haystack 应用侧配置**：无需额外 OTel SDK 配置，Haystack 2.x 自动读取 `OTEL_EXPORTER_OTLP_ENDPOINT` 环境变量，`OTEL_SERVICE_NAME` 按进程区分（api / ingestion-worker / retrieval-worker / stamping-worker）。

**权限服务侧配置（只在 P-AUTHC 初始化处读，业务不感知）**：

```
AUTHZ_BASE_URL              # 权限服务地址（唯一）
AUTHZ_TIMEOUT_MS            # 按其 SLO 设定，四端点可分别配置
AUTHZ_EVENT_STREAM_URL      # VisibilityChanged 订阅地址
AUTHZ_CLIENT_CREDENTIAL     # 服务间鉴权凭据（经 secret_ref 存 Vault/K8s Secret，不进 .env 明文）
```

## 19. 扩容路径契约：只改配置不改代码

当前（单机验证）：全部服务一台机器，少量 worker 副本。

第一步（单机提并发）：只调 worker 副本数（摄入×4、检索×6、盖戳×2），存储层与代码不动。Haystack Pipeline 对象无状态，天然支持水平扩展。

第二步（多机）：PostgreSQL 迁独立主机或读写分离；Milvus 拆多机；Redis 视负载引入 Cluster/Sentinel；编排从 compose 升级 Swarm/K8s。

**★ 权限服务相关的扩容注意事项**：
- **连接池**：P-AUTHC 到权限服务的 HTTP 连接必须池化，且池大小随副本数增长（api 副本×6 意味着对权限服务并发连接×6）；扩容前须与权限服务确认其容量与限流策略；
- **盖戳队列并发上限**：一次大 KB 权限变更会涌入数万条事件，必须设队列级并发上限（建议初始 ≤10 并发调用）；
- **重试放大**：fail-closed 的重试会放大对权限服务的压力，须有指数退避 + 抖动 + 熔断（§25.3）。

---

# 第五部分：端到端场景

## 20. 场景一：一次在线查询（完整链路）

```
客户端 POST /conversations/{id}/query
  → [网关 PEP#1] JWT 签名/过期校验、限流（不做授权）
  → [P-AUTHC PEP#2] build_context（trace_id=request_id，保管 credential）
  → [P-AUTHC PEP#3a] require_permission(kb:read, conversation 绑定的 KB)
      → POST /v1/check（x-client-id: interactive-backend）
      → deny → emit_audit_event(AUTH_DENY, decision_id) → 403
      → indeterminate/超时 → 503 + 技术告警
  → [P-AUTHC] mint_ctx_token(audience="retrieval-worker", ttl_s=600)
  → [P-AUTHC] get_prefilter(scope)
      → suspended=true → 直接返回"未找到足够信息"，不派发任务（型一封禁）
  → [B-CHAT] API 层分发 retrieve_and_generate_task（携带 ctx_token + pipeline_name + yaml_version）
             SSE 订阅 query-stream:{task_id}（30s 超时）

  → [retrieval-worker] 从 P-CONFIG YAML 仓库反序列化 Haystack 查询 Pipeline
      → [P-CONFIG] resolve_retrieval_config（turn→conversation→kb→tenant 级联）
      → [B-CHAT] 确定业务候选 KB（静态绑定或动态路由）
      → [B-RETRIEVE 层 1] 候选 = prefilter.kbs ∩ 业务候选（交集，不是并集）
          → 交集为空 → 返回"未找到足够信息"（不告知原因）
      → 对每个候选 KB：
          → compile_filter(pf, scope, kb_id) → MetadataFilter（六条件）
          → Haystack Pipeline.run({
                "SentenceTransformersTextEmbedder": {"text": query},
                "MilvusEmbeddingRetriever": {"filters": metadata_filter, "top_k": k_prime},
                "MilvusBM25Retriever":      {"filters": metadata_filter, "top_k": k_prime},
            })
              ★ 两路必须传入同一个 MetadataFilter 对象
              → DocumentJoiner（RRF 融合）
      → [B-RETRIEVE 层 2] 结果数 < min_results → 补检索
          （收窄业务候选通道，最多 2 轮，MetadataFilter 不变）
          → 仍不足 → "未找到足够信息"
      → [B-RETRIEVE 层 3] strict=true →
          → 按 (document_id, kb_id) 去重 → P-AUTHC.filter_items() → POST /v1/filter
          → 丢弃判否条目，记 CHUNK_FILTERED(denied_decision_ids)
          → 传输失败/超时 → 整批 deny
      → SentenceTransformersRanker（只排序不筛选，经 P-MODEL）
      → HierarchicalMerger（如需，合并后父块重过 MetadataFilter）
      → PromptBuilder（模板从 P-MODEL.resolve_prompt 取）
      → LiteLLMGenerator（经 P-MODEL.invoke_llm）
      → 引用校验 + 复述长度守卫（查询 Pipeline 后置 Component）
      → 发布流事件 retrieved(chunk_ids) / token / done
      → [B-CHAT] 写 conversation_turn（参数快照含 pipeline_yaml_version + authz_decision_ref）
      → [P-AUDIT] emit_audit_event(KB_QUERY)
  → [B-CHAT] API 层转发流至客户端，done 结束
  → [P-OBS] Haystack Pipeline 节点 span 经内置 OTel tracing 自动导出 + authz.* span 手动附加
  → [P-MODEL] 模型调用明细经 LiteLLM callback 上报 Langfuse
  → ★ 四方共用同一 trace_id/request_id：
       Grafana(排障) / Langfuse(模型与成本) / audit_log(合规) / 权限服务审计(判定依据)
       且 audit_log.authz_decision_ref = 权限服务的 decision_id（主键级串联）
```

**体现边界**：API 进程只做 I/O 转发不做计算；权限判定全部在外部，本系统零判定；三层链路各司其职；fail-closed 贯穿；存在性三通道纪律；可观测 fail-open 与权限 fail-closed 并存且互不污染。

## 21. 场景二：一次文档摄入（登记 + 解析 + 盖戳三阶段）

```
【阶段一 登记】
  客户端上传 → [B-DOC] submit_ingest_task(identity, kb_id, file_bytes, filename)
    → [P-AUTHC] require_permission(kb:write, kb_id)
    → [B-DOC] 计算指纹 → 物理去重（document 行锁）：
        已存在复用；不存在 → P-STORE.put + 建 document
        ★ 同事务先调 register_resource(doc, document_id, owner="user:"+user_id)
          失败 → 回滚，doc:authz_write_failed(502)
    → [B-DOC] 建/查挂载（同一 document 行锁）：
        ★ 同事务先调 link_resource(document_id, kb_id) → 成功后提交本地事务
        → P-AUDIT.emit_audit_event_txn(AUTHZ_WRITE, facade=register/link, change_id)
    → 返回 {document_id, mount_id, parse_status: not_parsed}（只登记，未触发解析）

【阶段二 解析】
  用户点"解析" → [B-DOC] trigger_parse(identity, mount_id)
    → [P-AUTHC] require_permission(kb:write, kb_id)
    → [B-DOC] 检查 KB 状态非 reindexing
    → [B-DOC] 解析去重：经 B-INGEST.get_parse_status(mount_id)
        → queued/processing/completed → doc:duplicate；failed → 允许重触发
    → [P-CONFIG] resolve_chunking_config(kb_id) → 锚定 version + pipeline_yaml_version
    → [B-DOC] 同事务写 outbox 发布 DocumentMounted（含 chunking_config_version）
    → [P-TASK] outbox_relay 投递 → [B-INGEST] 创建 ingest_execution（queued, epoch=1）→ submit_task
    → [ingestion-worker] 从 YAML 仓库反序列化 Haystack 摄入 Pipeline：
        → 状态/epoch 前置检查 → 置 processing
        → [P-STORE] get 原文件
        → Haystack Pipeline.run({"DocumentSplitter": {"documents": [raw_doc]}})
            → SentenceTransformersDocumentEmbedder（BGE-M3 稠密，经 P-MODEL）
            → BGE-M3SparseEmbedder（稀疏，自定义 @component，经 P-MODEL）
            → PermissionMetadataEnricher（注入权限字段：allow_stamps=[], vis_version=null）
            → MilvusDocumentStore.write（写向量库）
              ★ payload 此时 vis_version=null → 对任何人不可见（安全默认值，fail-closed 方向正确）
        → ★ 提交 stamp_channel_task 到 stamping_queue
        → 置 completed → 发布 DocumentParsed → P-AUDIT.DOC_INGEST + P-OBS 指标

【阶段三 盖戳】★ v14 核心新增
  → [stamping-worker] 从 YAML 仓库反序列化 Haystack 盖戳 Pipeline：
      → VisibilityStampComponent.run():
          → POST /v1/visibility（x-client-id: ingest）
            → 失败/超时 → ★ 不写任何东西、不 ack → Celery 重试（绝不落盘空戳记）
          → 若 unmounted=true → 清空该通道全部 chunk 戳记 → 结束
          → 版本单调性检查：响应.version < 当前 vis_version → 丢弃
          → 分批 upsert（500/批，批间让渡）：allow_stamps / deny_stamps / vis_version
          → 记游标支持断点续跑
          → P-AUDIT.STAMP_APPLIED（normal, fail-open）
  → ★ 此刻文档才真正可被检索命中

【失败兜底】
  → 解析失败 → 指数退避重试（30s→60s→120s，最多 3 次，复用锚定 version）
      → 用尽 → failed + failure_reason → DocumentParseFailed → 告警
  → 盖戳失败 → 重试用尽 → ★ 不标 failed，递增 stamp_task_dead_letter 告警
      → §14.5c 对账检出 orphan_stamp 自动补盖
  → 事件丢失 → 对账扫出"有挂载无执行记录"补发 DocumentMounted（mount_execution_gap）
  → 镜像缺失 → §13.7b 对账检出 mirror_gap，补调 link
```

## 21b. 场景二·补：文档删除与权限回收

```
【路径甲 从 KB 移除】→ delete_document_from_kb(identity, doc_id, kb_id, purge=false)
  → [P-AUTHC] require_permission(doc:unmount, doc_id, channel={kb: kb_id})（通道类动词）
  → [B-DOC] document 加行锁：
      → ★ 先调 unlink_resource(doc_id, kb_id)（超时 ≤2s，失败即回滚）
      → 成功后删该挂载 → 同事务发 DocumentUnmounted + AUTHZ_WRITE/DOC_DELETE 审计
  → [B-INGEST] 置 cancelling → 在途任务下个写点退出 → 清理该挂载 chunk/向量 → removed
  → ★ 同时可能收到权限服务发来的 VisibilityChanged(unmounted=true)
      → 盖戳管道照常处理（清空戳记），与本地清理同向，幂等安全

【路径乙 彻底删除】→ delete_document_from_kb(..., purge=true)
  → [P-AUTHC] require_permission(doc:purge, doc_id)
  → [B-DOC] document 加行锁：
      → 删各挂载 → 发 DocumentUnmounted（每个）→ 统计剩余挂载
      → 计数为 0 →
          → ★ 先调 retire_resource(doc, document_id)
             权限服务原子完成四件事：① 回收全部 acl ② 回收 restriction ③ 解除全部挂载（发 unmounted 事件）④ 置 retired
          → 成功后 P-STORE.delete + 删 document 记录 + 同事务写 DOC_DELETE/AUTHZ_WRITE 审计
      → 释放锁
  → retire 后该资源所有判定返回 retired；相同指纹重传 = 新建 document + 新 register，不命中残留 ACL
```

## 22. 场景三：一次 Embedding 换模型（维护窗口期）

```
六阶段：
1. 选窗口期：该 KB 低峰时段；时长 = 全量重嵌入耗时 + 盖戳耗时 + 评测校验预留
2. 进入维护模式：KB.status 置 reindexing → 查询照常用旧索引；摄入/trigger_parse 拒绝
3. 离线全量重处理：
   → 仅换模型：读全部历史 chunk，用新模型批量生成向量写新 collection
   → 切分也变：先重新解析+切分再嵌入
   ★ 新 collection chunk 先写空戳记（对任何人不可见）
   ★ 更新两个 Pipeline YAML（摄入 Pipeline + 查询 Pipeline 的 embedding 模型字段），递增 yaml_version
4. 评测校验：用该 KB 评测集跑新 Pipeline → 不达标不切换
5. 盖戳：为新 collection 全部 (doc, kb) 通道统一调 stamp_channel_task
   ★ 盖戳完成才允许切换路由指针（新 collection 在盖戳完成前对任何人不可见，切错也不泄露）
6. 原子切换 + 恢复：KB 路由指针 + embedding_model_id 一次性更新；status 改回 active
   旧 collection 保留观察期后下线
```

**★ 为什么不复制旧 collection 的戳记**：(a) 维护窗口期权限可能变更，复制会丢失这段时间的变更；(b) 复制等于绕过盖戳管道，破坏"戳记唯一来源是权限服务"这条不变量，且对账逻辑无法区分"复制来的正确戳记"与"手工写入的错误戳记"。

---

# 第六部分：长期运营与检索质量

## 23. 知识库生命周期管理

**数据质量治理**：文档去重；失效文档清理（由 DocumentUnmounted 驱动，经 cancelling 时序保证无孤儿）；内容时效性管理。

**KB 删除的双侧清理**：依次完成 ① 本系统解除全部挂载（每个发 DocumentUnmounted）→ ② 调 `retire(kb, kb_id)` → ③ 清理向量库 collection → ④ 删 kb_bound 目录。**顺序不可颠倒**——先 retire 后清理向量库，清理期间该 KB 检索请求被权限服务正确拒绝。

## 24. 容量规划与成本治理

**对权限服务的调用量（容量规划必含项）**：

| 调用源 | 量级估算 | 增长驱动 |
|-------|---------|---------|
| `/v1/check` | ≈ 交互请求数 | 用户活跃度 |
| `/v1/prefilter` | ≈ 检索次数 | 查询量 |
| `/v1/filter` | ≈ strict 库检索次数 × 1（批量） | 查询量 × strict 库占比 |
| `/v1/visibility` | ≈ 摄入通道数 + 权限变更事件数 | **★ 权限变更是尖峰型**——一次 kb 粒度授权 = 该 KB 文档数次调用，10 万文档的 KB 一次授权 = 10 万次调用 |
| 生命周期端口 | ≈ 上传/挂载/删除次数 | 内容运营节奏 |

**三条应对**：`stamping_queue` 并发上限（§19）；与权限服务确认限流阈值及被限流时的返回码；监控 `stamp_lag_seconds`（尖峰期间正常上升，是有界且可接受的行为）。

## 25. 灾备、高可用与降级

### 25.1 关键组件故障影响

| 组件 | 挂了的后果 | 归属 |
|-----|---------|------|
| **权限服务** | **所有请求 fail-closed 全拒；检索全面拒答** | **外部，不在我方控制** |
| 向量库 | 全部检索失败 | 我方 |
| PostgreSQL | 全系统不可用 | 我方 |
| IdP | 新登录失败（已签发 token 有效期内可用） | 外部 |
| 可观测栈 | **业务照常**（fail-open） | 我方 |

**权限服务不纳入 `/readyz`**（§9.4 的裁定与三条理由，此处不重复）。

### 25.2 决策缓存：默认关闭

`/v1/filter` 永久禁止缓存（上游契约明文）。

`/v1/check` 的决策缓存默认关闭。开启须同时满足五条件且经会签：TTL ≤ 60s；缓存 key 含 policy_version；**订阅失效事件**（当前阻塞项：VisibilityChanged 是资源粒度事件，不覆盖主体粒度封禁，条件 3 不满足，故**缓存不许开**）；TTL 内不续期；仅作服务不可达时的可用性垫。

### 25.3 熔断降级：拒答型（须会签）

权限服务不可达时，**降级只有拒答一种形态**：

| 面 | 行为 |
|---|-----|
| 交互面 | 返回 503 `auth:authz_unavailable` + "服务暂时不可用" |
| 检索面 | 返回"服务暂时不可用，请稍后重试"（与"未找到足够信息"可区分） |
| 摄入面（登记/挂载/删除） | 写路径失败，返回 502，业务事务回滚 |
| 盖戳 | 不 ack、重试；戳记保持旧值 |

**不允许"降级为只检索 public"或"跳过 prefilter 用上次候选通道"**（关系型策略下不存在"无需查权益表即可证明可见"的等级；prefilter 是上界，绕过意味着型一封禁用户可以检索）。

**必须会签的三件事**：降级触发条件；降级期间的审计标记字段；恢复判据（半开探测策略）。

**熔断实现要点**：指数退避 + 抖动 + 熔断器（半开探测），防止 fail-closed 的重试放大对方故障。

### 25.4 权限服务"边界声明"在我方的落点

| 权限服务做不到 | 我方的应对 |
|-------------|---------|
| 撤权在非 strict 库有传播窗口 | 敏感 KB 开 `strict=true`；接受非 strict 库的有界自愈窗口；对账兜底 |
| 主体属性变更受 JWT 有效期限制 | JWT 短 TTL（建议 ≤15 分钟）；即时收权走 `add_restriction`（管理台） |
| 文档运营状态不是权限概念 | 用 `is_enabled`（本系统）或 `unlink`（权限服务），绝不用 `add_restriction` |
| 时序侧信道（补检索轮数与延迟差异可区分有权无权） | **在案未修**（与上游同步）；回访条件：系统开放给外部用户，或红队实际演示利用 |

## 26. 检索质量保障体系

### 26.0 边界澄清

| | 回答什么 | 归属 |
|---|---------|------|
| 通用可观测 | 检索快不快、稳不稳 | P-OBS |
| 模型观测 | 调了哪个模型、花多少 token | P-MODEL/Langfuse |
| 质量评测 | Recall@K、忠实度——改动前后变好还是变坏 | §26.1-26.5 |
| **权限** | **这个人能不能看这条** | **外部权限服务 + §6** |

**★ 权限与质量的交叉点**：评测集须标注构造时使用的主体身份，评测运行时用同一身份；**权限导致的空结果必须在评测报告中单独归类，不计入召回率分母**（否则开了 strict 的敏感 KB 会显得"质量很差"，进而有人去调检索参数去修一个根本不是检索问题的问题）。

**`retrieval_insufficient_total` 必须与 `filtered_rate{layer=layer1}` 联合看**：过滤率高且结果不足 → 权限问题；过滤率正常但结果不足 → 检索质量问题。单看任一都会误判。

### 26.1 离线评测体系

**评测集来源**：人工构造（每 KB 不少于 50-100 条三元组）；生产流量回流（抽样标注）；用户反馈信号。多 KB 按 KB 独立维护。

**Haystack 评测工具**：Haystack 2.x 提供 `EvaluationRunResult` 与 `RAGEvaluator`，可集成到契约测试框架（与 RAGAS/DeepEval 配合使用）。

**CI 集成**：embedding/chunk 策略/rerank/prompt 变更前必须跑一次；**Pipeline YAML 变更同样触发评测**（变更 PR 须附评测结果对比基线）；质量下降超阈值阻止合并。

### 26.2 在线监控与质量提升

用户反馈接入 Metric（按 kb_id/tenant_id 分组负反馈计数）；代理指标（检索结果为空比例、重新生成点击率）；抽样人工复核（1% 生产流量）。

### 26.3 检索记录策略（四个目的不能用同一份记录）

| 目的 | 归属 | 存原文？ | 访问控制 |
|-----|-----|---------|---------|
| 审计合规 | P-AUDIT | **否（脱敏，用 query_hash + decision_id）** | 合规审计员 |
| 质量评测 | 独立存储（分级采样） | 是 | 严格受限 |
| 性能监控 | P-OBS | 否 | SRE |
| 模型调用与成本 | P-MODEL/Langfuse | **是** | **严格受限** |

**★ JWT 原文与戳记内容不得出现在以上任何一处记录中**（JWT 泄露 = 身份泄露；戳记泄露 = 权限结构泄露，比 prompt 原文限制更严）。


---

# 第七部分：测试发布工程

## 27. 测试与发布工程

v14 的测试格局 = 本系统契约测试（§27.1，含全部权限纪律断言 + Haystack 增量项）+ 联合契约测试（§27.2，20 项，需权限服务双侧参与）。

### 27.1 本系统契约测试（CI 强制）

**权限相关（全部继承自 v13）**：

| 类别 | 检查点 |
|-----|-------|
| 权限出口唯一性 | 断言除 P-AUTHC 外，任何模块出现权限服务 base URL 字面量、`x-client-id` 字面量、直接 HTTP 调用权限服务的代码路径即失败 |
| 零判定断言 | 断言业务代码中不存在任何本地权限判断——扫描 `uploaded_by == user_id`、`owner`、`read_only`、`kb_reader` 等 privilege 字面量 |
| client_id 硬编码 | 断言不存在任何从业务模块入参决定 `x-client-id` 的代码路径 |
| 动词合法性 | 断言代码中所有 action 字面量都在上游动词目录 16 个之内；断言废除动词（`doc:write`/`acl:update`/`doc:delete`）零出现 |
| 通道类动词 | 断言 `doc:retrieve`/`doc:unmount` 的调用点必传 `channel.kb`；`doc:retrieve` 不经 `/v1/check` |
| 三态映射 | 注入 allow/deny/indeterminate/超时四种响应，断言四种业务行为正确；断言 `indeterminate` 与 `deny` 在 Metric 中标签不同；断言未知 obligation → deny + 告警 |
| fail-closed 全覆盖 | 注入权限服务不可达，断言：交互端点 503、检索整体拒答、`filter_items` 返回空、prefilter 失败不降级为无过滤查询、**不产生任何身份替换** |
| 层 1 过滤器完整性 | 断言注入向量库的过滤条件恒含六条件；断言 `vis_version=null` 的 chunk 不被返回 |
| 事后过滤禁令 | 断言不存在"先无过滤查询再应用层筛选"的代码路径；断言补检索各轮的过滤条件**完全相同** |
| 存在性三通道 | 断言检索响应中不含被过滤条目的任何痕迹；断言 deny 文案与"未找到足够信息"一致；断言 reasons 不出现在用户可见响应中 |
| view/download 分权 | 构造只有 read_only 的用户，断言 view 200 而 download 403；断言 download 路径独立调 `/v1/check` |
| 戳记通道粒度 | 构造一份文档挂两个成员不同的 KB，断言两处 chunk 的 `allow_stamps` 不同 |
| 盖戳纪律 | `/v1/visibility` 失败时不写任何东西、不 ack；版本单调性（构造乱序旧事件，断言不覆盖新戳记）；`unmounted=true` 清空；分批与断点续跑；重复投递幂等 |
| 写路径顺序 | 断言 register/link/unlink/retire **在本地事务提交之前**调用；调用失败时本地事务回滚；幂等键确定性可重算；批量操作逐资源使用不同幂等键 |
| JWT 不外泄 | 断言 `credential` 不出现在日志、Trace span attribute、审计 payload、任务参数中 |
| 三种失败语义并存 | 对照断言：同时注入 Collector 不可达 + 权限服务不可达 + 审计写失败，断言三条语义互不污染 |
| 就绪检查边界 | 停掉可观测栈断言 `/readyz` 仍 200；**停掉权限服务断言 `/readyz` 仍 200** |

**Haystack 增量项（v14 新增）**：

| 类别 | 检查点 |
|-----|-------|
| Haystack 防腐层隔离 | 断言除指定防腐层适配子模块外，任何模块 `import haystack` 即失败（与"禁 import llama_index"同型） |
| Component 权限零判断 | 断言所有 `@component` 的 `run()` 方法内不出现 P-AUTHC 接口调用、不出现 `user_id`/`roles`/`principals` 参数 |
| MetadataFilter 六条件完整性 | 断言注入每个 Retriever Component 的 `MetadataFilter` 恒含六条件；构造 `vis_version=null` 的 Document 写入，断言检索时不被返回 |
| **★ 两路 filter 一致性** | 断言 hybrid 模式下稠密路和稀疏路的 `MetadataFilter` 对象**引用相等**（或内容相等）；**若两路使用不同 filter 即严重缺陷** |
| Pipeline YAML 版本化 | 断言每次 Pipeline 变更后 `pipeline_yaml_version` 递增；断言 worker 按 `pipeline_name` 从仓库取 YAML，不从任务参数取 |
| P-MODEL 防腐 | 断言 Generator/Embedder/Ranker/PromptBuilder 实例不在 B-INGEST/B-RETRIEVE/B-CHAT 的模块签名中出现 |
| 异步包装 | 断言 Haystack Pipeline 不在 API 进程内同步执行（`pipeline.run()` 调用只出现在 worker 进程内） |
| 三种合成模式 Pipeline 变体 | 断言 compact/refine/tree_summarize 三种 Pipeline 在相同输入下生成语义一致的输出（关键 chunk 引用一致） |

**其余静态边界测试**（事件 schema 演进兼容、内部接口窄投影、统一错误码 REST 映射、单一写者等）继承 v13 全套。

### 27.2 【联合契约测试】与权限服务双侧参与（20 项）

> **为什么单侧测试不够**：§27.1 的所有权限测试都对着 mock 权限服务跑——验证的是我们的处理逻辑，验证不了我们对契约的理解是否正确。跨系统缺口恰恰产生于理解偏差。

**必测项清单**：

| # | 测什么 | 验证的假设 | 失败的后果 |
|---|-------|----------|----------|
| J-1 | 分享可检索性：仅经 `read_only` 获得访问权、对该 KB 无 `kb_reader` 的用户执行 RAG 检索 | `prefilter.kbs` 确实包含 `doc_authorized`（doc 级授权反查） | "文档点得开、搜不到"缺陷复现 |
| J-2 | 同一用户不能命中同 KB 内其他未授权文档 | 候选通道放宽未削弱层 1 戳记过滤 | 越权 |
| J-3 | 型一封禁：封禁某主体后调 prefilter | 返回 `suspended: true` | 被封禁用户仍能检索 |
| J-4 | 型二封禁的派生覆盖：封禁 `doc:view` 后检索该文档 | 派生覆盖 `doc:retrieve` 生效 | 封禁不生效 |
| J-5 | 通道封禁：封禁某 KB 的 `kb:read` 后检索 | 该通道的 `doc:retrieve` 被 `channel_denied` 短路 | 越权 |
| J-6 | 戳记内容正确性：授权 `group:eng` 后调 `/v1/visibility` | 返回 `group:eng` 原样，**不展开成 user 列表** | 存储放大，且与 JWT 侧匹配失败 |
| J-7 | kb 粒度授权的事件形态：给大 KB 授权后观察 `VisibilityChanged` | **✅ 已确认：KB 粒度聚合发出**，本系统在 §14.5.4 实现展开逻辑；payload 结构（是否含 doc_ids）仍需联调验证 | 若展开逻辑有 bug → 绝大部分文档戳记不更新 |
| J-8 | strict 实时性：撤权后立即检索 strict 库 | 层 3 `/v1/filter` 即时拒（零窗口） | strict 的全部价值落空 |
| J-9 | 非 strict 自愈：撤权后等待事件传播 | 戳记在有界时间内收敛 | 永久越权 |
| J-10 | retire 四合一：调 `retire` 后验证 | acl 回收、restriction 回收、挂载解除、retired 置位四件事全部发生 | 残留权限、指纹重传越权 |
| J-11 | 镜像缺失行为：不调 `register` 直接判定 | 返回 `unknown_resource` 全拒 | 若放行 → §13.7 整个同步登记设计失去必要性 |
| J-12 | 动词与端点绑定：`doc:retrieve` 走 `/v1/check` | 返回 `invalid_request` | 我方本地拦截可能过严或过松 |
| J-13 | 准入矩阵：用 `retrieval` 的 client_id 调 `doc:view` | 返回 `invalid_request` | 我方 client_id 分配错误 |
| J-14 | 批量端点可用性：`/v1/check/batch` 对 `interactive-backend` 是否开放 | **✅ 已确认：开放**，走单次往返；单批上限保守取 ≤200，自身上限联调时验证（J-16 顺带确认） | — |
| J-15 | prefilter 是否接受 ctx_token | **✅ 已确认：接受**；worker 直接用 ctx_token 调 prefilter；**子问题：audience 值待确认**（`"retrieval-worker"` 是否在权限服务的合法 audience 列表内） | audience 值填错 → worker 第一次调 prefilter 即 invalid_request |
| J-16 | filter 上限与超限行为：传 201 条 | 整批 `invalid_request` | 我方分批逻辑错误 |
| J-17 | decision_id 可追溯：用 decision_id 在权限服务侧查询 | 能查到当时的判定输入快照 | 取证链断裂 |
| J-18 | 超时行为：注入网络延迟超过 SLO | 我方 fail-closed 且不产生半成功状态 | 状态不一致 |
| J-19 | 限流行为：突发大量 `/v1/visibility` 调用 | **✅ 已按行业惯例落地**：假设返回 429 + `Retry-After` 头，退避序列 1s→2s→4s→8s→16s（上限 60s，±20% 抖动）；联调时验证权限服务实际返回码，若非 429 在 P-AUTHC 一处修改错误映射即可 | 若权限服务用非标码且本系统未识别 → 把限流当 5xx 处理，退避节奏可能不匹配 |
| J-20 | `is_enabled=false` 不在 strict 保证内：strict 库中停用文档后立即检索 | **仍能检索到**（证明边界为真） | 若实际拦截了，说明我们对边界理解错误 |

**运行机制**：在联调环境对真实权限服务实例跑，不对 mock；进 CI（可每日构建而非每次提交）；任一项失败即阻断发布，**且必须双方联合定位**。

**当前开口状态**：
- ✅ J-7 主方向已定（KB 粒度聚合），payload 结构（是否含 doc_ids）联调时验证
- ✅ J-14 已定（check/batch 开放），单批上限联调时确认
- ✅ J-15 主方向已定（接受 ctx_token），audience 值需提前与权限服务确认
- ✅ J-19 已按行业惯例落地（429 + Retry-After，退避 1→2→4→8→16s），联调时验证实际返回码

### 27.3 发布工程

**Haystack Pipeline YAML 的发布约束**：
- Pipeline YAML 纳入 Git 版本控制，变更须走 PR + Review
- Pipeline 变更视为"算法配置变更"，须附评测结果对比基线方可合并（§26.1）
- Embedding 模型变更同时触发 Pipeline YAML 版本递增 + 维护窗口期（§22）

**其余发布纪律**：`strict` 从 false 改 true 可直接生效（安全增强）；从 true 改 false 须走评审（安全降级）；P-AUTHC 适配层升级须与上游契约版本对齐且跑 §27.2 全套；动词或端点变更必须视为破坏性变更，走双方协调的发布窗口。

---

# 第八部分：优先级与全局视图

## 28. 优先级排序建议

**P0（不做会出大问题）**：

1. 模块注册表与依赖规则、单一写者
2. **P-AUTHC 适配层与五端点调用（§6、§6A）**——所有权限调用的前提
3. **层 1 六条件过滤器编译注入（§15.1、§6A.3）**——以 `MetadataFilter` 对象注入 Haystack Retriever
4. **盖戳管道（§14.5）**——层 1 的数据来源；不做则检索永远返回空
5. **写路径同步维护结构镜像（§13.7）**——不做则一切判定 `unknown_resource` 全拒
6. RAG 业务主链路工程化（B-INGEST/B-RETRIEVE/B-CHAT）
7. 摄入任务可靠性（幂等/失败隔离/健康检查）
8. 事件可靠投递（Outbox + 对账）
9. **搭建 Haystack Pipeline 骨架（摄入 + 查询两条）+ 防腐层约束**（@component 协议、Haystack 类型不出签名）
10. **Celery 包装层正确性**（`run_pipeline_async` 的序列化/反序列化、YAML 版本一致性、Pipeline 状态隔离）——做错这一层，所有 Haystack Pipeline 都跑不起来
11. 切分策略与检索参数（P-CONFIG + 切分委托）
12. 文档与知识库多对多挂载模型（B-DOC）
13. 联合契约测试 J-1 至 J-11——**契约理解错误发现得越晚，返工越大**

**P0 追加（权限外置特有）**：

- 零判定纪律的 CI 断言（§27.1 零判定断言 + 权限出口唯一性 + **Haystack Component 权限零判断**）
- 动词迁移完整性（三个废除动词漏改一处即运行时 `invalid_request`）
- **两路 filter 一致性断言**——Haystack 混合检索最容易犯的缺口，单路测试永远不会暴露
- 盖戳失败纪律——"绝不落盘空戳记"必须在盖戳管道第一版就有
- 写路径调用顺序（先权限服务后本地提交）
- 拒答型降级的会签与实现（§25.3）
- 存在性三通道纪律（§15.6）

**P1（可维护性与扩展性）**：

- 统一接口规格模板 + 统一错误模型 + 事件信封
- 摄入状态机并发加固（cancelling 态 + execution_epoch 栅栏）
- 登记/解析解耦 + 配置版本锚定
- **跨系统对账（§13.7b 镜像对账、§14.5c 戳记对账）**
- P-MODEL 防腐层（Haystack Generator/Embedder/Ranker 不出模块签名）
- P-CONFIG 配置中心；RAG 质量评测体系；模型注册表与 Prompt 版本池
- 契约测试全套（§27.1）
- 联合契约测试 J-12 至 J-20

**P2（长期运营）**：

- 容量规划与成本治理（含对权限服务的调用量规划，§24）
- 灰度/蓝绿发布；Haystack Pipeline YAML 灰度（按 KB 粒度 A/B 测试）
- 彻底删除并发安全行锁
- **生成层复述守卫（§16.4）**——防 `doc:retrieve` 被当作 `doc:download` 用；若面向外部客户提级为 P1

## 29. 完整模块地图

```
┌─────────────────────────────────────────────────────────────────┐
│ 第 0 章 全局规则                                                 │
│   模块注册表 / 依赖方向 / 单一写者 / 门面红线（四条）             │
│   ★ 红线 3：禁止越过 P-AUTHC 直连权限服务                       │
│   ★ 红线 4：禁止在 Haystack Component 内部旁路 P-MODEL 调模型    │
├─────────────────────────────────────────────────────────────────┤
│ 共享契约（第一部分）                                            │
│   RequestContext(含 credential) + 最小投影(AccessScope 含        │
│   credential / AuthzCallContext 刻意不含 tenant)                │
│   / 统一错误模型（5 个 auth:* + doc:authz_write_failed）         │
│   / 事件信封 + 投递语义 + 对账 / ID命名（动词以上游为准）         │
├═════════════════════════════════════════════════════════════════┤
│ ★★ 外部：权限服务（不属本系统，契约见上游文档）                  │
│   决策面 /v1/check /v1/filter /v1/prefilter /v1/context         │
│   投影面 /v1/visibility + VisibilityChanged 事件流              │
│   管理面 register/link/unlink/retire                            │
│   权益数据 acl/role_binding/restriction —— 全在对方              │
├═════════════════════════════════════════════════════════════════┤
│ 平台能力模块（第二部分，横切，被依赖）                          │
│   P-AUTHC → 权限服务唯一出口（本系统零判定）                    │
│            build_context / require_permission / check(_batch)   │
│            filter_items / get_prefilter / mint_ctx_token        │
│            register/link/unlink/retire_resource                 │
│            三态映射 + 四类 fail-closed                           │
│            client_id 硬编码，业务模块不可指定                    │
│            订阅 VisibilityChanged 转交 B-INGEST                 │
│   P-AUDIT → emit_audit_event/_txn（独占 audit_log）             │
│            authz_decision_ref 索引 = 跨系统取证主键             │
│   P-OBS   → Trace/Log/Metric 门面；Collector 唯一入口            │
│            ★ Haystack Pipeline 节点自动产生 span（无需额外埋点） │
│            → Grafana 唯一出口；仅暴露 3000/4317/4318            │
│            全程 fail-open，不纳入 readyz                        │
│   P-TASK  → submit_task/subscribe_stream/Outbox relay           │
│            ★ run_pipeline_async（包装 Haystack Pipeline         │
│              为 Celery 任务，API 进程禁止调 pipeline.run()）     │
│            四队列：ingestion / retrieval / stamping / relay     │
│   P-STORE → StorageBackend（SeaweedFS S3）                      │
│   P-MODEL → ★ 封装 Haystack Generator/Embedder/Ranker/          │
│               PromptBuilder（类型不出签名）                     │
│             invoke_*/resolve_prompt + Langfuse（模型线）        │
│   P-CONFIG→ resolve_retrieval(含 strict/haystack_pipeline_name) │
│             resolve_chunking(含 haystack_strategy)              │
├═════════════════════════════════════════════════════════════════┤
│ 业务模块（第三部分，纵向，依赖平台）                            │
│   B-DOC 文档与目录  → 独占 document/挂载关系/directory/outbox   │
│     ├ 多对多挂载 / 登记(submit)与解析(trigger)解耦              │
│     ├ ★ 写路径同步调生命周期端口（先服务后提交，失败即回滚）    │
│     ├ ★ 删除 = doc:unmount(通道) / doc:purge + retire 四合一    │
│     └ 发 DocumentMounted/Unmounted/MountEnabledChanged          │
│   B-INGEST 摄入管线 → 独占 ingest_execution/chunks              │
│     ├ ★ Haystack 摄入 Pipeline（经 P-TASK run_pipeline_async）  │
│     │   DocumentSplitter → Embedder → SparseEmbedder            │
│     │   → PermissionMetadataEnricher → MilvusDocumentStore       │
│     ├ 执行状态机（epoch 栅栏 + cancelling 态 + 协作式取消）      │
│     ├ ★★ 盖戳管道：VisibilityChanged → /v1/visibility →        │
│     │     upsert(allow_stamps/deny_stamps/vis_version)          │
│     │     六条纪律：失败不落盘 / unmounted 清空 / 版本单调 /    │
│     │              分批让渡 / 断点续跑 / 审计 fail-open         │
│     │     ★ 只搬运不计算 —— 不构成第二权威的全部理由             │
│     └ 摄入先落空戳记（安全默认值，对任何人不可见）              │
│   B-RETRIEVE 检索   → 独占向量库集合                            │
│     ├ ★ Haystack 查询 Pipeline（经 P-TASK run_pipeline_async）  │
│     │   TextEmbedder → MilvusEmbeddingRetriever(filters=flt)    │
│     │                → MilvusBM25Retriever(filters=flt)         │
│     │   ★ 两路必须传入同一个 MetadataFilter 对象               │
│     │   → DocumentJoiner(RRF) → SentenceTransformersRanker      │
│     │   → HierarchicalMerger（父块重过 MetadataFilter）          │
│     │   → PromptBuilder → LiteLLMGenerator                      │
│     ├ 层 1：compile_filter() → MetadataFilter（六条件）注入     │
│     │        权限注入在 Component 外部，Component 内零判断       │
│     ├ 层 2：过采样 k×1.5 + 补检索（MetadataFilter 不变）        │
│     ├ 层 3：strict → P-AUTHC.filter_items() → /v1/filter        │
│     │        (doc,kb) 去重，≤200，永久禁缓存，超时整批 deny     │
│     └ 事后过滤禁令 + 存在性三通道纪律                           │
│   B-CHAT 对话编排   → 独占 conversation/conversation_turn       │
│     ├ 检索任务分发（传 ctx_token 非 JWT）+ 流式回传             │
│     ├ 静态绑定/动态路由（均须与 prefilter 取交集）              │
│     └ ★ 复述长度守卫（防 retrieve 被当作 download 用）          │
├═════════════════════════════════════════════════════════════════┤
│ 部署架构（第四部分）计算-存储分离 / 任务化                      │
│   存储层（无 OPA/Keycloak）/ 计算层（四类 worker）              │
│   / 外部依赖层（权限服务三个面）/ 可观测层                      │
│   ★ Haystack Pipeline YAML 仓库（P-CONFIG 管理，版本化）        │
│   ★ Haystack Pipeline 无状态，天然支持水平扩展                  │
│   ★ 扩容成比例放大对权限服务的调用，须与对方对齐容量            │
├─────────────────────────────────────────────────────────────────┤
│ 端到端场景（第五部分）查询 / 摄入(登记+解析+盖戳三阶段)         │
│   / 删除(同步 unlink/retire) / 换模型窗口(须重新盖戳 + 更新    │
│   两个 Pipeline YAML)                                           │
├─────────────────────────────────────────────────────────────────┤
│ 运营与质量（第六部分，含降级/熔断/边界声明落点）                │
│ 测试发布（第七部分，§27.1 单侧 + Haystack 增量项 + §27.2 联合 20 项） │
└─────────────────────────────────────────────────────────────────┘

依赖方向：B-* ──▶ P-* ──▶ 外部权限服务（单向）；禁止环
         任何模块不得越过 P-AUTHC 直连权限服务
         任何模块不得在 Haystack Component 内部写权限逻辑
数据归属：每张表唯一写者；权益数据完全不在本系统
事件契约：本系统事件经 Outbox 至少一次 + 幂等消费 + 对账
         VisibilityChanged 是外部事件，我方只订阅不发布
快照失效：戳记由 VisibilityChanged 失效 + 对账兜底；strict 库另有层 3 实时
并发有序：cancelling 态 + execution_epoch 栅栏 + 戳记版本单调性
框架边界：Haystack 仅在 B-INGEST/B-RETRIEVE/B-CHAT 内部经防腐层；类型不出签名
         OTel tracing 由 Haystack 内置，节点即 span，P-OBS 无需额外配置
         权限注入以 MetadataFilter 对象形式，在 Component 外部传入
权限四铁律：
   1. 判定权威在权限服务，本系统零判定
   2. 戳记只存原始主体，禁止展开成员（架构上展不开）
   3. 一切权限与结构事实变更必经权限服务管理面
   4. fail-closed 无例外
三种失败语义并存且互不污染：
   可观测 fail-open / 审计高风险 fail-closed / 权限 fail-closed
四方取证串联：Grafana / Langfuse / audit_log / 权限服务审计
             经 trace_id + authz_decision_ref（= decision_id）
```

## 30. 核心设计决策速查

| 决策点 | 本设计的选择 | 放弃的替代方案及原因 |
|-------|-----------|-------------------|
| **算法框架** | **Haystack 2.x（Pipeline + @component 协议）** | LlamaIndex——防腐层更难写干净，权限注入点不显式，OTel 需手动埋点 |
| **Pipeline 异步化** | **Celery 包装（`run_pipeline_async`），Haystack Pipeline 只在 worker 进程内同步执行** | 直接在 API 进程内调 `pipeline.run()`——计算密集与 I/O 密集抢占同组进程，且无法接入队列限流 |
| **权限注入形态** | **MetadataFilter 对象在 Component 外部传入，Component 内零判断** | 在 Component 内部调 P-AUTHC——Component 成为第二权威，且无法单独测试 |
| **Haystack 升级策略** | **Pipeline YAML 版本化 + 变更须附评测对比基线** | 直接升级不测试——Haystack 版本间 Component API 可能变化，影响 Pipeline 执行 |
| **权限判定** | **完全外置到权限服务，本系统零判定** | 自持 Rego/ACL——重复建设，判定权威唯一性需持续论证 |
| **权限出口** | **收敛到 P-AUTHC 一个模块** | 各业务模块自行调用——契约变更即全库改造，fail-closed 逻辑写错三遍 |
| **戳记来源** | **唯一来源 `/v1/visibility`，B-INGEST 只搬运不计算** | 本地推导或加工——一旦加工即成第二权威 |
| **结构镜像维护** | **写路径同步调用，先服务后提交本地** | 异步事件登记——登记窗口内资源不可访问；失败则永久不可访问 |
| **权限回收** | **同步调 `retire`（四合一原子）** | 异步 `DocumentPurged`——回收窗口 + 指纹重传越权隐患 |
| **候选通道** | **`prefilter.kbs` ∩ 业务绑定（交集，prefilter 是上界）** | 自行计算候选——曾因只按 kb:read 裁剪而使分享在检索路径失效 |
| **层 1 过滤** | **六条件全部在向量库内执行（MetadataFilter）** | 事后过滤——召回坍塌 + 计数泄露 + 上下文泄露，三条独立成立 |
| **撤权实时性** | **分级**：默认信戳记 + 事件自愈；敏感库开 `strict` 走 `/v1/filter` | 全量实时复核（成本不可接受）/ 只信戳记（窗口内越权） |
| **决策缓存** | **默认关闭**；开启须五条件 + 会签，**当前条件 3 不满足** | 开缓存提性能——陈旧的 allow 比一次拒绝更危险 |
| **权限服务不可达** | **拒答型降级**（503/"服务暂时不可用"） | 降级为只检索 public——关系型策略下不存在"无需查权益表即可证明可见"的等级 |
| **盖戳队列** | **独立 `stamping_queue`** | 复用摄入队列——大 KB 权限变更会把摄入任务全部挤到队尾 |
| **摄入后的戳记初值** | **空戳记（对任何人不可见）** | 复制或推导——fail-closed 方向正确；不可见是可用性问题不是安全问题 |
| **换模型时的戳记处理** | **新 collection 重新盖戳，不复制旧戳记** | 复制旧戳记——丢失维护窗口期权限变更，且绕过盖戳管道破坏不变量 |

---

> **v14 一句话收敛**：v13 的所有权限纪律（P-AUTHC 五端点、盖戳管道六条纪律、写路径结构镜像、三层检索链路、四类 fail-closed、联合契约测试 20 项）+ Haystack 2.x 的 Pipeline 运行时和 @component 协议。Haystack 让"组件可插拔"和"管道跑起来"变成框架责任，本系统把节省出来的研发精力集中在"怎么在企业安全约束下让管道跑起来"。
>
> **两个提问（每次评审必跑，§0.5）：**
> **(1) 这个概念的权威源在哪？** 说不出来就是空白。答"在权限服务那边"不算答案，要答出上游契约的章节号。
> **(2) 同一个问题，系统里有几处在回答？** 超过一处就要有人证明它们相等。跨系统的"两处"最危险——两边的文档各自完整、各自自洽，缺口只在缝隙里，且联合契约测试是唯一的真实验证。

