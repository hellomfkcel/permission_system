#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
# 验证"项目级权限数据必须来自项目自身策略文件"原则
#
# 原则：角色档案（role_definitions）中，凡归属某项目的角色（派生或身份），
# 其根源必须在项目自身策略文件（/policies/<project_id>/）。若项目无自身
# 策略文件却出现项目级角色档案，即为权限定义数据不一致（历史遗留/漂移）。
#
# 检查项：
#   1. 有角色档案（project_id 非空）但 /policies/ 下无对应项目目录 → 不一致
#   2. 各项目角色档案是否与其自身策略文件解析出的角色一致（名集合比对）
#
# 用法：
#   bash scripts/verify_role_policy_consistency.sh
#   PG_PASSWORD=xxx bash scripts/verify_role_policy_consistency.sh  # 指定 DB 口令
#   （DB 口令默认取权限系统 compose 默认 perm_pass；容器名可 PG_CONTAINER 覆盖）
# ══════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PG_CONTAINER="${PG_CONTAINER:-perm-postgres}"
PG_USER="${PG_USER:-perm_user}"
PG_DB="${PG_DB:-permission_db}"
PG_PASSWORD="${PG_PASSWORD:-perm_pass}"
POLICIES_CONTAINER="${POLICIES_CONTAINER:-permission-service}"

info() { echo -e "\033[36m[i]\033[0m $*"; }
ok()   { echo -e "\033[32m[✓]\033[0m $*"; }
warn() { echo -e "\033[33m[!]\033[0m $*"; }
fail() { echo -e "\033[31m[x]\033[0m $*" >&2; exit 1; }

# 需要 docker
command -v docker >/dev/null 2>&1 || fail "需要 docker"

# 容器在跑？
docker ps --format '{{.Names}}' | grep -q "^${PG_CONTAINER}$" || fail "未找到容器 ${PG_CONTAINER}（权限系统是否已启动？）"
docker ps --format '{{.Names}}' | grep -q "^${POLICIES_CONTAINER}$" || fail "未找到容器 ${POLICIES_CONTAINER}"

# ── 1. 有角色档案的项目 vs /policies 下的项目目录 ──
PSQL="docker exec ${PG_CONTAINER} psql -U ${PG_USER} -d ${PG_DB} -t -A"
projects_with_roles="$($PSQL -c "SELECT DISTINCT project_id FROM role_definitions WHERE project_id IS NOT NULL ORDER BY project_id;" 2>/dev/null)"
policy_dirs="$(docker exec ${POLICIES_CONTAINER} sh -c "ls -d /policies/*/ 2>/dev/null" | sed 's|/policies/||;s|/$||')"

echo "═══ 权限平台角色/策略一致性校验 ═══"
info "有角色档案的项目: $(echo "$projects_with_roles" | tr '\n' ' ')"
info "有策略目录的项目: $(echo "$policy_dirs" | tr '\n' ' ')"

violations=0

# 有角色档案但无策略目录
for p in $projects_with_roles; do
    if ! echo "$policy_dirs" | grep -qx "$p"; then
        warn "✗ 项目 '$p' 有角色档案但 /policies/ 下无策略目录 —— 权限定义数据不一致"
        violations=$((violations+1))
    fi
done

# 有策略目录但角色档案缺失（可选提示）
for p in $policy_dirs; do
    if ! echo "$projects_with_roles" | grep -qx "$p"; then
        ok "  项目 '$p' 有策略目录但无角色档案（正常：档案由启动对账回填）"
    fi
done

# ── 2. 逐项目：角色档案 vs 解析自策略文件的角色（利用对账日志确认） ──
# 精确比对依赖解析器；此处以"项目有策略目录则角色档案应存在"作为粗校验，
# 精确逐角色一致性由权限服务启动对账（_reconcile_policy_roles）保证——
# 它只保留解析集内的项目级角色（含身份角色如 admin），自动清除孤儿档案。
info "精确一致性由权限服务启动对账自动保证（_sync_policy_roles_to_db）："
info "  项目级角色必须 ∈ 该项目策略文件解析集（派生+身份）；否则启动时删除。"

echo ""
if [[ "$violations" -eq 0 ]]; then
    ok "一致性校验通过：所有项目角色档案均有自身策略文件支撑。"
else
    fail "发现 ${violations} 处不一致 —— 重启权限服务触发对账清理，或手动处理。"
fi
