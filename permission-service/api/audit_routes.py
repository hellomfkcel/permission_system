"""管理台 API — 审计日志查询 + 策略模拟器 + 事件重放。"""

import json
import structlog
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Query, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.config import settings
from models.change_log import PermissionChange
from models.role_definition import RoleDefinition
from services.jwt_parser import parse_principal
from services.cerbos_adapter import get_cerbos
from services.event_publisher import get_event_publisher
from api.auth_routes import get_current_admin, get_project_scope, ProjectScope, require_platform_permission
from schemas.responses import Principal

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["admin-audit"])


def _truncate(s: str, max_len: int = 200) -> str:
    """截断字符串用于展示。"""
    return s if len(s) <= max_len else s[:max_len] + "..."


# ── 响应模型 ──


class AuditEntry(BaseModel):
    id: str
    event_type: str
    resource_type: str | None
    resource_id: str | None
    kb_id: str | None
    tenant_id: str
    change_detail: dict
    version: int
    created_at: str


class SimulateRequest(BaseModel):
    """策略模拟器请求。"""
    credential: str | None = Field(None, description="JWT credential (可选)")
    principal: dict | None = Field(None, description="Principal JSON")
    action: str = Field(..., description="动词")
    resource: dict = Field(..., description="Resource JSON {kind, id, attr?}")
    project_id: str | None = Field(
        None, description="按项目过滤授权数据；不传则跨项目查询"
    )


class SimulateResult(BaseModel):
    decision: str
    resource_id: str
    action: str
    decision_id: str | None = Field(None, description="Cerbos call ID — 跨系统取证主键")
    matched_rules: list[str] = Field(default_factory=list, description="命中的策略规则说明")
    cerbos_verdict: str | None = Field(None, description="原始 Cerbos 判决 EFFECT_ALLOW/DENY/INDETERMINATE")
    principal_summary: dict = Field(default_factory=dict, description="解析后的 Principal 摘要")
    resource_summary: dict = Field(default_factory=dict, description="使用的 Resource 摘要")


# ── 端点 ──


