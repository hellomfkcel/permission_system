"""Pytest 配置与共享 fixtures。"""

import time
import uuid

import pytest
import httpx
from jose import jwt

from tests.utils import (
    make_token, make_admin_headers, BASE_URL, SERVICE_API_KEY, TEST_PROJECT_ID,
)

# 私钥路径由 tests/utils.py 统一解析（默认仓库内 config/，可用环境变量覆盖）
from tests.utils import JWT_PRIVATE_KEY_PATH  # noqa: F401


@pytest.fixture
def test_token() -> str:
    """测试用普通用户 JWT token。"""
    return make_token()


@pytest.fixture
def admin_token() -> str:
    """管理员 JWT token。"""
    return make_token(sub="admin-user", roles=["system_admin", "user"])


@pytest.fixture
def admin_headers() -> dict:
    """带管理员认证的请求头。"""
    return make_admin_headers()


def _hex_with_letter(length: int = 12) -> str:
    """返回含至少一个字母的 hex 片段。

    幂等键校验会拒绝"全数字的独立十进制段"（时间戳误判），
    测试资源 ID 若恰好是全数字 hex 会误触发 422，故强制含字母。
    """
    _id = uuid.uuid4().hex[:length]
    while _id.isdigit():
        _id = uuid.uuid4().hex[:length]
    return _id


@pytest.fixture
def unique_id() -> str:
    """唯一 ID，用于测试隔离（保证含字母，避免幂等键时间戳误判）。"""
    return _hex_with_letter(12)


@pytest.fixture
def api():
    """同步 HTTP 客户端（用于简单请求）。"""
    return httpx.Client(base_url=BASE_URL, timeout=15.0)


@pytest.fixture
def headers(test_token):
    """/v1/* 服务间端点请求头。

    自平台改为多项目接入后，/v1/* 需同时携带 X-Client-Id（project_clients 注册）
    与 X-Api-Key（project_api_keys 签发），缺任一项分别返回 403 / 401。
    """
    return {
        "X-Request-Id": f"test-{uuid.uuid4().hex[:8]}",
        "X-Client-Id": "interactive-backend",
        "X-Api-Key": SERVICE_API_KEY,
    }
