#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
# 外部权限系统 — 启动/运维脚本（Docker 部署形态）
#
# 用法（在仓库根目录执行，或任意目录执行本脚本的绝对路径）：
#   scripts/start.sh start           # 按序启动：secrets → 基础设施+Keycloak
#                                    #            → alembic 迁移 → 权限服务 → 管理台
#   scripts/start.sh stop            # 停止全部（保留数据卷）
#   scripts/start.sh restart         # 重启全部
#   scripts/start.sh status          # 查看各服务健康状态
#   scripts/start.sh logs [服务名]    # 查看日志（-f 跟随）
#   scripts/start.sh migrate         # 手动执行数据库迁移（alembic upgrade head）
#   scripts/start.sh init-secrets    # 校验/生成 secret 文件（幂等）
#
#   BUILD=1 scripts/start.sh start   # 强制重建镜像（代码有变更时）；默认 BUILD=0 复用已有镜像秒起
#   RESET=1 scripts/start.sh start   # 全新部署：先清空全部数据卷（数据不可恢复）
#
# ── 快速开始：首次启动 ─────────────────────────────────────────────
# 1) cp .env.example .env，填写 POSTGRES_PASSWORD / PERM_REDIS_PASSWORD / EXTERNAL_HOST 等必需变量
# 2) scripts/start.sh start
#    自动完成：init_secrets（幂等）→ 基础设施+Keycloak → alembic 迁移 → 权限服务 → 管理台。
#    首次镜像缺失时 compose 自动构建（BUILD=0 下镜像缺失仍会 build）。
#    前提：Keycloak realm/client 已配置（或指向既有 IdP）；RAG 侧密钥已同步（见依赖）。
# ── 快速开始：日常运行（镜像已存在，代码无变更）────────────────────
# scripts/start.sh start            # 复用镜像，秒起（BUILD=0 默认，不构建）
# scripts/start.sh restart          # 同上
# ── 代码有变更后 ────────────────────────────────────────────────────
# BUILD=1 scripts/start.sh start    # 强制重建镜像后再启动（否则跑旧镜像）
#
# 依赖：
#   - Keycloak（本编排内 keycloak 服务或既有 IdP），realm/client 已配置
#   - scripts/init_secrets.sh 可用的 openssl
# ══════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Keycloak 已并入 docker-compose.yml，统一单一 compose 编排
COMPOSE="docker compose -f docker-compose.yml"

INFRA_TIMEOUT="${INFRA_TIMEOUT:-180}"   # 基础设施健康等待上限（秒）
APP_TIMEOUT="${APP_TIMEOUT:-180}"       # 应用就绪等待上限（秒）
BUILD="${BUILD:-0}"                     # 1=强制重建镜像，0=复用已有镜像（默认）
RESET="${RESET:-0}"                     # 1=全新部署：先清空全部数据卷（down -v，数据不可恢复）

info() { echo -e "\033[36m[i]\033[0m $*"; }
ok()   { echo -e "\033[32m[✓]\033[0m $*"; }
warn() { echo -e "\033[33m[!]\033[0m $*"; }
fail() { echo -e "\033[31m[x]\033[0m $*" >&2; exit 1; }

# ── 等待全部服务 healthy ─────────────────────────────────────────
wait_healthy() {
    local compose_cmd="$1" timeout="$2" label="$3"
    shift 3
    local wanted="$*"   # 可选：只等指定服务；空则等全部
    local waited=0 bad=""
    info "等待 $label 全部就绪（上限 ${timeout}s）..."
    while (( waited < timeout )); do
        bad="$(eval "$compose_cmd ps --format json 2>/dev/null" | python3 -c "
import json,sys
wanted=set('''$wanted'''.split())
bad=[]
for line in sys.stdin:
    line=line.strip()
    if not line: continue
    r=json.loads(line)
    name=(r.get('Service') or r.get('Name') or '?')
    if wanted and name not in wanted: continue
    h=(r.get('Health') or '').lower()
    s=(r.get('State') or '').lower()
    if s=='running' and (h=='healthy' or h==''):
        continue
    bad.append(name)
print(' '.join(bad))
" || echo "UNPARSE")"
        if [[ -z "$bad" ]]; then
            ok "$label 全部就绪"
            return 0
        fi
        if [[ "$bad" == "UNPARSE" ]]; then
            sleep 3; waited=$((waited+3)); continue
        fi
        sleep 3; waited=$((waited+3))
    done
    echo ""
    warn "超时。尚未就绪的服务: $bad"
    eval "$compose_cmd ps"
    fail "$label 启动超时（${timeout}s），请查看上方状态与日志。"
}

wait_http() {
    local url="$1" timeout="$2" label="$3"
    local waited=0
    info "等待 $label 就绪（${url}）..."
    while (( waited < timeout )); do
        if curl -fsS -m 3 "$url" >/dev/null 2>&1; then
            ok "$label 就绪"
            return 0
        fi
        sleep 3; waited=$((waited+3))
    done
    fail "$label 未在 ${timeout}s 内就绪，请检查日志: $0 logs"
}

