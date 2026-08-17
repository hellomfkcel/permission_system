"""JWT 解析 — 从 credential 提取 principal 信息。

支持两种 credential 格式：
1. 标准 JWT（开发模式 dev-login / 生产模式 Keycloak）
2. ctx_token（RAG worker 异步任务携带的上下文令牌）

ctx_token 解析逻辑与 api/context.py 的 verify_ctx_token 保持同步。
"""

import base64
import hashlib
import hmac
import json
import time

from jose import jwt
from jose.exceptions import JWTError as JoseJWTError

from app.config import settings
from schemas.responses import Principal


def _norm_list(val) -> list[str]:
    """规范化列表字段。"""
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        return [val]
    return []


def _b64_decode(data: str) -> str:
    """URL-safe base64 解码（兼容 ctx_token 的 padding 缺失）。"""
    padding = 4 - len(data) % 4
    if padding != 4:
        data += "=" * padding
    return base64.urlsafe_b64decode(data).decode()


def _get_ctx_token_secret() -> bytes:
    """获取 ctx_token 签名密钥（与 api/context.py 签发逻辑一致）。"""
    if settings.ctx_token_secret:
        return settings.ctx_token_secret.encode()
    return hashlib.sha256(settings.get_redis_url().encode()).digest()


def _resolve_credential_from_ctx_token(credential: str) -> str:
    """从 ctx_token 中提取原始 JWT credential。

    ctx_token 格式: ctx.{header_b64}.{payload_b64}.{signature_b64}
    payload 内容: {"credential": "<JWT>", "audience": "...", "iat": ..., "exp": ...}

    验证：
    1. 格式检查（ctx. 前缀 + 4 段）
    2. 过期检查（exp 字段）
    3. HMAC-SHA256 签名验证

    Returns:
        原始 JWT 字符串，供后续 parse_principal 解析。

    Raises:
        ValueError: 格式/过期/签名任一验证失败。
    """
    parts = credential.split(".")
    if len(parts) != 4 or parts[0] != "ctx":
        raise ValueError("invalid ctx_token format: expected ctx.{header}.{payload}.{signature}")

    try:
        payload_json = _b64_decode(parts[2])
        payload = json.loads(payload_json)
    except Exception as e:
        raise ValueError(f"failed to decode ctx_token payload: {e}") from e

    # 验证过期
    now = int(time.time())
    if payload.get("exp", 0) < now:
        raise ValueError("ctx_token expired")

    # 验证签名
    secret = _get_ctx_token_secret()
    msg = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    expected_sig = hmac.new(secret, msg, hashlib.sha256).hexdigest()
    actual_sig = _b64_decode(parts[3])
    if not hmac.compare_digest(expected_sig, actual_sig):
        raise ValueError("ctx_token signature verification failed")

    inner_credential = payload.get("credential", "")
    if not inner_credential:
        raise ValueError("ctx_token payload missing credential field")

    return inner_credential


def parse_principal(credential: str) -> Principal:
    """从 JWT credential（或 ctx_token）构建 Principal 对象。

    当 credential 以 "ctx." 开头时，视为 ctx_token——
    先从中提取原始 JWT，再继续正常的 JWT 解析流程。

    包含: user_id, roles, groups, tenant_id, principals 展开。

    Raises:
        JoseJWTError: JWT 校验失败时抛出。
        ValueError: ctx_token 解析失败时抛出。
    """
    # ── ctx_token 解析分支（J-15 闭环）──
    if credential.startswith("ctx."):
        credential = _resolve_credential_from_ctx_token(credential)

    # 读取公钥
    with open(settings.jwt_public_key_path) as f:
        public_key = f.read()

    # P2 优化：校验 JWT Issuer（生产模式）
    decode_opts: dict = {"verify_aud": False}  # 生产模式暂不校验 aud
    issuer = None
    if settings.jwt_allowed_issuers:
        allowed = [i.strip() for i in settings.jwt_allowed_issuers.split(",") if i.strip()]
        if allowed:
            issuer = allowed[0]  # jose 只接受单个 issuer 字符串
            # 若 token issuer 不在白名单，jose 会抛出 JWTError

    claims: dict = jwt.decode(
        credential,
        public_key,
        algorithms=[settings.jwt_algorithm],
        issuer=issuer,
        options=decode_opts,
    )

    # 若配置了多 issuer 白名单但 jose 只支持单 issuer，
    # 则在解码后手动检查 issuer 是否在白名单内
    if settings.jwt_allowed_issuers:
        allowed = [i.strip() for i in settings.jwt_allowed_issuers.split(",") if i.strip()]
        token_iss = claims.get("iss", "")
        if token_iss and len(allowed) > 1 and token_iss not in allowed:
            raise JoseJWTError(f"Issuer '{token_iss}' not in allowed list: {allowed}")

    # 优先使用 preferred_username（可读的用户名），回退到 sub（可能是 UUID）
    # Keycloak SSO: sub = UUID, preferred_username = "alice"
    # dev-login: sub = "alice" (手动设置)
    user_id: str = claims.get("preferred_username") or claims.get("sub", "unknown")
    tenant_id: str = claims.get("tenant", claims.get("tenant_id", ""))
    # 兼容两种 JWT 格式:
    #   生产模式 (Keycloak): roles 在 realm_access.roles 中
    #   开发模式 (RAG dev-login): roles 在顶层 "roles" 数组中
    roles: list[str] = _norm_list(
        claims.get("realm_access", {}).get("roles", [])
    ) or _norm_list(claims.get("roles", []))
    groups: list[str] = _norm_list(
        claims.get("groups", [])
    ) or _norm_list(claims.get("group", []))

    # 展开 principals
    principals: list[str] = [f"user:{user_id}"]
    for g in groups:
        principals.append(f"group:{g}")
    for r in roles:
        principals.append(f"role:{r}")

    return Principal(
        user_id=user_id,
        tenant_id=tenant_id,
        roles=roles,
        groups=groups,
        principals=principals,
        raw_jwt=credential,
    )
