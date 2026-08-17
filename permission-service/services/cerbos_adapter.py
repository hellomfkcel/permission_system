"""Cerbos PDP 调用适配器。"""

import asyncio
import time as _time
import httpx
from app.config import settings

# tracing 未初始化时静默降级，不阻断判定
try:
    from opentelemetry import trace as _otel_trace
    _TRACER = _otel_trace.get_tracer("permission-service")
except Exception:
    _TRACER = None


class CerbosAdapter:
    """封装 Cerbos PDP /api/check/resources 调用。

    超时：连接 5s，读取 10s。
    重试：5xx 与网络错误重试 2 次，指数退避 200ms→400ms；4xx 不重试。
    """
    MAX_RETRIES = 2
    RETRY_BACKOFF_BASE = 0.2  # 200ms base

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.cerbos_pdp_url,
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
        )

    async def check_resources(
        self,
        request_id: str,
        principal: dict,
        resources: list[dict],
    ) -> dict:
        """调用 Cerbos /api/check/resources 批量判定。

        产生 OTel span "cerbos.check_resources"。

        Args:
            request_id: 请求追踪 ID。
            principal: Cerbos principal 对象 (id, roles, attr)。
            resources: [{actions, resource: {kind, id, attr}}, ...]。

        Returns:
            Cerbos 原始响应 JSON dict，含 results 和 cerbosCallId。

        Raises:
            httpx.HTTPStatusError: 4xx 客户端错误（不重试）。
            httpx.RequestError: 网络错误（经重试后仍失败）。
        """
        span = None
        _start = _time.monotonic()
        try:
            if _TRACER is not None:
                span = _TRACER.start_span("cerbos.check_resources")
                span.set_attribute("cerbos.request_id", request_id)
                span.set_attribute("cerbos.resource_count", len(resources))
                if resources:
                    first = resources[0]
                    if "actions" in first:
                        span.set_attribute("cerbos.actions", ",".join(first["actions"]))
        except Exception:
            span = None

        last_exc = None
        try:
            for attempt in range(self.MAX_RETRIES + 1):
                try:
                    resp = await self._client.post(
                        "/api/check/resources",
                        json={
                            "requestId": request_id,
                            "principal": principal,
                            "resources": resources,
                        },
                    )
                    resp.raise_for_status()
                    result = resp.json()

                    if span is not None:
                        try:
                            span.set_attribute("cerbos.attempts", attempt + 1)
                            span.set_attribute("cerbos.elapsed_ms",
                                             int((_time.monotonic() - _start) * 1000))
                            call_id = result.get("cerbosCallId", "")
                            if call_id:
                                span.set_attribute("cerbos.call_id", call_id)
                        except Exception:
                            pass

                    return result

                except httpx.HTTPStatusError as exc:
                    if 400 <= exc.response.status_code < 500:
                        raise
                    last_exc = exc
                    if attempt < self.MAX_RETRIES:
                        wait = self.RETRY_BACKOFF_BASE * (2 ** attempt)
                        await asyncio.sleep(wait)

                except (httpx.RequestError, httpx.TimeoutException) as exc:
                    last_exc = exc
                    if attempt < self.MAX_RETRIES:
                        wait = self.RETRY_BACKOFF_BASE * (2 ** attempt)
                        await asyncio.sleep(wait)

            raise last_exc  # type: ignore[misc]

        finally:
            if span is not None:
                try:
                    if last_exc is not None:
                        span.set_attribute("error", True)
                        span.set_attribute("error.message", str(last_exc)[:200])
                    span.end()
                except Exception:
                    pass

    async def close(self) -> None:
        await self._client.aclose()


# 全局单例
_cerbos: CerbosAdapter | None = None


def get_cerbos() -> CerbosAdapter:
    """获取 CerbosAdapter 全局单例。"""
    global _cerbos
    if _cerbos is None:
        _cerbos = CerbosAdapter()
    return _cerbos
