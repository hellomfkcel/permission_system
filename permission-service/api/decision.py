"""决策面 API — /v1/check + /v1/check/batch + /v1/filter。"""

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
    resolve_granted_actions,
    resolve_bound_roles,
    check_subject_ban,
    check_resource_restriction,
    get_resource_acl,
    get_resource_attr,
)
from services.cerbos_adapter import get_cerbos
from app.metrics_collector import record_authz_decision, record_authz_call_failed

router = APIRouter(prefix="/v1", tags=["decision"])


# ── 三态映射 ──

DECISION_MAP: dict[str, str] = {
    "EFFECT_ALLOW": "allow",
    "EFFECT_DENY": "deny",
}
# 其他值 (EFFECT_UNSPECIFIED 等) → indeterminate


def _project_of(request: Request) -> str | None:
    """取本次请求归属的项目 ID。

    由 ClientIdValidationMiddleware 从 X-Api-Key / X-Client-Id 解析后注入。
    授权数据查询按此过滤，实现判定期的项目隔离。
    """
    return getattr(request.state, "project_id", None)


def _cerbos_roles(jwt_roles: list[str], bound_roles: set[str]) -> list[str]:
    """构造 Cerbos principal.roles。

    并入平台侧角色绑定，使匹配 roles 字段的策略也能消费角色绑定；
    默认追加 user，与派生角色的 parentRoles 约定一致。
    """
    return sorted({*jwt_roles, *bound_roles, "user"})