@router.get("/audit", response_model=list[AuditEntry])
async def list_audit_entries(
    resource_type: str | None = Query(None),
    resource_id: str | None = Query(None),
    principal: str | None = Query(None, description="按主体过滤 (change_detail JSONB 查询)"),
    decision_id: str | None = Query(None, description="按 Cerbos decision_id 精确查询"),
    from_time: str | None = Query(None, description="起始时间 (ISO 8601)"),
    to_time: str | None = Query(None, description="结束时间 (ISO 8601)"),
    from_version: int | None = Query(None),
    project_id: str | None = Query(None, description="按项目 ID 过滤 (change_detail JSONB)"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    scope: ProjectScope = Depends(get_project_scope),
    _perm: None = Depends(require_platform_permission("audit_mgmt", "platform:read")),
) -> list[AuditEntry]:
    """审计日志查询 — 按条件过滤变更历史。需要管理员认证。

    新增 principal、decision_id、from_time、to_time 过滤参数。
    项目级过滤通过 change_detail JSONB 的 project_id 字段实现。
    """
    from sqlalchemy import cast, String
    from datetime import datetime

    conditions = []
    if resource_type:
        conditions.append(PermissionChange.resource_type == resource_type)
    if resource_id:
        conditions.append(PermissionChange.resource_id == resource_id)
    if from_version is not None:
        conditions.append(PermissionChange.version >= from_version)
    # principal 过滤（从 change_detail JSONB 的 principal 字段查询）
    if principal:
        conditions.append(
            PermissionChange.change_detail["principal"].astext.ilike(f"%{principal}%")
        )
    # decision_id 过滤
    if decision_id:
        conditions.append(
            PermissionChange.change_detail["decision_id"].astext == decision_id
        )
    # 时间范围过滤
    if from_time:
        try:
            ft = datetime.fromisoformat(from_time)
            conditions.append(PermissionChange.created_at >= ft)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid from_time format. Use ISO 8601.")
    if to_time:
        try:
            tt = datetime.fromisoformat(to_time)
            conditions.append(PermissionChange.created_at <= tt)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid to_time format. Use ISO 8601.")

    # 项目范围过滤
    if project_id:
        conditions.append(
            PermissionChange.change_detail["project_id"].astext == project_id
        )
    elif not scope.is_platform_admin and scope.project_ids is not None:
        # 非 platform_admin → 仅显示所属项目的审计日志
        if len(scope.project_ids) > 0:
            conditions.append(
                PermissionChange.change_detail["project_id"].astext.in_(scope.project_ids)
            )

    stmt = (
        select(PermissionChange)
        .where(*conditions)
        .order_by(desc(PermissionChange.version))
        .limit(limit)
    )
    result = await db.execute(stmt)
    entries = result.scalars().all()

    return [
        AuditEntry(
            id=str(e.id),
            event_type=e.event_type,
            resource_type=e.resource_type,
            resource_id=e.resource_id,
            kb_id=e.kb_id,
            tenant_id=e.tenant_id,
            change_detail=e.change_detail,
            version=e.version,
            created_at=e.created_at.isoformat() if e.created_at else "",
        )
        for e in entries
    ]


@router.post("/simulate", response_model=SimulateResult)
async def simulate(
    body: SimulateRequest,
    admin: Principal = Depends(get_current_admin),
    _perm: None = Depends(require_platform_permission("playground", "platform:read")),
) -> SimulateResult:
    """策略模拟器 — 走真实 ACL + 角色绑定 + 封禁查询链路的权限判定。需要管理员认证。

    与 /v1/check 使用完全相同的组装链路（resolve_granted_actions +
    get_resource_acl + check_subject_ban + get_resource_attr），
    确保模拟结果反映实际权限数据，而非仅原始 Cerbos 策略。

    """
    from app.database import async_session
    from services.acl_resolver import (
        resolve_granted_actions,
        check_subject_ban,
        get_resource_acl,
        get_resource_attr,
    )

    cerbos = get_cerbos()

    resource_kind = body.resource.get("kind", "kb")
    resource_id = body.resource.get("id", "test")
    resource_attr = body.resource.get("attr", {}) or {}

    # ── 解析主体身份 ──
    if body.credential:
        parsed = parse_principal(body.credential)
        user_id = parsed.user_id
        principals = parsed.principals
        roles = list(parsed.roles)
        tenant_id = parsed.tenant_id
    elif body.principal:
        pid = body.principal.get("id", "user:anonymous")
        user_id = pid.replace("user:", "") if pid.startswith("user:") else pid
        principals = [pid]
        roles = list(body.principal.get("roles", ["user"]))
        tenant_id = (body.principal.get("attr", {}) or {}).get("tenant_id", "tenant-dev")
    else:
        user_id = "anonymous"
        principals = ["user:anonymous"]
        roles = ["user"]
        tenant_id = "tenant-dev"

    # ── 查询真实授权记录 → granted_actions（ABAC）+ acl / role_acl（ACL）──
    channel_kb = resource_attr.get("kb_id")
    async with async_session() as db:
        granted_actions = await resolve_granted_actions(
            db, list(principals), resource_kind, resource_id,
            channel_kb=channel_kb, project_id=body.project_id,
        )

        # 查询型一封禁
        is_banned = await check_subject_ban(
            db, list(principals), tenant_id, body.project_id,
        )

        # 查询资源属性
        res_attr = await get_resource_attr(
            db, resource_kind, resource_id, body.project_id,
        )
        acl, role_acl = await get_resource_acl(
            db, resource_kind, resource_id,
            principals=list(principals),
            principal_id=f"user:{user_id}",
            project_id=body.project_id,
        )
        res_attr["acl"] = acl
        res_attr["role_acl"] = role_acl
        # 合并用户提供的 attr（用户输入覆盖 DB 查询）
        merged_attr = {**res_attr, **resource_attr}

    # ── 构造 Cerbos principal（含真实 granted_actions）──
    cerbos_principal = {
        "id": f"user:{user_id}",
        "roles": roles,
        "attr": {
            "tenant_id": tenant_id,
            "granted_actions": granted_actions,
        },
    }

    # ── 型一封禁 → 直接 deny ──
    if is_banned:
        return SimulateResult(
            decision="deny",
            resource_id=resource_id,
            action=body.action,
            decision_id=None,
            matched_rules=[f"型一封禁: {', '.join(principals)} 已被全局封禁（suspended）"],
            cerbos_verdict="PRE_DENY",
            principal_summary={
                "id": cerbos_principal["id"],
                "roles": roles,
                "granted_actions": granted_actions,
                "banned": True,
            },
            resource_summary={
                "kind": resource_kind,
                "id": resource_id,
                "attr_keys": list(merged_attr.keys()),
            },
        )

    # ── 调 Cerbos PDP ──
    result = await cerbos.check_resources(
        request_id="simulate",
        principal=cerbos_principal,
        resources=[{
            "actions": [body.action],
            "resource": {
                "kind": resource_kind,
                "id": resource_id,
                "attr": merged_attr,
            },
        }],
    )

    # 解析结果
    item = result.get("results", [{}])[0]
    verdict = item.get("actions", {}).get(body.action, "EFFECT_DENY")

    decision_map = {"EFFECT_ALLOW": "allow", "EFFECT_DENY": "deny"}

    # ── 分析命中的策略规则 ──
    matched_rules: list[str] = []
    meta = item.get("meta", {})
    effective_derived_roles = meta.get("effectiveDerivedRoles", [])

    if effective_derived_roles:
        matched_rules.append(f"派生角色命中: {', '.join(effective_derived_roles)}")
    elif "system_admin" in roles:
        matched_rules.append("派生角色命中: admin (system_admin → admin → unconditional allow)")
    else:
        matched_rules.append("未命中派生角色（检查 granted_actions）")

    if verdict == "EFFECT_ALLOW":
        matched_rules.append(f"资源策略: {resource_kind}.yaml → action={body.action} → ALLOW")
    elif verdict == "EFFECT_DENY":
        matched_rules.append(f"资源策略: {resource_kind}.yaml → action={body.action} → DENY")
    else:
        matched_rules.append(f"资源策略: action={body.action} → {verdict}")

    obligations = item.get("obligations", {})
    if obligations:
        matched_rules.append(f"义务(obligations): {_truncate(str(obligations))}")

    return SimulateResult(
        decision=decision_map.get(verdict, "indeterminate"),
        resource_id=resource_id,
        action=body.action,
        decision_id=result.get("cerbosCallId"),
        matched_rules=matched_rules,
        cerbos_verdict=verdict,
        principal_summary={
            "id": cerbos_principal["id"],
            "roles": roles,
            "granted_actions": granted_actions,
        },
        resource_summary={
            "kind": resource_kind,
            "id": resource_id,
            "attr_keys": list(merged_attr.keys()),
            "db_attrs": {k: v for k, v in res_attr.items() if v is not None},
        },
    )


# ══════════════════════════════════════════════════════════════
# 事件重放 / 补消费端点
# ══════════════════════════════════════════════════════════════


class ReplayRequest(BaseModel):
    """事件重放请求。"""
    from_version: int = Field(..., ge=1, description="起始版本号（含）")
    to_version: int | None = Field(None, description="结束版本号（含），默认到最新")
    limit: int = Field(500, ge=1, le=5000, description="最大重放条数")
    dry_run: bool = Field(True, description="dry_run=True 仅列出事件不发布")


class ReplayResult(BaseModel):
    from_version: int
    to_version: int
    total_events: int
    replayed: int = 0
    skipped: int = 0
    dry_run: bool = True
    events: list[dict] = Field(default_factory=list, description="重放的事件摘要列表")


@router.post("/events/replay", response_model=ReplayResult)
async def replay_events(
    body: ReplayRequest,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(get_current_admin),
    _perm: None = Depends(require_platform_permission("audit_mgmt", "platform:write")),
) -> ReplayResult:
    """事件重放 / 补消费 — 从指定版本开始重新发布事件到 Redis。需要管理员认证。

    使用场景：
    - RAG 侧事件订阅断开后补消费
    - 对账发现 stamp_drift 后批量修复
    - 权限变更事件丢失后的补偿

    dry_run=True（默认）：仅列出事件，不实际发布。
    dry_run=False：发布到 Redis Pub/Sub "visibility_changed" 频道。
    """
    # 查询指定版本范围内的事件
    conditions = [PermissionChange.version >= body.from_version]
    if body.to_version is not None:
        conditions.append(PermissionChange.version <= body.to_version)

    stmt = (
        select(PermissionChange)
        .where(*conditions)
        .order_by(PermissionChange.version)
        .limit(body.limit)
    )
    result = await db.execute(stmt)
    entries = result.scalars().all()

    to_version = body.to_version or (
        entries[-1].version if entries else body.from_version
    )

    events: list[dict] = []
    replayed = 0
    skipped = 0

    publisher = get_event_publisher()

    for entry in entries:
        event = {
            "version": entry.version,
            "event_type": entry.event_type,
            "resource_type": entry.resource_type,
            "resource_id": entry.resource_id,
            "tenant_id": entry.tenant_id,
            "created_at": entry.created_at.isoformat() if entry.created_at else "",
            "change_detail": entry.change_detail,
        }

        if not body.dry_run:
            try:
                await publisher.publish_event(
                    change_id=entry.id,
                    version=entry.version,
                    tenant_id=entry.tenant_id,
                    resource_type=entry.resource_type or "unknown",
                    resource_id=entry.resource_id or "",
                    event_type=entry.event_type or "VisibilityChanged",
                    kb_id=entry.kb_id,
                    change_detail=entry.change_detail,
                    unmounted="unmounted" in str(entry.change_detail).lower(),
                )
                replayed += 1
            except Exception:
                skipped += 1
            events.append({**event, "replayed": True})
        else:
            events.append(event)

    return ReplayResult(
        from_version=body.from_version,
        to_version=to_version,
        total_events=len(entries),
        replayed=replayed,
        skipped=skipped,
        dry_run=body.dry_run,
        events=events[:100],  # 最多返回 100 条摘要（避免响应过大）
    )


# ══════════════════════════════════════════════════════════════
# 策略文件内容服务
# ══════════════════════════════════════════════════════════════


class PolicyFileResponse(BaseModel):
    path: str
    name: str
    yaml_content: str


class PolicyWriteRequest(BaseModel):
    """策略文件写入请求 — 含 YAML 内容和变更说明。"""
    yaml_content: str = Field(..., description="策略 YAML 内容", min_length=1)
    message: str = Field("", description="变更说明（用于审计）")


class PolicyWriteResult(BaseModel):
    path: str
    result: str  # "created" | "updated"
    message: str


def _get_policy_root(project_id: str | None = None) -> Path:
    """获取 Cerbos 策略文件根目录。

    支持按 project 隔离的策略目录。
    - project_id=None → 返回 policies/ 根目录（向后兼容）
    - project_id="rag-v14" → 返回 policies/rag-v14/
    """
    import os
    base = Path(__file__).parent.parent.parent / "cerbos" / "policies"
    if project_id:
        return base / project_id
    return base


def _validate_policy_yaml(yaml_content: str) -> tuple[bool, str]:
    """验证策略 YAML 基本结构。

    支持单文档和多文档（--- 分隔）YAML 格式。
    Cerbos 常用多文档格式：第一个文档含 apiVersion，后续文档含实际规则。

    Returns:
        (is_valid, error_message)
    """
    try:
        import yaml
        # 使用 safe_load_all 支持多文档 YAML
        docs = list(yaml.safe_load_all(yaml_content))
        # 过滤掉 None（空文档）
        parsed_docs = [d for d in docs if d is not None]
        if not parsed_docs:
            return False, "YAML 内容为空或无法解析"
        # 合并所有文档：将多文档的键合并到一个 dict 中进行校验
        merged: dict = {}
        for d in parsed_docs:
            if isinstance(d, dict):
                merged.update(d)
        if not merged:
            return False, "YAML 根节点必须是字典/mapping"
        # 检查必要字段
        if "apiVersion" not in merged:
            return False, "缺少 apiVersion 字段"
        if "derivedRoles" not in merged and "resourcePolicy" not in merged:
            return False, "必须包含 derivedRoles 或 resourcePolicy 定义"
        return True, ""
    except ImportError:
        # yaml 库不可用时跳过验证
        return True, ""
    except Exception as e:
        return False, f"YAML 解析错误: {str(e)[:200]}"


async def _sync_policy_roles_to_db(project_id: str) -> dict[str, int]:
    """策略文件写入后，把新出现的角色建档到 role_definitions 表。

    只同步档案信息（名称、激活角色、项目归属），不写权限 —— 角色能执行哪些动作
    由策略文件回答。建档的目的是让新角色在管理台可见、可写描述、可统计绑定数。

    Returns:
        {"created": N, "updated": N}
    """
    from app.database import async_session
    from sqlalchemy import select
    from services.cerbos_policy_parser import (
        PLATFORM_NAMESPACE,
        parse_permissions_matrix as _parse_matrix,
    )

    created = 0
    updated = 0

    try:
        matrix = _parse_matrix(project_id)
    except Exception as e:
        logger.warning("policy_role_sync_parse_failed", project_id=project_id, error=str(e)[:200])
        return {"created": 0, "updated": 0}

    if not matrix.get("roles"):
        return {"created": 0, "updated": 0}

    async with async_session() as db:
        for role_info in matrix["roles"]:
            name = role_info.get("name", "")
            if not name:
                continue
            # 仅同步 Cerbos 派生角色（跳过 user/system_admin 等身份角色，
            # 它们的档案由 Keycloak / 平台种子数据维护）
            if role_info.get("kind") != "derived":
                continue

            parent_roles = role_info.get("activated_by", [])
            # 平台层命名空间（"platform"）与无归属（""）的角色不属于任何项目
            parsed_project = role_info.get("project_id") or ""
            role_project_id = (
                None
                if parsed_project in ("", PLATFORM_NAMESPACE)
                else parsed_project
            )

            existing = await db.scalar(
                select(RoleDefinition).where(RoleDefinition.name == name)
            )

            if existing:
                existing.parent_keycloak_roles = parent_roles
                existing.project_id = role_project_id
                updated += 1
            else:
                new_role = RoleDefinition(
                    name=name,
                    description="Cerbos 派生角色（由策略文件自动建档）",
                    parent_keycloak_roles=parent_roles,
                    is_system=True,
                    project_id=role_project_id,
                )
                db.add(new_role)
                created += 1

        await db.commit()

    logger.info("policy_role_sync_complete", project_id=project_id, created=created, updated=updated)
    return {"created": created, "updated": updated}


def _validate_policy_path(policy_path: str) -> tuple[bool, str]:
    """安全校验策略文件路径 — 防止路径遍历攻击。

    Returns:
        (is_safe, sanitized_path)
    """
    import os
    if os.path.isabs(policy_path):
        return False, ""
    # 禁止 .. 路径遍历
    safe = os.path.normpath(policy_path)
    if safe.startswith("..") or safe.startswith("/"):
        return False, ""
    # 必须以 .yaml 或 .yml 结尾
    if not (safe.endswith(".yaml") or safe.endswith(".yml")):
        return False, ""
    return True, safe


# ══════════════════════════════════════════════════════════════
# 策略文件的命名空间访问控制
#
# 策略目录本身就是权限模型的一部分（docs/permission_model_v2.md §1）：
#   policies/platform/   平台层，全局唯一
#   policies/{project}/  项目层
# 因此"能不能改这个文件"必须按命名空间判，而不是只看有没有 policy_mgmt 写权限 ——
# 否则某个项目的管理员可以改写平台层策略，或改写别的项目的策略。
# ══════════════════════════════════════════════════════════════


def _target_namespace(project_id: str | None, safe_path: str) -> str:
    """推导写入/删除目标所在的命名空间。

    project_id 指定 → 该项目；否则取路径首段（platform/... → "platform"），
    首段不是目录时视为策略根目录（""）。
    """
    from pathlib import Path

    if project_id:
        return project_id
    parts = Path(safe_path).parts
    return parts[0] if len(parts) >= 3 else ""


def _assert_namespace_access(scope: ProjectScope, namespace: str) -> None:
    """校验管理员是否有权操作该命名空间。

    平台层与策略根目录对所有项目生效，只有平台管理员可写；
    项目命名空间要求该项目在管理员的项目范围内。
    """
    from services.cerbos_policy_parser import PLATFORM_NAMESPACE

    if namespace in ("", PLATFORM_NAMESPACE):
        if not scope.is_platform_admin:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Only platform administrators may modify platform-layer policies; "
                    "they apply to every project."
                ),
            )
        return
    if not scope.can_access(namespace):
        raise HTTPException(
            status_code=403, detail=f"No access to project '{namespace}'"
        )


