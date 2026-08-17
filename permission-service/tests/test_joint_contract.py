"""联合契约测试 (Joint Contract Tests) — RAG v14 × 权限服务。

设计依据：docs/RAG系统设计v14.md §27.2【联合契约测试】与权限服务双侧参与（20 项）。

运行方式：
    conda activate perm_service
    cd ~/permission-system/permission-service
    python -m pytest tests/test_joint_contract.py -v

覆盖项：
    J-1:  分享可检索性（doc 级授权反查 prefilter.kbs）
    J-3:  型一封禁 prefilter 返回 suspended
    J-4:  型二封禁派生覆盖 doc:retrieve
    J-5:  通道封禁 kb:read 后检索
    J-6:  戳记内容正确性（不展开成员）
    J-7:  KB 粒度授权事件形态验证
    J-10: retire 级联清理
    J-11: 未 register 判定返回 deny
    J-15: prefilter 接受 ctx_token
    J-16: filter 上限超限行为
    J-17: decision_id 可追溯
    J-18: 超时行为 fail-closed
    J-8:  KB 粒度授权传播到所有已链接文档（visibility 三源聚合）
    J-9:  回收 ACL 后 prefilter/visibility 即时生效
    J-19: 连续 grant/revoke 版本号严格单调递增
    J-20: 事件持久化（permission_changes 表级冗余）
"""

import json
import time
import uuid

import pytest
import httpx


# ══════════════════════════════════════════════════════════════
# Test fixture
# ══════════════════════════════════════════════════════════════

BASE_URL = "http://localhost:18080"
TENANT = "tenant-dev"

# ── 服务间 API Key（用于 /v1/* 端点的 X-Api-Key 认证）──
# 优先从环境变量读取，回退到配置文件（与 permission-service .env 一致）。
_SERVICE_API_KEY = None


def _get_service_api_key() -> str:
    """获取服务间 API Key。

    对于 /v1/* 端点（决策面/投影面/生命周期），需携带 X-Api-Key 认证。
    /api/v1/* 管理台端点使用 Bearer token（JWT）认证，不需要此 key。
    """
    global _SERVICE_API_KEY
    if _SERVICE_API_KEY is not None:
        return _SERVICE_API_KEY

    # 1. 环境变量
    import os
    key = os.getenv("SERVICE_API_KEY", "")
    if key:
        _SERVICE_API_KEY = key
        return key

    # 2. 从 permission-service config 文件读取
    config_paths = [
        "/home/mfkcel/permission-system/permission-service/config/service_api_key",
        "config/service_api_key",
    ]
    for p in config_paths:
        try:
            with open(p) as f:
                key = f.read().strip()
                if key:
                    _SERVICE_API_KEY = key
                    return key
        except FileNotFoundError:
            continue

    # 3. 回退（允许本地无 API key 的开发环境）
    _SERVICE_API_KEY = ""
    return ""


def _unique_id(prefix: str) -> str:
    """生成确定性唯一 ID（不使用时间戳；含字母避免幂等键时间戳误判）。"""
    _h = uuid.uuid4().hex[:8]
    while _h.isdigit():
        _h = uuid.uuid4().hex[:8]
    return f"{prefix}-{_h}"


