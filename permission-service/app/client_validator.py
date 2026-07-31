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

# ── 准入矩阵（设计依据 §6A.1）──
# 每个端点路径前缀 → 允许的 client_id 集合
# /api/v1/* 管理台端点由 Bearer token (get_current_admin) 保护，
# 不强制 X-Client-Id 校验。
ALLOWED_CLIENTS: dict[str, set[str]] = {
    # 决策面 — interactive-backend 调用
    "/v1/check":       {"interactive-backend"},
    # 投影面 — retrieval 调用（prefilter），ingest 调用（visibility）
    "/v1/prefilter":   {"retrieval"},
    "/v1/visibility":  {"ingest"},
    # 上下文 — interactive-backend 调用
    "/v1/context":     {"interactive-backend"},
    # 生命周期端口 — RAG B-DOC 通过 P-AUTHC 调用
    "/v1/resources":   {"interactive-backend"},
    # 检索过滤 — retrieval 调用
    "/v1/filter":      {"retrieval"},
}

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


def validate_client_id(path: str, client_id: str | None) -> None:
    """校验 client_id 是否在端点允许列表中。

    Raises:
        HTTPException(403): client_id 不在准入矩阵中。
    """
    if client_id is None:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "missing_client_id",
                "message": "X-Client-Id header is required for this endpoint.",
            },
        )

    # 查找匹配的路径前缀
    for prefix, allowed in ALLOWED_CLIENTS.items():
        if path.startswith(prefix):
            if client_id in allowed:
                return  # 校验通过
            # client_id 不在允许列表中
            logger.warning(
                "client_id_rejected",
                path=path,
                client_id=client_id,
                allowed=sorted(allowed),
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "invalid_client_id",
                    "message": (
                        f"Client-ID '{client_id}' is not authorized for {path}. "
                        f"Allowed: {sorted(allowed)}"
                    ),
                },
            )

    # 路径不在准入矩阵中 → 允许通过（管理台 API 等由 Bearer token 保护）
    return


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

        # ── 服务间认证：X-Api-Key（v1 端点，可选） ──
        api_key_required = (
            _settings.service_api_key
            and path.startswith("/v1/")
            and not path.startswith("/api/v1/")
        )
        if api_key_required:
            api_key = (
                request.headers.get("X-Api-Key")
                or request.headers.get("x-api-key")
            )
            # P0-2 修复：使用模块级 _settings 单例（确保 Docker/K8s secret 文件加载的值生效）
            if api_key != _settings.service_api_key:
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

        # 管理台 Bearer Auth 路径跳过 X-Client-Id 校验
        for bp in BEARER_AUTH_PATHS:
            if path.startswith(bp):
                return await call_next(request)

        client_id = _get_client_id(request)

        # 查找匹配的路径前缀并校验 client_id
        for prefix, allowed in ALLOWED_CLIENTS.items():
            if path.startswith(prefix):
                if client_id is None:
                    logger.warning(
                        "client_id_missing",
                        path=path,
                        method=request.method,
                    )
                    return JSONResponse(
                        status_code=403,
                        content={
                            "error": "missing_client_id",
                            "message": (
                                f"X-Client-Id header is required for {path}. "
                                f"Allowed clients: {sorted(allowed)}"
                            ),
                        },
                    )
                if client_id not in allowed:
                    logger.warning(
                        "client_id_rejected",
                        path=path,
                        client_id=client_id,
                        allowed=sorted(allowed),
                    )
                    return JSONResponse(
                        status_code=403,
                        content={
                            "error": "invalid_client_id",
                            "message": (
                                f"Client-ID '{client_id}' is not authorized for {path}. "
                                f"Allowed: {sorted(allowed)}"
                            ),
                        },
                    )
                break  # 找到匹配的 prefix 且校验通过

        return await call_next(request)
