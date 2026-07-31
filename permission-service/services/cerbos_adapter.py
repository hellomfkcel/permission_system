"""Cerbos PDP 调用适配器。

设计依据：docs/外部系统设计.md §2.5.1 权限判定流程 + 实施方案步骤 3.1。

P2-5 加固：添加 httpx 重试 + 指数退避 + 超时熔断。
Cerbos PDP 临时不可达时自动重试（最多 2 次），避免单次网络抖动导致判定失败。
"""

import asyncio
import httpx
from app.config import settings


class CerbosAdapter:
    """封装 Cerbos PDP /api/check/resources 调用。

    P2-5 加固：
    - 连接超时 5s，读取超时 10s
    - 5xx/网络错误自动重试 2 次（指数退避 200ms→400ms）
    - 4xx 不重试（客户端错误）
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

        带重试：5xx/网络错误自动重试，4xx 立即抛出。

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
        last_exc = None

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
                return resp.json()

            except httpx.HTTPStatusError as exc:
                # 4xx 是客户端错误，不重试
                if 400 <= exc.response.status_code < 500:
                    raise
                # 5xx 服务端错误，可重试
                last_exc = exc
                if attempt < self.MAX_RETRIES:
                    wait = self.RETRY_BACKOFF_BASE * (2 ** attempt)
                    await asyncio.sleep(wait)

            except (httpx.RequestError, httpx.TimeoutException) as exc:
                # 网络错误/超时，可重试
                last_exc = exc
                if attempt < self.MAX_RETRIES:
                    wait = self.RETRY_BACKOFF_BASE * (2 ** attempt)
                    await asyncio.sleep(wait)

        # 重试用尽，抛出最后的异常
        raise last_exc  # type: ignore[misc]

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
