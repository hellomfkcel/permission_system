"""授权守卫静态门禁 —— 防止"模块级准入通过、项目级校验缺失"这类回归。

背景（G5–G8）：授权有两个正交维度。
  1. 模块准入（能不能进这个模块）—— 由 require_platform_permission 等强制依赖把守，写在
     函数签名里，忘不掉。
  2. 项目/层级范围（能不能操作这个项目/这一层的数据）—— 长期靠每个端点手写内联校验，
     散落各处、极易漏写。读端点漏了就跨项目泄漏，写端点漏了就跨项目越权。

联调只能抽样命中若干实例，证明不了"其余端点都没漏"。本测试改为穷举：静态解析所有路由
处理函数，凡是"触及项目级数据"（带 project_id 形参，或引用带 project_id 列的模型）的端点，
必须出现至少一个范围/权属强制标记；否则测试失败，把"漏写"变成 CI 红灯。

纯静态源码解析，不起服务、不连数据库。新增端点若确属无需项目范围校验（平台级/服务鉴权/
无状态/公开），需显式加入 ALLOWLIST 并写明理由 —— 让豁免成为一次有意识的决定。
"""

from __future__ import annotations

import ast
import pathlib
import re

API_DIR = pathlib.Path(__file__).resolve().parent.parent / "api"

# 带 project_id 列、按项目隔离的数据模型。引用它们即视为"触及项目级数据"。
PROJECT_SCOPED_MODELS = {
    "ACLEntry", "RoleBinding", "RoleDefinition", "Restriction",
    "ResourceRegistry", "PermissionChange",
    "ProjectMember", "ProjectClient", "ProjectApiKey", "ProjectAudience",
}

# 任一标记出现即认为该端点落实了范围/权属强制。
# —— 显式项目范围校验
# —— Cerbos 命名空间 / 授权层级校验
# —— 项目成员 / 平台管理员依赖
# —— 按管理员可见项目集合过滤
# —— /v1 服务鉴权：project_id 由 ClientIdValidationMiddleware 依 API Key 注入 request.state
ENFORCEMENT_MARKERS = {
    "assert_project_scope",
    "can_access",
    "_assert_namespace_access",
    "_assert_policy_layer",
    "_validate_grant_scope",
    "_audit_project_conditions",
    "require_project_member",
    "require_platform_admin",
    "get_admin_project_ids",
    "filter_condition",
    "scope.project_ids",
    "is_platform_admin",
    "request.state",
    "_project_of",
    "_caller_project",
}

# 主动把"调用方给的 project_id"与"调用方可见范围"对齐的标记（区别于 is_platform_admin /
# filter_condition 这类被动过滤）。用于第二道更严的检查：凡端点签名里出现调用方直接提交的
# project_id（Query / Form），就必须校验它属于调用方，否则就是 G5/G6 那种"某分支带范围逻辑、
# 但显式 project_id 分支绕过校验"的隐患。
ASSERT_MARKERS = {
    "assert_project_scope",
    "_assert_namespace_access",
    "_validate_grant_scope",
    "_audit_project_conditions",
    "_caller_project",
    "can_access",
}

# 调用方直接提交 project_id 的签名形态（Query/Form 参数）。
_CALLER_SUPPLIED_PID = re.compile(r"project_id[^=\n]*=\s*(Query|Form)\(")

