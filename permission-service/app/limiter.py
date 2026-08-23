"""限流器单例 — 供各 API 路由模块使用。"""

from slowapi import Limiter
from slowapi.util import get_remote_address


def _client_ip_key(request) -> str:
    """限流键：真实客户端 IP。

    反代（LB/Ingress）后 request.client.host 是代理 IP，所有客户端共享同一个限流桶
    → 无法区分攻击者。此处：仅当直连对端在 TRUSTED_PROXY_IPS 白名单内时，
    才信任 X-Forwarded-For 首跳作为真实客户端 IP；否则回落直连对端（防伪造 XFF）。
    """
    from app.config import settings

    peer = request.client.host if request.client else "unknown"
    trusted = {ip.strip() for ip in (settings.trusted_proxy_ips or "").split(",") if ip.strip()}
    if trusted and peer in trusted:
        xff = request.headers.get("X-Forwarded-For", "")
        first = xff.split(",")[0].strip() if xff else ""
        if first:
            return first
    return peer


limiter = Limiter(
    key_func=_client_ip_key,
    default_limits=["1000/minute"],
)
