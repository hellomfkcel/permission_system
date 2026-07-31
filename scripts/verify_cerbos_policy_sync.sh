#!/bin/bash
# Cerbos 策略同步验证脚本
# 验证 permission-system 和 RAG 系统之间的 Cerbos 策略一致性
# 设计依据：docs/外部系统设计.md §9 文件索引 + docs/RAG系统设计v14.md §27.1

set -e

PERM_DIR="/home/mfkcel/permission-system/cerbos/policies"
RAG_DIR="/home/mfkcel/proj_rag_dev/cerbos/policies"

echo "=== Cerbos Policy Sync Verification ==="
echo "Permission System: $PERM_DIR"
echo "RAG System: $RAG_DIR"
echo ""

HAS_DIFF=0

# Compare derived roles
if diff -q "$PERM_DIR/derived_roles/rag_roles.yaml" "$RAG_DIR/derived_roles/rag_roles.yaml" > /dev/null 2>&1; then
    echo "✅ derived_roles/rag_roles.yaml — IDENTICAL"
else
    echo "❌ derived_roles/rag_roles.yaml — DIFFERS!"
    diff "$PERM_DIR/derived_roles/rag_roles.yaml" "$RAG_DIR/derived_roles/rag_roles.yaml"
    HAS_DIFF=1
fi

# Compare resource policies
for policy in kb.yaml document.yaml; do
    if diff -q "$PERM_DIR/resource_policies/$policy" "$RAG_DIR/resource_policies/$policy" > /dev/null 2>&1; then
        echo "✅ resource_policies/$policy — IDENTICAL"
    else
        echo "❌ resource_policies/$policy — DIFFERS!"
        diff "$PERM_DIR/resource_policies/$policy" "$RAG_DIR/resource_policies/$policy"
        HAS_DIFF=1
    fi
done

echo ""
if [ $HAS_DIFF -eq 0 ]; then
    echo "✅ All Cerbos policies are synchronized between systems."
else
    echo "❌ Policy divergence detected. Sync required!"
    exit 1
fi