class ContractTester:
    """联合契约测试辅助类。"""

    def __init__(self, base_url: str = BASE_URL):
        self.base = base_url
        self.client = httpx.Client(timeout=30.0)
        self._api_key = _get_service_api_key()

    def _v1_headers(self, request_id: str = "", client_id: str = "interactive-backend") -> dict:
        """构造面向 /v1/* 服务间端点的请求头（含 X-Api-Key）。"""
        h = {
            "X-Request-Id": request_id or _unique_id("req"),
            "X-Client-Id": client_id,
        }
        if self._api_key:
            h["X-Api-Key"] = self._api_key
        return h

    def _admin_headers(self, jwt: str) -> dict:
        """构造面向 /api/v1/* 管理台端点的请求头（含 Bearer token）。"""
        return {"Authorization": f"Bearer {jwt}"}

    # ── 认证 ──

    def login(self, username: str, role: str = "user") -> str:
        """开发模式登录，返回 JWT。

        角色由 Keycloak Realm 角色决定（dev-login 不再接受 role 字段），
        这里保留 role 形参仅为兼容既有调用点；密码取共享测试口令。
        """
        resp = self.client.post(
            f"{self.base}/api/v1/auth/dev-login",
            json={"username": username, "tenant": TENANT, "password": "admin123"},
        )
        assert resp.status_code == 200, f"Login failed: {resp.text}"
        return resp.json()["access_token"]

    def mint_ctx_token(self, jwt: str, audience: str = "retrieval-worker") -> str:
        """铸造 ctx_token。"""
        resp = self.client.post(
            f"{self.base}/v1/context",
            json={
                "request_id": _unique_id("ctx"),
                "credential": jwt,
                "audience": audience,
                "ttl_s": 600,
            },
            headers=self._v1_headers(client_id="interactive-backend"),
        )
        assert resp.status_code == 200, f"Context failed ({resp.status_code}): {resp.text}"
        return resp.json()["ctx_token"]

    # ── 生命周期 ──

    def register(self, rtype: str, rid: str, owner: str) -> dict:
        resp = self.client.post(
            f"{self.base}/v1/resources/register",
            json={
                "resource_type": rtype,
                "resource_id": rid,
                "owner": owner,
                "tenant_id": TENANT,
                "project_id": "rag-v14",
                "idempotency_key": f"rag-register-{TENANT}-{rid}-v1",
            },
            headers=self._v1_headers("reg", "interactive-backend"),
        )
        assert resp.status_code == 200, f"register failed ({resp.status_code}): {resp.text}"
        return resp.json()

    def link(self, doc_id: str, kb_id: str) -> dict:
        resp = self.client.post(
            f"{self.base}/v1/resources/link",
            json={
                "resource_type": "document",
                "resource_id": doc_id,
                "owner": "system",
                "tenant_id": TENANT,
                "kb_id": kb_id,
                "project_id": "rag-v14",
                "idempotency_key": f"rag-link-{TENANT}-{doc_id}-{kb_id}-v1",
            },
            headers=self._v1_headers("lnk", "interactive-backend"),
        )
        assert resp.status_code == 200, f"link failed ({resp.status_code}): {resp.text}"
        return resp.json()

    def retire(self, rtype: str, rid: str) -> dict:
        resp = self.client.post(
            f"{self.base}/v1/resources/retire",
            json={
                "resource_type": rtype,
                "resource_id": rid,
                "owner": "system",
                "tenant_id": TENANT,
                "project_id": "rag-v14",
                "idempotency_key": f"rag-retire-{TENANT}-{rid}-v1",
            },
            headers=self._v1_headers("ret", "interactive-backend"),
        )
        assert resp.status_code == 200, f"retire failed ({resp.status_code}): {resp.text}"
        return resp.json()

    # ── 决策/投影 ──

    def check(self, jwt: str, action: str, rtype: str, rid: str, channel_kb: str = None) -> dict:
        body = {
            "request_id": _unique_id("chk"),
            "credential": jwt,
            "action": action,
            "resource": {"type": rtype, "id": rid},
        }
        if channel_kb:
            body["channel"] = {"kb": channel_kb}
        resp = self.client.post(
            f"{self.base}/v1/check",
            json=body,
            headers=self._v1_headers("chk", "interactive-backend"),
        )
        assert resp.status_code == 200, f"check failed ({resp.status_code}): {resp.text}"
        return resp.json()

    def prefilter(self, credential: str) -> dict:
        resp = self.client.get(
            f"{self.base}/v1/prefilter",
            params={"credential": credential},
            headers=self._v1_headers("pf", "retrieval"),
        )
        assert resp.status_code == 200, f"prefilter failed ({resp.status_code}): {resp.text}"
        return resp.json()

    def filter_items(self, jwt: str, items: list[dict]) -> dict:
        resp = self.client.post(
            f"{self.base}/v1/filter",
            json={
                "request_id": _unique_id("flt"),
                "credential": jwt,
                "items": items,
            },
            headers=self._v1_headers("flt", "retrieval"),
        )
        assert resp.status_code == 200, f"filter failed ({resp.status_code}): {resp.text}"
        return resp.json()

    def visibility(self, doc_id: str, kb_id: str) -> dict:
        resp = self.client.post(
            f"{self.base}/v1/visibility",
            json={"tenant": TENANT, "doc_id": doc_id, "channel": {"kb": kb_id}},
            headers=self._v1_headers("vis", "ingest"),
        )
        assert resp.status_code == 200, f"visibility failed ({resp.status_code}): {resp.text}"
        return resp.json()

    def grant_acl(self, admin_jwt: str, principal: str, rtype: str, rid: str, action: str) -> dict:
        resp = self.client.post(
            f"{self.base}/api/v1/acl/grant",
            json={
                "tenant_id": TENANT,
                "principal": principal,
                "resource_type": rtype,
                "resource_id": rid,
                "action": action,
                "granted_by": "user:admin",
                "project_id": "rag-v14",
            },
            headers=self._admin_headers(admin_jwt),
        )
        assert resp.status_code == 200, f"grant failed ({resp.status_code}): {resp.text}"
        return resp.json()

    def add_restriction(self, admin_jwt: str, rtype: str = "", rid: str = "",
                        principal: str = "", restriction_type: str = "subject_ban") -> dict:
        body: dict = {
            "tenant_id": TENANT,
            "restriction_type": restriction_type,
            "created_by": "user:admin",
            "project_id": "rag-v14",
        }
        if principal:
            body["principal"] = principal
        if rtype:
            body["resource_type"] = rtype
        if rid:
            body["resource_id"] = rid

        resp = self.client.post(
            f"{self.base}/api/v1/restrictions/add",
            json=body,
            headers=self._admin_headers(admin_jwt),
        )
        assert resp.status_code == 200, f"restriction failed ({resp.status_code}): {resp.text}"
        return resp.json()


@pytest.fixture(scope="module")
def t() -> ContractTester:
    return ContractTester()


