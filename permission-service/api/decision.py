"""决策面 API — /v1/check + /v1/check/batch + /v1/filter。

设计依据：docs/外部系统设计.md §2.4.1 决策面 API + 实施方案步骤 3.3/3.4。
"""

import uuid

from fastapi import APIRouter, Body, Header, HTTPException, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.limiter import limiter
from app.config import settings
from schemas.requests import CheckRequest, CheckBatchRequest, FilterRequest
from schemas.responses import (
    DecisionResponse,
    CheckBatchResponse,
    CheckBatchResult,
    FilterResponse,
)
from services.jwt_parser import parse_principal
from services.acl_resolver import (
    resolve_granted_actions_by_principal,
    check_subject_ban,
    check_resource_restriction,
    get_resource_attr,
)
from services.cerbos_adapter import get_cerbos
from app.metrics_collector import record_authz_decision, record_authz_call_failed, record_authz_obligation_unknown

router = APIRouter(prefix="/v1", tags=["decision"])


# ── 三态映射 ──

DECISION_MAP: dict[str, str] = {
    "EFFECT_ALLOW": "allow",
    "EFFECT_DENY": "deny",
}
# 其他值 (EFFECT_UNSPECIFIED 等) → indeterminate


@router.post("/check", response_model=DecisionResponse)
@limiter.limit(f"{settings.check_rate_limit}/second")
async def check_permission(
    request: Request,
    request_id: str = Header(..., alias="X-Request-Id"),
    client_id: str = Header(..., alias="X-Client-Id"),
    body: CheckRequest = Body(...),
    db: AsyncSession = Depends(get_db),
) -> DecisionResponse:
    """单条权限判定。

    流程：
    1. 解析 JWT → Principal
    2. 查询 ACL + role_bindings → granted_actions
    3. 查询 restrictions → 型一封禁检查
    4. 构造 Cerbos principal + resource
    5. 调用 Cerbos /api/check/resources
    6. 三态映射
    """
    # 1. 解析主体
    try:
        principal = parse_principal(body.credential)
    except Exception as e:
        raise HTTPException(status_code=401, detail="Invalid credential") from e

    # 2-4. 查询 ACL + 封禁 + 资源属性
    channel_kb = body.channel.kb if body.channel else None
    principal_actions = await resolve_granted_actions_by_principal(
        db, principal.principals, body.action, body.resource.type, body.resource.id,
        channel_kb=channel_kb,
    )
    # Build granted_actions dict: {resource_id: [action_suffix, ...]}
    # Cerbos derived roles expect values like ["read"] not ["kb:read"]
    # e.g. {"kb-xxx": ["read", "write"], "kb-yyy": ["read"]}
    #
    # P1-4 修复：对于 document 资源，granted_actions 必须以 kb_id 为键
    # （而非 doc_id），因为 Cerbos derivedRoles rag_roles.yaml 在资源类型非 kb 时
    # 从 request.resource.attr.kb_id 取值查找 granted_actions。
    # 错误：granted_actions[doc_id] → Cerbos 找不到 → 判定为 deny
    # 正确：granted_actions[kb_id] → Cerbos 正确匹配 derived role
    granted_actions: dict[str, list[str]] = {}
    # 确定 granted_actions 的键：kb 资源用 resource.id，document 资源用 channel.kb
    grant_key = body.resource.id
    if body.resource.type == "document" and body.channel and body.channel.kb:
        grant_key = body.channel.kb
    for p_actions in principal_actions.values():
        for action_name in p_actions:
            # Strip resource prefix: "kb:read" → "read", "doc:view" → "view"
            suffix = action_name.split(":", 1)[-1] if ":" in action_name else action_name
            # Associate with the correct key (kb_id for docs, resource_id for kbs)
            granted_actions.setdefault(grant_key, [])
            if suffix not in granted_actions[grant_key]:
                granted_actions[grant_key].append(suffix)

    is_suspended = await check_subject_ban(
        db, principal.principals, principal.tenant_id,
    )

    if is_suspended:
        return DecisionResponse(
            decision="deny",
            decision_id=request_id,
            reasons=["subject_banned"],
        )

    # 5. 构造 Cerbos principal（roles 必须唯一）
    cerbos_roles: list[str] = list({
        role for role in principal.roles + ["user"]
    })
    cerbos_principal = {
        "id": f"user:{principal.user_id}",
        "roles": cerbos_roles,
        "attr": {
            "tenant_id": principal.tenant_id,
            "granted_actions": granted_actions,
        },
    }

    # 6. 查 resource attr
    resource_attr = await get_resource_attr(
        db, body.resource.type, body.resource.id,
    )
    if body.channel and body.channel.kb:
        resource_attr["kb_id"] = body.channel.kb

    # 7. Cerbos 判定
    cerbos = get_cerbos()
    try:
        result = await cerbos.check_resources(
            request_id=request_id,
            principal=cerbos_principal,
            resources=[{
                "actions": [body.action],
                "resource": {
                    "kind": body.resource.type,
                    "id": body.resource.id,
                    "attr": resource_attr,
                },
            }],
        )
    except Exception:
        record_authz_call_failed("check", "connection")
        raise  # 重新抛出，由上层 fail-closed 处理

    # 8. 三态映射
    item = result.get("results", [{}])[0]
    verdict = item.get("actions", {}).get(body.action, "EFFECT_DENY")

    decision = DECISION_MAP.get(verdict, "indeterminate")
    decision_id = result.get("cerbosCallId", request_id)

    record_authz_decision("check", decision)

    return DecisionResponse(
        decision=decision,
        decision_id=decision_id,
        reasons=[],
    )


