#!/bin/bash
# Cerbos 策略同步验证脚本
# 比对两个策略目录的差异，用于确认权限平台与接入方（如各业务系统本地副本）
# 使用同一份策略。
#
# 用法:
#   scripts/verify_cerbos_policy_sync.sh <目录A> <目录B> [项目ID]
#
# 未指定项目 ID 时比对整个策略树；指定时只比对该项目命名空间。
# 目录也可通过环境变量提供：CERBOS_POLICIES_DIR_A / CERBOS_POLICIES_DIR_B

set -euo pipefail

DIR_A="${1:-${CERBOS_POLICIES_DIR_A:-}}"
DIR_B="${2:-${CERBOS_POLICIES_DIR_B:-}}"
PROJECT="${3:-}"

if [[ -z "$DIR_A" || -z "$DIR_B" ]]; then
    echo "用法: $0 <目录A> <目录B> [项目ID]" >&2
    echo "或设置 CERBOS_POLICIES_DIR_A / CERBOS_POLICIES_DIR_B 环境变量" >&2
    exit 2
fi

if [[ -n "$PROJECT" ]]; then
    DIR_A="$DIR_A/$PROJECT"
    DIR_B="$DIR_B/$PROJECT"
fi

for d in "$DIR_A" "$DIR_B"; do
    if [[ ! -d "$d" ]]; then
        echo "目录不存在: $d" >&2
        exit 2
    fi
done

echo "=== Cerbos Policy Sync Verification ==="
echo "A: $DIR_A"
echo "B: $DIR_B"
echo ""

# 只比对策略文件本身，忽略 .versions/ 归档目录
if diff -r -q -x '.versions' "$DIR_A" "$DIR_B" > /dev/null 2>&1; then
    echo "策略一致。"
    exit 0
fi

echo "存在差异："
diff -r -x '.versions' "$DIR_A" "$DIR_B" || true
exit 1
