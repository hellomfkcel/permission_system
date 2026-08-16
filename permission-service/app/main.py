"""权限服务后端 — FastAPI 入口。

设计依据：docs/外部系统设计.md §2.1 定位 + 实施方案步骤 2.4/11.1/11.2。
"""

import asyncio
import os as _os
import structlog
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.database import check_db
from app.limiter import limiter
from app.client_validator import ClientIdValidationMiddleware

# ── OTel Tracing（必须在 FastAPI app 创建前初始化）──
from app.observability import init_tracing, instrument_fastapi, init_langfuse
init_tracing(_os.getenv("OTEL_SERVICE_NAME", "permission-service"))

# ── 结构化日志 ──

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ],
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)


# ── Keycloak 定时同步后台任务 ──
# 设计依据：docs/外部系统设计.md §4.2 权限服务与 Keycloak 的数据同步
# —— 每 15 分钟定时同步用户/组数据到本地 user_cache 表。

_SYNC_INTERVAL_S = 15 * 60  # 15 分钟
_INITIAL_SYNC_DELAY_S = 30  # 启动后 30 秒首次同步


async def _keycloak_sync_loop(stop_event: asyncio.Event) -> None:
    """Keycloak 用户/组定时同步循环。

    在后台运行，每 15 分钟自动同步一次。
    首次同步在启动后 30 秒执行。
    同步失败不中断循环，仅记录错误日志。
    """
    from idp.keycloak_sync import get_keycloak_sync

    # 首次延迟（等待服务完全就绪）
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=_INITIAL_SYNC_DELAY_S)
        # 如果在等待期间收到停止信号 → 退出
        return
    except asyncio.TimeoutError:
        pass  # 正常超时 → 开始首次同步

    sync_count = 0
    while not stop_event.is_set():
        sync_count += 1
        try:
            sync = get_keycloak_sync()
            result = await sync.sync_users()
            created = result.get("created", 0)
            updated = result.get("updated", 0)
            deleted = result.get("deleted", 0)
            logger.info(
                "keycloak_sync_completed",
                iteration=sync_count,
                created=created,
                updated=updated,
                deleted=deleted,
            )
            # P2-3: Emit Keycloak sync metrics
            from app.metrics_collector import record_keycloak_sync_success
            record_keycloak_sync_success(
                created=created, updated=updated, deleted=deleted,
            )
        except Exception as exc:
            # 同步失败记录告警但继续运行（不因为 Keycloak 不可达而崩溃）
            logger.warning(
                "keycloak_sync_failed",
                iteration=sync_count,
                error=str(exc)[:200],
            )
            # P2-3: Emit Keycloak sync failure metric
            from app.metrics_collector import record_keycloak_sync_failed
            record_keycloak_sync_failed()

        # 等待下次同步间隔（支持提前停止）
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_SYNC_INTERVAL_S)
            return  # 收到停止信号
        except asyncio.TimeoutError:
            pass  # 正常超时 → 继续下一轮同步