@router.post("/check/batch", response_model=CheckBatchResponse)
@limiter.limit(f"{settings.check_batch_rate_limit}/second")
async def check_batch(
    request: Request,
    request_id: str = Header(..., alias="X-Request-Id"),
    client_id: str = Header(..., alias="X-Client-Id"),
    body: CheckBatchRequest = Body(...),
    db: AsyncSession = Depends(get_db),
) -> CheckBatchResponse:
    """批量权限判定 — 单次往返，逐资源独立决策。

    设计依据：J-14 联合契约测试 — 批量端点对 interactive-backend 开放。
    单批 ≤200 条，逐资源独立决策。
    整批传输失败/超时 → 整批判否（fail-closed）。

    流程：
    1. 解析 JWT → Principal（一次）
    2. 查询 ACL + 型一封禁（一次）
    3. 构造 Cerbos principal + 批量资源
    4. 批量调 Cerbos /api/check/resources
    5. 逐条三态映射 → 每资源独立 decision + decision_id
    """
    # 1. 解析主体（一次）
    try:
        principal = parse_principal(body.credential)
    except Exception as e:
        raise HTTPException(status_code=401, detail="Invalid credential") from e

    # 2. 型一封禁检查（一次）
    is_suspended = await check_subject_ban(
        db, principal.principals, principal.tenant_id,
    )
    if is_suspended:
        return CheckBatchResponse(
            results=[
                CheckBatchResult(
                    action=item.action,
                    resource_type=item.resource.type,
                    resource_id=item.resource.id,
                    decision="deny",
                    decision_id=request_id,
                )
                for item in body.items
            ],
            request_id=request_id,
        )

    # 3. 收集所有唯一的 (action, resource_type) 组合以查询 ACL
    # 查询所有 ACL：对每个不同的 action+resource_type 批量查询
    action_resources: dict[tuple[str, str], set[str]] = {}
    # 同时跟踪每个 resource_id 对应的 channel_kb（document 资源的 KB 归属）
    resource_channels: dict[str, str | None] = {}
    for item in body.items:
        key = (item.action, item.resource.type)
        action_resources.setdefault(key, set()).add(item.resource.id)
        if item.resource.type == "document" and item.channel and item.channel.kb:
            resource_channels[item.resource.id] = item.channel.kb

    # 聚合 granted_actions: {resource_id: [action_suffix, ...]}
    # P1-4 修复：document 资源以 kb_id 为键（与单条 check 一致）
    all_granted: dict[str, list[str]] = {}
    for (action, res_type), res_ids in action_resources.items():
        for rid in res_ids:
            kb = resource_channels.get(rid)
            pa = await resolve_granted_actions_by_principal(
                db, principal.principals, action, res_type, rid,
                channel_kb=kb,
            )
            for p_actions in pa.values():
                for a in p_actions:
                    suffix = a.split(":", 1)[-1] if ":" in a else a
                    # document 资源使用 kb_id 作为 granted_actions 键
                    # 需要在下方 Cerbos resources 循环中补充处理
                    all_granted.setdefault(rid, [])
                    if suffix not in all_granted[rid]:
                        all_granted[rid].append(suffix)

    # 4. 构造 Cerbos principal（一次）
    cerbos_roles: list[str] = list({
        role for role in principal.roles + ["user"]
    })
    cerbos_principal = {
        "id": f"user:{principal.user_id}",
        "roles": cerbos_roles,
        "attr": {
            "tenant_id": principal.tenant_id,
            "granted_actions": all_granted,
        },
    }

    # 5. 构建 Cerbos resources 数组
    cerbos_resources: list[dict] = []
    for item in body.items:
        resource_attr = await get_resource_attr(
            db, item.resource.type, item.resource.id,
        )
        if item.channel and item.channel.kb:
            resource_attr["kb_id"] = item.channel.kb

        cerbos_resources.append({
            "actions": [item.action],
            "resource": {
                "kind": item.resource.type,
                "id": item.resource.id,
                "attr": resource_attr,
            },
        })

    # 6. Cerbos 批量判定（单次往返）
    try:
        cerbos = get_cerbos()
        result = await cerbos.check_resources(
            request_id=request_id,
            principal=cerbos_principal,
            resources=cerbos_resources,
        )
    except Exception:
        # fail-closed: 整批判否
        record_authz_call_failed("check_batch", "connection")
        return CheckBatchResponse(
            results=[
                CheckBatchResult(
                    action=item.action,
                    resource_type=item.resource.type,
                    resource_id=item.resource.id,
                    decision="deny",
                    decision_id="error",
                )
                for item in body.items
            ],
            request_id=request_id,
        )

    # 7. 逐条三态映射
    results_raw = result.get("results", [])
    batch_results: list[CheckBatchResult] = []
    for i, item_result in enumerate(results_raw):
        if i >= len(body.items):
            break
        item = body.items[i]
        verdict = item_result.get("actions", {}).get(
            item.action, "EFFECT_DENY"
        )
        decision = DECISION_MAP.get(verdict, "indeterminate")

        record_authz_decision("check_batch", decision)

        batch_results.append(CheckBatchResult(
            action=item.action,
            resource_type=item.resource.type,
            resource_id=item.resource.id,
            decision=decision,
            decision_id=result.get("cerbosCallId", request_id),
        ))

    return CheckBatchResponse(
        results=batch_results,
        request_id=request_id,
    )


