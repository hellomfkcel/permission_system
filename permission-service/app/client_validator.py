"""X-Client-Id 准入矩阵校验中间件。

设计依据：docs/外部系统设计.md §6A.1 五端点使用总表
          + docs/RAG系统设计v14.md §6A.1 client_id 准入矩阵
          + docs/权限管理系统架构设计.md §6A.1

每个端点只接受特定的 client_id，防止业务模块伪装身份。
校验失败 → 403 Forbidden + 安全告警日志。
"""

import json
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response, JSONResponse
import structlog

from app.config import Settings

# 模块级 Settings 实例（单例模式）。
# 修复：使用缓存的实例而非每次请求新建，确保 Docker/K8s secret 文件加载的值生效。
_settings = Settings()

logger = structlog.get_logger(__name__)

# ── 客户端缓存（Phase 1: 替代硬编码 ALLOWED_CLIENTS）──
# 从 project_clients 表加载，60s TTL 内存缓存。
# {client_id: project_id}
_client_cache: dict[str, str] = {}
_client_cache_ts: float = 0.0
_CLIENT_CACHE_TTL = 60.0  # 秒

# ── API Key 缓存（Phase 1: 替代单全局 SERVICE_API_KEY）──
# 从 project_api_keys 表加载，60s TTL。
# {api_key_hash: project_id}
_api_key_cache: dict[str, str] = {}
_api_key_cache_ts: float = 0.0


async def _refresh_client_cache() -> dict[str, str]:
    """从 project_clients 表刷新客户端缓存。"""
    global _client_cache, _client_cache_ts
    import time as _t
    from app.database import async_session
    from sqlalchemy import select
    from models.project import ProjectClient

    now = _t.monotonic()
    if _client_cache and (now - _client_cache_ts) < _CLIENT_CACHE_TTL:
        return _client_cache

    async with async_session() as db:
        result = await db.execute(
            select(ProjectClient.client_id, ProjectClient.project_id)
        )
        _client_cache = {row.client_id: row.project_id for row in result}
        _client_cache_ts = now
    return _client_cache


async def _refresh_api_key_cache() -> dict[str, str]:
    """从 project_api_keys 表刷新 API Key 缓存。

    Returns:
        {key_hash: project_id}，仅包含未吊销的 key。
    """
    global _api_key_cache, _api_key_cache_ts
    import time as _t
    from app.database import async_session
    from sqlalchemy import select
    from models.project import ProjectApiKey

    now = _t.monotonic()
    if _api_key_cache and (now - _api_key_cache_ts) < _CLIENT_CACHE_TTL:
        return _api_key_cache

    async with async_session() as db:
        result = await db.execute(
            select(ProjectApiKey.key_hash, ProjectApiKey.project_id)
            .where(ProjectApiKey.revoked == False)  # noqa: E712
        )
        _api_key_cache = {row.key_hash: row.project_id for row in result}
        _api_key_cache_ts = now
    return _api_key_cache


def invalidate_client_cache() -> None:
    """主动失效客户端缓存（project_clients 变更后调用）。"""
    global _client_cache, _client_cache_ts
    _client_cache.clear()
    _client_cache_ts = 0.0


def invalidate_api_key_cache() -> None:
    """主动失效 API Key 缓存（project_api_keys 变更后调用）。"""
    global _api_key_cache, _api_key_cache_ts
    _api_key_cache.clear()
    _api_key_cache_ts = 0.0


async def validate_api_key(api_key: str) -> str | None:
    """校验 API Key 并返回对应的 project_id。

    替代原来 config.py 中的单全局 service_api_key 校验。

    Returns:
        匹配的 project_id，若无效则返回 None。
    """
    import hashlib
    if not api_key:
        return None
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    cache = await _refresh_api_key_cache()
    return cache.get(key_hash)

# ── Bearer Auth 路径（管理台 API）—— 不强制 X-Client-Id ──
# 这些端点通过 get_current_admin 依赖注入验证 Bearer token
BEARER_AUTH_PATHS = {"/api/v1"}