def _assert_policy_layer(yaml_content: str, namespace: str, target: Path) -> None:
    """校验策略内容与目标命名空间对齐，且不与已有策略撞模块 ID。

    Cerbos 的模块 ID 是全局的：资源策略按 (resource, version, scope)、
    派生角色集合按 name。目录层级不构成命名空间，所以同名模块写在两个项目目录下
    就是同一个模块的两份定义，加载结果不确定。

    两条规则：
      1. 平台层资源（platform / project_permission）只能写在 platform/ 命名空间；
         项目目录里出现它们，等于用项目级写权限改平台层准入。
      2. 新写入的模块 ID 不得与其他文件已声明的重复。
    """
    import yaml as _yaml
    from services.cerbos_policy_parser import PLATFORM_NAMESPACE, get_resource_layer

    try:
        documents = [d for d in _yaml.safe_load_all(yaml_content) if isinstance(d, dict)]
    except Exception:
        return  # 结构校验由 _validate_policy_yaml 负责

    for doc in documents:
        policy = doc.get("resourcePolicy") or doc.get("resource_policy")
        if isinstance(policy, dict):
            resource = policy.get("resource", "")
            if (
                resource
                and get_resource_layer(resource) == "platform"
                and namespace != PLATFORM_NAMESPACE
            ):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"'{resource}' is a platform-layer resource and may only be "
                        f"defined under policies/{PLATFORM_NAMESPACE}/; "
                        f"writing it into '{namespace or 'the policy root'}' would "
                        "duplicate the Cerbos module ID."
                    ),
                )

    _assert_no_module_id_conflict(documents, target)


