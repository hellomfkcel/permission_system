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
BUILD="${BUILD:-1}"                     # 1=构建镜像，0=仅拉取/复用已有镜像

info() { echo -e "\033[36m[i]\033[0m $*"; }
ok()   { echo -e "\033[32m[✓]\033[0m $*"; }
warn() { echo -e "\033[33m[!]\033[0m $*"; }
fail() { echo -e "\033[31m[x]\033[0m $*" >&2; exit 1; }

# ── 等待全部服务 healthy ─────────────────────────────────────────
wait_healthy() {
    local compose_cmd="$1" timeout="$2" label="$3"
    local waited=0 bad=""
    info "等待 $label 全部就绪（上限 ${timeout}s）..."
    while (( waited < timeout )); do
        bad="$(eval "$compose_cmd ps --format json 2>/dev/null" | python3 -c "
import json,sys
bad=[]
for line in sys.stdin:
    line=line.strip()
    if not line: continue
    r=json.loads(line)
    h=(r.get('Health') or '').lower()
    s=(r.get('State') or '').lower()
    if s=='running' and (h=='healthy' or h==''):
        continue
    bad.append(r.get('Service') or r.get('Name') or '?')
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

    # 1. secrets 校验/生成
    cmd_init_secrets

    # 2. 基础设施 + Keycloak IdP（同一 compose，内部按 depends_on healthy 排序）
    info "启动基础设施（perm-postgres / perm-redis / cerbos）+ Keycloak..."
    $COMPOSE up -d $build_flag perm-postgres perm-redis cerbos keycloak
    wait_healthy "$COMPOSE" "$INFRA_TIMEOUT" "基础设施 + Keycloak"

    # 3. 数据库迁移（先于服务启动）
    cmd_migrate

    # 4. 权限服务后端（depends_on keycloak healthy）
    info "启动 permission-service..."
    $COMPOSE up -d $build_flag permission-service
    wait_healthy "$COMPOSE" "$INFRA_TIMEOUT" "permission-service"
    wait_http "http://localhost:${PERMISSION_SERVICE_HOST_PORT:-18080}/healthz" "$APP_TIMEOUT" "permission-service(/healthz)"

    # 5. 管理台前端
    info "启动 admin-console..."
    $COMPOSE up -d $build_flag admin-console
    wait_http "http://localhost:${ADMIN_CONSOLE_HOST_PORT:-3002}" "$APP_TIMEOUT" "admin-console(:3002)"

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
    help|--help|-h) cmd_help ;;
    *)             fail "未知命令: $1（可用: start|stop|restart|status|logs|migrate|init-secrets|help）" ;;
esac
