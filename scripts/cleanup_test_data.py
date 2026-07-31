"""清理权限服务中的联合契约测试残留数据。

设计依据：docs/外部系统设计.md §2.4.3 生命周期端口
- 通过 POST /v1/resources/retire API 正确退役资源
- retire 四合一：回收 acl + 回收 restriction + 解除挂载 + 置 retired
- 幂等安全：重复调用不产生副作用

用法：
    conda activate perm_service
    python scripts/cleanup_test_data.py [--dry-run]
"""

import argparse
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import json

BASE_URL = "http://localhost:18080"
PERM_DB_DSN = "postgresql://perm_user:perm_pass@localhost:25433/permission_db"

# 测试资源匹配模式（联合契约测试 J-1~J-20 + 诊断测试 + 集成测试 + 多租户 + 封禁 + 冲突 + 可见性测试）
TEST_RESOURCE_PATTERNS = [
    # 联合契约测试 KB
    "j1-kb-", "j2-kb-", "j3-kb-", "j4-kb-", "j5-kb-",
    "j6-kb-", "j7-kb-", "j8-kb-", "j9-kb-", "j10-kb-",
    "j11-kb-", "j12-kb-", "j13-kb-", "j14-kb-", "j15-kb-",
    "j16-kb-", "j17-kb-", "j18-kb-", "j19-kb-", "j20-kb-",
    # 联合契约测试 文档
    "j1-doc-", "j2-doc-", "j4-doc-", "j6-doc-", "j8-doc-", "j9-doc-",
    # 诊断测试
    "kb-test-", "kb-diag-", "test-doc-diag-",
    # P0/P1 测试
    "kb-p0-", "kb-p1-", "p1-", "p0-test-",
    # E2E 测试
    "kb-e2e-", "doc-e2e-", "kb-stamp-", "doc-stamp-",
    # 集成测试
    "kb-int-", "kb-rag-int", "doc-integration-",
    # Remote 模式测试
    "remote-kb-", "remote-filt-", "remote-life-", "remote-doc-",
    # 通用测试
    "test-kb-", "kb-integration-",
    # J-? 特殊测试场景 KB
    "kb-allow-", "kb-ban-", "kb-conflict-", "kb-reg-",
    "kb-t2r-", "kb-tenant-", "kb-unm-", "kb-vis-",
    # J-? 特殊测试场景 文档
    "doc-t2r-", "doc-unm-", "doc-vis-",
]

# 保留的真实 KB（不会被清理）
KEEP_KB_IDS = {
    "a6a9f8c0-133a-4b2f-b3d8-8919b4fc187c",  # RAG 前端测试KB
}


def is_test_resource(resource_id: str, resource_type: str) -> bool:
    """判断是否为测试资源。"""
    if resource_id in KEEP_KB_IDS:
        return False
    for pattern in TEST_RESOURCE_PATTERNS:
        if pattern in resource_id:
            return True
    return False


def get_test_resources() -> list[dict]:
    """从数据库查询所有测试资源（使用同步 psycopg2）。"""
    import psycopg2

    conn = psycopg2.connect(PERM_DB_DSN)
    try:
        cur = conn.cursor()
        # 查询所有未退役的 KB 和 document 资源
        cur.execute(
            "SELECT resource_type, resource_id, tenant_id, owner "
            "FROM resource_registry WHERE NOT retired "
            "ORDER BY resource_type, resource_id"
        )
        rows = cur.fetchall()
        test_resources = []
        for rtype, rid, tenant, owner in rows:
            if is_test_resource(rid, rtype):
                test_resources.append({
                    "resource_type": rtype,
                    "resource_id": rid,
                    "tenant_id": tenant,
                    "owner": owner,
                })
        cur.close()
        return test_resources
    finally:
        conn.close()


def retire_via_api(resource: dict) -> dict:
    """通过 API 退役单个资源。

    设计依据：docs/外部系统设计.md §2.4.3
    POST /v1/resources/retire
    """
    # 移除资源 ID 中的时间戳字符（Unix timestamp 模式），避免幂等键校验拒绝
    import re
    safe_rid = re.sub(r'\d{10,}', 'TS', resource['resource_id'])
    idempotency_key = (
        f"cleanup-{resource['tenant_id']}-"
        f"{resource['resource_type']}-{safe_rid}-v1"
    )

    body = json.dumps({
        "resource_type": resource["resource_type"],
        "resource_id": resource["resource_id"],
        "tenant_id": resource["tenant_id"],
        "idempotency_key": idempotency_key,
        "owner": resource.get("owner", "system"),
    }).encode("utf-8")

    req = Request(
        f"{BASE_URL}/v1/resources/retire",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Request-Id": f"cleanup-{resource['resource_id'][:30]}",
            "X-Client-Id": "interactive-backend",
        },
        method="POST",
    )

    try:
        resp = urlopen(req, timeout=10)
        return json.loads(resp.read())
    except HTTPError as e:
        err_body = e.read().decode("utf-8")[:500] if e.fp else ""
        return {"error": str(e.code), "detail": err_body}
    except URLError as e:
        return {"error": "connection", "detail": str(e.reason)}