def _assert_no_module_id_conflict(documents: list[dict], target: Path) -> None:
    """新内容声明的模块 ID 不得已被其他策略文件占用。"""
    import yaml as _yaml
    from pathlib import Path as _Path

    incoming_resources: set[tuple[str, str, str]] = set()
    incoming_sets: set[str] = set()
    for doc in documents:
        policy = doc.get("resourcePolicy") or doc.get("resource_policy")
        if isinstance(policy, dict) and policy.get("resource"):
            incoming_resources.add((
                policy["resource"],
                policy.get("version", "default"),
                policy.get("scope", ""),
            ))
        derived = doc.get("derivedRoles") or doc.get("derived_roles")
        if isinstance(derived, dict) and derived.get("name"):
            incoming_sets.add(derived["name"])

    if not incoming_resources and not incoming_sets:
        return

    root = _get_policy_root()
    try:
        resolved_target = target.resolve()
    except OSError:
        resolved_target = target

    for existing in root.rglob("*.y*ml"):
        if ".versions" in existing.parts:
            continue
        if existing.parent.name not in ("derived_roles", "resource_policies"):
            continue
        try:
            if existing.resolve() == resolved_target:
                continue
            docs = [
                d for d in _yaml.safe_load_all(existing.read_text(encoding="utf-8"))
                if isinstance(d, dict)
            ]
        except Exception:
            continue
        for doc in docs:
            policy = doc.get("resourcePolicy") or doc.get("resource_policy")
            if isinstance(policy, dict) and policy.get("resource"):
                key = (
                    policy["resource"],
                    policy.get("version", "default"),
                    policy.get("scope", ""),
                )
                if key in incoming_resources:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Resource policy {key} is already defined in "
                            f"{existing.relative_to(root)}. Cerbos module IDs are "
                            "global; edit that file instead."
                        ),
                    )
            derived = doc.get("derivedRoles") or doc.get("derived_roles")
            if isinstance(derived, dict) and derived.get("name") in incoming_sets:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Derived role set '{derived['name']}' is already defined in "
                        f"{existing.relative_to(root)}. Pick a project-scoped name."
                    ),
                )


