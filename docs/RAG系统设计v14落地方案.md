# RAG 系统设计 v14 落地方案

> **本文档配套**：RAG 系统设计 v14（权限外置 + Haystack 框架版）
> **核心原则**：每个阶段是一个可独立运行的完整系统；后续阶段只加新东西，不修改前面阶段的接口、表结构和事件契约。

---

## 目录

1. [落地总览](#一落地总览)
2. [关键设计决策](#二关键设计决策)
3. [开发环境搭建（docker-compose）](#三开发环境搭建docker-compose)
4. [阶段一：单条链路跑通](#四阶段一单条链路跑通)
5. [阶段二：检索质量加固](#五阶段二检索质量加固)
6. [阶段三：工程加固](#六阶段三工程加固)
7. [阶段四：生产就绪](#七阶段四生产就绪)
8. [各阶段能力对照表](#八各阶段能力对照表)

---

## 一、落地总览

### 1.1 四个阶段

| 阶段 | 目标 | 核心交付 | 完成标准 |
|-----|-----|---------|---------|
| **阶段一** | 单条链路跑通 | 上传 + 检索 + 权限三道 + 盖戳 | 查询能返回有意义答案，clearance 不足的用户拿不到高密级文档 |
| **阶段二** | 检索质量加固 | rerank + 层 3 strict + 多轮改写 | Context Recall ≥ 0.70（RAGAS） |
| **阶段三** | 工程加固 | 幂等摄入 + 跨系统对账 + 熔断降级 | 重启断点续传，撤权 5 分钟内自愈 |
| **阶段四** | 生产就绪 | 分布式部署 + CI 质量门禁 | P99 < 8s，通过安全渗透测试 |

### 1.2 "不修改前面"的实现条件

后续阶段不修改前面阶段的前提是：**每个接口签名、每张表的字段、每个事件的 payload 从第一天就按 v14 最终形态定义**。

阶段一的工作量分两块：
- **接口骨架**：按最终形态定死，包括暂时不实现的接口也要有签名和 no-op 实现
- **功能实现**：本阶段实际执行的逻辑

后续阶段填实现，不动骨架。具体体现在：
- `ingest_execution` 表的 `execution_epoch`、`pipeline_yaml_version` 字段阶段一就建好，阶段三才真正用上
- `conversation_turn` 表的 `resolved_query`、`authz_decision_ref` 字段阶段一就建好，阶段二填真实值
- `filter_items` 接口阶段一就有，`strict` 默认 false，阶段二开 strict 时不加新接口
- `invoke_rerank` 接口阶段一就有（no-op），阶段二补实现

---

## 二、关键设计决策

### 2.1 为什么 P-AUTHC 和盖戳管道在阶段一就完整实现

**P-AUTHC**：权限是系统的安全边界，简化版的权限等于没有权限。P-AUTHC 的接口一旦定好，后续阶段不需要动它，只是调用它的地方越来越多。阶段一就完整实现，后面三个阶段都不碰它。

**盖戳管道**：盖戳六条纪律（尤其是"失败不落盘空戳记"）是安全纪律，不是可以后补的功能。空戳记的 chunk 对任何人不可见，方向是对的；但如果第一版盖戳写错（失败时落了空戳记），修起来要全量重盖，成本极高。六条纪律必须在第一版就全部到位。

### 2.2 为什么表结构从一开始就按最终形态建

表结构变更在有数据之后是最贵的操作。以下字段阶段一就建好，即使暂时填默认值：

| 表 | 字段 | 阶段一的值 | 真正使用的阶段 |
|---|-----|---------|-------------|
| `ingest_execution` | `execution_epoch` | 固定为 1 | 阶段三（并发加固） |
| `ingest_execution` | `pipeline_yaml_version` | 固定为 "v1" | 阶段二（多 Pipeline 版本） |
| `conversation_turn` | `resolved_query` | 等于 `user_question` | 阶段二（多轮改写） |
| `conversation_turn` | `authz_decision_ref` | 空字符串 | 阶段二（审计补全） |
| `conversation_turn` | `pipeline_yaml_version` | 固定为 "v1" | 阶段二 |
| `document` | `content_fingerprint` | 计算写入 | 阶段三（MD5 幂等去重） |

### 2.3 阶段一暂时 no-op 的接口

以下接口阶段一有签名和框架，但实现为 no-op 或简化实现：

| 接口/功能 | 阶段一实现 | 升级阶段 |
|---------|---------|--------|
| `invoke_rerank` | 直接返回输入列表（不排序） | 阶段二 |
| `filter_items`（层 3） | strict 默认 false，接口有但不调 `/v1/filter` | 阶段二 |
| `emit_audit_event` | structlog 记日志，不落 `audit_log` 表 | 阶段二 |
| `resolve_retrieval_config` 的 turn/conversation 级联 | 只有 kb/tenant 两层 | 阶段二 |
| `check_batch` | 串行调 N 次 `check`（性能低但正确） | 阶段二（走 `/v1/check/batch`） |
| 多轮对话改写 | `resolved_query = user_question`（直接透传） | 阶段二 |
| 目录管理 | 接口签名有，实现返回 not_implemented | 阶段二 |


---

## 三、开发环境搭建

### 3.1 两个 compose 文件的职责划分

开发期把基础设施和计算层分开管理，理由很直接：

- **基础设施**（postgres / redis / milvus / seaweedfs / cerbos）不会变，用 docker-compose 一次拉起，后台常驻即可
- **计算层**（api / worker）是正在开发的代码，要频繁改代码、热重载、加断点，本地直接跑进程比在容器里调试舒服得多

两个文件的用途：

| 文件 | 用途 | 使用场景 |
|-----|-----|---------|
| `docker-compose.infra.yml` | 基础设施（仅依赖服务） | 开发期常驻，一次启动不动 |
| `docker-compose.app.yml` | 计算层（api + worker） | 上线部署用，开发期不用 |

### 3.2 目录结构

```
rag-v14/
├── docker-compose.infra.yml    # 基础设施（开发期使用）
├── docker-compose.app.yml      # 计算层（上线部署使用）
├── .env                        # 环境变量（不进 Git）
├── .env.example                # 环境变量模板（进 Git）
├── Dockerfile                  # 计算层容器镜像（上线部署使用）
├── requirements.txt
├── Makefile                    # 本地开发快捷命令
├── pipelines/                  # Haystack Pipeline YAML 仓库
│   ├── ingest_v1.yaml
│   └── query_v1.yaml
├── cerbos/                     # Cerbos 权限服务配置（进 Git）
│   ├── .cerbos.yaml
│   ├── policies/
│   │   ├── resource_policies/
│   │   │   ├── document.yaml
│   │   │   └── kb.yaml
│   │   ├── principal_policies/
│   │   │   └── admin.yaml
│   │   └── derived_roles/
│   │       └── rag_roles.yaml
│   └── schemas/
│       ├── principal.json
│       ├── document.json
│       └── kb.json
├── scripts/
│   ├── init.sql                # 建表 SQL
│   └── seed_dev.sql            # 开发期测试数据
├── src/
│   ├── permission/             # P-AUTHC
│   ├── ingest/                 # B-INGEST
│   ├── retrieve/               # B-RETRIEVE
│   ├── chat/                   # B-CHAT
│   ├── doc/                    # B-DOC
│   ├── platform/
│   │   ├── task/               # P-TASK
│   │   ├── model/              # P-MODEL
│   │   ├── config/             # P-CONFIG
│   │   ├── audit/              # P-AUDIT
│   │   ├── obs/                # P-OBS
│   │   └── store/              # P-STORE
│   ├── api/                    # FastAPI 路由
│   └── main.py
└── tests/
    ├── contract/               # 契约测试（§27.1）
    └── integration/            # 集成测试
```

### 3.3 docker-compose.infra.yml（基础设施，开发期使用）

只包含不会变的依赖服务。所有端口对外暴露，本地进程可以直接通过 `localhost` 连接。

```yaml
version: "3.9"

networks:
  rag-network:
    name: rag-network
    driver: bridge

volumes:
  postgres_data:
  redis_data:
  etcd_data:
  minio_data:
  milvus_data:
  seaweedfs_data:
  cerbos_audit:

services:

  # ════════════════════════════════════════════════════════
  # 存储层
  # ════════════════════════════════════════════════════════

  # 用途：业务关系库（文档登记、挂载关系、对话记录、审计日志、outbox）
  # 本地连接：postgresql://rag:<password>@localhost:5432/rag
  # 使用方：api / ingestion-worker / retrieval-worker / stamping-worker / outbox-relay
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: rag
      POSTGRES_USER: rag
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./scripts/init.sql:/docker-entrypoint-initdb.d/init.sql
    ports:
      - "5432:5432"
    networks:
      - rag-network
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U rag"]
      interval: 5s
      timeout: 3s
      retries: 5
    restart: unless-stopped

  # 用途：① Celery broker（ingestion / retrieval / stamping 三类任务队列）
  #        ② Celery result backend
  #        ③ 检索结果流式回传（query-stream:{task_id} 频道）
  # 本地连接：redis://:${REDIS_PASSWORD}@localhost:6379/0
  # 使用方：api / 三类 worker / outbox-relay
  redis:
    image: redis:7-alpine
    command: redis-server --requirepass ${REDIS_PASSWORD} --appendonly yes
    volumes:
      - redis_data:/data
    ports:
      - "6379:6379"
    networks:
      - rag-network
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "${REDIS_PASSWORD}", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5
    restart: unless-stopped

  # 用途：Milvus 的元数据存储（仅 milvus 内部依赖，业务代码不直接访问）
  etcd:
    image: quay.io/coreos/etcd:v3.5.14
    environment:
      ETCD_AUTO_COMPACTION_MODE: revision
      ETCD_AUTO_COMPACTION_RETENTION: "1000"
      ETCD_QUOTA_BACKEND_BYTES: "4294967296"
    volumes:
      - etcd_data:/etcd
    command: >
      etcd
      --advertise-client-urls=http://0.0.0.0:2379
      --listen-client-urls=http://0.0.0.0:2379
      --data-dir=/etcd
    networks:
      - rag-network
    healthcheck:
      test: ["CMD", "etcdctl", "endpoint", "health"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  # 用途：Milvus 的原始向量数据持久化（仅 milvus 内部依赖，业务代码不直接访问）
  minio:
    image: minio/minio:RELEASE.2024-01-01T00-00-00Z
    environment:
      MINIO_ACCESS_KEY: ${MINIO_ACCESS_KEY:-minioadmin}
      MINIO_SECRET_KEY: ${MINIO_SECRET_KEY:-minioadmin}
    volumes:
      - minio_data:/data
    command: minio server /data --console-address ":9001"
    networks:
      - rag-network
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  # 用途：向量库，存储 chunk 向量 + 权限戳记 payload
  #        B-INGEST 写入 / B-RETRIEVE 查询 / B-INGEST 盖戳管道更新戳记
  # 本地连接：localhost:19530（gRPC，Haystack MilvusDocumentStore 使用）
  # 使用方：ingestion-worker / retrieval-worker / stamping-worker
  milvus:
    image: milvusdb/milvus:v2.4.13
    command: milvus run standalone
    environment:
      ETCD_ENDPOINTS: etcd:2379
      MINIO_ADDRESS: minio:9000
      MINIO_ACCESS_KEY_ID: ${MINIO_ACCESS_KEY:-minioadmin}
      MINIO_SECRET_ACCESS_KEY: ${MINIO_SECRET_KEY:-minioadmin}
    volumes:
      - milvus_data:/var/lib/milvus
    ports:
      - "19530:19530"
      - "9091:9091"
    networks:
      - rag-network
    depends_on:
      etcd:
        condition: service_healthy
      minio:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9091/healthz"]
      interval: 10s
      timeout: 5s
      retries: 10
    restart: unless-stopped

  # 用途：文档原文件对象存储（S3 兼容网关）
  #        B-DOC 上传时写入 / B-INGEST 读取原文解析 / B-DOC 删除时清理
  # 本地连接：http://localhost:8333
  # 使用方：api（上传/删除）/ ingestion-worker（读原文）
  seaweedfs:
    image: chrislusf/seaweedfs:3.68
    command: server -s3 -s3.port=8333
    volumes:
      - seaweedfs_data:/data
    ports:
      - "8333:8333"
    networks:
      - rag-network
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8333/"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  # ════════════════════════════════════════════════════════
  # 权限服务（Cerbos PDP）
  # ════════════════════════════════════════════════════════

  # 用途：外部权限服务，承担本系统全部授权判定（本系统零判定）
  #   /v1/check       → P-AUTHC 交互端点门禁（api 调用）
  #   /v1/check/batch → P-AUTHC 批量权限判定（api 调用）
  #   /v1/filter      → P-AUTHC 检索后逐条复核，strict 库专用（retrieval-worker 调用）
  #   /v1/prefilter   → P-AUTHC 检索前编译六条件过滤器（retrieval-worker 调用）
  #   /v1/context     → P-AUTHC 铸造异步任务主体 token（api 调用）
  #   /v1/visibility  → B-INGEST 盖戳管道取 allow_stamps/deny_stamps（stamping-worker 调用）
  #   管理面 register/link/unlink/retire → P-AUTHC 写路径同步调用（api 调用）
  # 本地连接：http://localhost:3592
  # 使用方：api / retrieval-worker / stamping-worker
  cerbos:
    image: ghcr.io/cerbos/cerbos:0.39.0
    command: server --config=/config/.cerbos.yaml
    volumes:
      - ./cerbos/.cerbos.yaml:/config/.cerbos.yaml:ro
      - ./cerbos/policies:/policies:ro
      - ./cerbos/schemas:/schemas:ro
      - cerbos_audit:/audit
    ports:
      - "3592:3592"
      - "3593:3593"
    networks:
      - rag-network
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3592/_cerbos/health"]
      interval: 5s
      timeout: 3s
      retries: 10
    restart: unless-stopped
```

### 3.4 docker-compose.app.yml（计算层，上线部署使用）

**开发期不使用此文件**，直接在本地跑进程（见 §3.7）。此文件在上线部署时使用，与 `docker-compose.infra.yml` 配合：

```bash
# 上线部署时同时启动基础设施和计算层
docker-compose -f docker-compose.infra.yml -f docker-compose.app.yml up -d
```

```yaml
version: "3.9"

networks:
  rag-network:
    name: rag-network
    external: true    # 复用 infra 已创建的网络

volumes:
  model_cache:        # BGE-M3 等模型缓存，ingestion-worker 和 retrieval-worker 共享

services:

  # 用途：FastAPI HTTP 服务，对外唯一入口
  #        接收上传、查询、管理请求；派发 Celery 任务；转发流式结果给客户端
  #        不执行任何 Haystack Pipeline（计算全部在 worker 中运行）
  # 依赖：postgres / redis / milvus / cerbos（由 infra 提供）
  api:
    build:
      context: .
      dockerfile: Dockerfile
    command: uvicorn src.main:app --host 0.0.0.0 --port 8000
    environment: &app_env
      DATABASE_URL: postgresql+asyncpg://rag:${POSTGRES_PASSWORD}@postgres:5432/rag
      REDIS_URL: redis://:${REDIS_PASSWORD}@redis:6379/0
      MILVUS_HOST: milvus
      MILVUS_PORT: "19530"
      S3_ENDPOINT_URL: http://seaweedfs:8333
      S3_ACCESS_KEY: ${MINIO_ACCESS_KEY:-minioadmin}
      S3_SECRET_KEY: ${MINIO_SECRET_KEY:-minioadmin}
      S3_BUCKET: rag-files
      AUTHZ_BASE_URL: http://cerbos:3592
      AUTHZ_TIMEOUT_MS: ${AUTHZ_TIMEOUT_MS:-2000}
      AUTHZ_EVENT_STREAM_URL: ${AUTHZ_EVENT_STREAM_URL:-}
      AUTHZ_CLIENT_CREDENTIAL: ${AUTHZ_CLIENT_CREDENTIAL:-}
      OLLAMA_BASE_URL: ${OLLAMA_BASE_URL}
      OLLAMA_MODEL: ${OLLAMA_MODEL:-qwen2.5:7b}
      JWT_PUBLIC_KEY_URL: ${JWT_PUBLIC_KEY_URL}
      OTEL_EXPORTER_OTLP_ENDPOINT: ${OTEL_COLLECTOR_ENDPOINT}
      OTEL_SERVICE_NAME: api
      PIPELINE_YAML_DIR: /app/pipelines
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
    volumes:
      - ./pipelines:/app/pipelines
    ports:
      - "8000:8000"
    networks:
      - rag-network
    restart: unless-stopped

  # 用途：摄入任务 worker，消费 ingestion_queue
  #        执行 Haystack 摄入 Pipeline：文档解析 → 切分 → BGE-M3 嵌入 → 写 Milvus
  #        摄取完成后提交盖戳任务到 stamping_queue
  # 依赖：postgres / redis / milvus / seaweedfs（由 infra 提供）
  ingestion-worker:
    build:
      context: .
      dockerfile: Dockerfile
    command: >
      celery -A src.platform.task.celery_app worker
      -Q ingestion_queue
      --concurrency=2
      -n ingestion-worker@%h
      --loglevel=${LOG_LEVEL:-info}
    environment:
      <<: *app_env
      OTEL_SERVICE_NAME: ingestion-worker
      SENTENCE_TRANSFORMERS_HOME: /models
    volumes:
      - ./pipelines:/app/pipelines
      - model_cache:/models
    networks:
      - rag-network
    restart: unless-stopped

  # 用途：检索任务 worker，消费 retrieval_queue
  #        执行 Haystack 查询 Pipeline：嵌入 → Milvus 混合检索（六条件过滤）
  #        → RRF 融合 → Rerank → PromptBuilder → LLM 生成 → 流式推送到 Redis
  # 依赖：postgres / redis / milvus / cerbos（由 infra 提供）
  retrieval-worker:
    build:
      context: .
      dockerfile: Dockerfile
    command: >
      celery -A src.platform.task.celery_app worker
      -Q retrieval_queue
      --concurrency=4
      -n retrieval-worker@%h
      --loglevel=${LOG_LEVEL:-info}
    environment:
      <<: *app_env
      OTEL_SERVICE_NAME: retrieval-worker
      SENTENCE_TRANSFORMERS_HOME: /models
    volumes:
      - ./pipelines:/app/pipelines
      - model_cache:/models
    networks:
      - rag-network
    restart: unless-stopped

  # 用途：盖戳任务 worker，消费 stamping_queue（独立队列，不与摄入混用）
  #        从 Cerbos /v1/visibility 取戳记，批量 upsert 到 Milvus chunk payload
  #        独立队列防止大 KB 权限变更产生的大量盖戳任务挤压摄入任务
  # 依赖：redis / milvus / cerbos（由 infra 提供）
  stamping-worker:
    build:
      context: .
      dockerfile: Dockerfile
    command: >
      celery -A src.platform.task.celery_app worker
      -Q stamping_queue
      --concurrency=4
      -n stamping-worker@%h
      --loglevel=${LOG_LEVEL:-info}
    environment:
      <<: *app_env
      OTEL_SERVICE_NAME: stamping-worker
    volumes:
      - ./pipelines:/app/pipelines
    networks:
      - rag-network
    restart: unless-stopped

  # 用途：B-DOC outbox relay
  #        轮询 outbox 表，将领域事件可靠投递到 Redis 消息总线
  #        发布 DocumentMounted / DocumentUnmounted / MountEnabledChanged 等事件
  # 依赖：postgres / redis（由 infra 提供）
  outbox-relay:
    build:
      context: .
      dockerfile: Dockerfile
    command: python -m src.platform.task.outbox_relay
    environment:
      <<: *app_env
      OTEL_SERVICE_NAME: outbox-relay
    networks:
      - rag-network
    restart: unless-stopped
```

### 3.5 Dockerfile 与 requirements.txt

Dockerfile 和 requirements.txt 供 `docker-compose.app.yml` 上线部署时构建镜像使用。开发期本地直接跑进程，不需要构建镜像。

**Dockerfile**：

```dockerfile
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple && \
    pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn && \
    pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY pipelines/ ./pipelines/

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**requirements.txt**：

```text
# ── Haystack 核心 ────────────────────────────────────────────────────
haystack-ai==2.3.1
haystack-experimental==0.2.0

# ── Haystack 官方 Integration ────────────────────────────────────────
milvus-haystack==0.3.1
fastembed-haystack==1.1.0
FlagEmbedding==1.2.11

# ── 向量库客户端 ──────────────────────────────────────────────────────
pymilvus==2.4.9

# ── LLM 网关 ─────────────────────────────────────────────────────────
litellm==1.42.0
openai==1.40.0

# ── 嵌入与重排序模型 ─────────────────────────────────────────────────
sentence-transformers==3.1.1
torch==2.3.1+cpu    # GPU 环境替换为 torch==2.3.1+cu121

# ── 异步任务 ─────────────────────────────────────────────────────────
celery==5.4.0
redis==5.0.7
kombu==5.3.4

# ── Web 框架 ─────────────────────────────────────────────────────────
fastapi==0.115.0
uvicorn[standard]==0.31.0
python-multipart==0.0.12

# ── 数据库 ───────────────────────────────────────────────────────────
asyncpg==0.29.0
sqlalchemy[asyncio]==2.0.35
alembic==1.13.2

# ── 对象存储 ─────────────────────────────────────────────────────────
boto3==1.35.0

# ── 权限服务客户端（P-AUTHC） ────────────────────────────────────────
httpx==0.27.2
tenacity==9.0.0
circuitbreaker==2.0.0

# ── JWT ──────────────────────────────────────────────────────────────
python-jose[cryptography]==3.3.0
PyJWT==2.9.0

# ── 可观测（OTel） ───────────────────────────────────────────────────
opentelemetry-sdk==1.27.0
opentelemetry-exporter-otlp==1.27.0
opentelemetry-instrumentation-fastapi==0.48b0
opentelemetry-instrumentation-celery==0.48b0
opentelemetry-instrumentation-asyncpg==0.48b0
opentelemetry-instrumentation-httpx==0.48b0

# ── 日志 ─────────────────────────────────────────────────────────────
structlog==24.4.0

# ── 模型观测（阶段二接入） ───────────────────────────────────────────
langfuse==2.43.3
langfuse-haystack==0.1.0

# ── RAG 质量评估（阶段二使用） ───────────────────────────────────────
ragas==0.1.21

# ── 工具 ─────────────────────────────────────────────────────────────
pydantic==2.9.2
pydantic-settings==2.5.2
python-dotenv==1.0.1
```

### 3.6 .env.example

```bash
# ── 数据库 ───────────────────────────────────────────────────────────
POSTGRES_PASSWORD=change_me_in_dev

# ── Redis ────────────────────────────────────────────────────────────
REDIS_PASSWORD=change_me_in_dev

# ── 对象存储（SeaweedFS S3 网关） ─────────────────────────────────────
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin

# ── LLM（宿主机已部署 Ollama） ─────────────────────────────────────────
# Mac / Windows
OLLAMA_BASE_URL=http://host.docker.internal:11434
# Linux：改为 Docker 网关 IP（通常 172.17.0.1）
# OLLAMA_BASE_URL=http://172.17.0.1:11434
OLLAMA_MODEL=qwen2.5:7b

# ── 权限服务（Cerbos） ─────────────────────────────────────────────────
# 开发期本地进程直连 localhost；容器内进程用服务名 cerbos
# 本地进程（api / worker）填 localhost：
AUTHZ_BASE_URL=http://localhost:3592
# 上线部署（docker-compose.app.yml）时改为：
# AUTHZ_BASE_URL=http://cerbos:3592
AUTHZ_TIMEOUT_MS=2000
AUTHZ_EVENT_STREAM_URL=
AUTHZ_CLIENT_CREDENTIAL=

# ── JWT 公钥地址（IdP 提供） ───────────────────────────────────────────
JWT_PUBLIC_KEY_URL=http://idp:8080/realms/rag/protocol/openid-connect/certs

# ── 统一可观测平台（OTel Collector 地址由平台团队提供） ─────────────────
OTEL_COLLECTOR_ENDPOINT=http://<otel-collector-host>:4317

# ── 日志级别 ─────────────────────────────────────────────────────────
LOG_LEVEL=INFO

# ── 本地开发：基础设施连接地址（本地进程直连 localhost） ─────────────────
DATABASE_URL=postgresql+asyncpg://rag:${POSTGRES_PASSWORD}@localhost:5432/rag
REDIS_URL=redis://:${REDIS_PASSWORD}@localhost:6379/0
MILVUS_HOST=localhost
MILVUS_PORT=19530
S3_ENDPOINT_URL=http://localhost:8333
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin
S3_BUCKET=rag-files
PIPELINE_YAML_DIR=./pipelines
```

### 3.7 Cerbos 配置文件

#### cerbos/.cerbos.yaml

```yaml
server:
  httpListenAddr: ":3592"
  grpcListenAddr: ":3593"

storage:
  driver: disk
  disk:
    directory: /policies
    watchForChanges: true

schema:
  enforcement: warn

audit:
  enabled: true
  backend: local
  local:
    storagePath: /audit
    retentionPeriod: 168h
```

#### cerbos/policies/derived_roles/rag_roles.yaml

```yaml
apiVersion: api.cerbos.dev/v1
derivedRoles:
  name: rag_roles
  definitions:
    - name: kb_reader
      parentRoles: ["user"]
      condition:
        match:
          expr: >
            request.resource.kind == "kb" &&
            "read" in request.resource.attr.granted_actions

    - name: kb_writer
      parentRoles: ["user"]
      condition:
        match:
          expr: >
            request.resource.kind == "kb" &&
            "write" in request.resource.attr.granted_actions

    - name: kb_admin
      parentRoles: ["user"]
      condition:
        match:
          expr: >
            request.resource.kind == "kb" &&
            "manage" in request.resource.attr.granted_actions
```

#### cerbos/policies/resource_policies/document.yaml

```yaml
apiVersion: api.cerbos.dev/v1
resourcePolicy:
  version: "default"
  resource: "document"
  importDerivedRoles:
    - rag_roles

  rules:
    - actions: ["doc:view"]
      effect: EFFECT_ALLOW
      derivedRoles: ["kb_reader", "kb_writer", "kb_admin"]
      condition:
        match:
          expr: >
            request.resource.attr.is_enabled == true &&
            request.resource.attr.retired == false

    - actions: ["doc:download"]
      effect: EFFECT_ALLOW
      derivedRoles: ["kb_reader", "kb_writer", "kb_admin"]
      condition:
        match:
          expr: >
            request.resource.attr.is_enabled == true &&
            request.resource.attr.retired == false &&
            request.resource.attr.allow_download == true

    - actions: ["doc:retrieve"]
      effect: EFFECT_ALLOW
      derivedRoles: ["kb_reader", "kb_writer", "kb_admin"]
      condition:
        match:
          all:
            of:
              - expr: request.resource.attr.is_enabled == true
              - expr: request.resource.attr.retired == false
              - expr: >
                  !("denied" in request.resource.attr.restrictions) ||
                  !request.resource.attr.restrictions.denied.hasAny(
                    [request.principal.id])

    - actions: ["doc:unmount"]
      effect: EFFECT_ALLOW
      derivedRoles: ["kb_writer", "kb_admin"]
      condition:
        match:
          expr: request.resource.attr.retired == false

    - actions: ["doc:purge"]
      effect: EFFECT_ALLOW
      derivedRoles: ["kb_admin"]
      condition:
        match:
          expr: request.resource.attr.retired == false

    - actions: ["doc:share"]
      effect: EFFECT_ALLOW
      roles: ["system_admin"]
```

#### cerbos/policies/resource_policies/kb.yaml

```yaml
apiVersion: api.cerbos.dev/v1
resourcePolicy:
  version: "default"
  resource: "kb"
  importDerivedRoles:
    - rag_roles

  rules:
    - actions: ["kb:read"]
      effect: EFFECT_ALLOW
      derivedRoles: ["kb_reader", "kb_writer", "kb_admin"]
      condition:
        match:
          expr: request.resource.attr.retired == false

    - actions: ["kb:write"]
      effect: EFFECT_ALLOW
      derivedRoles: ["kb_writer", "kb_admin"]
      condition:
        match:
          expr: >
            request.resource.attr.retired == false &&
            request.resource.attr.status != "reindexing"

    - actions: ["kb:manage"]
      effect: EFFECT_ALLOW
      derivedRoles: ["kb_admin"]

    - actions: ["kb:grant"]
      effect: EFFECT_ALLOW
      roles: ["system_admin"]
```

#### cerbos/policies/principal_policies/admin.yaml

```yaml
apiVersion: api.cerbos.dev/v1
principalPolicy:
  version: "default"
  principal: "system_admin"

  rules:
    - resource: "*"
      actions: ["*"]
      effect: EFFECT_ALLOW
```

#### cerbos/schemas/principal.json

```json
{
  "$schema": "https://cerbos.dev/cerbos-policy/v1/jsonschema/principal.json",
  "type": "object",
  "properties": {
    "tenant_id": { "type": "string" },
    "granted_actions": {
      "type": "object",
      "additionalProperties": {
        "type": "array",
        "items": { "type": "string" }
      }
    }
  },
  "required": ["tenant_id"]
}
```

### 3.8 P-AUTHC 调用 Cerbos 的 Principal 组装

```python
# src/permission/cerbos_client.py
from cerbos.sdk.client import CerbosClient
from cerbos.sdk.model import Principal, Resource, ResourceList

def build_principal(user) -> Principal:
    return Principal(
        id=f"user:{user.id}",
        roles=user.roles,       # ["user"] 或 ["system_admin"]
        attr={
            "tenant_id": user.tenant_id,
            # granted_actions 由本系统数据库管理，每次请求时从 DB 取出传入
            # 格式：{"kb-uuid-001": ["read", "write"], "kb-uuid-002": ["read"]}
            "granted_actions": user.granted_actions,
        }
    )
```

### 3.9 Makefile（本地开发快捷命令）

```makefile
.PHONY: infra infra-down dev-api dev-ingest dev-retrieve dev-stamp dev-relay db-init db-seed

# ── 基础设施 ──────────────────────────────────────────────────────────

# 启动基础设施（首次或重启后执行）
infra:
	docker-compose -f docker-compose.infra.yml up -d

# 停止基础设施（保留数据）
infra-down:
	docker-compose -f docker-compose.infra.yml down

# 完全重置（删除所有数据，慎用）
infra-reset:
	docker-compose -f docker-compose.infra.yml down -v

# ── 数据库初始化 ──────────────────────────────────────────────────────

# 初始化数据库表（首次启动后执行一次）
db-init:
	python -m src.scripts.init_db

# 写入开发期测试数据（admin / reader / writer）
db-seed:
	python -m src.scripts.seed_dev

# ── 本地进程（开发期直接跑，不走 Docker） ─────────────────────────────

# FastAPI（8000 端口，--reload 代码改动自动重载）
dev-api:
	uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload

# 摄入 worker（消费 ingestion_queue）
dev-ingest:
	celery -A src.platform.task.celery_app worker \
		-Q ingestion_queue --concurrency=2 \
		-n ingestion-worker@%%h --loglevel=info

# 检索 worker（消费 retrieval_queue）
dev-retrieve:
	celery -A src.platform.task.celery_app worker \
		-Q retrieval_queue --concurrency=2 \
		-n retrieval-worker@%%h --loglevel=info

# 盖戳 worker（消费 stamping_queue）
dev-stamp:
	celery -A src.platform.task.celery_app worker \
		-Q stamping_queue --concurrency=2 \
		-n stamping-worker@%%h --loglevel=info

# outbox relay
dev-relay:
	python -m src.platform.task.outbox_relay

# ── 上线部署 ─────────────────────────────────────────────────────────

# 启动全栈（infra + app，上线使用）
deploy:
	docker-compose -f docker-compose.infra.yml -f docker-compose.app.yml up -d

# 只重启计算层（上线后更新代码用）
deploy-restart-app:
	docker-compose -f docker-compose.app.yml restart
```

### 3.10 启动步骤（开发期）

```bash
# 1. 复制并填写环境变量
cp .env.example .env
# 必须填写：
#   OTEL_COLLECTOR_ENDPOINT  ← 统一可观测平台地址
#   JWT_PUBLIC_KEY_URL       ← IdP 公钥地址
# 默认值可用，无需修改：
#   POSTGRES_PASSWORD / REDIS_PASSWORD / MINIO_ACCESS_KEY 等

# 2. 启动基础设施
make infra
# 等待所有服务 healthy（约 30-60 秒，主要等 Milvus）
docker-compose -f docker-compose.infra.yml ps

# 3. 初始化数据库（首次启动执行一次）
make db-init

# 4. 写入开发期测试数据
make db-seed

# 5. 验证 Cerbos 策略加载
curl http://localhost:3592/_cerbos/health

# 6. 预热嵌入模型（BGE-M3 首次下载约 2GB，仅首次需要）
python -m src.scripts.warmup_models

# 7. 开多个终端分别启动各进程
make dev-api        # 终端 1：FastAPI
make dev-ingest     # 终端 2：摄入 worker
make dev-retrieve   # 终端 3：检索 worker
make dev-stamp      # 终端 4：盖戳 worker
make dev-relay      # 终端 5：outbox relay
```

> **提示**：日常开发只需要开发哪个模块就启动对应进程，不需要全部跑起来。比如只开发文档上传流程，启动 `dev-api` 和 `dev-ingest` 就够了。

### 3.11 常用开发命令

```bash
# 查看基础设施状态
docker-compose -f docker-compose.infra.yml ps

# 查看基础设施日志
docker-compose -f docker-compose.infra.yml logs -f cerbos

# 手动测试 Cerbos 权限判定
curl -s http://localhost:3592/api/check/resources \
  -H "Content-Type: application/json" \
  -d '{
    "requestId": "test-001",
    "principal": {
      "id": "user:usr-reader-001",
      "roles": ["user"],
      "attr": {"tenant_id": "tenant-dev", "granted_actions": {"kb-001": ["read"]}}
    },
    "resources": [{
      "actions": ["doc:view"],
      "resource": {
        "kind": "document", "id": "doc-001",
        "attr": {"is_enabled": true, "retired": false, "allow_download": false}
      }
    }]
  }' | python3 -m json.tool

# 运行契约测试
pytest tests/contract/ -v

# 重置 Milvus 中所有向量数据（慎用）
python -m src.scripts.reset_milvus

# 完全重置开发环境（删除所有数据）
make infra-reset
```

## 四、阶段一：单条链路跑通

### 4.1 目标与完成标准

**目标**：一次上传 + 一次查询能端到端跑通，权限是真实三道校验，不是 mock。

**完成标准**（验收 checklist）：

```
[ ] 上传 txt/md 文件 → 写入 Milvus，vis_version 有值（盖戳已完成）
[ ] 发查询 → 返回有意义的答案 + 来源引用（chunk_id 列表）
[ ] clearance=1 的用户查询 classification=3 的文档，返回"未找到足够信息"
[ ] KB-A 的文档不出现在 KB-B 的查询结果里
[ ] 端到端 P50 < 10s（CPU 模式，含 LLM 生成）
[ ] 权限服务不可达时，查询返回 503（而不是 500 或空结果）
[ ] 盖戳失败时，chunk 的 vis_version 保持 null（不落空戳记）
```

### 4.2 阶段一实现范围

#### 平台模块

**P-AUTHC（完整实现，后续不动）**：
- JWT 本地校签 → `build_context`（含 credential 原样保管）
- `check`（单条判定，调 `/v1/check`）
- `get_prefilter` → `compile_filter` → `MetadataFilter`（六条件编译）
- `filter_items`（接口有，strict 默认 false，阶段二开启时不加新接口）
- `mint_ctx_token`（调 `/v1/context`）
- `register_resource` / `link_resource` / `unlink_resource` / `retire_resource`
- `VisibilityChanged` 事件订阅与转交 B-INGEST
- 三态映射与四类 fail-closed（全部实现，包括 indeterminate 的独立告警）

**P-TASK（完整实现）**：
- `run_pipeline_async`（Celery 包装，三类队列）
- `subscribe_stream`（Redis Pub/Sub 流式回传）
- `outbox_relay`（B-DOC outbox 搬运）

**P-MODEL（简化实现，接口已定）**：
- `invoke_embedding`：BGE-M3 稠密 + 稀疏
- `invoke_llm`：Ollama / Qwen2.5（开发期）
- `invoke_rerank`：**no-op**，直接返回输入列表（阶段二升级）
- `resolve_prompt`：hardcode 初始模板，接口签名已定

**P-CONFIG（简化实现，接口已定）**：
- `resolve_retrieval_config`：kb → tenant 两层级联（turn/conversation 层接口有，先返回 None）
- `resolve_chunking_config`：word / sentence 两种 strategy

**P-OBS**：
- structlog 日志
- Haystack 内置 OTel tracing（自动，无需额外代码）
- 通过 `OTEL_EXPORTER_OTLP_ENDPOINT` 上报到统一可观测平台的 OTel Collector
- 无需自建任何可观测组件

**P-AUDIT（简化实现，接口已定）**：
- `emit_audit_event`：**structlog 记日志，不落 `audit_log` 表**（阶段二升级为落库）
- `emit_audit_event_txn`：接口有，实现与 `emit_audit_event` 相同（阶段三升级为同事务）

**P-STORE**：
- SeaweedFS S3 网关，`put` / `get` / `generate_presigned_url` / `delete` 完整实现

#### 业务模块

**B-DOC（简化实现）**：
- `submit_ingest_task`：登记 + `register` + `link` + 同步触发解析（`auto_parse=true`）
- `trigger_parse`：发 `DocumentMounted` 事件
- `delete_document_from_kb`（purge=false）：`unlink` + 解除挂载
- 目录管理：接口签名有，返回 `501 Not Implemented`

**B-INGEST（完整实现）**：

Haystack 摄入 Pipeline（`pipelines/ingest_v1.yaml`）：
```
DocumentSplitter（word/sentence）
    → SentenceTransformersDocumentEmbedder（BGE-M3 稠密）
    → BGE-M3SparseEmbedder（稀疏，自定义 @component）
    → PermissionMetadataEnricher（注入权限字段，allow_stamps=[], vis_version=null）
    → MilvusDocumentStore（写向量库）
```

盖戳管道（`stamp_channel_task`，六条纪律全部实现）：
- 失败不落盘、unmounted 清空、版本单调性、分批让渡、断点续跑、审计 fail-open

**B-RETRIEVE（简化实现）**：

Haystack 查询 Pipeline（`pipelines/query_v1.yaml`）：
```
SentenceTransformersTextEmbedder（稠密）
    → MilvusEmbeddingRetriever(filters=MetadataFilter)   ← 六条件注入
BGE-M3SparseTextEmbedder（稀疏）
    → MilvusBM25Retriever(filters=MetadataFilter)        ← 同一 filter 对象
两路 → DocumentJoiner(join_mode=reciprocal_rank_fusion)
    → [SentenceTransformersRanker 占位，no-op]           ← 阶段二填实现
```

- 层 1：六条件 MetadataFilter 注入，两路 filter 对象引用相等
- 层 2：过采样（oversample_factor=1.5），补检索最多 2 轮
- 层 3：接口有，`strict` 默认 false，不调 `/v1/filter`

**B-CHAT（简化实现）**：

Haystack 查询 Pipeline 生成节点（compact 模式）：
```
[检索结果]
    → PromptBuilder（hardcode 模板）
    → LiteLLMGenerator（Ollama 后端）
```

- `conversation` + `conversation_turn` 表按最终形态建
- `resolved_query = user_question`（直接透传，阶段二改写）
- 单轮对话（`bound_kb_ids` 在 conversation 创建时指定）
- 流式回传（Redis Pub/Sub + SSE）

### 4.3 数据库初始化 SQL（按最终形态建表）

```sql
-- ── 知识库 ──────────────────────────────────────────────────────────
CREATE TABLE knowledge_bases (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   VARCHAR(64) NOT NULL,
    name        VARCHAR(128) NOT NULL,
    description TEXT DEFAULT '',
    owner_id    VARCHAR(64) NOT NULL,
    status      VARCHAR(16) DEFAULT 'active',  -- active / reindexing
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (tenant_id, name)
);

-- ── 文档 ────────────────────────────────────────────────────────────
CREATE TABLE documents (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           VARCHAR(64) NOT NULL,
    filename            VARCHAR(256) NOT NULL,
    content_fingerprint VARCHAR(64) NOT NULL,    -- SHA-256，阶段三用于 MD5 幂等去重
    storage_path        VARCHAR(512) NOT NULL,
    file_size           BIGINT DEFAULT 0,
    mime_type           VARCHAR(64) DEFAULT '',
    uploaded_by         VARCHAR(64) NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT now(),
    UNIQUE (tenant_id, content_fingerprint)
);

-- ── 挂载关系 ─────────────────────────────────────────────────────────
CREATE TABLE document_kb_mounts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id),
    kb_id       UUID NOT NULL REFERENCES knowledge_bases(id),
    is_enabled  BOOLEAN DEFAULT true,
    mounted_by  VARCHAR(64) NOT NULL,
    mounted_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (document_id, kb_id)
);

-- ── 摄入执行状态 ─────────────────────────────────────────────────────
CREATE TABLE ingest_executions (
    mount_id                UUID PRIMARY KEY REFERENCES document_kb_mounts(id),
    document_id             UUID NOT NULL,
    kb_id                   UUID NOT NULL,
    parse_status            VARCHAR(16) DEFAULT 'not_parsed',
    -- not_parsed / queued / processing / completed / failed / cancelling / removed
    chunking_config_version VARCHAR(32) DEFAULT 'v1',
    pipeline_yaml_version   VARCHAR(32) DEFAULT 'v1',    -- 阶段二开始真正用
    execution_epoch         INTEGER DEFAULT 1,           -- 阶段三开始真正用（栅栏令牌）
    failure_reason          TEXT DEFAULT '',
    retry_count             INTEGER DEFAULT 0,
    updated_at              TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_ingest_executions_kb_status ON ingest_executions(kb_id, parse_status);

-- ── 对话 ─────────────────────────────────────────────────────────────
CREATE TABLE conversations (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     VARCHAR(64) NOT NULL,
    user_id       VARCHAR(64) NOT NULL,
    bound_kb_ids  UUID[] NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ DEFAULT now()
);

-- ── 对话轮次 ─────────────────────────────────────────────────────────
CREATE TABLE conversation_turns (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id          UUID NOT NULL REFERENCES conversations(id),
    turn_index               INTEGER NOT NULL,
    user_question            TEXT NOT NULL,
    resolved_query           TEXT NOT NULL,      -- 阶段一 = user_question，阶段二改写
    retrieval_params_snapshot JSONB,
    retrieved_chunk_ids      TEXT[] DEFAULT '{}',
    trace_id                 VARCHAR(64) DEFAULT '',
    authz_decision_ref       VARCHAR(64) DEFAULT '',  -- 阶段二审计补全
    pipeline_yaml_version    VARCHAR(32) DEFAULT 'v1',
    created_at               TIMESTAMPTZ DEFAULT now(),
    UNIQUE (conversation_id, turn_index)
);

-- ── 目录 ─────────────────────────────────────────────────────────────
CREATE TABLE directories (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        VARCHAR(64) NOT NULL,
    name             VARCHAR(128) NOT NULL,
    parent_id        UUID REFERENCES directories(id),
    directory_type   VARCHAR(16) DEFAULT 'manual',  -- kb_bound / manual
    bound_kb_id      UUID UNIQUE REFERENCES knowledge_bases(id),
    created_by       VARCHAR(64) NOT NULL,
    created_at       TIMESTAMPTZ DEFAULT now()
);

-- ── Outbox（B-DOC 分区） ─────────────────────────────────────────────
CREATE TABLE outbox (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type           VARCHAR(64) NOT NULL,
    payload              JSONB NOT NULL,
    tenant_id            VARCHAR(64) NOT NULL,
    trace_id             VARCHAR(64) DEFAULT '',
    status               VARCHAR(16) DEFAULT 'pending',  -- pending / sent
    created_at           TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_outbox_pending ON outbox(status, created_at) WHERE status = 'pending';

-- ── 审计日志（阶段一建表，阶段二开始落数据） ──────────────────────────
CREATE TABLE audit_logs (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type         VARCHAR(64) NOT NULL,
    request_id         VARCHAR(64) NOT NULL,
    user_id            VARCHAR(64),
    tenant_id          VARCHAR(64),
    client_ip          VARCHAR(64),
    is_service_account BOOLEAN DEFAULT false,
    action             VARCHAR(64),
    resource_type      VARCHAR(32),
    resource_id        VARCHAR(128),
    allowed            BOOLEAN,
    authz_decision_ref VARCHAR(64),     -- 跨系统取证主键
    risk_level         VARCHAR(16) DEFAULT 'normal',
    payload            JSONB,
    created_at         TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_audit_tenant_time ON audit_logs(tenant_id, created_at DESC);
CREATE INDEX idx_audit_request    ON audit_logs(request_id);
CREATE INDEX idx_audit_decision   ON audit_logs(authz_decision_ref)
    WHERE authz_decision_ref IS NOT NULL;

-- ── P-CONFIG：检索参数（阶段一用默认值，阶段二开始级联生效） ────────────
CREATE TABLE retrieval_configs (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_type        VARCHAR(16) NOT NULL,   -- tenant / kb / conversation / turn
    scope_id          VARCHAR(128) NOT NULL,
    top_k             INTEGER DEFAULT 10,
    retrieval_mode    VARCHAR(16) DEFAULT 'hybrid',
    fusion_method     VARCHAR(16) DEFAULT 'rrf',
    synthesis_mode    VARCHAR(32) DEFAULT 'compact',
    strict            BOOLEAN DEFAULT false,
    oversample_factor FLOAT DEFAULT 1.5,
    min_results       INTEGER DEFAULT 3,
    refetch_max_rounds INTEGER DEFAULT 2,
    haystack_pipeline_name VARCHAR(64) DEFAULT 'query_v1',
    UNIQUE (scope_type, scope_id)
);

-- ── P-CONFIG：切分配置 ───────────────────────────────────────────────
CREATE TABLE chunking_configs (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kb_id                UUID NOT NULL REFERENCES knowledge_bases(id),
    version              VARCHAR(32) NOT NULL,
    haystack_strategy    VARCHAR(32) DEFAULT 'sentence',
    split_length         INTEGER DEFAULT 256,
    split_overlap        INTEGER DEFAULT 32,
    language             VARCHAR(16) DEFAULT 'zh',
    pipeline_yaml_version VARCHAR(32) DEFAULT 'v1',
    created_at           TIMESTAMPTZ DEFAULT now(),
    UNIQUE (kb_id, version)
);
```

### 4.4 Haystack Pipeline YAML（阶段一）

**pipelines/ingest_v1.yaml**：
```yaml
version: ignore
components:
  - name: splitter
    type: haystack.components.preprocessors.DocumentSplitter
    init_parameters:
      split_by: sentence
      split_length: 256
      split_overlap: 32

  - name: dense_embedder
    type: haystack_integrations.components.embedders.fastembed.FastembedDocumentEmbedder
    init_parameters:
      model: BAAI/bge-m3

  - name: sparse_embedder
    type: src.ingest.components.BGE-M3SparseEmbedder  # 自定义 @component
    init_parameters: {}

  - name: perm_enricher
    type: src.ingest.components.PermissionMetadataEnricher  # 自定义 @component
    init_parameters: {}

  - name: writer
    type: haystack_integrations.document_stores.milvus.MilvusDocumentStore
    init_parameters:
      connection_args:
        uri: "http://${MILVUS_HOST}:${MILVUS_PORT}"

connections:
  - sender: splitter.documents
    receiver: dense_embedder.documents
  - sender: dense_embedder.documents
    receiver: sparse_embedder.documents
  - sender: sparse_embedder.documents
    receiver: perm_enricher.documents
  - sender: perm_enricher.documents
    receiver: writer.documents
```

**pipelines/query_v1.yaml**：
```yaml
version: ignore
components:
  - name: dense_text_embedder
    type: haystack_integrations.components.embedders.fastembed.FastembedTextEmbedder
    init_parameters:
      model: BAAI/bge-m3

  - name: sparse_text_embedder
    type: src.retrieve.components.BGE-M3SparseTextEmbedder
    init_parameters: {}

  - name: dense_retriever
    type: haystack_integrations.components.retrievers.milvus.MilvusEmbeddingRetriever
    init_parameters:
      top_k: 20

  - name: sparse_retriever
    type: haystack_integrations.components.retrievers.milvus.MilvusBM25Retriever
    init_parameters:
      top_k: 20

  - name: joiner
    type: haystack.components.joiners.DocumentJoiner
    init_parameters:
      join_mode: reciprocal_rank_fusion

  - name: prompt_builder
    type: haystack.components.builders.PromptBuilder
    init_parameters:
      template: |
        你是企业知识库助手，请基于以下文档内容回答问题。
        如文档中没有相关信息，请如实说明，不要编造。
        {% for doc in documents %}
        [来源 {{ loop.index }}] {{ doc.content }}
        {% endfor %}
        问题：{{ query }}

  - name: generator
    type: haystack.components.generators.openai.OpenAIGenerator
    init_parameters:
      api_base_url: "${OLLAMA_BASE_URL}/v1"
      model: "${OLLAMA_MODEL}"
      api_key: "ollama"    # Ollama 不校验 api_key，此处为占位值，随便填非空字符串

connections:
  - sender: dense_text_embedder.embedding
    receiver: dense_retriever.query_embedding
  - sender: sparse_text_embedder.sparse_embedding
    receiver: sparse_retriever.query_sparse_embedding
  - sender: dense_retriever.documents
    receiver: joiner.documents
  - sender: sparse_retriever.documents
    receiver: joiner.documents
  - sender: joiner.documents
    receiver: prompt_builder.documents
  - sender: prompt_builder.prompt
    receiver: generator.prompt
```

### 4.5 阶段一不做、接口已占位的内容

```
rerank              → invoke_rerank 接口有，返回输入列表（no-op）
层 3 strict 复核    → filter_items 接口有，strict 默认 false
多轮对话改写        → resolved_query = user_question
目录管理            → 接口签名有，返回 501
批量操作 check_batch → 串行调 N 次 check（正确但慢）
P-AUDIT 落库        → emit_audit_event 记日志，不落 audit_logs 表
purge=true 删除     → delete_document_from_kb(purge=true) 返回 501
```


---

## 五、阶段二：检索质量加固

### 5.1 目标与完成标准

**目标**：检索质量达到可业务验证的水平，填实阶段一占位的接口。

**完成标准**：
```
[ ] Context Recall ≥ 0.70（RAGAS，首批评测集，每 KB ≥ 50 条三元组）
[ ] Faithfulness ≥ 0.75
[ ] strict=true 的 KB 撤权后 10s 内生效（层 3 实时复核）
[ ] 多轮追问"那第二条呢"，resolved_query 改写后能检索到正确结果
[ ] Langfuse 可以看到每次查询的完整链路（含 chunk 命中、模型调用）
```

### 5.2 新增内容（不修改阶段一的任何接口或表结构）

**填实 no-op 接口**：

| 阶段一占位 | 阶段二实现 | 改动范围 |
|---------|---------|---------|
| `invoke_rerank`（no-op） | 接入 `SentenceTransformersRanker`（BGE Reranker v2） | P-MODEL 内部实现，接口签名不变 |
| `filter_items`（no-op） | 真正调 `/v1/filter`，`strict` 可配 | B-RETRIEVE 内部逻辑，接口不变 |
| `emit_audit_event`（记日志） | 落 `audit_logs` 表 | P-AUDIT 内部实现，接口不变 |
| `resolved_query = user_question` | 真正做多轮查询改写（LLM 改写） | B-CHAT 内部逻辑，表结构不变 |
| `check_batch`（串行 N 次） | 走 `/v1/check/batch` 单次往返 | P-AUTHC 内部实现，接口不变 |

**新增功能（新接口，新表）**：

```
目录管理（B-DOC）：
  create_directory / list_directory / delete_directory
  实现 document_directory_entry 的增删查

登记与解析解耦（B-DOC）：
  submit_ingest_task 支持 auto_parse=false（默认改为 false）
  trigger_parse 作为独立接口对外暴露

惰性解析策略（P-CONFIG）：
  chunking_configs 表支持 semantic 和 hierarchical strategy
  Haystack SemanticDocumentSplitter 接入

P-MODEL 升级：
  model_registry 表真正写入数据（不再 hardcode）
  prompt_template 版本化（不再 hardcode 模板）
  Langfuse 接入（LiteLLM callback）

P-CONFIG 级联补全：
  resolve_retrieval_config 补上 turn / conversation 层

质量体系（新增）：
  首批评测集（每 KB 50 条问答三元组，人工构造）
  RAGAS 基线评估脚本
  评测集按 KB 独立维护，权限导致的空结果单独归类
```

**Haystack Query Pipeline 升级**（更新 `pipelines/query_v2.yaml`，不修改 v1）：

```
[原有节点不变]
    → DocumentJoiner(RRF)
    → SentenceTransformersRanker(model=BAAI/bge-reranker-v2-m3-japanese)  ← 新增
    → PromptBuilder（从 P-MODEL.resolve_prompt 取，不再 hardcode）
    → LiteLLMGenerator
```

---

## 六、阶段三：工程加固

### 6.1 目标与完成标准

**目标**：系统具备持续运行能力，不怕重启、不怕并发、不怕权限变更。

**完成标准**：
```
[ ] 摄入失败重启后断点续传，不重复写入 Milvus chunk
[ ] 文档删除（purge=true）后 Milvus chunk 30s 内清理完毕
[ ] strict 库撤权后即时生效，非 strict 库 5 分钟内自愈（戳记对账）
[ ] 镜像对账定时任务跑起来，mirror_gap 告警能触发
[ ] 权限服务宕机超过阈值，熔断器打开，查询统一返回 503 降级文案
[ ] execution_epoch 栅栏：重提交后旧僵尸任务不会覆盖新结果
```

### 6.2 新增内容（不修改前两阶段的接口或表结构）

**摄入幂等（execution_epoch 真正生效）**：

```python
# ingest_document_task 在每个关键写点前重读 epoch
def should_abort(mount_id, current_epoch) -> bool:
    record = db.get(mount_id)
    return (record.execution_epoch != current_epoch or
            record.parse_status == 'cancelling')
```

阶段一 `execution_epoch` 固定为 1 从未被用；阶段三真正用上——每次重提交 +1，旧任务检测到 epoch 不符自动退出。

**彻底删除（purge=true）**：

```
delete_document_from_kb(purge=true):
  → 删各挂载 → 发 DocumentUnmounted（每个）
  → 计数为 0 → retire_resource(doc, document_id)
    （权限服务原子完成：回收 acl + restriction + 解挂 + retired）
  → P-STORE.delete 物理文件 → 删 document 记录
  → 同事务写 DOC_DELETE + AUTHZ_WRITE 审计（emit_audit_event_txn）
```

**P-AUDIT 高风险同步写入**：

```
emit_audit_event_txn 升级：
  DOC_DELETE / AUTHZ_WRITE → 与业务事务同库同事务提交
  写失败 → 业务回滚（阶段一是 fire-and-forget，阶段三收紧为同事务）
```

**跨系统对账（新增定时任务）**：

```
结构镜像对账（§13.7b，每小时）：
  对 document_kb_mounts 表抽样，调 /v1/check 验 unknown_resource
  发现缺口 → 补调 link，递增 mirror_gap，告警

戳记对账（§14.5c）：
  strict 库：每 15 分钟全量
  普通库：每小时 10% 抽样
  发现 vis_version=null → 补提交 stamp_channel_task，递增 orphan_stamp
  发现 vis_version 落后 → 重新盖戳，递增 stamp_drift
```

**熔断降级（§25.3）**：

```
P-AUTHC 熔断器（circuitbreaker 库）：
  - 阈值：30s 内失败率 > 50%（或连续失败 10 次）→ 熔断打开
  - 半开探测：每 60s 放行一次探测请求
  - 熔断打开时：所有权限调用直接返回 503 auth:authz_unavailable
  - 降级文案（B-CHAT）："服务暂时不可用，请稍后重试"
    （与"未找到足够信息"在 HTTP status 和 error_code 上可区分）
```

**P-OBS Metric 补全**：

```python
# 以下指标从阶段三开始真正采集
authz_decision_total{endpoint, decision}    # allow/deny/indeterminate 三态分开
authz_call_failed_total{endpoint, kind}
stamp_drift                                  # Gauge
orphan_stamp                                 # Gauge
mirror_gap                                   # Gauge
filtered_rate{layer}                         # layer1 / layer3
```

**生成层引用校验**：

```python
# B-CHAT：LLM 生成完成后，验证引用的 chunk_id 在本次候选集内
def validate_citations(answer: str, candidate_chunk_ids: set[str]) -> str:
    # 提取答案中的 chunk_id 引用，过滤掉不在候选集的幻觉引用
    ...
```

---

## 七、阶段四：生产就绪

### 7.1 目标与完成标准

**目标**：具备上生产的条件，可承载真实业务流量。

**完成标准**：
```
[ ] 查询 P99 < 8s（分布式环境，含 LLM 生成）
[ ] Milvus 单节点故障服务不中断（分布式集群）
[ ] CI 质量门禁：Context Recall 下降 > 5% 阻断合并
[ ] 联合契约测试 J-12 至 J-20 进 CI（每日构建）
[ ] 通过安全渗透测试（无高危漏洞）
[ ] BGE-M3 INT8 量化上线（显存减 50%，吞吐提升 ~40%）
```

### 7.2 新增内容（不修改前三阶段的接口或表结构）

**基础设施升级**（配置变更，代码不变）：

```yaml
# docker-compose 升级为生产配置
Milvus:      standalone → 分布式集群
             Proxy×2 + QueryNode×3 + DataNode×2 + IndexNode×2
PostgreSQL:  单节点 → 主从（PGBouncer 连接池）
Redis:       单节点 → Sentinel（3 节点）
SeaweedFS:   单节点 → 多节点（S3 纠删码）
```

**BGE-M3 量化**：

```
SentenceTransformersDocumentEmbedder 和 SentenceTransformersTextEmbedder
改为加载 INT8 量化版本（fastembed 支持）
Haystack Pipeline YAML 更新模型路径，pipeline_yaml_version 递增
```

**CI/CD 质量门禁**：

```yaml
# .github/workflows/quality-gate.yml
- name: Run RAGAS evaluation
  run: |
    python scripts/eval_ragas.py \
      --eval-set tests/eval_sets/ \
      --baseline metrics/baseline.json \
      --threshold-recall 0.70 \
      --threshold-faithfulness 0.75

- name: Fail on regression
  if: steps.eval.outputs.regression == 'true'
  run: exit 1
```

**联合契约测试进 CI**（每日构建）：

```
J-12 至 J-20 从"人工联调"升级为"每日自动跑"
需要权限服务提供一个稳定的联调环境（非生产）
任一项失败阻断当天发布
```

**生成层复述守卫**（§16.4，若面向外部客户）：

```python
# B-CHAT：检测 LLM 是否逐字复述超过 chunk 原文 60%
def check_verbatim_ratio(answer: str, chunk_content: str) -> float:
    # 最长公共子串 / chunk 长度
    ...
# 超限 → 截断并追加引用提示
```

---

## 八、各阶段能力对照表

| 能力 | 阶段一 | 阶段二 | 阶段三 | 阶段四 |
|-----|:-----:|:-----:|:-----:|:-----:|
| 上传文档 + 检索返回答案 | ✅ | ✅ | ✅ | ✅ |
| P-AUTHC 权限三道校验 | ✅ 完整 | ✅ | ✅ | ✅ |
| 盖戳管道（六条纪律） | ✅ 完整 | ✅ | ✅ | ✅ |
| 写路径结构镜像维护 | ✅ 完整 | ✅ | ✅ | ✅ |
| 存在性三通道纪律 | ✅ 完整 | ✅ | ✅ | ✅ |
| 多 KB 隔离 | ✅ | ✅ | ✅ | ✅ |
| rerank 精排 | ⬜ no-op | ✅ | ✅ | ✅ BGE-M3 INT8 |
| 层 3 strict 逐条复核 | ⬜ 接口占位 | ✅ | ✅ | ✅ |
| 多轮对话查询改写 | ⬜ 透传 | ✅ | ✅ | ✅ |
| P-AUDIT 落库 | ⬜ 记日志 | ✅ | ✅ 同事务 | ✅ |
| 目录管理 | ⬜ 501 | ✅ | ✅ | ✅ |
| 批量操作 check_batch | ⬜ 串行 | ✅ 批量端点 | ✅ | ✅ |
| Langfuse 模型观测 | ❌ | ✅ | ✅ | ✅ |
| RAGAS 质量评测 | ❌ | ✅ 手动 | ✅ | ✅ CI 自动 |
| 摄入幂等 / epoch 栅栏 | ⬜ 字段在 | ⬜ 字段在 | ✅ | ✅ |
| 彻底删除（purge=true） | ⬜ 501 | ⬜ 501 | ✅ | ✅ |
| 跨系统对账 | ❌ | ❌ | ✅ | ✅ |
| 熔断降级（§25.3） | ❌ | ❌ | ✅ | ✅ |
| 生成层引用校验 | ❌ | ❌ | ✅ | ✅ |
| authz Metric 三态分标签 | ❌ | ❌ | ✅ | ✅ |
| Milvus 分布式集群 | ❌ | ❌ | ❌ | ✅ |
| CI 质量门禁（RAGAS） | ❌ | ❌ | ❌ | ✅ |
| 联合契约测试进 CI | ❌ | ❌ | ❌ | ✅ |
| BGE-M3 INT8 量化 | ❌ | ❌ | ❌ | ✅ |
| 生成层复述守卫 | ❌ | ❌ | ❌ | ✅（可选） |
| 安全渗透测试 | ❌ | ❌ | ❌ | ✅ |

**图例**：✅ 完整实现 / ⬜ 接口占位或简化实现 / ❌ 未做

---

## 附录：开口清单（开工前需关闭）

| 状态 | 开口 | 确认方式 |
|-----|-----|---------|
| ✅ 已定 | J-7 事件聚合（KB 粒度），展开逻辑在 §14.5.4 | 联调验证 payload 是否含 doc_ids |
| ✅ 已定 | J-14 check/batch 开放，单批 ≤200 | 联调验证批量上限 |
| ✅ 已定 | J-15 prefilter 接受 ctx_token | **🔴 audience 值需在开工前确认** |
| ✅ 已定 | J-19 限流返回 429 + Retry-After | 联调验证实际返回码 |
| 🟡 待选 | 结构镜像对账方案（暂按方案 b 实现） | 阶段三开工前选定 |
| 🟠 待会签 | 审计日志保留期（建议 180 天） | 上线前与权限服务书面确认 |
| 🟠 待会签 | 熔断降级触发条件 + 恢复判据 | 阶段三实现前会签 |
| 🟠 待会签 | 灾后恢复触发全量对账的预案 | 阶段四上线前写进灾备预案 |
| 🟡 待产品 | 复述守卫严格程度（60% 阈值） | 面向外部客户时产品裁定 |
| 🟡 待产品 | 授权 UI 入口形态（跳管理台 vs 扩准入） | 阶段二 UI 设计前产品裁定 |