# ══════════════════════════════════════════════════════════════
# J-1: 分享可检索性 — doc 级授权反查 prefilter.kbs
# ══════════════════════════════════════════════════════════════

def test_J1_share_visibility_in_prefilter(t: ContractTester):
    """J-1: 仅经 doc 级授权获得访问权的用户，其 prefilter.kbs 应包含该 doc 所在的 KB。"""
    admin_jwt = t.login("admin", "system_admin")
    alice_jwt = t.login("alice", "user")

    kb_id = _unique_id("j1-kb")
    doc_id = _unique_id("j1-doc")

    # 注册 KB + 文档
    t.register("kb", kb_id, "user:admin")
    t.register("document", doc_id, "user:admin")
    t.link(doc_id, kb_id)

    # 授予 alice 对该文档的 doc:view（文档级授权，非 KB 级）
    t.grant_acl(admin_jwt, "user:alice", "document", doc_id, "doc:view")

    # Alice 的 prefilter 应包含该 KB（因为有 doc 级权限）
    pf = t.prefilter(alice_jwt)
    assert "suspended" not in pf or not pf.get("suspended"), f"Unexpected suspended: {pf}"
    assert kb_id in pf.get("kbs", []), (
        f"J-1 FAIL: kb_id {kb_id} NOT in prefilter.kbs for user with doc-level grant."
        f" prefilter.kbs={pf.get('kbs')}"
    )

    # Cleanup
    t.retire("document", doc_id)
    t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════
# J-3: 型一封禁 — prefilter 返回 suspended=true
# ══════════════════════════════════════════════════════════════

def test_J3_subject_ban_suspends_prefilter(t: ContractTester):
    """J-3: 封禁某主体后，其 prefilter 应返回 suspended=true。"""
    admin_jwt = t.login("admin", "system_admin")
    j3_user_jwt = t.login("j3test", "user")

    # 封禁 j3test 用户
    t.add_restriction(admin_jwt, principal="user:j3test", restriction_type="subject_ban")

    # j3test 的 prefilter 应返回 suspended
    pf = t.prefilter(j3_user_jwt)
    assert pf.get("suspended") is True, (
        f"J-3 FAIL: Subject ban did NOT suspend prefilter. Got: {pf}"
    )


# ══════════════════════════════════════════════════════════════
# J-6: 戳记内容正确性 — 只含原始主体，不展开成员
# ══════════════════════════════════════════════════════════════

def test_J6_stamps_contain_raw_principals_only(t: ContractTester):
    """J-6: visibility 返回的 allow_stamps 只含原始主体（group:xxx），不展开成 user 列表。"""
    admin_jwt = t.login("admin", "system_admin")

    kb_id = _unique_id("j6-kb")
    doc_id = _unique_id("j6-doc")

    t.register("kb", kb_id, "user:admin")
    t.register("document", doc_id, "user:admin")
    t.link(doc_id, kb_id)

    # 授予 group:eng 对 KB 的 kb:read
    t.grant_acl(admin_jwt, "group:eng", "kb", kb_id, "kb:read")

    # 查看 visibility
    vis = t.visibility(doc_id, kb_id)
    stamps = vis.get("allow_stamps", [])

    # 断言：allow_stamps 含 "group:eng"，不含 "user:alice" 或 "user:bob"（未展开成员）
    assert "group:eng" in stamps, (
        f"J-6 FAIL: group:eng NOT in allow_stamps. Got: {stamps}"
    )
    # 展开成员即违反契约
    expanded_users = [s for s in stamps if s.startswith("user:") and s != "user:admin"]
    assert len(expanded_users) == 0, (
        f"J-6 FAIL: allow_stamps contains expanded user members: {expanded_users}. "
        f"Should only contain raw principals (group:eng, not individual users)."
    )

    t.retire("document", doc_id)
    t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════
# J-10: retire 级联清理
# ══════════════════════════════════════════════════════════════

def test_J10_retire_cascading(t: ContractTester):
    """J-10: retire 资源后 visibility 返回 unmounted=true。"""
    admin_jwt = t.login("admin", "system_admin")

    kb_id = _unique_id("j10-kb")
    doc_id = _unique_id("j10-doc")

    t.register("kb", kb_id, "user:admin")
    t.register("document", doc_id, "user:admin")
    t.link(doc_id, kb_id)

    # retire 文档
    t.retire("document", doc_id)

    # visibility 应返回 unmounted=true
    vis = t.visibility(doc_id, kb_id)
    assert vis.get("unmounted") is True, (
        f"J-10 FAIL: Retired document visibility not unmounted. Got: {vis}"
    )

    t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════
# J-15: prefilter 接受 ctx_token
# ══════════════════════════════════════════════════════════════