@router.get("/policies", response_model=list[PolicyFileResponse])
async def list_policy_files(
    admin: Principal = Depends(get_current_admin),
    scope: ProjectScope = Depends(get_project_scope),
    _perm: None = Depends(require_platform_permission("policy_mgmt", "platform:read")),
    project_id: str | None = Query(None, description="按项目过滤策略（可选，不传=全部可见项目）"),
) -> list[PolicyFileResponse]:
    """列出 Cerbos 活跃策略文件及其 YAML 内容。需要管理员认证。

    项目模式（传 project_id）：仅列出该项目的策略文件，需在管理员项目范围内。
    平台模式（不传 project_id）：列出管理员可见范围内的策略文件。平台管理员看
    全部；项目级管理员只看自己项目的目录，外加平台层与根目录下的公共策略
    （对所有项目生效，可读不可写）。

    排除 .versions/ 目录（版本历史快照，非活跃策略）。
    """
    from pathlib import Path
    from services.cerbos_policy_parser import PLATFORM_NAMESPACE

    policy_root = _get_policy_root()

    if project_id:
        _assert_namespace_access(scope, project_id)
        allowed: set[str] | None = {project_id}
    elif scope.is_platform_admin:
        allowed = None  # 全部命名空间
    else:
        # 自己的项目 + 全局命名空间（平台层 / 根目录），只读
        allowed = set(scope.project_ids or set()) | {"", PLATFORM_NAMESPACE}

    result: list[PolicyFileResponse] = []
    for yaml_file in policy_root.rglob("*.yaml"):
        rel_path = yaml_file.relative_to(policy_root)
        rel_str = str(rel_path)
        if ".versions" in Path(rel_str).parts:
            continue
        namespace = _target_namespace(None, rel_str)
        if allowed is not None and namespace not in allowed:
            continue
        try:
            content = yaml_file.read_text(encoding="utf-8")
        except Exception:
            content = f"# Error reading {rel_str}"
        parent = rel_path.parent.name if str(rel_path.parent) != "." else ""
        name = f"{parent}/{rel_path.stem}" if parent else rel_path.stem
        # 项目模式下路径相对该项目目录，保持既有契约
        display_path = (
            str(Path(rel_str).relative_to(project_id)) if project_id else rel_str
        )
        result.append(PolicyFileResponse(
            path=display_path, name=name, yaml_content=content,
        ))
    result.sort(key=lambda p: p.path)
    return result


@router.put("/policies/{policy_path:path}", response_model=PolicyWriteResult)
async def write_policy_file(
    policy_path: str,
    body: PolicyWriteRequest,
    principal: Principal = Depends(get_current_admin),
    scope: ProjectScope = Depends(get_project_scope),
    _perm: None = Depends(require_platform_permission("policy_mgmt", "platform:write")),
    project_id: str | None = Query(None, description="目标项目 ID（写入 policies/{project_id}/ 目录）"),
) -> PolicyWriteResult:
    """创建或更新 Cerbos 策略文件。

    安全措施：
    1. 路径遍历防护（禁止 .. 和绝对路径）
    2. YAML 结构校验
    3. 仅允许 .yaml/.yml 扩展名
    4. 变更审计日志

    project_id 为 None 时写入 policies/ 根目录（向后兼容）；
    提供 project_id 时写入 policies/{project_id}/ 子目录。
    """
    # 路径安全校验
    is_safe, safe_path = _validate_policy_path(policy_path)
    if not is_safe:
        raise HTTPException(
            status_code=422,
            detail="Invalid policy path. Must be a relative path ending with .yaml or .yml",
        )

    # YAML 内容校验
    is_valid, error_msg = _validate_policy_yaml(body.yaml_content)
    if not is_valid:
        raise HTTPException(status_code=422, detail=f"Invalid policy YAML: {error_msg}")

    # 命名空间访问控制 + 内容与层级对齐 + 模块 ID 冲突检查
    namespace = _target_namespace(project_id, safe_path)
    _assert_namespace_access(scope, namespace)

    # 确保目录存在（按项目隔离）
    policy_root = _get_policy_root(project_id)
    target_file = policy_root / safe_path
    _assert_policy_layer(body.yaml_content, namespace, target_file)
    target_file.parent.mkdir(parents=True, exist_ok=True)

    # 检查是新文件还是更新
    is_new = not target_file.exists()

    # 自动保存旧版本快照（策略版本管理）
    if not is_new:
        _snapshot_policy_version(policy_root, safe_path, target_file)

    # 写入策略文件
    try:
        target_file.write_text(body.yaml_content, encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write policy file: {str(e)}")

    # Cerbos PDP 自动热加载（watchForChanges: true），无需手动触发

    # 策略文件写入后失效角色动作映射缓存，
    # 确保下次权限判定使用最新的 Cerbos YAML 解析结果。
    from services.cerbos_policy_parser import invalidate_role_actions_cache
    invalidate_role_actions_cache()

    # 自动同步角色到数据库（使角色管理、角色绑定页面可发现 Cerbos YAML 中定义的角色）
    if project_id:
        await _sync_policy_roles_to_db(project_id)

    logger.info(
        "policy_file_written",
        path=safe_path,
        project_id=project_id,
        action="created" if is_new else "updated",
        principal=principal.user_id,
        message=body.message,
    )

    return PolicyWriteResult(
        path=safe_path,
        result="created" if is_new else "updated",
        message=body.message or f"Policy {safe_path} {'created' if is_new else 'updated'}",
    )


class PolicyUploadResult(BaseModel):
    path: str
    name: str
    result: str  # "created" | "updated"
    message: str


@router.post("/policies/upload", response_model=PolicyUploadResult)
async def upload_policy_file(
    file: UploadFile = File(..., description="策略 YAML 文件 (.yaml/.yml)"),
    project_id: str = Form(..., description="目标项目 ID"),
    policy_subpath: str = Form("", description="可选: 子路径（如 resource_policies/kb.yaml），为空则自动推导"),
    message: str = Form("", description="变更说明"),
    principal: Principal = Depends(get_current_admin),
    scope: ProjectScope = Depends(get_project_scope),
    _perm: None = Depends(require_platform_permission("policy_mgmt", "platform:write")),
) -> PolicyUploadResult:
    """上传 Cerbos 策略文件到指定项目目录。

    流程：
    1. 读取上传文件内容（UTF-8）
    2. 校验 YAML 结构（apiVersion 等必要字段）
    3. 自动推导子目录（derived_roles 或 resource_policies）
    4. 写入 cerbos/policies/{project_id}/{subpath}/{filename}
    5. 失效角色动作映射缓存

    policy_subpath 为空时，根据 YAML 内容自动推导：
    - 包含 derivedRoles → derived_roles/{filename}
    - 包含 resourcePolicy → resource_policies/{filename}
    """
    # 校验文件扩展名
    filename = file.filename or "upload.yaml"
    if not (filename.endswith(".yaml") or filename.endswith(".yml")):
        raise HTTPException(
            status_code=422,
            detail="仅支持 .yaml 或 .yml 后缀的策略文件",
        )

    # 读取文件内容
    try:
        content_bytes = await file.read()
        yaml_content = content_bytes.decode("utf-8-sig")  # 支持 BOM
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="文件必须是 UTF-8 编码")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取文件失败: {str(e)[:200]}")

    if not yaml_content.strip():
        raise HTTPException(status_code=422, detail="文件内容为空")

    # 校验 YAML 结构
    is_valid, error_msg = _validate_policy_yaml(yaml_content)
    if not is_valid:
        raise HTTPException(status_code=422, detail=f"策略文件校验失败: {error_msg}")

    # 推导子目录路径
    if policy_subpath.strip():
        safe_subpath = policy_subpath.strip().lstrip("/")
        # 确保以 .yaml/.yml 结尾
        if not (safe_subpath.endswith(".yaml") or safe_subpath.endswith(".yml")):
            safe_subpath = f"{safe_subpath.rstrip('/')}/{filename}"
    else:
        # 根据 YAML 内容类型推导子目录
        import yaml as _yaml_lib
        try:
            docs = list(_yaml_lib.safe_load_all(yaml_content))
            parsed = next((d for d in docs if d is not None), {})
            if not isinstance(parsed, dict):
                parsed = {}
        except Exception:
            parsed = {}
        if isinstance(parsed, dict) and "derivedRoles" in parsed:
            subdir = "derived_roles"
        else:
            subdir = "resource_policies"
        safe_subpath = f"{subdir}/{filename}"

    # 路径安全校验
    is_safe, safe_path = _validate_policy_path(safe_subpath)
    if not is_safe:
        raise HTTPException(
            status_code=422,
            detail=f"推导的路径无效: {safe_subpath}",
        )

    # 命名空间访问控制 + 内容与层级对齐 + 模块 ID 冲突检查
    namespace = _target_namespace(project_id, safe_path)
    _assert_namespace_access(scope, namespace)

    # 写入项目目录
    policy_root = _get_policy_root(project_id)
    target_file = policy_root / safe_path
    _assert_policy_layer(yaml_content, namespace, target_file)
    target_file.parent.mkdir(parents=True, exist_ok=True)

    is_new = not target_file.exists()

    try:
        target_file.write_text(yaml_content, encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"写入策略文件失败: {str(e)}")

    # 失效缓存
    from services.cerbos_policy_parser import invalidate_role_actions_cache
    invalidate_role_actions_cache()

    # 自动同步角色到数据库
    await _sync_policy_roles_to_db(project_id)

    logger.info(
        "policy_file_uploaded",
        path=safe_path,
        project_id=project_id,
        filename=filename,
        action="created" if is_new else "updated",
        principal=principal.user_id,
    )

    parent = str(Path(safe_path).parent.name) if str(Path(safe_path).parent) != "." else ""
    name = f"{parent}/{Path(safe_path).stem}" if parent else Path(safe_path).stem

    return PolicyUploadResult(
        path=safe_path,
        name=name,
        result="created" if is_new else "updated",
        message=message or f"Policy {safe_path} {'created' if is_new else 'updated'}",
    )


