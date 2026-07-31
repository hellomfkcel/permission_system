"""测试工具函数。"""

import os
import time
import uuid

from jose import jwt

JWT_PRIVATE_KEY_PATH = "/home/mfkcel/proj_rag_dev/config/jwt_private.pem"
BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:18080")


def make_token(
    sub: str = "test-user",
    tenant: str = "tenant-test",
    roles: list[str] | None = None,
    groups: list[str] | None = None,
) -> str:
    """生成测试用 JWT token。

    注意: groups=None 时默认 ["engineering"]（表示未明确指定），
    groups=[] 时表示明确无组（用于封禁测试）。
    """
    with open(JWT_PRIVATE_KEY_PATH) as f:
        key = f.read()
    claims = {
        "sub": sub,
        "tenant": tenant,
        "realm_access": {"roles": roles if roles is not None else ["user"]},
        "groups": groups if groups is not None else ["engineering"],
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    return jwt.encode(claims, key, algorithm="RS256")


def make_admin_headers(sub: str = "admin-user", tenant: str = "tenant-test") -> dict:
    """生成带 Authorization Bearer token 的请求头（用于管理台 API 鉴权测试）。"""
    token = make_token(sub=sub, tenant=tenant, roles=["system_admin", "user"])
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Request-Id": f"test-{uuid.uuid4().hex[:8]}",
        "X-Client-Id": "admin-console",
    }
