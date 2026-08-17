# 可观测性审计报告 v20 —— trace 全链路与 trace↔Loki 互跳

> 日期：2026-08-17　环境：真实全栈（PG + Redis + Cerbos + FastAPI），穷举探测实际产出的
> span 与日志。目标：trace_id 能否跟踪完整调用链？trace 与 Loki 能否互跳、看到完整日志便于排障？
> 结论：埋点框架已就位，但**日志不带 trace_id、request_id≠trace_id、非 JSON、DB/Redis 未埋点**，
> 导致互跳与全链路排障不成立。已在代码层修复并真实验证。

## 一、现状（已就位的部分）

- OpenTelemetry：`init_tracing` 建 TracerProvider + OTLP/HTTP 导出到 Collector(4318)→Tempo→Grafana。
- FastAPI 自动埋点（每个 HTTP 请求一个 span）；Cerbos 调用有手动子 span `cerbos.check_resources`。
- Metrics（`metrics_collector` + `/metrics`）、Langfuse（模型观测，fail-open）。
- 设计约定（RAG系统设计v14 §）：**request_id == trace_id 全链路复用**，使 Grafana/Loki/Langfuse/
  审计日志四方互跳。

## 二、穷举探测发现的缺口

真实发一条带 `traceparent` + `X-Request-Id` 的请求，观察实际日志：修复前该请求只产出一行
uvicorn 明文访问日志 `INFO: ... "GET ..." 200 OK` —— 不含 trace_id、不含 request_id、非结构化。

| 缺口 | 现象 | 后果 |
| --- | --- | --- |
| **OBS-1 日志无 trace 关联** | structlog 处理器链无任何注入 OTel `trace_id`/`span_id` 的步骤 | **trace↔Loki 无法互跳**：Tempo 一条 trace 找不到对应 Loki 日志，反向也不行 |
| **OBS-2 request_id≠trace_id** | `X-Request-Id` 与 OTel trace_id 各走各的，未对齐 | 违背设计"request_id=trace_id 四方互跳"，跨系统对不上号 |
| **OBS-3 非 JSON 日志** | `ConsoleRenderer`（带 ANSI 色），业务请求路径几乎不落结构化日志 | Loki/Promtail 无法把 trace_id/project_id 解析成可查字段 |
| **OBS-4 调用链 span 不全** | 只装了 fastapi/asgi instrumentor；DB(asyncpg/SQLAlchemy)、Redis、出站 httpx **未埋点** | trace 里只有 HTTP+Cerbos，看不到 `resolve_granted_actions`/`get_resource_acl`/`check_subject_ban` 的 DB 查询与事件发布，链路不完整 |
| 基础设施 | 仓库内无 observability compose（Collector/Tempo/Loki/Grafana），app compose 也未设 OTEL 端点/日志驱动 | 后端观测栈需外部单独部署（设计本就分栈，属部署事项） |

## 三、修复（代码层，已真实验证）

### 3.1 每条日志注入 trace 关联 + JSON（OBS-1/3）

`app/observability.py` 新增 structlog 处理器 `add_otel_trace_context`：从当前 span 取
`trace_id`(32hex)/`span_id`(16hex) 注入每条日志。`app/main.py` 处理器链改为
`merge_contextvars → add_otel_trace_context → 时间戳/级别 → 渲染`；`LOG_FORMAT=json`
（或 `PRODUCTION=true`）时用 `JSONRenderer`，否则 Console（本地排障）。

### 3.2 request_id == trace_id + 请求级关联中间件（OBS-2）

`app/main.py` 新增 `correlation_middleware`：request_id 取值 `X-Request-Id → trace_id → UUID`，
把 request_id/trace_id/path/method 绑进 structlog contextvars（本请求内**所有**日志自动带上），
落一条结构化 `http_request`（method/path/status/duration_ms/trace_id/request_id），并回写
`X-Request-Id` 响应头。

### 3.3 补全 DB/Redis/出站 span（OBS-4）

`instrument_db_and_cache()` 按需启用 SQLAlchemy/Redis/HTTPX instrumentor（可选依赖，未装静默
跳过，装上即生效），启动生命周期调用；三个 instrumentor 已加入 `requirements.txt`。

### 3.4 真实验证

发带 `traceparent: 00-<TID>-...-01` 的请求：

```
# 访问日志（JSON）
{"status":200,"duration_ms":77.17,"event":"http_request",
 "request_id":"4bf9…4736","method":"GET","path":"/api/v1/roles/definitions",
 "trace_id":"4bf9…4736","span_id":"ce5b…5c45","timestamp":"…","level":"info"}
```

- **上游 trace 延续**：日志 `trace_id` == 传入 `traceparent` 的 trace-id（`4bf9…4736`）→ 分布式链路
  贯通本服务。
- **request_id == trace_id**（设计对齐），响应头回写 `X-Request-Id: 4bf9…4736`。
- **整条请求的日志都带同一 trace_id**：一次策略写入请求的三条日志
  `policy_role_sync_complete` / `policy_file_written` / `http_request` 全部带 `trace_id=bbbb…` ——
  从任一条日志都能顺到整条链路，Grafana 可 Tempo↔Loki 双向跳转。
- **优雅降级**：DB/Redis/httpx instrumentor 未安装时静默跳过，服务照常启动（readyz 200）。
- 回归：离线静态测试 24 项全过；`compileall` 通过。

## 四、部署侧待办（非代码，供运维落地互跳）

1. 安装可选依赖（已在 requirements）：`opentelemetry-instrumentation-{sqlalchemy,redis,httpx}`，
   DB/Redis/出站 span 即自动补全。
2. 部署观测栈（设计为独立 compose）：OTel Collector(4317/4318) → Tempo + Loki + Prometheus →
   Grafana(3000)。permission-service 设 `OTEL_EXPORTER_OTLP_ENDPOINT`、`LOG_FORMAT=json`。
3. 采集：Promtail/agent 收 JSON 容器日志，把 `trace_id` 提为标签/字段。
4. Grafana 关联：Loki data source 配 **derived field** `trace_id` → 跳 Tempo；Tempo **trace-to-logs**
   按 `trace_id` 回跳 Loki。至此 trace↔Loki 双向互跳、全链路日志可查。

## 五、结论

修复前：trace 只覆盖 HTTP+Cerbos，日志不带 trace_id、非 JSON、request_id 与 trace_id 脱节 ——
**trace↔Loki 不能互跳，排障看不到完整链路**。修复后：request_id 对齐 trace_id、每条日志带
trace_id/span_id、JSON 可解析、上游 trace 贯通、整请求日志同 trace 关联，并补齐 DB/Redis/出站
埋点（依赖到位即生效）。代码层互跳能力已具备并验证；剩余为部署侧接入观测栈与 Grafana 关联配置。

修复清单：

| 项 | 文件 | 改动 |
| --- | --- | --- |
| OBS-1/3 | `app/observability.py`,`app/main.py` | `add_otel_trace_context` 处理器 + contextvars + JSON 渲染（env 门控）|
| OBS-2 | `app/main.py` | `correlation_middleware`：request_id=trace_id、绑定 contextvars、结构化访问日志、回写响应头 |
| OBS-4 | `app/observability.py`,`app/main.py`,`requirements.txt` | `instrument_db_and_cache()` 按需埋点 + 加入三个 instrumentor 依赖 |
