"""权限服务 — OpenTelemetry Tracing + Langfuse 模型观测初始化。

OTel Collector 单一出口（HTTP 4318 / gRPC 4317）→ Tempo → Grafana/Tempo 查询。
FastAPI 自动埋点 + Cerbos PDP 调用手动 span。
"""

import os
import logging
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

_log = logging.getLogger(__name__)
_tracing_initialized = False
_langfuse_initialized = False


def init_tracing(service_name: str = "permission-service"):
    """初始化 OpenTelemetry Tracing。

    在应用启动时调用一次。将全部 span 导出到 OTel Collector（HTTP 4318）。
    """
    global _tracing_initialized
    if _tracing_initialized:
        return

    otel_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    if not otel_endpoint:
        _log.warning("OTEL_EXPORTER_OTLP_ENDPOINT not set, tracing disabled")
        _tracing_initialized = True
        return

    resource = Resource.create({SERVICE_NAME: service_name})
    provider = TracerProvider(resource=resource)

    otlp_exporter = OTLPSpanExporter(endpoint=f"{otel_endpoint}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(
        otlp_exporter,
        max_export_batch_size=256,
        schedule_delay_millis=2000,
        max_queue_size=2048,
    ))

    trace.set_tracer_provider(provider)

    # 注册进程退出时的优雅关闭
    _register_shutdown_hook()

    _tracing_initialized = True
    _log.info("otel_tracing_initialized", service_name=service_name, endpoint=otel_endpoint)


def _register_shutdown_hook():
    """注册 atexit 和 SIGTERM 处理器，确保进程退出时 span 被 flush。"""
    import atexit
    import signal as _signal

    def _flush():
        provider = trace.get_tracer_provider()
        if hasattr(provider, "shutdown"):
            try:
                provider.shutdown()
            except Exception:
                pass

    atexit.register(_flush)
    try:
        _signal.signal(_signal.SIGTERM, lambda *_args: _flush())
    except Exception:
        pass


def get_tracer(name: str = "permission-service"):
    """获取 OpenTelemetry Tracer 实例。"""
    return trace.get_tracer(name)


def current_trace_id() -> str | None:
    """当前 span 的 trace_id（32 位 hex），无有效 span 时返回 None。

    供请求中间件把 request_id 对齐到 trace_id（设计：request_id == trace_id），
    实现 Grafana/Tempo ↔ Loki ↔ 审计日志互跳。
    """
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        return format(ctx.trace_id, "032x")
    return None


def add_otel_trace_context(logger, method_name, event_dict):
    """structlog 处理器：把当前 OTel span 的 trace_id / span_id 注入每条日志。

    这是 trace ↔ Loki 互跳的关键 —— 日志带上 trace_id 后，Grafana 才能从 Tempo
    的一条 trace 跳到对应的 Loki 日志，或反向从日志跳回 trace。无有效 span 时不注入。
    """
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def instrument_fastapi(app):
    """对 FastAPI 应用进行自动埋点。

    自动为每个 HTTP 请求创建 span，记录 method/path/status_code。
    必须在 app 首次接收请求 scope 之前调用（参阅 RAG 系统 main.py 注释）。
    """
    FastAPIInstrumentor.instrument_app(app)


def instrument_db_and_cache() -> None:
    """对 DB / Redis / 出站 HTTP 埋点，补全调用链的 span 覆盖。

    FastAPI 自动埋点只给到 HTTP 请求 span，Cerbos 调用有手动 span；但
    resolve_granted_actions / get_resource_acl / check_subject_ban 等落到 Postgres
    的查询、Redis 事件发布、出站 httpx 调用都不在 trace 里，导致排障时看不到
    "请求 → DB → 判定 → 事件" 的完整链路。此处按需启用对应 instrumentor。

    这些 instrumentor 属可选依赖（opentelemetry-instrumentation-{sqlalchemy,redis,
    httpx}），未安装时静默跳过 —— 不阻断启动，装上即自动生效。
    """
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from app.database import engine
        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
        _log.info("otel_sqlalchemy_instrumented")
    except Exception as exc:
        _log.info("otel_sqlalchemy_skipped", reason=str(exc)[:120])

    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        RedisInstrumentor().instrument()
        _log.info("otel_redis_instrumented")
    except Exception as exc:
        _log.info("otel_redis_skipped", reason=str(exc)[:120])

    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
        _log.info("otel_httpx_instrumented")
    except Exception as exc:
        _log.info("otel_httpx_skipped", reason=str(exc)[:120])


# Cerbos 调用的 span 由 services/cerbos_adapter.py 自行创建
# （"cerbos.check_resources"），不在此另设包装函数。


# ── Langfuse 集成 ───────────────────────────────────────────────


def init_langfuse():
    """初始化 Langfuse 模型观测（fail-open）。"""
    global _langfuse_initialized
    if _langfuse_initialized:
        return

    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")

    if not public_key or not secret_key:
        _log.info("langfuse_skipped", reason="keys not set")
        _langfuse_initialized = True
        return

    try:
        import langfuse
        langfuse_host = os.getenv("LANGFUSE_HOST", "http://localhost:13000")
        lc = langfuse.Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=langfuse_host or None,
        )
        lc.start_as_current_observation(as_type="span", name="langfuse-init")
        _log.info("langfuse_initialized", host=langfuse_host, version="v4")
        _langfuse_initialized = True
    except Exception as exc:
        _log.warning("langfuse_init_failed", error=str(exc))
        _langfuse_initialized = True