# 显式豁免：(文件名, 函数名) -> 理由。
# 仅限确实不涉及项目级数据隔离的端点；每条都要能一句话说清为什么安全。
ALLOWLIST: dict[tuple[str, str], str] = {
    # —— 平台层：tenant_mgmt 整个模块是平台专属，require_platform_permission 即边界；
    #    租户不是项目级资源，对 PermissionChange/ResourceRegistry 的引用是级联/登记附带。
    ("tenant_routes.py", "create_tenant"): "平台专属模块，tenant 非项目级",
    ("tenant_routes.py", "delete_tenant"): "平台专属模块，tenant 非项目级",
    # —— 公开 / 会话端点：不读项目数据，project_id 仅用于签发上下文或回显。
    ("auth_routes.py", "get_system_config"): "平台配置，敏感字段已按 settings 软门（G4）",
    ("auth_routes.py", "get_my_platform_access"): "按当前主体自身解析，不跨项目取数",
    ("auth_routes.py", "validate_token"): "无状态令牌校验",
    ("auth_routes.py", "dev_login"): "登录签发，无项目数据",
    ("auth_routes.py", "refresh_token"): "令牌刷新，无项目数据",
    ("auth_routes.py", "sync_users_from_keycloak"): "Keycloak 同步，平台级",
    ("auth_routes.py", "list_groups_from_keycloak"): "分组目录，平台级",
    # —— 无状态策略工具：不落库、不按项目取数。
    ("audit_routes.py", "validate_policy_yaml"): "纯 YAML 结构校验，无数据访问",
    ("audit_routes.py", "get_deploy_status"): "Cerbos 探活 + 文件计数，无项目数据",
}


def _iter_route_functions():
    """产出 (文件名, 函数名, 是否有路由装饰器, 源码) 四元组。"""
    for path in sorted(API_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        src_lines = path.read_text(encoding="utf-8").splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            is_route = any(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr in ("get", "post", "put", "delete", "patch")
                and isinstance(d.func.value, ast.Name)
                and d.func.value.id == "router"
                for d in node.decorator_list
            )
            if not is_route:
                continue
            seg = "\n".join(src_lines[node.lineno - 1 : node.end_lineno])
            yield path.name, node.name, seg


def _touches_project_data(source: str) -> bool:
    if "project_id" in source:
        return True
    return any(model in source for model in PROJECT_SCOPED_MODELS)


def _is_enforced(source: str) -> bool:
    return any(marker in source for marker in ENFORCEMENT_MARKERS)


def test_every_project_scoped_endpoint_enforces_scope():
    """每个触及项目级数据的端点都必须出现范围/权属强制标记（或在 ALLOWLIST 中）。"""
    offenders: list[str] = []
    for filename, func, source in _iter_route_functions():
        if (filename, func) in ALLOWLIST:
            continue
        if _touches_project_data(source) and not _is_enforced(source):
            offenders.append(f"{filename}::{func}")

    assert not offenders, (
        "以下端点触及项目级数据但未见范围/权属强制标记，"
        "属 G5–G8 同类隐患；请补 scope 校验，或确属安全时加入 ALLOWLIST 并注明理由：\n  "
        + "\n  ".join(offenders)
    )


def test_caller_supplied_project_id_is_validated():
    """凡端点签名里出现调用方直接提交的 project_id（Query/Form），必须校验其属于调用方。

    这道检查针对 G5/G6 那种"函数里有 is_platform_admin / filter_condition 等被动过滤、但显式
    project_id 分支直接落进查询条件、没有 assert_project_scope 之类主动校验"的形态 —— 仅靠
    "是否出现任一 enforcement 标记"看不出来，必须要求出现 ASSERT_MARKER。
    """
    offenders: list[str] = []
    for filename, func, source in _iter_route_functions():
        if (filename, func) in ALLOWLIST:
            continue
        if _CALLER_SUPPLIED_PID.search(source) and not any(
            m in source for m in ASSERT_MARKERS
        ):
            offenders.append(f"{filename}::{func}")

    assert not offenders, (
        "以下端点接收调用方提交的 project_id（Query/Form）却未见对其做范围校验"
        "（assert_project_scope / can_access / _assert_namespace_access 等），"
        "属 G5/G6 同类隐患：\n  " + "\n  ".join(offenders)
    )


def test_allowlist_entries_still_exist():
    """ALLOWLIST 不得残留已删除/改名的端点，避免豁免悄悄失效或误伤。"""
    live = {(f, fn) for f, fn, _ in _iter_route_functions()}
    stale = [f"{f}::{fn}" for (f, fn) in ALLOWLIST if (f, fn) not in live]
    assert not stale, "ALLOWLIST 存在失效条目（端点已删除或改名）：\n  " + "\n  ".join(stale)