@router.post("/filter", response_model=FilterResponse)
@limiter.limit(f"{settings.filter_rate_limit}/second")
async def filter_items(
    request: Request,
    request_id: str = Header(..., alias="X-Request-Id"),
    client_id: str = Header(..., alias="X-Client-Id"),
    body: FilterRequest = Body(...),
    db: AsyncSession = Depends(get_db),
) -> FilterResponse:
    """批量 doc:retrieve 判定（仅 strict KB 层 3 使用）。

    对每条 item 执行 doc:retrieve 判定，批量发 Cerbos。
    失败/超时 → 整批判 deny。
    """
    # 1. 解析主体
    try:
        principal = parse_principal(body.credential)
    except Exception as e:
        raise HTTPException(status_code=401, detail="Invalid credential") from e

    # 1.5 型二封禁检查 — 对每条 item 检查是否有资源限制
    # 被型二封禁的项直接加入 denied，不发送到 Cerbos 判定
    # 设计依据：§2.3.1 restrictions 表 + J-4 联合契约测试发现
    pre_denied_ids: set[str] = set()
    for item in body.items:
        restricted_principals = await check_resource_restriction(
            db, item.resource_type, item.resource_id, principal.tenant_id,
        )
        if restricted_principals and any(
            p in principal.principals for p in restricted_principals
        ):
            pre_denied_ids.add(item.resource_id)

    # 2. 查询 ACL — 构建 granted_actions dict
    # Key: resource_id (for kb) or kb_id (for document, per Cerbos derived roles)
    # Value: list of action suffixes like ["read", "write"]
    cerbos_resources: list[dict] = []
    granted_actions: dict[str, list[str]] = {}
    kb_read_principals: set[str] = set()  # track which KBs user has kb:read on

    # First pass: collect KB-level permissions (kb:read → "read" on kb_id)
    for item in body.items:
        kb_id = item.channel.kb
        if kb_id not in kb_read_principals:
            # Check if user has kb:read on this KB
            kb_actions = await resolve_granted_actions_by_principal(
                db, principal.principals, "kb:read",
                "kb", kb_id,
            )
            # Also query doc:retrieve ACLs on specific docs (fine-grained access)
            doc_actions = await resolve_granted_actions_by_principal(
                db, principal.principals, "doc:retrieve",
                item.resource_type, item.resource_id,
                channel_kb=kb_id,
            )
            # Merge: kb:read → "read", doc:retrieve → "read" (both map to read on kb)
            has_access = False
            for p_actions in kb_actions.values():
                for a in p_actions:
                    if a.startswith("kb:"):
                        suffix = a.split(":", 1)[-1]
                        granted_actions.setdefault(kb_id, [])
                        if suffix not in granted_actions[kb_id]:
                            granted_actions[kb_id].append(suffix)
                        has_access = True
            for p_actions in doc_actions.values():
                for a in p_actions:
                    if a in ("doc:retrieve", "doc:view"):
                        # Map document actions to kb-level "read"
                        granted_actions.setdefault(kb_id, [])
                        if "read" not in granted_actions[kb_id]:
                            granted_actions[kb_id].append("read")
                        has_access = True
            if has_access:
                kb_read_principals.add(kb_id)

    # 需经 Cerbos 判定的项（排除已被型二封禁的资源）
    cerbos_items: list[dict] = []
    cerbos_idx_map: list[int] = []  # cerbos_idx → original body.items idx

    for idx, item in enumerate(body.items):
        if item.resource_id in pre_denied_ids:
            continue  # 型二封禁 → 跳过 Cerbos 判定
        resource_attr = await get_resource_attr(
            db, item.resource_type, item.resource_id,
        )
        resource_attr["kb_id"] = item.channel.kb
        resource_attr["tenant_id"] = principal.tenant_id

        cerbos_resources.append({
            "actions": ["doc:retrieve"],
            "resource": {
                "kind": item.resource_type,
                "id": item.resource_id,
                "attr": resource_attr,
            },
        })
        cerbos_idx_map.append(idx)

    # 3. 构造 Cerbos principal（roles 必须唯一）
    cerbos_roles: list[str] = list({
        role for role in principal.roles + ["user"]
    })
    cerbos_principal = {
        "id": f"user:{principal.user_id}",
        "roles": cerbos_roles,
        "attr": {
            "tenant_id": principal.tenant_id,
            "granted_actions": granted_actions,
        },
    }

    # 4. Cerbos 批量判定
    try:
        cerbos = get_cerbos()
        result = await cerbos.check_resources(
            request_id=request_id,
            principal=cerbos_principal,
            resources=cerbos_resources,
        )
    except Exception:
        # fail-closed: 整批 deny
        record_authz_call_failed("filter", "connection")
        return FilterResponse(
            allowed=[],
            denied=[item.resource_id for item in body.items],
            decision_id="error",
        )

    # 5. 分类
    allowed: list[str] = []
    denied: list[str] = list(pre_denied_ids)  # 型二封禁项直接 deny
    results = result.get("results", [])
    for i, item_result in enumerate(results):
        if i >= len(cerbos_idx_map):
            break
        body_idx = cerbos_idx_map[i]
        item = body.items[body_idx]
        verdict = item_result.get("actions", {}).get("doc:retrieve", "EFFECT_DENY")
        if verdict == "EFFECT_ALLOW":
            allowed.append(item.resource_id)
        else:
            denied.append(item.resource_id)

    record_authz_decision("filter", "allow" if allowed else "deny")

    return FilterResponse(
        allowed=allowed,
        denied=denied,
        decision_id=result.get("cerbosCallId", request_id),
    )
