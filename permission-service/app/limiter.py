"""限流器单例 — 供各 API 路由模块使用。"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["1000/minute"],
)