# ── 公开端点（无需 client_id 校验）──
PUBLIC_PATHS = {
    "/healthz",
    "/readyz",
    "/metrics",
    "/docs",
    "/openapi.json",
    "/redoc",
}


def _get_client_id(request: Request) -> str | None:
    """从请求头提取 X-Client-Id。"""
    return request.headers.get("X-Client-Id") or request.headers.get("x-client-id")


async def validate_client_id(path: str, client_id: str | None) -> str | None:
    """校验 client_id 是否在 project_clients 表中注册。

    Phase 1: 替代硬编码 ALLOWED_CLIENTS，改为从 DB 查询 + 缓存。

    Returns:
        匹配到的 project_id，用于注入 request.state.project_id。

    Raises:
        HTTPException(403): client_id 不在注册表中。
    """
    if client_id is None:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "missing_client_id",
                "message": "X-Client-Id header is required for this endpoint.",
            },
        )

    cache = await _refresh_client_cache()
    if client_id in cache:
        return cache[client_id]

    logger.warning(
        "client_id_rejected",
        path=path,
        client_id=client_id,
    )
    raise HTTPException(
        status_code=403,
        detail={
            "error": "invalid_client_id",
            "message": (
                f"Client-ID '{client_id}' is not registered. "
                "Register it in the project management page."
            ),
        },
    )


class ClientIdValidationMiddleware(BaseHTTPMiddleware):
    """X-Client-Id 强制校验中间件 + 可选 X-Api-Key 服务间认证。

    在请求进入路由处理前：
    1. 校验 X-Client-Id header（决策面/投影面/生命周期端点）。
    2. 若配置了 SERVICE_API_KEY，校验 X-Api-Key header（服务间认证）。
    公开端点（/healthz, /readyz, /metrics, /docs）跳过校验。
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # 跳过公开端点
        if path in PUBLIC_PATHS or path.startswith("/docs") or path.startswith("/openapi"):
            return await call_next(request)

        # OPTIONS 预检请求跳过
        if request.method == "OPTIONS":
            return await call_next(request)

        # ── 服务间认证：X-Api-Key（v1 端点） ──
        # Phase 1: 从 project_api_keys 表校验，替代单全局 service_api_key。
        if path.startswith("/v1/") and not path.startswith("/api/v1/"):
            api_key = (
                request.headers.get("X-Api-Key")
                or request.headers.get("x-api-key")
            )
            project_id_from_key = await validate_api_key(api_key or "")
            if project_id_from_key is None:
                logger.warning(
                    "api_key_rejected",
                    path=path,
                    method=request.method,
                )
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": "invalid_api_key",
                        "message": "Invalid or missing X-Api-Key for service-to-service endpoint.",
                    },
                )
            # 注入 project_id 到 request.state
            request.state.project_id = project_id_from_key

        # 管理台 Bearer Auth 路径跳过 X-Client-Id 校验
        for bp in BEARER_AUTH_PATHS:
            if path.startswith(bp):
                return await call_next(request)

        client_id = _get_client_id(request)

        # Phase 1: 从 project_clients 表校验 client_id（替代硬编码 ALLOWED_CLIENTS）
        try:
            project_id_from_client = await validate_client_id(path, client_id)
        except HTTPException:
            raise  # 重新抛出 403

        # ★ P4 修复：API key 和 client_id 必须属于同一项目
        # 防止跨项目混用：RAG 的 API key + demo 的 client_id 必须被拒绝
        project_id_from_key = getattr(request.state, "project_id", None)
        if project_id_from_key and project_id_from_key != project_id_from_client:
            logger.warning(
                "project_mismatch",
                path=path,
                key_project=project_id_from_key,
                client_project=project_id_from_client,
                client_id=client_id,
            )
            return JSONResponse(
                status_code=403,
                content={
                    "error": "project_mismatch",
                    "message": (
                        f"API key belongs to project '{project_id_from_key}' "
                        f"but client_id '{client_id}' belongs to project '{project_id_from_client}'. "
                        "They must belong to the same project."
                    ),
                },
            )

        if not project_id_from_key:
            request.state.project_id = project_id_from_client

        return await call_next(request)