async def _bootstrap_default_project() -> None:
    """首次启动时把内置注册表落到一个具体项目上。

    平台以项目为接入单元，而 SERVICE_API_KEY 与内置 client_id / audience 是
    早期单项目形态的产物。此处在 projects 表为空时建一个项目承载它们，
    使既有接入方无需改配置即可继续工作。

    全新部署可设 BOOTSTRAP_PROJECT_ENABLED=false 跳过，改为在管理台手工建项目。
    项目 ID、名称、client_id、audience 均可通过 BOOTSTRAP_* 环境变量覆盖。
    """
    import hashlib
    from sqlalchemy import select, func as sa_func
    from app.database import async_session
    from models.project import (
        Project, ProjectClient, ProjectApiKey, ProjectAudience, ProjectMember,
    )

    if not settings.bootstrap_project_enabled:
        return

    async with async_session() as db:
        count = await db.scalar(select(sa_func.count()).select_from(Project))
        if count and count > 0:
            return

    project_id = settings.bootstrap_project_id
    client_ids = [c.strip() for c in settings.bootstrap_client_ids.split(",") if c.strip()]
    audiences = [a.strip() for a in settings.bootstrap_audiences.split(",") if a.strip()]

    async with async_session() as db:
        try:
            db.add(Project(
                id=project_id,
                name=settings.bootstrap_project_name,
                description="Created by first-start bootstrap",
            ))

            for cid in client_ids:
                db.add(ProjectClient(
                    project_id=project_id, client_id=cid,
                    description="Created by first-start bootstrap",
                ))

            for aud in audiences:
                db.add(ProjectAudience(project_id=project_id, audience=aud))

            if settings.service_api_key:
                db.add(ProjectApiKey(
                    project_id=project_id,
                    key_hash=hashlib.sha256(settings.service_api_key.encode()).hexdigest(),
                    key_prefix=settings.service_api_key[:16],
                    description="Created from SERVICE_API_KEY",
                ))

            if settings.bootstrap_admin_user:
                db.add(ProjectMember(
                    project_id=project_id, user_id=settings.bootstrap_admin_user,
                    role="project_admin", granted_by="bootstrap",
                ))

            await db.commit()
            logger.info(
                "bootstrap_project_created",
                project=project_id,
                clients=client_ids,
                audiences=audiences,
                has_api_key=bool(settings.service_api_key),
            )
        except Exception as exc:
            await db.rollback()
            logger.warning("bootstrap_project_failed", error=str(exc)[:200])


@asynccontextmanager
async def lifespan(application: FastAPI):
    """应用生命周期管理。

    启动时：验证生产安全配置 → 检查数据库连接 → 启动 Keycloak 定时同步。
    关闭时：取消后台任务 → 清理资源。
    """
    # P1-2: 生产安全启动检查
    from app.config import validate_production_secrets
    secret_warnings = validate_production_secrets()
    if secret_warnings:
        for w in secret_warnings:
            logger.warning("security_config_warning", detail=w)

    # P-MODEL: Langfuse 模型观测（fail-open，未配置 key 时跳过）
    init_langfuse()

    await check_db()

    # 首次启动引导：建立承载内置 client_id / audience / API Key 的项目
    await _bootstrap_default_project()

    logger.info("permission_service_starting",
                host=settings.host, port=settings.port)

    # 启动 Keycloak 同步后台任务（设计依据 §4.2）
    _stop_event = asyncio.Event()
    _sync_task = asyncio.create_task(_keycloak_sync_loop(_stop_event))

    yield

    # 关闭：停止后台任务 + 清理 HTTP 客户端连接池
    logger.info("permission_service_shutting_down")
    _stop_event.set()
    _sync_task.cancel()
    try:
        await _sync_task
    except asyncio.CancelledError:
        pass

    # P2 优化：清理 CerbosAdapter + EventPublisher 的 httpx 连接池
    try:
        from services.cerbos_adapter import get_cerbos
        await get_cerbos().close()
        logger.info("cerbos_adapter_closed")
    except Exception:
        pass
    try:
        from services.event_publisher import get_event_publisher
        await get_event_publisher().close()
        logger.info("event_publisher_closed")
    except Exception:
        pass


app = FastAPI(
    title="RAG Permission Service",
    version="1.0.0",
    lifespan=lifespan,
)

# ★ OTel FastAPI 自动埋点（必须在 middleware 注册之前调用，
#   因为 instrumentor 内部调用 add_middleware）
instrument_fastapi(app)

# 限流器状态
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS — 允许管理台前端调用（origins 从配置读取，支持环境变量覆盖）
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in settings.allowed_origins.split(",")
        if origin.strip()
    ],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["Content-Type", "Authorization", "X-Request-Id", "X-Client-Id"],
    allow_credentials=True,
    max_age=3600,
)

