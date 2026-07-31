"""限流器单例 — 供各 API 路由模块使用。

设计依据：docs/外部系统设计.md §2.4 性能目标 + docs/RAG系统设计v14.md §19 扩容路径。
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["1000/minute"],
)