async def _build_resource(
    db: AsyncSession,
    principal,
    resource_type: str,
    resource_id: str,
    channel_kb: str | None,
    project_id: str | None,
) -> dict:
    """组装 Cerbos resource.attr。

    三类属性分别有唯一出处：
      业务属性（retired / is_enabled / allow_download / project_id）→ resource_registry
      通道属性（kb_id）                                            → 请求中的 channel
      ACL（acl / role_acl）                                        → acl_entries
    策略文件里不含任何用户 ID 或角色名，全部由此处注入。
    """
    attr = await get_resource_attr(db, resource_type, resource_id, project_id)
    if channel_kb:
        attr["kb_id"] = channel_kb

    acl, role_acl = await get_resource_acl(
        db, resource_type, resource_id,
        principals=principal.principals,
        principal_id=f"user:{principal.user_id}",
        project_id=project_id,
    )
    attr["acl"] = acl
    attr["role_acl"] = role_acl
    return attr


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
    2. ABAC 路：KB / 项目级授权 → principal.attr.granted_actions
    3. 查询 restrictions → 型一封禁检查
    4. ACL 路：资源实例级授权 → resource.attr.acl / role_acl
    5. 调用 Cerbos /api/check/resources（唯一决策点）
    6. 三态映射，后端无条件遵从判定结果
    """
    # 1. 解析主体
    try:
        principal = parse_principal(body.credential)
    except Exception as e:
        raise HTTPException(status_code=401, detail="Invalid credential") from e

    # 2-4. 查询授权记录 + 封禁 + 资源属性（按项目过滤）
    project_id = _project_of(request)
    channel_kb = body.channel.kb if body.channel else None

    # ABAC 路：{kb_id: [...], project_id: ["manage"]}
    # 键的取法与 rag_roles.yaml 中派生角色一致（kb 资源用 resource.id，
    # document 资源用 kb_id），项目级授权另以 project_id 为键。
    granted_actions = await resolve_granted_actions(
        db, principal.principals, body.resource.type, body.resource.id,
        channel_kb=channel_kb, project_id=project_id,
    )

    is_suspended = await check_subject_ban(
        db, principal.principals, principal.tenant_id, project_id,
    )

    if is_suspended:
        return DecisionResponse(
            decision="deny",
            decision_id=request_id,
            reasons=["subject_banned"],
        )

    # 5. 构造 Cerbos principal（JWT 角色 + 适用的角色绑定）
    bound_roles = await resolve_bound_roles(
        db, principal.principals, body.resource.type, body.resource.id,
        channel_kb=channel_kb, project_id=project_id,
    )
    cerbos_principal = {
        "id": f"user:{principal.user_id}",
        "roles": _cerbos_roles(principal.roles, bound_roles),
        "attr": {
            "tenant_id": principal.tenant_id,
            "granted_actions": granted_actions,
        },
    }

    # 6. 查 resource attr（业务属性 + ACL 路的 acl / role_acl）
    resource_attr = await _build_resource(
        db, principal, body.resource.type, body.resource.id, channel_kb, project_id,
    )

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
        record_authz_call_failed("check", "connection", project_id)
        raise  # 重新抛出，由上层 fail-closed 处理

    # 8. 三态映射
    item = result.get("results", [{}])[0]
    verdict = item.get("actions", {}).get(body.action, "EFFECT_DENY")

    decision = DECISION_MAP.get(verdict, "indeterminate")
    decision_id = result.get("cerbosCallId", request_id)

    record_authz_decision("check", decision, project_id)

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
    project_id = _project_of(request)
    is_suspended = await check_subject_ban(
        db, principal.principals, principal.tenant_id, project_id,
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

    # 3. 聚合全批的 granted_actions（键为 kb_id / project_id，与单条 check 同源）
    all_granted: dict[str, list[str]] = {}
    for item in body.items:
        item_granted = await resolve_granted_actions(
            db, principal.principals, item.resource.type, item.resource.id,
            channel_kb=(item.channel.kb if item.channel else None),
            project_id=project_id,
        )
        for key, actions in item_granted.items():
            bucket = all_granted.setdefault(key, [])
            for action in actions:
                if action not in bucket:
                    bucket.append(action)

    # 4. 构造 Cerbos principal（JWT 角色 + 批内各资源适用的角色绑定并集）
    bound_roles: set[str] = set()
    for item in body.items:
        bound_roles |= await resolve_bound_roles(
            db, principal.principals, item.resource.type, item.resource.id,
            channel_kb=(item.channel.kb if item.channel else None),
            project_id=project_id,
        )
    cerbos_principal = {
        "id": f"user:{principal.user_id}",
        "roles": _cerbos_roles(principal.roles, bound_roles),
        "attr": {
            "tenant_id": principal.tenant_id,
            "granted_actions": all_granted,
        },
    }

    # 5. 构建 Cerbos resources 数组
    cerbos_resources: list[dict] = []
    for item in body.items:
        resource_attr = await _build_resource(
            db, principal, item.resource.type, item.resource.id,
            (item.channel.kb if item.channel else None), project_id,
        )

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
        record_authz_call_failed("check_batch", "connection", project_id)
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

        record_authz_decision("check_batch", decision, project_id)

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
        project_id = _project_of(request)
    pre_denied_ids: set[str] = set()
    for item in body.items:
        restricted_principals = await check_resource_restriction(
            db, item.resource_type, item.resource_id, principal.tenant_id,
            project_id,
        )
        if restricted_principals and any(
            p in principal.principals for p in restricted_principals
        ):
            pre_denied_ids.add(item.resource_id)

    # 2. ABAC 路：按 KB / 项目作用域聚合 granted_actions
    # 文档级直授只走 ACL 路（resource.attr.acl，见 _build_resource），
    # 不折进 granted_actions，避免单篇文档的授权放大成整个 KB 的检索可见性。
    cerbos_resources: list[dict] = []
    granted_actions: dict[str, list[str]] = {}

    for item in body.items:
        item_granted = await resolve_granted_actions(
            db, principal.principals, item.resource_type, item.resource_id,
            channel_kb=item.channel.kb, project_id=project_id,
        )
        for key, actions in item_granted.items():
            bucket = granted_actions.setdefault(key, [])
            for action in actions:
                if action not in bucket:
                    bucket.append(action)

    # 需经 Cerbos 判定的项（排除已被型二封禁的资源）
    cerbos_idx_map: list[int] = []  # cerbos_idx → original body.items idx

    for idx, item in enumerate(body.items):
        if item.resource_id in pre_denied_ids:
            continue  # 型二封禁 → 跳过 Cerbos 判定
        resource_attr = await _build_resource(
            db, principal, item.resource_type, item.resource_id,
            item.channel.kb, project_id,
        )
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

    # 3. 构造 Cerbos principal（JWT 角色 + 批内各通道适用的角色绑定并集）
    bound_roles: set[str] = set()
    for item in body.items:
        bound_roles |= await resolve_bound_roles(
            db, principal.principals, item.resource_type, item.resource_id,
            channel_kb=item.channel.kb, project_id=project_id,
        )
    cerbos_principal = {
        "id": f"user:{principal.user_id}",
        "roles": _cerbos_roles(principal.roles, bound_roles),
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
        record_authz_call_failed("filter", "connection", project_id)
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

    record_authz_decision("filter", "allow" if allowed else "deny", project_id)

    return FilterResponse(
        allowed=allowed,
        denied=denied,
        decision_id=result.get("cerbosCallId", request_id),
    )
