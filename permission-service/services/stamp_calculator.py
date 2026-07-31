"""可见性戳记计算服务 — visibility 端点专用。

设计依据：docs/外部系统设计.md §2.5.2 可见性投影 + §5.1 VisibilityChanged 事件。

此模块是 acl_resolver 戳记函数的轻量封装层，提供语义清晰的导入路径。
实际实现在 acl_resolver.py 中（决策面与投影面共用同一套 ACL 查询逻辑）。
"""

from services.acl_resolver import (
    get_allow_stamps_for_channel,
    get_deny_stamps_for_channel,
)

__all__ = [
    "get_allow_stamps_for_channel",
    "get_deny_stamps_for_channel",
]
