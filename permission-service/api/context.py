"""ctx_token 铸造 — POST /v1/context。

设计依据：docs/外部系统设计.md §2.4.1 决策面 API + 实施方案步骤 3.4。
"""

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app.config import settings
from schemas.requests import ContextRequest
from schemas.responses import ContextResponse

router = APIRouter(prefix="/v1", tags=["context"])

# ── audience 缓存（Phase 1: 替代硬编码 _ALLOWED_AUDIENCES）──
# 从 project_audiences 表加载，60s TTL 内存缓存。
_audience_cache: set[str] = set()
_audience_cache_ts: float = 0.0
_AUDIENCE_CACHE_TTL = 60.0


def invalidate_audience_cache() -> None:
    """主动失效 audience 缓存（project_audiences 变更后调用）。"""
    global _audience_cache, _audience_cache_ts
    _audience_cache.clear()
    _audience_cache_ts = 0.0


async def _refresh_audience_cache() -> set[str]:
    """从 project_audiences 表刷新合法 audience 集合。"""
    global _audience_cache, _audience_cache_ts
    import time as _t
    from app.database import async_session
    from sqlalchemy import select
    from models.project import ProjectAudience

    now = _t.monotonic()
    if _audience_cache and (now - _audience_cache_ts) < _AUDIENCE_CACHE_TTL:
        return _audience_cache

    async with async_session() as db:
        result = await db.execute(select(ProjectAudience.audience))
        _audience_cache = {row.audience for row in result}
        _audience_cache_ts = now
    return _audience_cache


def _sign(payload: dict, secret: bytes) -> str:
    """HMAC-SHA256 签名。"""
    msg = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    sig = hmac.new(secret, msg, hashlib.sha256).hexdigest()
    return sig


def _b64_encode(data: str) -> str:
    """URL-safe base64 编码。"""
    import base64

    return base64.urlsafe_b64encode(data.encode()).rstrip(b"=").decode()


def _b64_decode(data: str) -> str:
    """URL-safe base64 解码。"""
    import base64

    padding = 4 - len(data) % 4
    if padding != 4:
        data += "=" * padding
    return base64.urlsafe_b64decode(data).decode()


@router.post("/context", response_model=ContextResponse)
async def mint_context_token(
    body: ContextRequest,
) -> ContextResponse:
    """铸造 ctx_token — HMAC-SHA256 签名打包。

    约束：ttl_s ≤ 600；audience 必须校验；过期即失效。
    """
    # ttl_s 上限校验
    ttl_s = min(body.ttl_s, 600)

    # audience 校验（Phase 1: 从 DB 查询，替代硬编码 _ALLOWED_AUDIENCES）
    allowed = await _refresh_audience_cache()
    if body.audience not in allowed:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_audience",
                "message": (
                    f"Unknown audience '{body.audience}'. "
                    f"Register it in the project management page."
                ),
            },
        )

    iat = int(time.time())
    exp = iat + ttl_s

    payload = {
        "credential": body.credential,
        "audience": body.audience,
        "iat": iat,
        "exp": exp,
    }

    # 签名密钥：使用独立的 ctx_token_secret
    # 若未配置则回退到 Redis URL hash（保持向后兼容），生产环境必须显式配置
    if settings.ctx_token_secret:
        secret = settings.ctx_token_secret.encode()
    else:
        secret = hashlib.sha256(settings.get_redis_url().encode()).digest()
    signature = _sign(payload, secret)

    # 打包为 ctx.xxx.xxx 格式
    header_b64 = _b64_encode('{"alg":"HS256","typ":"JWT"}')
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload_b64 = _b64_encode(payload_json)
    sig_b64 = _b64_encode(signature)

    ctx_token = f"ctx.{header_b64}.{payload_b64}.{sig_b64}"

    expires_at_dt = datetime.fromtimestamp(exp, tz=timezone.utc)
    return ContextResponse(
        ctx_token=ctx_token,
        expires_at=expires_at_dt.isoformat(),
    )


def verify_ctx_token(ctx_token: str, expected_audience: str) -> dict | None:
    """验证 ctx_token 并返回 payload，验证失败返回 None。

    用于 RAG 侧 worker 验证 ctx_token。
    """
    parts = ctx_token.split(".")
    if len(parts) != 4 or parts[0] != "ctx":
        return None

    try:
        payload_json = _b64_decode(parts[2])
        payload = json.loads(payload_json)
    except Exception:
        return None

    # 验证过期
    now = int(time.time())
    if payload.get("exp", 0) < now:
        return None

    # 验证 audience
    if payload.get("audience", "") != expected_audience:
        return None

    # 验证签名 — 使用与签发时相同的密钥派生逻辑
    if settings.ctx_token_secret:
        secret = settings.ctx_token_secret.encode()
    else:
        secret = hashlib.sha256(settings.get_redis_url().encode()).digest()
    expected_sig = _sign(payload, secret)
    actual_sig = _b64_decode(parts[3])
    if not hmac.compare_digest(expected_sig, actual_sig):
        return None

    return payload