# X-Client-Id 准入矩阵强制校验（设计依据 §6A.1）
# 必须在 CORS 之后、路由处理之前执行
app.add_middleware(ClientIdValidationMiddleware)


# ── 健康检查 ──


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    # 权限服务不纳入 /readyz（设计依据：RAG系统设计v14.md §9.4）
    return {"status": "ready"}


# ── 可观测性 ──


@app.get("/metrics")
async def metrics():
    """Prometheus 兼容的指标暴露端点。

    提供关键业务指标：端点调用计数、判定结果分布、事件发布计数。
    设计依据：docs/RAG系统设计v14.md §8.3 Metric 关键指标。
    """
    from sqlalchemy import text
    from app.database import async_session
    from app.metrics_collector import get_prometheus_metrics

    # Prometheus 格式的 in-memory 计数器指标
    prom_lines = get_prometheus_metrics()

    # DB 查询的 gauge 指标
    gauge_lines: list[str] = []
    try:
        async with async_session() as db:
            # ACL 活跃计数
            r = await db.execute(text("SELECT count(*) FROM acl_entries WHERE NOT revoked"))
            acl_total = r.scalar() or 0
            gauge_lines.append("# HELP permission_service_acl_entries_active Active ACL entries")
            gauge_lines.append("# TYPE permission_service_acl_entries_active gauge")
            gauge_lines.append(f"permission_service_acl_entries_active {acl_total}")

            # 资源计数
            r = await db.execute(text("SELECT count(*) FROM resource_registry WHERE NOT retired"))
            gauge_lines.append("# HELP permission_service_resources_active Active registered resources")
            gauge_lines.append("# TYPE permission_service_resources_active gauge")
            gauge_lines.append(f"permission_service_resources_active {r.scalar() or 0}")

            # 活跃封禁
            r = await db.execute(text("SELECT count(*) FROM restrictions WHERE NOT removed"))
            gauge_lines.append("# HELP permission_service_restrictions_active Active restrictions")
            gauge_lines.append("# TYPE permission_service_restrictions_active gauge")
            gauge_lines.append(f"permission_service_restrictions_active {r.scalar() or 0}")

            # 变更事件总数
            r = await db.execute(text("SELECT count(*) FROM permission_changes"))
            gauge_lines.append("# HELP permission_service_permission_changes_total Total change log entries")
            gauge_lines.append("# TYPE permission_service_permission_changes_total gauge")
            gauge_lines.append(f"permission_service_permission_changes_total {r.scalar() or 0}")

            # 全局版本号
            r = await db.execute(text("SELECT last_value FROM global_permission_version"))
            row = r.fetchone()
            gauge_lines.append("# HELP permission_service_global_version Global permission version")
            gauge_lines.append("# TYPE permission_service_global_version gauge")
            gauge_lines.append(f"permission_service_global_version {row[0] if row else 0}")
    except Exception:
        pass

    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        prom_lines + "\n".join(gauge_lines) + "\n",
        media_type="text/plain",
    )


# ── 路由注册 ──
from api.decision import router as decision_router
from api.context import router as context_router
from api.projection import router as projection_router
from api.lifecycle import router as lifecycle_router
from api.acl_routes import router as acl_router
from api.role_routes import router as role_router
from api.restriction_routes import router as restriction_router
from api.audit_routes import router as audit_router
from api.auth_routes import router as auth_router
from api.resource_routes import router as resource_admin_router
from api.tenant_routes import router as tenant_router
from api.role_definitions_routes import router as role_def_router
from api.project_routes import router as project_router

app.include_router(decision_router)
app.include_router(context_router)
app.include_router(projection_router)
app.include_router(lifecycle_router)
app.include_router(acl_router)
app.include_router(role_router)
app.include_router(restriction_router)
app.include_router(audit_router)
app.include_router(auth_router)
app.include_router(resource_admin_router)
app.include_router(tenant_router)
app.include_router(role_def_router)
app.include_router(project_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
