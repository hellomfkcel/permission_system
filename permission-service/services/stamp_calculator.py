"""可见性戳记计算服务 — visibility 端点专用。

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
