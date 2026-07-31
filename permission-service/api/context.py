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

# ── 合法 audience 注册表 ──
# 设计依据：docs/RAG系统设计v14.md §6.7 + §6A.5
# audience 必须与消费方服务名一致。
# 新增消费方须在此注册后方可调用 /v1/context。
_ALLOWED_AUDIENCES: set[str] = {
    "retrieval-worker",   # RAG 系统 retrieval-worker（Celery 异步检索任务）
    "ingestion-worker",   # RAG 系统 ingestion-worker（预留）
    "stamping-worker",    # RAG 系统 stamping-worker（预留）
}


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

    # audience 校验：必须在注册表中
    if body.audience not in _ALLOWED_AUDIENCES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_audience",
                "message": (
                    f"Unknown audience '{body.audience}'. "
                    f"Allowed: {sorted(_ALLOWED_AUDIENCES)}"
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