@router.delete("/policies/{policy_path:path}", response_model=PolicyWriteResult)
async def delete_policy_file(
    policy_path: str,
    principal: Principal = Depends(get_current_admin),
    scope: ProjectScope = Depends(get_project_scope),
    _perm: None = Depends(require_platform_permission("policy_mgmt", "platform:write")),
    project_id: str | None = Query(None, description="目标项目 ID"),
) -> PolicyWriteResult:
    """删除 Cerbos 策略文件。

    安全措施：路径遍历防护 + 仅允许 .yaml/.yml 文件。
    """
    is_safe, safe_path = _validate_policy_path(policy_path)
    if not is_safe:
        raise HTTPException(
            status_code=422,
            detail="Invalid policy path. Must be a relative path ending with .yaml or .yml",
        )

    _assert_namespace_access(scope, _target_namespace(project_id, safe_path))

    policy_root = _get_policy_root(project_id)
    target_file = policy_root / safe_path

    if not target_file.exists():
        raise HTTPException(status_code=404, detail=f"Policy file not found: {safe_path}")

    try:
        target_file.unlink()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete policy file: {str(e)}")

    # 策略文件删除后失效角色动作映射缓存。
    from services.cerbos_policy_parser import invalidate_role_actions_cache
    invalidate_role_actions_cache()

    logger.info(
        "policy_file_deleted",
        path=safe_path,
        principal=principal.user_id,
    )

    return PolicyWriteResult(
        path=safe_path,
        result="deleted",
        message=f"Policy {safe_path} deleted",
    )


# ══════════════════════════════════════════════════════════════
# 策略部署状态检查
# ══════════════════════════════════════════════════════════════


class DeployStatusResult(BaseModel):
    status: str  # "healthy" | "degraded" | "unknown"
    policies_count: int = 0
    cerbos_version: str = ""
    message: str = ""
    last_deployed_at: str | None = None


