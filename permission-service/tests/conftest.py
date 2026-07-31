"""Pytest 配置与共享 fixtures。"""

import time
import uuid

import pytest
import httpx
from jose import jwt

from tests.utils import make_token, make_admin_headers, BASE_URL

JWT_PRIVATE_KEY_PATH = "/home/mfkcel/proj_rag_dev/config/jwt_private.pem"


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


@pytest.fixture
def unique_id() -> str:
    """唯一 ID，用于测试隔离。"""
    return uuid.uuid4().hex[:12]


@pytest.fixture
def api():
    """同步 HTTP 客户端（用于简单请求）。"""
    return httpx.Client(base_url=BASE_URL, timeout=15.0)


@pytest.fixture
def headers(test_token):
    """标准请求头（决策面 API 使用 X-Request-Id + X-Client-Id）。"""
    return {
        "X-Request-Id": f"test-{uuid.uuid4().hex[:8]}",
        "X-Client-Id": "interactive-backend",
    }
