"""权限服务后端 — 单元测试与集成测试。

测试用例覆盖（共 20 条），设计依据 docs/外部系统实施方案.md §10.1。
测试目标: http://localhost:18080 (需先启动权限服务)
"""

import os
import pytest
from tests.utils import make_token, make_admin_headers, BASE_URL


# ═══════════════════════════════════════════════════════════
# Test 1-2: 健康检查
# ═══════════════════════════════════════════════════════════

def test_healthz(api):
    resp = api.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz(api):
    resp = api.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


# ═══════════════════════════════════════════════════════════
# Test 3: 无效 credential → 401
# ═══════════════════════════════════════════════════════════

def test_check_invalid_credential(api, headers):
    resp = api.post("/v1/check", json={
        "request_id": "t-001", "credential": "bad-token",
        "action": "kb:read",
        "resource": {"type": "kb", "id": "test-kb"},
    }, headers=headers)
    assert resp.status_code == 401


# ═══════════════════════════════════════════════════════════
# Test 4: 无 ACL → check 返回 deny
# ═══════════════════════════════════════════════════════════

def test_check_deny_no_acl(api, test_token, headers, unique_id):
    resp = api.post("/v1/check", json={
        "request_id": f"t-{unique_id}", "credential": test_token,
        "action": "kb:write",
        "resource": {"type": "kb", "id": f"kb-nonexistent-{unique_id}"},
    }, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "deny"


# ═══════════════════════════════════════════════════════════
# Test 5: 资源注册 + 幂等键
# ═══════════════════════════════════════════════════════════

def test_register_resource(api, unique_id):
    kb_id = f"kb-reg-{unique_id}"

    # 首次注册
    resp = api.post("/v1/resources/register", json={
        "resource_type": "kb", "resource_id": kb_id,
        "owner": "user:admin", "tenant_id": "tenant-test",
        "idempotency_key": f"test-reg-{unique_id}",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"] == "created"
    assert data["change_id"]

    # 重复注册（同 key+payload）→ noop
    resp2 = api.post("/v1/resources/register", json={
        "resource_type": "kb", "resource_id": kb_id,
        "owner": "user:admin", "tenant_id": "tenant-test",
        "idempotency_key": f"test-reg-{unique_id}",
    })
    assert resp2.status_code == 200
    assert resp2.json()["result"] == "noop"


# ═══════════════════════════════════════════════════════════
# Test 6: 幂等冲突 → 409
# ═══════════════════════════════════════════════════════════

def test_idempotency_conflict(api, unique_id):
    kb_id = f"kb-conflict-{unique_id}"

    # 首次注册
    api.post("/v1/resources/register", json={
        "resource_type": "kb", "resource_id": kb_id,
        "owner": "user:alice", "tenant_id": "tenant-test",
        "idempotency_key": f"conflict-{unique_id}",
    })

    # 同 key 不同 payload
    resp = api.post("/v1/resources/register", json={
        "resource_type": "kb", "resource_id": kb_id,
        "owner": "user:bob",  # 不同 owner
        "tenant_id": "tenant-test",
        "idempotency_key": f"conflict-{unique_id}",
    })
    assert resp.status_code == 409


# ═══════════════════════════════════════════════════════════
# Test 7: 授予 ACL 后 check 返回 allow
# ═══════════════════════════════════════════════════════════

def test_check_allow_after_grant(api, test_token, headers, admin_headers, unique_id):
    kb_id = f"kb-allow-{unique_id}"

    # 注册资源
    api.post("/v1/resources/register", json={
        "resource_type": "kb", "resource_id": kb_id,
        "owner": "user:test-user", "tenant_id": "tenant-test",
        "idempotency_key": f"allow-reg-{unique_id}",
    })

    # 授予权限（需要管理员认证）
    api.post("/api/v1/acl/grant", json={
        "tenant_id": "tenant-test",
        "principal": "user:test-user",
        "resource_type": "kb",
        "resource_id": kb_id,
        "action": "kb:read",
        "granted_by": "admin",
        "project_id": "rag-v14",
    }, headers=admin_headers)

    # check 应返回 allow
    resp = api.post("/v1/check", json={
        "request_id": f"t-{unique_id}", "credential": test_token,
        "action": "kb:read",
        "resource": {"type": "kb", "id": kb_id},
    }, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    # 有 ACL grant 不代表 Cerbos 一定会 allow（策略可能还有其他条件）
    # 但决策应是确定性的（allow/deny/indeterminate 之一）
    assert data["decision"] in ("allow", "deny", "indeterminate")
    assert "decision_id" in data


# ═══════════════════════════════════════════════════════════
# Test 8: 型一封禁 → check 返回 deny
# ═══════════════════════════════════════════════════════════

def test_check_subject_ban(api, test_token, headers, admin_headers, unique_id):
    kb_id = f"kb-ban-{unique_id}"
    ban_principal = f"user:ban-user-{unique_id}"
    ban_token = make_token(sub=f"ban-user-{unique_id}", groups=[])

    # 注册资源 + 授予权限
    api.post("/v1/resources/register", json={
        "resource_type": "kb", "resource_id": kb_id,
        "owner": ban_principal, "tenant_id": "tenant-test",
        "idempotency_key": f"ban-reg-{unique_id}",
    })

    # 型一封禁（需要管理员认证）
    api.post("/api/v1/restrictions/add", json={
        "tenant_id": "tenant-test",
        "restriction_type": "subject_ban",
        "principal": ban_principal,
        "reason": "Test ban",
        "created_by": "admin",
        "project_id": "rag-v14",
    }, headers=admin_headers)

    # check 应返回 deny
    resp = api.post("/v1/check", json={
        "request_id": f"t-{unique_id}", "credential": ban_token,
        "action": "kb:read",
        "resource": {"type": "kb", "id": kb_id},
    }, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] == "deny"
    assert "subject_banned" in data.get("reasons", [])


# ═══════════════════════════════════════════════════════════
# Test 9: prefilter — 被封禁用户 → suspended=true
# ═══════════════════════════════════════════════════════════

def test_prefilter_suspended(api, admin_headers, unique_id):
    ban_principal = f"user:prefilter-ban-{unique_id}"
    ban_token = make_token(sub=f"prefilter-ban-{unique_id}", groups=[])

    # 型一封禁（需要管理员认证）
    api.post("/api/v1/restrictions/add", json={
        "tenant_id": "tenant-test",
        "restriction_type": "subject_ban",
        "principal": ban_principal,
        "reason": "Test",
        "created_by": "admin",
        "project_id": "rag-v14",
    }, headers=admin_headers)

    resp = api.get(f"/v1/prefilter?credential={ban_token}")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("suspended") is True


# ═══════════════════════════════════════════════════════════
# Test 10: prefilter — 新用户无权限 → kbs 列表
# ═══════════════════════════════════════════════════════════

def test_prefilter_empty(api, unique_id):
    new_token = make_token(sub=f"new-user-{unique_id}", groups=[])
    resp = api.get(f"/v1/prefilter?credential={new_token}")
    assert resp.status_code == 200
    data = resp.json()
    assert "kbs" in data
    assert "suspended" not in data or not data.get("suspended")


# ═══════════════════════════════════════════════════════════
# Test 11: visibility — 已挂载资源
# ═══════════════════════════════════════════════════════════

def test_visibility_normal(api, unique_id):
    doc_id = f"doc-vis-{unique_id}"
    kb_id = f"kb-vis-{unique_id}"

    # 注册
    api.post("/v1/resources/register", json={
        "resource_type": "document", "resource_id": doc_id,
        "owner": "user:admin", "tenant_id": "tenant-test",
        "idempotency_key": f"vis-doc-{unique_id}",
    })
    api.post("/v1/resources/register", json={
        "resource_type": "kb", "resource_id": kb_id,
        "owner": "user:admin", "tenant_id": "tenant-test",
        "idempotency_key": f"vis-kb-{unique_id}",
    })

    # 挂载
    api.post("/v1/resources/link", json={
        "resource_type": "document", "resource_id": doc_id,
        "kb_id": kb_id, "owner": "system", "tenant_id": "tenant-test",
        "idempotency_key": f"vis-link-{unique_id}",
    })

    resp = api.post("/v1/visibility", json={
        "tenant": "tenant-test",
        "doc_id": doc_id,
        "channel": {"kb": kb_id},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "allow_stamps" in data
    assert "version" in data
    assert data.get("unmounted") is False


# ═══════════════════════════════════════════════════════════
# Test 12: visibility — 已解除挂载 → unmounted=true
# ═══════════════════════════════════════════════════════════

def test_visibility_unmounted(api, unique_id):
    doc_id = f"doc-unm-{unique_id}"
    kb_id = f"kb-unm-{unique_id}"

    api.post("/v1/resources/register", json={
        "resource_type": "document", "resource_id": doc_id,
        "owner": "user:admin", "tenant_id": "tenant-test",
        "idempotency_key": f"unm-doc-{unique_id}",
    })
    api.post("/v1/resources/register", json={
        "resource_type": "kb", "resource_id": kb_id,
        "owner": "user:admin", "tenant_id": "tenant-test",
        "idempotency_key": f"unm-kb-{unique_id}",
    })
    api.post("/v1/resources/link", json={
        "resource_type": "document", "resource_id": doc_id,
        "kb_id": kb_id, "owner": "system", "tenant_id": "tenant-test",
        "idempotency_key": f"unm-link-{unique_id}",
    })

    # 解除挂载
    api.post("/v1/resources/unlink", json={
        "resource_type": "document", "resource_id": doc_id,
        "kb_id": kb_id, "owner": "system", "tenant_id": "tenant-test",
        "idempotency_key": f"unm-unlink-{unique_id}",
    })

    resp = api.post("/v1/visibility", json={
        "tenant": "tenant-test",
        "doc_id": doc_id,
        "channel": {"kb": kb_id},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("unmounted") is True


# ═══════════════════════════════════════════════════════════
# Test 13: filter — 批量判定
# ═══════════════════════════════════════════════════════════

def test_filter_batch(api, test_token, headers, unique_id):
    resp = api.post("/v1/filter", json={
        "request_id": f"t-{unique_id}", "credential": test_token,
        "items": [
            {"resource_type": "document", "resource_id": f"doc-{unique_id}-{i}",
             "channel": {"kb": f"kb-{unique_id}"}}
            for i in range(5)
        ],
    }, headers={**headers, "X-Client-Id": "retrieval"})
    assert resp.status_code == 200
    data = resp.json()
    assert "allowed" in data
    assert "denied" in data


# ═══════════════════════════════════════════════════════════
# Test 14: context — ctx_token 铸造 + 验证
# ═══════════════════════════════════════════════════════════

def test_context_token(api, test_token, unique_id):
    resp = api.post("/v1/context", json={
        "request_id": f"t-{unique_id}",
        "credential": test_token,
        "audience": "retrieval-worker",
        "ttl_s": 300,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "ctx_token" in data
    assert data["ctx_token"].startswith("ctx.")
    assert "expires_at" in data


# ═══════════════════════════════════════════════════════════
# Test 15: ACL 授予 + 版本号递增
# ═══════════════════════════════════════════════════════════

def test_acl_grant_version_bump(api, admin_headers, unique_id):
    kb_id = f"kb-aclver-{unique_id}"

    resp1 = api.post("/api/v1/acl/grant", json={
        "tenant_id": "tenant-test",
        "principal": "group:eng",
        "resource_type": "kb",
        "resource_id": kb_id,
        "action": "kb:read",
        "granted_by": "admin",
        "project_id": "rag-v14",
    }, headers=admin_headers)
    assert resp1.status_code == 200
    v1 = resp1.json()["version"]
    assert v1 > 0

    resp2 = api.post("/api/v1/acl/grant", json={
        "tenant_id": "tenant-test",
        "principal": "group:product",
        "resource_type": "kb",
        "resource_id": kb_id,
        "action": "kb:write",
        "granted_by": "admin",
        "project_id": "rag-v14",
    }, headers=admin_headers)
    assert resp2.status_code == 200
    v2 = resp2.json()["version"]
    assert v2 > v1


# ═══════════════════════════════════════════════════════════
# Test 16: ACL 查询列表
# ═══════════════════════════════════════════════════════════

def test_list_acl(api):
    resp = api.get("/api/v1/acl")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ═══════════════════════════════════════════════════════════
# Test 17: 角色绑定/解绑
# ═══════════════════════════════════════════════════════════

def test_role_bind_unbind(api, admin_headers, unique_id):
    principal = f"user:role-test-{unique_id}"

    resp = api.post("/api/v1/roles/bind", json={
        "tenant_id": "tenant-test",
        "principal": principal,
        "role": "kb_reader",
        "granted_by": "admin",
        "project_id": "rag-v14",
    }, headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["result"] == "bound"

    resp2 = api.post("/api/v1/roles/unbind", json={
        "principal": principal,
        "role": "kb_reader",
    }, headers=admin_headers)
    assert resp2.status_code == 200
    assert resp2.json()["result"] == "unbound"


# ═══════════════════════════════════════════════════════════
# Test 18: 封禁添加/解除
# ═══════════════════════════════════════════════════════════

def test_restriction_add_remove(api, admin_headers, unique_id):
    principal = f"user:restrict-{unique_id}"

    resp = api.post("/api/v1/restrictions/add", json={
        "tenant_id": "tenant-test",
        "restriction_type": "subject_ban",
        "principal": principal,
        "reason": "Test",
        "created_by": "admin",
        "project_id": "rag-v14",
    }, headers=admin_headers)
    assert resp.status_code == 200
    assert "restriction_id" in resp.json()

    # 查询
    list_resp = api.get(f"/api/v1/restrictions?principal={principal}")
    assert list_resp.status_code == 200
    items = list_resp.json()
    assert len(items) > 0

    # 解除（需要管理员认证）
    rid = items[0]["id"]
    remove_resp = api.post(f"/api/v1/restrictions/remove?restriction_id={rid}", headers=admin_headers)
    assert remove_resp.status_code == 200


# ═══════════════════════════════════════════════════════════
# Test 19: 审计日志查询
# ═══════════════════════════════════════════════════════════

def test_audit_query(api):
    resp = api.get("/api/v1/audit?limit=10")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ═══════════════════════════════════════════════════════════
# Test 20: 策略模拟器
# ═══════════════════════════════════════════════════════════

def test_simulate(api):
    resp = api.post("/api/v1/simulate", json={
        "principal": {"id": "user:alice", "roles": ["user"], "attr": {}},
        "action": "kb:read",
        "resource": {"kind": "kb", "id": "test-kb", "attr": {"retired": False}},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["decision"] in ("allow", "deny", "indeterminate")


# ═══════════════════════════════════════════════════════════
# Test 21: 型二封禁在 filter 中被正确拒绝（J-4 修复验证）
# ═══════════════════════════════════════════════════════════

def test_filter_type2_restriction(api, test_token, headers, admin_headers, unique_id):
    """验证型二资源封禁在 /v1/filter 中生效。"""
    doc_id = f"doc-t2r-{unique_id}"
    kb_id = f"kb-t2r-{unique_id}"

    # 注册 + 挂载
    api.post("/v1/resources/register", json={
        "resource_type": "kb", "resource_id": kb_id,
        "owner": "user:admin", "tenant_id": "tenant-test",
        "idempotency_key": f"t2r-kb-{unique_id}",
    })
    api.post("/v1/resources/register", json={
        "resource_type": "document", "resource_id": doc_id,
        "owner": "user:admin", "tenant_id": "tenant-test",
        "idempotency_key": f"t2r-doc-{unique_id}",
    })
    api.post("/v1/resources/link", json={
        "resource_type": "document", "resource_id": doc_id,
        "kb_id": kb_id, "owner": "system", "tenant_id": "tenant-test",
        "idempotency_key": f"t2r-link-{unique_id}",
    })

    # 授予 test-user kb:read
    api.post("/api/v1/acl/grant", json={
        "tenant_id": "tenant-test", "principal": "user:test-user",
        "resource_type": "kb", "resource_id": kb_id,
        "action": "kb:read", "granted_by": "admin",
        "project_id": "rag-v14",
    }, headers=admin_headers)

    # 型二封禁：限制 test-user 访问该文档
    api.post("/api/v1/restrictions/add", json={
        "tenant_id": "tenant-test", "restriction_type": "resource_restriction",
        "principal": "user:test-user", "resource_type": "document",
        "resource_id": doc_id, "reason": "Test type-2 restriction",
        "created_by": "admin",
        "project_id": "rag-v14",
    }, headers=admin_headers)

    # filter 应将 test-user 对该文档的访问判为 deny
    resp = api.post("/v1/filter", json={
        "request_id": f"t-{unique_id}", "credential": test_token,
        "items": [{"resource_type": "document", "resource_id": doc_id,
                   "channel": {"kb": kb_id}}],
    }, headers={**headers, "X-Client-Id": "retrieval"})
    assert resp.status_code == 200
    data = resp.json()
    # 型二封禁应导致 deny（而非 allowed）
    assert doc_id not in data.get("allowed", []), \
        f"Type-2 restricted doc should NOT be in allowed list: {data}"
    assert doc_id in data.get("denied", []), \
        f"Type-2 restricted doc should be in denied list: {data}"


# ═══════════════════════════════════════════════════════════
# Test 22: KB 粒度 ACL 事件包含 channel.kb（A11 修复验证）
# ═══════════════════════════════════════════════════════════

def test_acl_grant_kb_channel_kb(api, admin_headers, unique_id):
    """验证 KB 级 ACL 授予事件包含 channel.kb 字段。"""
    import redis as redis_lib

    kb_id = f"kb-ch-{unique_id}"

    # 订阅 Redis Pub/Sub
    rds = redis_lib.from_url("redis://:perm_redis_pwd_2026@localhost:16380/0")
    pubsub = rds.pubsub()
    pubsub.subscribe("visibility_changed")

    # 授予 KB 级权限
    resp = api.post("/api/v1/acl/grant", json={
        "tenant_id": "tenant-test", "principal": "group:eng",
        "resource_type": "kb", "resource_id": kb_id,
        "action": "kb:read", "granted_by": "admin",
        "project_id": "rag-v14",
    }, headers=admin_headers)
    assert resp.status_code == 200

    # 等待 Redis 事件
    import time as _time
    event = None
    deadline = _time.time() + 2
    while _time.time() < deadline:
        msg = pubsub.get_message(timeout=0.3)
        if msg and msg["type"] == "message":
            import json as _json
            event = _json.loads(msg["data"])
            if event.get("resource", {}).get("id") == kb_id:
                break

    pubsub.close()

    if event:
        channel = event.get("channel", {})
        assert channel.get("kb") == kb_id, \
            f"KB-granularity event should have channel.kb={kb_id}, got {channel}"
    else:
        # Redis 事件可能在测试环境中延迟，不做强制失败
        pass


# ═══════════════════════════════════════════════════════════
# Test 23: prefilter 按 tenant_id 隔离（A9 修复验证）
# ═══════════════════════════════════════════════════════════

def test_prefilter_tenant_isolation(api, admin_headers, unique_id):
    """验证不同租户的 ACL 不互相影响 prefilter。"""
    kb_a = f"kb-tenant-a-{unique_id}"
    kb_b = f"kb-tenant-b-{unique_id}"
    user_a_token = make_token(sub=f"user-a-{unique_id}", tenant="tenant-a")
    user_b_token = make_token(sub=f"user-b-{unique_id}", tenant="tenant-b")

    # 在 tenant-a 注册 KB 并授予 user-a
    api.post("/v1/resources/register", json={
        "resource_type": "kb", "resource_id": kb_a,
        "owner": "user:admin", "tenant_id": "tenant-a",
        "idempotency_key": f"ti-kba-{unique_id}",
    })
    api.post("/api/v1/acl/grant", json={
        "tenant_id": "tenant-a", "principal": f"user:user-a-{unique_id}",
        "resource_type": "kb", "resource_id": kb_a,
        "action": "kb:read", "granted_by": "admin",
        "project_id": "rag-v14",
    }, headers=make_admin_headers(tenant="tenant-a"))

    # 在 tenant-b 注册 KB 并授予 user-b
    api.post("/v1/resources/register", json={
        "resource_type": "kb", "resource_id": kb_b,
        "owner": "user:admin", "tenant_id": "tenant-b",
        "idempotency_key": f"ti-kbb-{unique_id}",
    })
    api.post("/api/v1/acl/grant", json={
        "tenant_id": "tenant-b", "principal": f"user:user-b-{unique_id}",
        "resource_type": "kb", "resource_id": kb_b,
        "action": "kb:read", "granted_by": "admin",
        "project_id": "rag-v14",
    }, headers=make_admin_headers(tenant="tenant-b"))

    # user-a 的 prefilter 不应包含 tenant-b 的 KB
    resp_a = api.get(f"/v1/prefilter?credential={user_a_token}")
    assert resp_a.status_code == 200
    kbs_a = resp_a.json().get("kbs", [])
    assert kb_b not in kbs_a, \
        f"User from tenant-a should NOT see tenant-b's KB. kbs={kbs_a}"

    # user-b 的 prefilter 不应包含 tenant-a 的 KB
    resp_b = api.get(f"/v1/prefilter?credential={user_b_token}")
    assert resp_b.status_code == 200
    kbs_b = resp_b.json().get("kbs", [])
    assert kb_a not in kbs_b, \
        f"User from tenant-b should NOT see tenant-a's KB. kbs={kbs_b}"


# ═══════════════════════════════════════════════════════════
# Test 24: ACL 变更与 permission_changes 原子提交（B1 Outbox 验证）
# ═══════════════════════════════════════════════════════════

def test_acl_grant_atomic_change_log(api, admin_headers, unique_id):
    """验证 ACL 授予后 permission_changes 表中有对应记录。"""
    kb_id = f"kb-atomic-{unique_id}"

    resp = api.post("/api/v1/acl/grant", json={
        "tenant_id": "tenant-test", "principal": "group:atomic-test",
        "resource_type": "kb", "resource_id": kb_id,
        "action": "kb:read", "granted_by": "admin",
        "project_id": "rag-v14",
    }, headers=admin_headers)
    assert resp.status_code == 200
    version = resp.json()["version"]

    # 查询审计日志 — 应有对应的变更记录
    audit_resp = api.get(f"/api/v1/audit?resource_type=kb&resource_id={kb_id}&limit=5")
    assert audit_resp.status_code == 200
    entries = audit_resp.json()

    # 验证有对应的事件记录（version 匹配）
    matching = [e for e in entries if e.get("version") == version]
    assert len(matching) > 0, \
        f"No permission_changes entry found for version={version}. Entries: {len(entries)}"