@router.get("/policies/deploy-status", response_model=DeployStatusResult)
async def get_deploy_status(
    principal: Principal = Depends(get_current_admin),
    _perm: None = Depends(require_platform_permission("policy_mgmt", "platform:read")),
) -> DeployStatusResult:
    """查询 Cerbos PDP 策略部署状态（P2-6 新增）。

    混合校验方案（方案 C）：
    1. 文件系统统计策略 YAML 文件数（与 PDP 加载的数据源一致）
    2. HTTP 探活 Cerbos PDP（GET / 返回 200 = PDP 运行中）
    3. 综合判定健康状态

    Cerbos PDP HTTP API 不存在 /api/policies 端点（仅 gRPC Admin API 有此能力），
    因此改用文件计数 + HTTP 探活的混合校验方案。
    """
    import httpx
    from pathlib import Path

    status = "unknown"
    message = ""
    cerbos_version = ""
    policies_count = 0

    # ── 1. 文件系统统计策略文件数 ──
    # Cerbos 配置 storage.driver=disk + watchForChanges=true，
    # 策略文件即 PDP 加载的权威数据源。
    # 排除 .versions/ 目录（版本历史快照，非活跃策略）。
    policy_root = _get_policy_root()
    try:
        yaml_files = list(policy_root.rglob("*.yaml")) + list(policy_root.rglob("*.yml"))
        # 去重 + 排除 .versions/ 目录
        active_files = set()
        for f in yaml_files:
            resolved = f.resolve()
            if ".versions" not in resolved.parts:
                active_files.add(resolved)
        policies_count = len(active_files)
    except Exception:
        policies_count = 0  # 文件系统不可达时回退

    # ── 2. HTTP 探活 Cerbos PDP ──
    pdp_base = settings.cerbos_pdp_url.rstrip("/")
    pdp_reachable = False
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(f"{pdp_base}/")
            if resp.status_code == 200:
                pdp_reachable = True
                # 尝试从响应中提取 Cerbos 版本（rapidoc HTML 或 JSON）
                ct = resp.headers.get("content-type", "")
                if "json" in ct:
                    try:
                        ver_data = resp.json()
                        cerbos_version = str(ver_data.get("version", ""))
                    except Exception:
                        pass
        except Exception:
            pdp_reachable = False

    # ── 3. 综合判定 ──
    if pdp_reachable and policies_count > 0:
        status = "healthy"
        message = f"{policies_count} policies loaded by Cerbos PDP"
    elif pdp_reachable and policies_count == 0:
        status = "degraded"
        message = "Cerbos PDP is running but no policy files found — check cerbos/policies/ directory"
    elif not pdp_reachable:
        status = "unknown"
        message = "Cannot reach Cerbos PDP — service may be starting or unreachable"

    return DeployStatusResult(
        status=status,
        policies_count=policies_count,
        cerbos_version=cerbos_version,
        message=message,
        last_deployed_at=None,  # Cerbos 文件监控自动热加载，无精确部署时间
    )


# ══════════════════════════════════════════════════════════════
# YAML 语法校验
# ══════════════════════════════════════════════════════════════


class ValidatePolicyRequest(BaseModel):
    yaml_content: str = Field(..., description="待校验的 YAML 策略内容")


class ValidatePolicyResult(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    has_api_version: bool = False
    has_resource_policy: bool = False
    actions_count: int = 0


@router.post("/policies/validate", response_model=ValidatePolicyResult)
async def validate_policy_yaml(
    body: ValidatePolicyRequest,
    principal: Principal = Depends(get_current_admin),
    _perm: None = Depends(require_platform_permission("policy_mgmt", "platform:read")),
) -> ValidatePolicyResult:
    """校验 Cerbos 策略 YAML 语法和基本结构（P2-2 新增）。

    校验项：
    1. YAML 语法合法性
    2. apiVersion 字段存在性
    3. derivedRoles 或 resourcePolicy 字段存在性
    4. resourcePolicy 的 rules/actions 完整性
    5. EFFECT_ALLOW/EFFECT_DENY 合法性

    """
    import yaml as yaml_lib

    errors: list[str] = []
    warnings: list[str] = []
    has_api_version = False
    has_resource_policy = False
    actions_count = 0

    # 1. YAML 语法校验
    try:
        parsed = yaml_lib.safe_load(body.yaml_content)
    except yaml_lib.YAMLError as e:
        return ValidatePolicyResult(
            valid=False,
            errors=[f"YAML syntax error: {str(e)}"],
        )

    if parsed is None:
        return ValidatePolicyResult(
            valid=False,
            errors=["Empty YAML document"],
        )

    if not isinstance(parsed, dict):
        return ValidatePolicyResult(
            valid=False,
            errors=["YAML root must be a mapping (dictionary)"],
        )

    # 2. apiVersion 检查
    if "apiVersion" in parsed:
        api_ver = parsed["apiVersion"]
        if api_ver != "api.cerbos.dev/v1":
            warnings.append(f"Unknown apiVersion: {api_ver}, expected api.cerbos.dev/v1")
        has_api_version = True
    else:
        errors.append("Missing apiVersion field (should be 'api.cerbos.dev/v1')")

    # 3. 策略类型检查
    rp = parsed.get("resourcePolicy")
    dr = parsed.get("derivedRoles")
    if rp and isinstance(rp, dict):
        has_resource_policy = True
        rules = rp.get("rules", [])
        if not rules:
            warnings.append("resourcePolicy has no rules defined")
        for i, rule in enumerate(rules):
            if not isinstance(rule, dict):
                errors.append(f"rule[{i}] is not a mapping")
                continue
            act = rule.get("actions", [])
            if not act:
                errors.append(f"rule[{i}]: missing 'actions' field")
            else:
                actions_count += len(act)
            effect = rule.get("effect")
            if effect and effect not in ("EFFECT_ALLOW", "EFFECT_DENY"):
                errors.append(f"rule[{i}]: invalid effect '{effect}' (must be EFFECT_ALLOW or EFFECT_DENY)")
            if not rule.get("derivedRoles") and effect != "EFFECT_DENY":
                warnings.append(f"rule[{i}]: no derivedRoles specified")
    elif dr and isinstance(dr, dict):
        has_api_version = has_api_version  # derivedRoles 策略有 apiVersion 即可
    elif not rp and not dr:
        warnings.append("No resourcePolicy or derivedRoles found in document")

    return ValidatePolicyResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        has_api_version=has_api_version,
        has_resource_policy=has_resource_policy,
        actions_count=actions_count,
    )


# ══════════════════════════════════════════════════════════════
# 策略版本管理 — 历史快照 + Diff
# ══════════════════════════════════════════════════════════════

import hashlib
import difflib