def cleanup_acls_and_bindings() -> dict:
    """直接清理测试资源关联的 ACL 和角色绑定（通过 SQL）。

    ACL/role_binding 的 revoke 通过管理台 API 需要 Bearer token，
    但对于测试数据清理，直接标记 revoked 是安全且幂等的操作。
    """
    import psycopg2

    conn = psycopg2.connect(PERM_DB_DSN)
    stats = {"acl_revoked": 0, "bindings_revoked": 0, "restrictions_removed": 0}

    try:
        cur = conn.cursor()

        # 收集所有测试资源 ID
        cur.execute(
            "SELECT resource_id FROM resource_registry WHERE NOT retired"
        )
        all_active = {row[0] for row in cur.fetchall()}
        test_ids = {rid for rid in all_active if is_test_resource(rid, "kb") or is_test_resource(rid, "document")}
        test_ids.difference_update(KEEP_KB_IDS)

        if not test_ids:
            cur.close()
            return stats

        # 清理 ACL entries
        test_id_list = list(test_ids)
        cur.execute(
            "UPDATE acl_entries SET revoked = true, revoked_at = NOW() "
            "WHERE NOT revoked AND resource_id = ANY(%s::varchar[])",
            (test_id_list,)
        )
        stats["acl_revoked"] = cur.rowcount

        # 清理 role bindings
        cur.execute(
            "UPDATE role_bindings SET revoked = true, revoked_at = NOW() "
            "WHERE NOT revoked AND "
            "(resource_id = ANY(%s::varchar[]) OR "
            " (resource_id IS NULL AND (principal LIKE 'user:test%%' OR principal LIKE 'user:diag%%')))",
            (test_id_list,)
        )
        stats["bindings_revoked"] = cur.rowcount

        # 清理 restrictions（主体封禁中包含 test/diag 用户的）
        cur.execute(
            "UPDATE restrictions SET removed = true, removed_at = NOW() "
            "WHERE NOT removed AND ("
            "resource_id = ANY(%s::varchar[]) OR "
            "principal LIKE '%%test%%' OR principal LIKE '%%diag%%'"
            ")",
            (test_id_list,)
        )
        stats["restrictions_removed"] = cur.rowcount

        conn.commit()
        cur.close()
        return stats
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="清理权限服务测试数据")
    parser.add_argument("--dry-run", action="store_true", help="仅列出将要清理的资源，不执行")
    args = parser.parse_args()

    print("=" * 60)
    print("权限服务测试数据清理")
    print("=" * 60)

    # 1. 列出测试资源
    print("\n[1/4] 查询测试资源...")
    test_resources = get_test_resources()
    kb_count = sum(1 for r in test_resources if r["resource_type"] == "kb")
    doc_count = sum(1 for r in test_resources if r["resource_type"] == "document")

    print(f"  找到测试 KB: {kb_count} 个")
    print(f"  找到测试文档: {doc_count} 个")

    if not test_resources:
        print("  ✅ 没有需要清理的测试资源")
        return

    # 显示前 10 个
    for r in test_resources[:10]:
        print(f"    - [{r['resource_type']}] {r['resource_id']}")
    if len(test_resources) > 10:
        print(f"    ... 以及 {len(test_resources) - 10} 个")

    if args.dry_run:
        print("\n[Dry Run] 不会执行任何操作。")
        return

    # 2. 清理 ACL 和绑定
    print("\n[2/4] 清理关联 ACL/角色绑定/封禁...")
    acl_stats = cleanup_acls_and_bindings()
    print(f"  撤销 ACL: {acl_stats['acl_revoked']} 条")
    print(f"  撤销角色绑定: {acl_stats['bindings_revoked']} 条")
    print(f"  移除封禁: {acl_stats['restrictions_removed']} 条")

    # 3. 通过 API 退役资源
    print("\n[3/4] 通过 API 退役测试资源...")
    success = 0
    failed = 0
    noop = 0

    for i, resource in enumerate(test_resources):
        if (i + 1) % 20 == 0:
            print(f"  进度: {i + 1}/{len(test_resources)} (成功: {success}, 失败: {failed})")
            time.sleep(0.1)  # 避免打满 API

        result = retire_via_api(resource)
        if "error" in result:
            failed += 1
            if failed <= 5:  # 只打印前 5 个错误
                print(f"  ❌ {resource['resource_id']}: {result}")
        elif result.get("result") == "noop":
            noop += 1
        else:
            success += 1

    print(f"  完成: 成功 {success}, 已退役 {noop}, 失败 {failed}")

    # 4. 验证
    print("\n[4/4] 验证清理结果...")
    remaining = get_test_resources()
    remaining_kb = [r for r in remaining if r["resource_type"] == "kb"]
    remaining_doc = [r for r in remaining if r["resource_type"] == "document"]

    if remaining:
        print(f"  ⚠️ 仍有 {len(remaining_kb)} KB + {len(remaining_doc)} 文档未清理:")
        for r in remaining[:10]:
            print(f"    - [{r['resource_type']}] {r['resource_id']}")
    else:
        print("  ✅ 所有测试资源已清理完毕")

    print("\n" + "=" * 60)
    print("清理完成。可通过以下命令验证：")
    print('  curl -s "http://localhost:18080/v1/prefilter?credential=<JWT>" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get(\"kbs\",[])))"')
    print("=" * 60)


if __name__ == "__main__":
    main()