# ── 数据库迁移（幂等） ────────────────────────────────────────────
cmd_migrate() {
    info "执行数据库迁移（alembic upgrade head）..."
    $COMPOSE run --rm --no-deps permission-service alembic upgrade head
    ok "数据库迁移完成"
}

cmd_init_secrets() {
    bash scripts/init_secrets.sh
}

# ══════════════════════════════════════════════════════════════════
# 子命令
# ══════════════════════════════════════════════════════════════════

cmd_start() {
    local build_flag=""
    [[ "$BUILD" == "1" ]] && build_flag="--build"

    # RESET=1：全新部署，先清空全部数据卷（数据不可恢复）
    if [[ "$RESET" == "1" ]]; then
        warn "RESET=1 全新部署：即将清空全部数据卷（perm_pgdata / keycloak_data / perm_redis_data / perm_cerbos_audit），数据不可恢复！"
        $COMPOSE down -v --remove-orphans
        ok "数据卷已清空，将按当前 .env 全新初始化"
    fi

    # 1. secrets 校验/生成
    cmd_init_secrets

    # 2. 读取 .env（口令预检需要 POSTGRES_PASSWORD / KC_START_MODE；compose 仍自行读 .env）
    set -a; [ -f .env ] && source .env; set +a

    # 3. 基础设施（postgres/redis/cerbos 先起；Keycloak 拆后，因需在起它前做口令预检）
    info "启动 postgres/redis/cerbos..."
    $COMPOSE up -d $build_flag perm-postgres perm-redis cerbos
    wait_healthy "$COMPOSE" "$INFRA_TIMEOUT" "postgres/redis/cerbos" perm-postgres perm-redis cerbos

    # 口令预检（fail-fast）：既有 perm_pgdata 卷口令固化，POSTGRES_PASSWORD 对非空卷不生效；
    # 用容器内 @perm-postgres:5432 命中真实 scram 认证，在起 Keycloak 前暴露漂移。
    if [ "${KC_START_MODE:-start-dev}" = "start" ]; then
        if $COMPOSE exec -T perm-postgres psql \
            "postgresql://${POSTGRES_USER:-perm_user}:${POSTGRES_PASSWORD}@perm-postgres:5432/permission_db" \
            -tAc "SELECT 1" >/dev/null 2>&1; then
            ok "perm-postgres 口令预检通过（${POSTGRES_USER:-perm_user}）"
        else
            fail "perm-postgres 数据卷口令与 .env 不一致（perm_user 认证失败）。\n    处理：RESET=1 scripts/start.sh start 全量重建（清空数据卷，数据不可恢复）；\n          或恢复该卷首次初始化时的原 POSTGRES_PASSWORD 到 .env。"
        fi
        # 确保 keycloak 数据库存在（须在 Keycloak 启动前，否则全新空卷起不来）
        info "确保 perm-postgres 中 keycloak 数据库存在..."
        $COMPOSE exec -T perm-postgres psql -U perm_user -d permission_db -tAc \
            "SELECT 1 FROM pg_database WHERE datname='keycloak'" 2>/dev/null | grep -q 1 \
            || $COMPOSE exec -T perm-postgres psql -U perm_user -d permission_db -c "CREATE DATABASE keycloak" 2>&1 | tail -1
        ok "keycloak 数据库就绪（perm-postgres/keycloak）"
    fi

    # 4. Keycloak IdP
    info "启动 Keycloak..."
    $COMPOSE up -d $build_flag keycloak
    wait_healthy "$COMPOSE" "$INFRA_TIMEOUT" "Keycloak" keycloak

    # 4b. Keycloak realm/client/roles/用户 幂等 bootstrap（自愈 realm；RESET=1 后也能重建，与 deploy 等价）。
    #     若 bootstrap 写回 keycloak_client_secret 且值变化 → 后续强制重建 permission-service 刷新 secret 快照。
    _cs_before="$(cat permission-service/config/keycloak_client_secret 2>/dev/null | sha256sum | awk '{print $1}')"
    bash scripts/keycloak_bootstrap.sh || warn "keycloak_bootstrap 失败——realm 可能不完整，请查日志"
    _cs_after="$(cat permission-service/config/keycloak_client_secret 2>/dev/null | sha256sum | awk '{print $1}')"
    RECREATE_PERM="0"
    if [ -n "$_cs_before" ] && [ -n "$_cs_after" ] && [ "$_cs_before" != "$_cs_after" ]; then
        RECREATE_PERM="1"
        info "keycloak client secret 已变化——permission-service 将强制重建刷新 secret 快照"
    fi

    # 5. 数据库迁移（先于服务启动）
    cmd_migrate

    # 6. 权限服务后端（depends_on keycloak healthy）
    local rec_flag=""; [ "$RECREATE_PERM" = "1" ] && rec_flag="--force-recreate"
    info "启动 permission-service..."
    $COMPOSE up -d $build_flag $rec_flag permission-service
    wait_healthy "$COMPOSE" "$INFRA_TIMEOUT" "permission-service"
    wait_http "http://localhost:${PERMISSION_SERVICE_HOST_PORT:-18080}/healthz" "$APP_TIMEOUT" "permission-service(/healthz)"

    # 6b. project_api_keys key_hash 与当前 service_api_key 对齐（与 deploy.sh 一致，防 start.sh 路径分叉）
    info "对齐 project_api_keys 与当前 service_api_key..."
    NEWKEY_HASH="$(tr -d '\n\r' < permission-service/config/service_api_key | sha256sum | awk '{print $1}')"
    docker exec permission-service sh -c "python3 -c \"
import asyncio
from app.database import async_session
from sqlalchemy import text
async def _sync():
    async with async_session() as s:
        rows = await s.execute(text(\\\"SELECT project_id, key_hash FROM project_api_keys\\\"))
        for r in rows:
            if r.key_hash != '$NEWKEY_HASH':
                await s.execute(text(\\\"UPDATE project_api_keys SET key_hash='$NEWKEY_HASH' WHERE project_id=:p\\\").bindparams(p=r.project_id))
                print('synced project_api_keys:', r.project_id, '->', '$NEWKEY_HASH'[:12])
        await s.commit()
asyncio.run(_sync())
\"" 2>&1 | tail -2
    ok "project_api_keys 已与当前 service_api_key 对齐"

    # 7. 管理台前端 + 独立入口 nginx（SSO 统一入口）
    #     permission-nginx 必须一并拉起：它是 /realms 与 /admin 的 SSO 入口，
    #     漏起会导致登录跳转 http://EXTERNAL_HOST:18081/realms/... 不可达（页面不可访问）。
    info "启动 admin-console + permission-nginx..."
    $COMPOSE up -d $build_flag admin-console permission-nginx
    wait_http "http://localhost:${ADMIN_CONSOLE_HOST_PORT:-3002}" "$APP_TIMEOUT" "admin-console(:3002)"
    wait_http "http://localhost:${PERMISSION_NGINX_PORT:-18081}/realms/${KEYCLOAK_REALM:-rag-v14}/.well-known/openid-configuration" "$APP_TIMEOUT" "permission-nginx(:18081 SSO)"

    # 8. 部署后自检（smoke_check）
    info "运行 smoke 检查（scripts/smoke_check.sh）..."
    if bash scripts/smoke_check.sh; then
        ok "smoke 检查全部通过"
    else
        warn "smoke 检查存在失败项——请按上方 [x] 提示修复后重跑：bash scripts/smoke_check.sh"
    fi

    echo ""
    echo "══════ 外部权限系统已启动 ══════"
    echo "  权限服务   http://localhost:${PERMISSION_SERVICE_HOST_PORT:-18080}   (/healthz)"
    echo "  管理台     http://localhost:${ADMIN_CONSOLE_HOST_PORT:-3002}"
    echo "  Keycloak   http://localhost:${KEYCLOAK_HOST_PORT:-8080}"
    echo "  Cerbos     http://localhost:${CERBOS_HTTP_PORT:-14592}"
    echo "  日志       scripts/start.sh logs [-f] [服务名]"
    echo "  状态       scripts/start.sh status"
    echo ""
    echo "  RAG 联调核对（见 docs/ops/权限系统上线运维手册.md）:"
    echo "    AUTHZ_SERVICE_URL = http://<rag-host>:${PERMISSION_SERVICE_HOST_PORT:-18080}"
    echo "    SERVICE_API_KEY   = permission-service/config/service_api_key"
    echo "    事件流 Redis      = perm-redis :${PERM_REDIS_HOST_PORT:-16380} 的 visibility_changed 频道"
    echo "═══════════════════════════════"
}

cmd_stop() {
    $COMPOSE down
    ok "已停止（数据卷保留）。"
}

cmd_restart() {
    cmd_stop
    cmd_start
}

cmd_status() {
    $COMPOSE ps
}

cmd_logs() {
    local svc="${1:-}"
    if [[ "$svc" == "-f" || "$svc" == "--follow" ]]; then
        $COMPOSE logs -f --tail=200
    elif [[ -n "$svc" ]]; then
        $COMPOSE logs -f --tail=300 "$svc"
    else
        $COMPOSE logs --tail=200
    fi
}

cmd_help() {
    sed -n '1,22p' "$0" | sed 's/^# \{0,1\}//' | sed '1d'
}

case "${1:-help}" in
    start)         shift; cmd_start "$@" ;;
    stop)          shift; cmd_stop "$@" ;;
    restart)       shift; cmd_restart "$@" ;;
    status)        shift; cmd_status "$@" ;;
    logs)          shift; cmd_logs "$@" ;;
    migrate)       shift; cmd_migrate "$@" ;;
    init-secrets)  shift; cmd_init_secrets "$@" ;;
    backup)        bash scripts/backup.sh ;;
    help|--help|-h) cmd_help ;;
    *)             fail "未知命令: $1（可用: start|stop|restart|status|logs|migrate|init-secrets|help）" ;;
esac