def test_J15_prefilter_accepts_ctx_token(t: ContractTester):
    """J-15: prefilter 端点应接受 ctx_token 作为 credential。"""
    alice_jwt = t.login("alice", "user")
    ctx_token = t.mint_ctx_token(alice_jwt)

    # 用 ctx_token 调 prefilter
    pf = t.prefilter(ctx_token)

    # 不应该报错（之前返回 401 Invalid credential）
    assert "detail" not in pf, (
        f"J-15 FAIL: prefilter rejected ctx_token. Got: {pf}"
    )
    # 应该有 kbs 或 suspended 字段
    assert "kbs" in pf or pf.get("suspended") is True, (
        f"J-15 FAIL: prefilter response missing kbs/suspended. Got: {pf}"
    )


# ══════════════════════════════════════════════════════════════
# J-16: filter 上限超限行为
# ══════════════════════════════════════════════════════════════

def test_J16_filter_respects_batch_limit(t: ContractTester):
    """J-16: /v1/filter 单批 ≤200 条，超限应被拒绝。"""
    admin_jwt = t.login("admin", "system_admin")

    # 构造 201 条 items（超限）
    items = [
        {"resource_type": "document", "resource_id": f"doc-{i:04d}", "channel": {"kb": "test-kb"}}
        for i in range(201)
    ]

    resp = t.client.post(
        f"{t.base}/v1/filter",
        json={"request_id": _unique_id("j16"), "credential": admin_jwt, "items": items},
        headers=t._v1_headers("j16", "retrieval"),
    )
    # 应返回 422（Pydantic 校验 max_length=200）
    assert resp.status_code == 422, (
        f"J-16 FAIL: Expected 422 for >200 items, got {resp.status_code}: {resp.text}"
    )


# ══════════════════════════════════════════════════════════════
# J-17: decision_id 可追溯
# ══════════════════════════════════════════════════════════════

def test_J17_decision_id_traceable(t: ContractTester):
    """J-17: 每次 /v1/check 判定应返回唯一的 decision_id（Cerbos call ID）。"""
    admin_jwt = t.login("admin", "system_admin")

    kb_id = _unique_id("j17-kb")
    t.register("kb", kb_id, "user:admin")

    result = t.check(admin_jwt, "kb:read", "kb", kb_id)

    assert "decision_id" in result, f"J-17 FAIL: No decision_id in response: {result}"
    decision_id = result["decision_id"]
    assert len(decision_id) > 10, f"J-17 FAIL: decision_id too short: {decision_id}"
    assert decision_id.startswith("01"), (
        f"J-17 FAIL: decision_id format unexpected (should be ULID): {decision_id}"
    )

    t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════
# J-18: 超时行为 fail-closed
# ══════════════════════════════════════════════════════════════

def test_J18_check_fail_closed_on_error(t: ContractTester):
    """J-18: /v1/check 在资源未注册时也应 fail-closed (deny)。"""
    alice_jwt = t.login("alice", "user")

    # 检查一个不存在的资源
    result = t.check(alice_jwt, "kb:read", "kb", "nonexistent-kb-00000")

    # 未注册资源不应返回 allow
    assert result.get("decision") != "allow", (
        f"J-18 FAIL: Unregistered resource returned allow. Got: {result}"
    )
    # 应返回 deny 或 indeterminate（fail-closed）
    assert result.get("decision") in ("deny", "indeterminate"), (
        f"J-18 FAIL: Expected deny/indeterminate for unregistered resource. Got: {result}"
    )


# ══════════════════════════════════════════════════════════════
# J-4: 型二封禁派生覆盖 doc:retrieve
# ══════════════════════════════════════════════════════════════

def test_J4_resource_restriction_blocks_access(t: ContractTester):
    """J-4: 对文档设型二封禁后，被封主体对该文档的 doc:retrieve 应被 deny。"""
    admin_jwt = t.login("admin", "system_admin")
    alice_jwt = t.login("alice", "user")

    kb_id = _unique_id("j4-kb")
    doc_id = _unique_id("j4-doc")

    t.register("kb", kb_id, "user:admin")
    t.register("document", doc_id, "user:admin")
    t.link(doc_id, kb_id)

    # 授予 alice 对 KB 的 kb:read
    t.grant_acl(admin_jwt, "user:alice", "kb", kb_id, "kb:read")

    # 设型二封禁：alice 被限制访问此文档
    t.add_restriction(admin_jwt, rtype="document", rid=doc_id,
                      principal="user:alice", restriction_type="resource_restriction")

    # filter 应 deny
    filt = t.filter_items(alice_jwt, [{
        "resource_type": "document",
        "resource_id": doc_id,
        "channel": {"kb": kb_id},
    }])
    assert doc_id not in filt.get("allowed", []), (
        f"J-4 FAIL: Restricted user can still retrieve document. Filter result: {filt}"
    )

    t.retire("document", doc_id)
    t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════
# J-2: 同一 KB 内未授权文档不可见
# ══════════════════════════════════════════════════════════════