VERSIONS_DIR = ".versions"


def _snapshot_policy_version(policy_root: Path, safe_path: str, target_file: Path) -> str | None:
    """保存当前策略文件的版本快照。

    Args:
        policy_root: 策略根目录。
        safe_path: 相对路径（如 resource_policies/kb.yaml）。
        target_file: 目标文件完整路径。

    Returns:
        快照文件名（时间戳前缀），失败返回 None。
    """
    try:
        if not target_file.exists():
            return None

        # 版本快照存储目录: cerbos/policies/.versions/resource_policies/
        safe_path_obj = Path(safe_path)
        versions_parent = policy_root / VERSIONS_DIR / safe_path_obj.parent
        versions_parent.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        content_hash = hashlib.sha256(target_file.read_bytes()).hexdigest()[:12]
        snapshot_name = f"{timestamp}_{content_hash}.yaml"
        snapshot_file = versions_parent / snapshot_name

        import shutil
        shutil.copy2(target_file, snapshot_file)
        return snapshot_name
    except Exception:
        return None


class PolicyVersionEntry(BaseModel):
    """策略版本条目。"""
    version_id: str       # 时间戳 + hash
    created_at: str       # ISO 8601
    size_bytes: int
    message: str = ""


class PolicyDiffResponse(BaseModel):
    """策略版本 Diff 结果。"""
    path: str
    version_from: str
    version_to: str
    diff_lines: list[str] = Field(default_factory=list)
    added: int = 0
    removed: int = 0
    unchanged: int = 0


@router.get("/policies/{policy_path:path}/versions", response_model=list[PolicyVersionEntry])
async def list_policy_versions(
    policy_path: str,
    admin: Principal = Depends(get_current_admin),
    scope: ProjectScope = Depends(get_project_scope),
    _perm: None = Depends(require_platform_permission("policy_mgmt", "platform:read")),
) -> list[PolicyVersionEntry]:
    """列出策略文件的所有历史版本。需要管理员认证。

    历史快照与当前策略同属一个命名空间，访问控制口径一致。
    """
    is_safe, safe_path = _validate_policy_path(policy_path)
    if not is_safe:
        raise HTTPException(status_code=422, detail="Invalid policy path")

    _assert_namespace_access(scope, _target_namespace(None, safe_path))

    policy_root = _get_policy_root()
    safe_path_obj = Path(safe_path)
    versions_parent = policy_root / VERSIONS_DIR / safe_path_obj.parent

    if not versions_parent.exists():
        return []

    result: list[PolicyVersionEntry] = []
    for snap in sorted(versions_parent.iterdir(), reverse=True):
        if snap.suffix not in (".yaml", ".yml"):
            continue
        name = snap.stem
        # Parse timestamp from filename: 20260730T120000Z_abc123def456
        created_at = ""
        if "_" in name:
            ts_part = name.split("_")[0]
            try:
                dt = datetime.strptime(ts_part, "%Y%m%dT%H%M%SZ")
                created_at = dt.isoformat()
            except ValueError:
                created_at = ts_part

        result.append(PolicyVersionEntry(
            version_id=name,
            created_at=created_at,
            size_bytes=snap.stat().st_size,
        ))

    # 也包含当前版本作为 "current"
    target_file = policy_root / safe_path
    if target_file.exists():
        result.insert(0, PolicyVersionEntry(
            version_id="current",
            created_at=datetime.fromtimestamp(target_file.stat().st_mtime, tz=timezone.utc).isoformat(),
            size_bytes=target_file.stat().st_size,
            message="当前生效版本",
        ))

    return result


@router.get("/policies/{policy_path:path}/diff", response_model=PolicyDiffResponse)
async def diff_policy_versions(
    policy_path: str,
    admin: Principal = Depends(get_current_admin),
    scope: ProjectScope = Depends(get_project_scope),
    _perm: None = Depends(require_platform_permission("policy_mgmt", "platform:read")),
    v1: str = Query("current", description="基准版本 ID 或 'current'"),
    v2: str = Query(..., description="对比版本 ID"),
) -> PolicyDiffResponse:
    """对比两个策略版本的差异。需要管理员认证。

    Args:
        v1: 基准版本（'current' = 当前生效版本，或版本 ID）。
        v2: 对比版本 ID。
    """
    is_safe, safe_path = _validate_policy_path(policy_path)
    if not is_safe:
        raise HTTPException(status_code=422, detail="Invalid policy path")

    _assert_namespace_access(scope, _target_namespace(None, safe_path))

    policy_root = _get_policy_root()
    safe_path_obj = Path(safe_path)
    versions_parent = policy_root / VERSIONS_DIR / safe_path_obj.parent

    def _read_version(version_id: str) -> tuple[str, str]:
        """读取指定版本的内容。返回 (version_label, content)。"""
        if version_id == "current":
            target = policy_root / safe_path
            if not target.exists():
                raise HTTPException(status_code=404, detail="Current policy not found")
            return ("current", target.read_text(encoding="utf-8"))

        snap_file = versions_parent / f"{version_id}.yaml"
        if not snap_file.exists():
            snap_file = versions_parent / f"{version_id}.yml"
        if not snap_file.exists():
            raise HTTPException(status_code=404, detail=f"Version not found: {version_id}")
        return (version_id, snap_file.read_text(encoding="utf-8"))

    label1, content1 = _read_version(v1)
    label2, content2 = _read_version(v2)

    lines1 = content1.splitlines(keepends=True)
    lines2 = content2.splitlines(keepends=True)

    diff_result = list(difflib.unified_diff(
        lines1, lines2,
        fromfile=f"{safe_path}@{label1}",
        tofile=f"{safe_path}@{label2}",
        lineterm="",
    ))

    added = sum(1 for d in diff_result if d.startswith("+") and not d.startswith("+++"))
    removed = sum(1 for d in diff_result if d.startswith("-") and not d.startswith("---"))
    unchanged = len(diff_result) - added - removed - 3  # subtract 3 header lines

    return PolicyDiffResponse(
        path=safe_path,
        version_from=label1,
        version_to=label2,
        diff_lines=diff_result[:500],  # 最多 500 行 diff
        added=added,
        removed=removed,
        unchanged=max(0, unchanged),
    )
