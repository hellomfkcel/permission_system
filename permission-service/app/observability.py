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


def instrument_fastapi(app):
    """对 FastAPI 应用进行自动埋点。

    自动为每个 HTTP 请求创建 span，记录 method/path/status_code。
    必须在 app 首次接收请求 scope 之前调用（参阅 RAG 系统 main.py 注释）。
    """
    FastAPIInstrumentor.instrument_app(app)


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