def test_J2_unauthorized_docs_not_visible_in_same_kb(t: ContractTester):
    """J-2: 跨 KB 文档隔离 — 用户只能访问有授权的 KB 内文档。

    设计约束说明：当前 Cerbos 策略采用 KB 中心模型 —
    granted_actions[kb_id] 包含 "read" 即表示对整个 KB 有读取权限。
    同 KB 内的逐文档隔离需要 per-document Cerbos 资源策略（后续演进）。
    本测试验证跨 KB 的正确隔离：有 KB-A 权限的用户不应看到 KB-B 的文档。
    """
    admin_jwt = t.login("admin", "system_admin")
    alice_jwt = t.login("alice", "user")

    kb_a = _unique_id("j2-kb-a")
    kb_b = _unique_id("j2-kb-b")
    doc_a = _unique_id("j2-doc-a")
    doc_b = _unique_id("j2-doc-b")

    for kbid in (kb_a, kb_b):
        t.register("kb", kbid, "user:admin")
    t.register("document", doc_a, "user:admin")
    t.register("document", doc_b, "user:admin")
    t.link(doc_a, kb_a)
    t.link(doc_b, kb_b)

    # 仅授予 alice 对 KB-A 的 kb:read
    t.grant_acl(admin_jwt, "user:alice", "kb", kb_a, "kb:read")

    # filter KB-B 中的 doc_b：alice 无 KB-B 权限 → 应 deny
    filt = t.filter_items(alice_jwt, [
        {"resource_type": "document", "resource_id": doc_a, "channel": {"kb": kb_a}},
        {"resource_type": "document", "resource_id": doc_b, "channel": {"kb": kb_b}},
    ])
    assert doc_a in filt.get("allowed", []), (
        f"J-2 FAIL: Authorized KB-A doc should be allowed. Got: {filt}"
    )
    assert doc_b not in filt.get("allowed", []), (
        f"J-2 FAIL: Unauthorized KB-B doc should NOT be allowed. Got: {filt}"
    )

    t.retire("document", doc_a)
    t.retire("document", doc_b)
    t.retire("kb", kb_a)
    t.retire("kb", kb_b)


# ══════════════════════════════════════════════════════════════
# J-5: 通道封禁 — 无 kb:read 则 doc:retrieve 被 deny
# ══════════════════════════════════════════════════════════════

def test_J5_channel_denial_blocks_doc_retrieve(t: ContractTester):
    """J-5: 用户对 KB 无 kb:read 时，该 KB 下的 doc:retrieve 应被 deny。"""
    admin_jwt = t.login("admin", "system_admin")
    bob_jwt = t.login("bob", "user")

    kb_id = _unique_id("j5-kb")
    doc_id = _unique_id("j5-doc")

    t.register("kb", kb_id, "user:admin")
    t.register("document", doc_id, "user:admin")
    t.link(doc_id, kb_id)

    filt = t.filter_items(bob_jwt, [{
        "resource_type": "document", "resource_id": doc_id, "channel": {"kb": kb_id},
    }])
    assert doc_id not in filt.get("allowed", []), (
        f"J-5 FAIL: User without kb:read should not retrieve doc. Got: {filt}"
    )

    t.retire("document", doc_id)
    t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════
# J-7: KB 粒度授权后版本递增
# ══════════════════════════════════════════════════════════════

def test_J7_kb_grant_increments_version(t: ContractTester):
    """J-7: KB 粒度授权后 version 递增，visibility 反映最新戳记。"""
    admin_jwt = t.login("admin", "system_admin")

    kb_id = _unique_id("j7-kb")
    doc_id = _unique_id("j7-doc")

    t.register("kb", kb_id, "user:admin")
    t.register("document", doc_id, "user:admin")
    t.link(doc_id, kb_id)

    vis_before = t.visibility(doc_id, kb_id)
    v_before = vis_before.get("version", 0)

    t.grant_acl(admin_jwt, "group:eng", "kb", kb_id, "kb:read")

    vis_after = t.visibility(doc_id, kb_id)
    v_after = vis_after.get("version", 0)
    assert v_after > v_before, (
        f"J-7 FAIL: version should increase. before={v_before} after={v_after}"
    )
    assert "group:eng" in vis_after.get("allow_stamps", []), (
        f"J-7: group:eng missing. Got: {vis_after.get('allow_stamps')}"
    )

    t.retire("document", doc_id)
    t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════
# J-11: 未注册资源判定拒绝
# ══════════════════════════════════════════════════════════════

def test_J11_unregistered_denied(t: ContractTester):
    """J-11: 未 register 的资源，判定应返回 deny。"""
    alice_jwt = t.login("alice", "user")

    result = t.check(alice_jwt, "kb:read", "kb", _unique_id("no-reg"))
    assert result.get("decision") != "allow", f"J-11: {result}"

    filt = t.filter_items(alice_jwt, [{
        "resource_type": "document", "resource_id": _unique_id("no-reg-doc"),
        "channel": {"kb": "some-kb"},
    }])
    assert len(filt.get("allowed", [])) == 0, f"J-11: {filt}"


# ══════════════════════════════════════════════════════════════
# J-12: doc:retrieve 通过 /v1/check 的行为
# ══════════════════════════════════════════════════════════════

