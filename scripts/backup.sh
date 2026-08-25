#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
# 权限系统 — 数据备份脚本
#
# 产物：backups/YYYYMMDD-HHMMSS/ 下
#   - perm-pg.sql           权限库 PostgreSQL 逻辑备份（ACL/角色/项目）
#   - perm-redis.tar.gz     perm-redis 卷快照（事件流/任务）
#   - keycloak.tar.gz       Keycloak 卷快照（realm 配置 + 用户；KC24 kcadm 已移除 export，用卷快照）
#   - cerbos-audit.tar.gz   Cerbos 审计卷快照（判定审计，见保留期说明）
#   - perm-config.tar.gz    权限服务配置/secret 目录（keycloak_admin_password 等不可再生文件，
#                            含 keycloak_seed_users——RESET 后重建业务账号的依据）
#
# 用法（仓库根目录执行）：
#   bash scripts/backup.sh [保留份数=7]
#   scripts/start.sh backup
#
# 调度（生产建议，宿主 crontab）：
#   30 2 * * * cd /home/mfkcel/permission-system && bash scripts/backup.sh 7 >> /var/log/perm-backup.log 2>&1
# ══════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

KEEP="${1:-7}"
BACKUP_ROOT="$REPO_ROOT/backups"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_ROOT/$STAMP"

info() { echo -e "\033[36m[i]\033[0m $*"; }
ok()   { echo -e "\033[32m[✓]\033[0m $*"; }
warn() { echo -e "\033[33m[!]\033[0m $*"; }
fail() { echo -e "\033[31m[x]\033[0m $*" >&2; exit 1; }

command -v docker >/dev/null || fail "缺少 docker"
mkdir -p "$DEST"
info "备份到 $DEST ..."

# 1. 权限库 PostgreSQL 逻辑备份
info "权限库逻辑备份..."
if docker compose exec -T perm-postgres pg_dump -U perm_user permission_db > "$DEST/perm-pg.sql" 2>/dev/null; then
    ok "perm-pg.sql ($(du -h "$DEST/perm-pg.sql" | cut -f1))"
else
    fail "pg_dump 失败"
fi

# 2. perm-redis 卷快照
info "perm-redis 卷快照..."
if docker run --rm -v permission-system_perm_redis_data:/data -v "$DEST":/backup alpine \
    sh -c 'tar czf /backup/perm-redis.tar.gz -C /data .' 2>/dev/null; then
    ok "perm-redis.tar.gz"
else
    warn "perm-redis 卷快照失败"
fi

# 3. Keycloak 卷快照（realm 配置 + 用户；KC 24 的 kcadm 已移除 export，用卷快照）
info "Keycloak 卷快照..."
if docker run --rm -v permission-system_keycloak_data:/data -v "$DEST":/backup alpine \
    sh -c 'tar czf /backup/keycloak.tar.gz -C /data .' 2>/dev/null; then
    ok "keycloak.tar.gz"
else
    warn "Keycloak 卷快照失败（可跳过；恢复时可用 keycloak_bootstrap.sh 重建 realm）"
fi

# 4. Cerbos 审计卷快照（判定审计；保留期由 cerbos .cerbos.yaml retentionPeriod 控制）
info "Cerbos 审计卷快照..."
if docker run --rm -v permission-system_perm_cerbos_audit:/data -v "$DEST":/backup alpine \
    sh -c 'tar czf /backup/cerbos-audit.tar.gz -C /data .' 2>/dev/null; then
    ok "cerbos-audit.tar.gz"
else
    warn "Cerbos 审计卷快照失败"
fi

# 4.5 配置/secret 目录（keycloak_admin_password 等不可再生文件；全系统唯一恢复阻塞点的兜底）
info "配置/secret 备份（permission-service/config/）..."
if [ -d permission-service/config ] && [ -n "$(ls -A permission-service/config 2>/dev/null)" ]; then
    if tar czf "$DEST/perm-config.tar.gz" -C "$REPO_ROOT" permission-service/config 2>/dev/null; then
        ok "perm-config.tar.gz（含 keycloak_admin_password / service_api_key / ctx_token_secret / keycloak_seed_users 等）"
    else
        warn "perm-config.tar.gz 打包失败"
    fi
else
    warn "permission-service/config 为空或不存在，跳过配置备份"
fi

# 5. 保留轮转
old=$(ls -1d "$BACKUP_ROOT"/*/ 2>/dev/null | sort | head -n -"$KEEP" || true)
if [ -n "$old" ]; then
    echo "$old" | xargs -r rm -rf
    ok "已清理 $(echo "$old" | wc -l) 份旧备份（保留 $KEEP 份）"
fi

echo ""
ok "备份完成：$DEST（$(du -sh "$DEST" | cut -f1)）"
ls -lh "$DEST" | tail -n +2 | awk '{printf "  %-28s %s\n", $9, $5}'
