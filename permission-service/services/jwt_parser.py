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


# ── Keycloak JWKS（admin-console 的 Keycloak 签发 token 验签）────────
# parse_principal 默认用 RAG 共享公钥（RAG 自签 token）。admin-console 的
# Keycloak access_token 由 Keycloak 私钥签名，需经 realm JWKS 验签。缓存 1h。
_keycloak_jwks_keys: list | None = None
_keycloak_jwks_ts: float = 0.0


def _keycloak_jwks() -> list:
    """获取并缓存 Keycloak realm JWKS 公钥列表。失败返回空列表。"""
    global _keycloak_jwks_keys, _keycloak_jwks_ts
    now = time.time()
    if _keycloak_jwks_keys and (now - _keycloak_jwks_ts) < 3600:
        return _keycloak_jwks_keys

    url = (
        f"{settings.keycloak_server_url.rstrip('/')}"
        f"/realms/{settings.keycloak_realm}/protocol/openid-connect/certs"
    )
    try:
        import httpx
        resp = httpx.get(url, timeout=5.0)
        resp.raise_for_status()
        _keycloak_jwks_keys = (resp.json() or {}).get("keys", [])
        _keycloak_jwks_ts = now
    except Exception:
        _keycloak_jwks_keys = []
    return _keycloak_jwks_keys


def _decode_with_keycloak_jwks(credential: str, algorithm: str, opts: dict) -> dict:
    """用 Keycloak realm JWKS 验签（admin-console 的 Keycloak 签发 token）。

    校验 issuer 必须是配置的 Keycloak realm（支持外部 https://EXTERNAL_HOST/realms/{realm}）。
    """
    keys = _keycloak_jwks()
    if not keys:
        raise JoseJWTError("Keycloak JWKS unavailable")

    unverified = jwt.get_unverified_claims(credential)
    token_iss = unverified.get("iss", "")
    if f"/realms/{settings.keycloak_realm}" not in token_iss:
        raise JoseJWTError(f"Issuer '{token_iss}' is not the Keycloak realm {settings.keycloak_realm}")

    header = jwt.get_unverified_header(credential)
    kid = header.get("kid")
    candidates = [k for k in keys if k.get("kid") == kid]
    if not candidates:
        candidates = [k for k in keys if k.get("alg", "").startswith("RS")]

    last_err: JoseJWTError | None = None
    for key in candidates:
        try:
            return jwt.decode(credential, key, algorithms=[algorithm], options=opts)
        except JoseJWTError as exc:
            last_err = exc
    raise last_err or JoseJWTError("no matching Keycloak JWKS key")


def _decode_jwt(credential: str, public_key: str, algorithm: str, issuer: str | None, opts: dict) -> dict:
    """按序验签：① RAG 共享公钥（RAG 自签 token）→ ② Keycloak realm JWKS（admin-console token）。"""
    try:
        return jwt.decode(credential, public_key, algorithms=[algorithm], issuer=issuer, options=opts)
    except JoseJWTError:
        # RAG 公钥验不过 → 可能是 Keycloak 签发的 admin-console token
        return _decode_with_keycloak_jwks(credential, algorithm, opts)


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

    claims: dict = _decode_jwt(
        credential,
        public_key,
        settings.jwt_algorithm,
        issuer,
        decode_opts,
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