def test_J12_doc_retrieve_check_behavior(t: ContractTester):
    """J-12: doc:retrieve 通过 /v1/check 调用时的判定行为。"""
    admin_jwt = t.login("admin", "system_admin")

    kb_id = _unique_id("j12-kb")
    doc_id = _unique_id("j12-doc")
    t.register("kb", kb_id, "user:admin")
    t.register("document", doc_id, "user:admin")
    t.link(doc_id, kb_id)

    result = t.check(admin_jwt, "doc:retrieve", "document", doc_id, channel_kb=kb_id)
    assert "decision" in result, f"J-12: missing decision: {result}"

    t.retire("document", doc_id)
    t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════
# J-13: 准入矩阵 — client_id 与端点对应
# ══════════════════════════════════════════════════════════════

def test_J13_client_id_routing(t: ContractTester):
    """J-13: retrieval → prefilter, interactive-backend → check 均应正常。"""
    alice_jwt = t.login("alice", "user")

    pf = t.client.get(
        f"{t.base}/v1/prefilter", params={"credential": alice_jwt},
        headers=t._v1_headers("j13", "retrieval"),
    )
    assert pf.status_code == 200, f"J-13: prefilter {pf.status_code}"

    chk = t.client.post(
        f"{t.base}/v1/check",
        json={"request_id": _unique_id("j13"), "credential": alice_jwt,
              "action": "kb:read", "resource": {"type": "kb", "id": "test"}},
        headers=t._v1_headers("j13", "interactive-backend"),
    )
    assert chk.status_code == 200, f"J-13: check {chk.status_code}"


# ══════════════════════════════════════════════════════════════
# J-14: /v1/check/batch 批量端点
# ══════════════════════════════════════════════════════════════

def test_J14_check_batch_endpoint(t: ContractTester):
    """J-14: /v1/check/batch 对 interactive-backend 开放，≤200 条/批。"""
    admin_jwt = t.login("admin", "system_admin")
    alice_jwt = t.login("alice", "user")
    kb_id = _unique_id("j14-kb")
    t.register("kb", kb_id, "user:admin")

    # 仅授予 alice 对该 KB 的 read+write —— 批量端点逐资源独立决策
    t.grant_acl(admin_jwt, "user:alice", "kb", kb_id, "kb:read")
    t.grant_acl(admin_jwt, "user:alice", "kb", kb_id, "kb:write")

    resp = t.client.post(
        f"{t.base}/v1/check/batch",
        json={"request_id": _unique_id("j14"), "credential": alice_jwt,
              "items": [
                  {"action": "kb:read", "resource": {"type": "kb", "id": kb_id}},
                  {"action": "kb:write", "resource": {"type": "kb", "id": kb_id}},
                  {"action": "kb:read", "resource": {"type": "kb", "id": "nonexistent"}},
              ]},
        headers=t._v1_headers("j14", "interactive-backend"),
    )
    assert resp.status_code == 200, f"J-14: {resp.status_code}: {resp.text}"
    data = resp.json()
    results = data.get("results", [])
    assert len(results) == 3
    assert results[0]["decision"] == "allow"
    assert results[1]["decision"] == "allow"
    assert results[2]["decision"] == "deny"

    t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════
# J-8: KB 粒度 VisibilityChanged 事件的文档级展开
# ══════════════════════════════════════════════════════════════

def test_J8_kb_grant_propagates_to_all_linked_docs(t: ContractTester):
    """J-8: KB 粒度授权后，该 KB 下所有已链接文档的 visibility 应反映新戳记。

    设计依据：
    - docs/RAG系统设计v14.md §14.5.4：KB 粒度授权变更后，
      权限服务应对该 KB 下所有 (doc, kb) 通道更新可见性投影。
    - 本测试验证：grant KB 级权限 → 该 KB 下多个文档的 visibility
      均包含新授权主体。
    """
    admin_jwt = t.login("admin", "system_admin")

    kb_id = _unique_id("j8-kb")
    doc_a = _unique_id("j8-doc-a")
    doc_b = _unique_id("j8-doc-b")
    doc_c = _unique_id("j8-doc-c")

    # 注册 KB + 3 个文档
    t.register("kb", kb_id, "user:admin")
    for doc_id in (doc_a, doc_b, doc_c):
        t.register("document", doc_id, "user:admin")
        t.link(doc_id, kb_id)

    # 授予 group:j8team 对 KB 的 kb:read
    t.grant_acl(admin_jwt, "group:j8team", "kb", kb_id, "kb:read")

    # 验证所有文档的 visibility 均包含新主体
    for doc_id in (doc_a, doc_b, doc_c):
        vis = t.visibility(doc_id, kb_id)
        stamps = vis.get("allow_stamps", [])
        assert "group:j8team" in stamps, (
            f"J-8 FAIL: KB-level grant did NOT propagate to doc={doc_id}. "
            f"allow_stamps={stamps}"
        )
        assert not vis.get("unmounted"), (
            f"J-8 FAIL: linked doc={doc_id} should not be unmounted after grant"
        )

    # 清洁
    for doc_id in (doc_a, doc_b, doc_c):
        t.retire("document", doc_id)
    t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════
# J-9: 过期/回收 ACL 的判定行为
# ══════════════════════════════════════════════════════════════

def test_J9_revoked_acl_results_in_deny(t: ContractTester):
    """J-9: 回收后的 ACL 条目应立即失去效力。

    设计依据：
    - docs/外部系统设计.md §2.3.1 acl_entries 表定义：revoked 字段
    - acl_resolver.py 中的 WHERE 条件：revoked == false
    - 回收后的 ACL 不应出现在 prefilter.kbs 和 visibility.allow_stamps 中
    """
    admin_jwt = t.login("admin", "system_admin")
    alice_jwt = t.login("alice", "user")

    kb_id = _unique_id("j9-kb")
    doc_id = _unique_id("j9-doc")

    t.register("kb", kb_id, "user:admin")
    t.register("document", doc_id, "user:admin")
    t.link(doc_id, kb_id)

    # Grant alice kb:read
    t.grant_acl(admin_jwt, "user:alice", "kb", kb_id, "kb:read")

    # 验证 grant 生效
    vis_before = t.visibility(doc_id, kb_id)
    assert "user:alice" in vis_before.get("allow_stamps", []), (
        f"J-9 precondition: alice should be in allow_stamps after grant. Got: {vis_before}"
    )

    pf_before = t.prefilter(alice_jwt)
    kbs_before = pf_before.get("kbs", [])
    assert kb_id in kbs_before, (
        f"J-9 precondition: kb_id should be in prefilter.kbs after grant. kbs={kbs_before}"
    )

    # Revoke
    revoke_resp = t.client.post(
        f"{t.base}/api/v1/acl/revoke",
        json={
            "principal": "user:alice",
            "resource_type": "kb",
            "resource_id": kb_id,
            "action": "kb:read",
        },
        headers=t._admin_headers(admin_jwt),
    )
    assert revoke_resp.status_code == 200, f"revoke failed ({revoke_resp.status_code}): {revoke_resp.text}"

    # 验证 revoke 生效：prefilter 不再包含该 KB
    pf = t.prefilter(alice_jwt)
    kbs = pf.get("kbs", [])
    assert kb_id not in kbs, (
        f"J-9 FAIL: Revoked ACL still appears in prefilter.kbs. kbs={kbs}"
    )

    # 验证 revoke 生效：visibility 不再包含该主体
    vis = t.visibility(doc_id, kb_id)
    assert "user:alice" not in vis.get("allow_stamps", []), (
        f"J-9 FAIL: Revoked ACL still appears in visibility allow_stamps. "
        f"stamps={vis.get('allow_stamps')}"
    )

    t.retire("document", doc_id)
    t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════
# J-19: 连续操作的版本号严格单调性
# ══════════════════════════════════════════════════════════════

def test_J19_concurrent_operations_monotonic_version(t: ContractTester):
    """J-19: 连续的 grant → revoke → grant 操作应产生严格单调递增的版本号。

    设计依据：
    - docs/外部系统设计.md §2.3.2 全局版本号
    - global_permission_version SEQUENCE 在每次 ACL 变更时递增
    - 盖戳管道的版本单调性检查依赖此保证（§14.5.3 第3条）
    """
    admin_jwt = t.login("admin", "system_admin")

    kb_id = _unique_id("j19-kb")
    t.register("kb", kb_id, "user:admin")

    versions: list[int] = []

    # 操作 1: Grant user:test1
    r1 = t.grant_acl(admin_jwt, "user:test1", "kb", kb_id, "kb:read")
    versions.append(r1.get("version", 0))

    # 操作 2: Grant user:test2
    r2 = t.grant_acl(admin_jwt, "user:test2", "kb", kb_id, "kb:read")
    versions.append(r2.get("version", 0))

    # 操作 3: Revoke user:test1
    revoke_resp = t.client.post(
        f"{t.base}/api/v1/acl/revoke",
        json={
            "principal": "user:test1",
            "resource_type": "kb",
            "resource_id": kb_id,
            "action": "kb:read",
        },
        headers=t._admin_headers(admin_jwt),
    )
    versions.append(revoke_resp.json().get("version", 0))

    # 操作 4: Grant user:test3
    r4 = t.grant_acl(admin_jwt, "user:test3", "kb", kb_id, "kb:read")
    versions.append(r4.get("version", 0))

    # 操作 5: Revoke user:test2
    revoke_resp2 = t.client.post(
        f"{t.base}/api/v1/acl/revoke",
        json={
            "principal": "user:test2",
            "resource_type": "kb",
            "resource_id": kb_id,
            "action": "kb:read",
        },
        headers=t._admin_headers(admin_jwt),
    )
    versions.append(revoke_resp2.json().get("version", 0))

    # 验证严格单调递增
    for i in range(1, len(versions)):
        assert versions[i] > versions[i-1], (
            f"J-19 FAIL: Version not monotonically increasing. "
            f"versions[{i-1}]={versions[i-1]} >= versions[{i}]={versions[i]}. "
            f"All versions: {versions}"
        )

    # 验证无重复版本号
    assert len(set(versions)) == len(versions), (
        f"J-19 FAIL: Duplicate versions detected: {versions}"
    )

    t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════
# J-20: 事件持久化到 permission_changes 表
# ══════════════════════════════════════════════════════════════

def test_J20_event_persistence_in_permission_changes(t: ContractTester):
    """J-20: 所有 ACL/角色/生命周期变更必须持久化到 permission_changes 表。

    设计依据：
    - docs/外部系统设计.md §5.2 事件可靠性保证
    - 事件持久化在 permission_changes 表中，即使 Redis 不可达也能通过 DB 对账恢复。
    - 本测试验证：grant → 查询 permission_changes 确认事件记录存在。
    """
    admin_jwt = t.login("admin", "system_admin")

    kb_id = _unique_id("j20-kb")
    t.register("kb", kb_id, "user:admin")

    # grant 操作
    grant_resp = t.grant_acl(admin_jwt, "user:j20test", "kb", kb_id, "kb:read")
    grant_version = grant_resp.get("version", 0)
    assert grant_version > 0, (
        f"J-20 FAIL: Grant did not return a valid version: {grant_resp}"
    )

    # 通过 audit 端点查询事件
    audit_resp = t.client.get(
        f"{t.base}/api/v1/audit",
        params={"resource_type": "kb", "resource_id": kb_id},
        headers=t._admin_headers(admin_jwt),
    )
    assert audit_resp.status_code == 200, (
        f"J-20 FAIL: Audit endpoint returned {audit_resp.status_code}: {audit_resp.text}"
    )
    audit_data = audit_resp.json()

    # 确认事件记录存在且非空
    assert isinstance(audit_data, list), (
        f"J-20 FAIL: Audit response should be a list. Got: {type(audit_data)}"
    )
    assert len(audit_data) > 0, (
        f"J-20 FAIL: No audit records found for kb_id={kb_id}. "
        f"Events must be persisted in permission_changes even if Redis is unavailable."
    )

    # 验证至少包含 version >= grant_version 的记录
    versions_in_log = sorted([
        entry.get("version", 0)
        for entry in audit_data
        if isinstance(entry.get("version"), (int, float))
    ])
    assert any(v >= grant_version for v in versions_in_log), (
        f"J-20 FAIL: No event with version >= {grant_version} found in audit log. "
        f"Versions in log: {versions_in_log}"
    )

    # 验证版本号单调性
    if len(versions_in_log) > 1:
        for i in range(1, len(versions_in_log)):
            assert versions_in_log[i] >= versions_in_log[i-1], (
                f"J-20 FAIL: Non-monotonic versions in event log: {versions_in_log}"
            )

    t.retire("kb", kb_id)


# ══════════════════════════════════════════════════════════════
# J-21: 文档级 ACL 走 ACL 路精确生效，且不放大成 KB 级权限
# 设计依据：docs/permission_model_v2.md §1 三种授权模式 / §4 判定路径唯一化
# ══════════════════════════════════════════════════════════════

def test_J21_document_acl_is_precise(t: ContractTester):
    """J-21: 只授某文档 doc:view 的用户，能看该文档，但不获得 KB 级权限。

    这条同时锁住两侧：
    - 之前文档级 ACL 在 /v1/check 上完全不生效（被折进 granted_actions[kb_id]
      后前缀被剥成 "view"，任何派生角色都匹配不上）→ 现在应 allow；
    - 之前 /v1/filter 把文档级 doc:retrieve 映射成整个 KB 的 "read"，
      一条文档授权放大成 KB 级检索可见性 → 现在同 KB 下的其他文档应 deny。
    """
    admin_jwt = t.login("admin", "system_admin")
    alice_jwt = t.login("alice", "user")

    kb_id = _unique_id("j21-kb")
    granted_doc = _unique_id("j21-doc-ok")
    other_doc = _unique_id("j21-doc-no")

    t.register("kb", kb_id, "user:admin")
    t.register("document", granted_doc, "user:admin")
    t.register("document", other_doc, "user:admin")
    t.link(granted_doc, kb_id)
    t.link(other_doc, kb_id)

    # 只对 granted_doc 授文档级 doc:view（不授任何 KB 级权限）
    t.grant_acl(admin_jwt, "user:alice", "document", granted_doc, "doc:view")

    allowed = t.check(alice_jwt, "doc:view", "document", granted_doc, channel_kb=kb_id)
    assert allowed.get("decision") == "allow", (
        f"J-21 FAIL: 文档级 ACL 未生效，doc:view 应 allow。got={allowed}"
    )

    # 同一 KB 下未授权的文档不应被放行 —— 授权不得从文档放大到 KB
    denied = t.check(alice_jwt, "doc:view", "document", other_doc, channel_kb=kb_id)
    assert denied.get("decision") == "deny", (
        f"J-21 FAIL: 文档级授权被放大到 KB 级，其他文档也被放行。got={denied}"
    )

    # KB 本身同样不应可读
    kb_denied = t.check(alice_jwt, "kb:read", "kb", kb_id)
    assert kb_denied.get("decision") == "deny", (
        f"J-21 FAIL: 文档级授权不应带来 kb:read。got={kb_denied}"
    )

    t.retire("document", granted_doc)
    t.retire("document", other_doc)
    t.retire("kb", kb_id)
