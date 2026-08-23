#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
# 权限系统 — 生产部署脚本（把启动环境准备好）
#
# 四步配置模型：
#   1) 校验基础设施依赖正常运行
#   2) 处理可自动生成的配置值（强随机口令/密钥）
#   3) 处理需读取环境/基础设施的配置值（secret 文件、JWT 同步、可观测端点）
#   4) 处理需人工设置的值（缺失即 fail，并说明是什么值、从哪获取）
#
# 用法（仓库根目录执行）：
#   bash scripts/deploy.sh                 # 生产部署（默认 PRODUCTION=true）
#   PRODUCTION=0 bash scripts/deploy.sh    # 本地/联调（跳过强凭据强制）
#   BUILD=1 bash scripts/deploy.sh         # 强制重建镜像
#
# 人工设置值（第 4 步，缺失会 fail）：
#   EXTERNAL_HOST        浏览器访问管理台/Keycloak 的地址（域名或公网 IP）。
#                         来源：部署机的公网/可路由地址；改端口见 .env 的 *_HOST_PORT。
#   KEYCLOAK_ADMIN_PASSWORD  Keycloak master 管理员口令（首次初始化 keycloak 数据卷时固化，
#                         之后不再变）。来源：运维保管；全新部署可省略（脚本自动生成强口令）。
#
# 依赖：docker；宿主机已部署 RAG 系统（JWT 密钥对来源，见 RAG_CONFIG_DIR）。
# ══════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

COMPOSE="docker compose"
INFRA_SERVICES="perm-postgres perm-redis cerbos keycloak"
APP_SERVICES="permission-service admin-console"
INFRA_TIMEOUT="${INFRA_TIMEOUT:-180}"
APP_TIMEOUT="${APP_TIMEOUT:-120}"
BUILD="${BUILD:-0}"
# 部署意图：shell 显式设置（PRODUCTION=0 关 / PRODUCTION=true 开）优先，默认 true（生产）。
# .env 里的旧 PRODUCTION=false 是仓库开发默认，会被下方「生产强制」块覆盖，不在此覆盖部署意图。
PRODUCTION_INTENT="${PRODUCTION:-true}"
PRODUCTION="$PRODUCTION_INTENT"
RAG_CONFIG_DIR="${RAG_CONFIG_DIR:-/home/mfkcel/proj_rag_dev/config}"
ENV_FILE="$REPO_ROOT/.env"

info() { echo -e "\033[36m[i]\033[0m $*"; }
ok()   { echo -e "\033[32m[✓]\033[0m $*"; }
warn() { echo -e "\033[33m[!]\033[0m $*"; }
fail() { echo -e "\033[31m[x]\033[0m $*" >&2; exit 1; }

# ── 1. Pre-flight：docker 可用 ─────────────────────────────────────
command -v docker >/dev/null || fail "缺少 docker 命令"
docker info >/dev/null 2>&1 || fail "docker daemon 不可用"
command -v openssl >/dev/null || fail "缺少 openssl"

# ── 2. Env 准备 ───────────────────────────────────────────────────
[ -f "$ENV_FILE" ] || fail "缺少 .env——请 cp .env.example .env 后运行"

# 2a. 读取现有 .env（EXTERNAL_HOST 等）
set -a; source "$ENV_FILE"; set +a
# .env 的旧 PRODUCTION=false 不覆盖部署意图（PRODUCTION=true 默认）
PRODUCTION="$PRODUCTION_INTENT"; export PRODUCTION

# ── 第 4 步：人工值 ──
# EXTERNAL_HOST：浏览器可达地址
if [ -z "${EXTERNAL_HOST:-}" ]; then
    fail "EXTERNAL_HOST 未设置。\n    值：浏览器访问管理台/Keycloak 的地址（域名或公网 IP，如 http://rag.example.com 或 http://203.0.113.5）。\n    获取：部署机对外地址 / 域名解析。设置后写入 .env 的 EXTERNAL_HOST。"
fi

# KEYCLOAK_ADMIN_PASSWORD：keycloak 数据卷首次初始化时固化，之后不再变
KC_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-}"
if [ -z "$KC_ADMIN_PASSWORD" ] && [ -f "permission-service/config/keycloak_admin_password" ]; then
    KC_ADMIN_PASSWORD="$(cat permission-service/config/keycloak_admin_password)"
fi
if [ -z "$KC_ADMIN_PASSWORD" ]; then
    # 全新 keycloak 卷：自动生成强口令（旧卷口令已固化，无法改）
    if ! docker volume ls --format '{{.Name}}' | grep -q '^permission-system_keycloak_data$'; then
        KC_ADMIN_PASSWORD="$(openssl rand -base64 24 | tr -d '/+=')"
        ok "已生成新的 Keycloak admin 口令（首次初始化将固化，务必保管）"
    else
        fail "KEYCLOAK_ADMIN_PASSWORD 未设置，且已有 keycloak 数据卷（口令已固化无法改）。\n    值：既有 keycloak 卷的 master admin 口令（可能是部署早期设置的旧值）。\n    获取：向当时的部署者/运维索取；或删除 keycloak 数据卷后重新初始化（会清空 realm 数据）。"
    fi
fi
export KEYCLOAK_ADMIN_PASSWORD
# 同时写入 .env 供 compose 插值（比依赖 shell export 更稳），并持久化到 secret 文件
if grep -q '^KEYCLOAK_ADMIN_PASSWORD=' "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^KEYCLOAK_ADMIN_PASSWORD=.*|KEYCLOAK_ADMIN_PASSWORD=${KC_ADMIN_PASSWORD}|" "$ENV_FILE"
else
    echo "KEYCLOAK_ADMIN_PASSWORD=${KC_ADMIN_PASSWORD}" >> "$ENV_FILE"
fi
# 持久化到 secret 文件（permission-service 经 _FILE 读取）
mkdir -p permission-service/config
umask 077
printf '%s' "$KC_ADMIN_PASSWORD" > permission-service/config/keycloak_admin_password

# ── 第 2 步：自动生成强口令（仅缺失时） ──
gen_env_if_missing() { # key [hex_length]
    local key="$1" len="${2:-24}"
    if ! grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        echo "${key}=$(openssl rand -hex "$len")" >> "$ENV_FILE"
        ok "已生成 $key 并写入 .env"
    fi
}
gen_env_if_missing POSTGRES_PASSWORD 24
gen_env_if_missing PERM_REDIS_PASSWORD 24

# 生产强制：POSTGRES/REDIS 口令不得为默认弱值
if [ "$PRODUCTION" = "true" ]; then
    if grep -qE '^(POSTGRES_PASSWORD|PERM_REDIS_PASSWORD)=.*(perm_pass|perm_redis_pwd_2026).*$' "$ENV_FILE"; then
        fail "PRODUCTION=true 但 .env 仍是默认弱口令（perm_pass/perm_redis_pwd_2026）。\n    值：数据库/Redis 强随机口令。\n    获取：脚本可自动生成（删除 .env 里这两行后重跑）；若数据卷已用旧口令初始化，需删除数据卷重建（会清空数据）。"
    fi
    # .env 置 PRODUCTION=true
    if ! grep -q '^PRODUCTION=true$' "$ENV_FILE"; then
        sed -i 's/^PRODUCTION=.*/PRODUCTION=true/' "$ENV_FILE"
        ok "已置 .env PRODUCTION=true"
    fi
fi

# 重新加载（含新生成的口令）
set -a; source "$ENV_FILE"; set +a

# ── 第 3 步：读取环境/基础设施 ──
# OTel / Langfuse（可选，fail-open）：由 .env 的 OTEL_EXPORTER_OTLP_ENDPOINT / LANGFUSE_* 提供
# JWT 密钥对：从 RAG 侧同步（init_secrets.sh 处理，RAG 为源）

# ── 3. init_secrets：9 个 secret 校验/生成（幂等；JWT 从 RAG 拷贝） ──
info "初始化 secret（init_secrets.sh，幂等）..."
bash scripts/init_secrets.sh || fail "init_secrets 失败（检查 RAG_CONFIG_DIR=$RAG_CONFIG_DIR 下 JWT 密钥是否存在）"
ok "secret 就绪"

# JWT 密钥轮换同步：init_secrets 幂等跳过已存在文件，RAG 轮换后此处强制同步（RAG 为权威）
# 否则 RAG 私钥与权限公钥失配 → 权限服务验签失败（Invalid credential）
NEED_RECREATE_PERM="0"
if [ -f "$RAG_CONFIG_DIR/jwt_public.pem" ] && [ -f "permission-service/config/jwt_public.pem" ]; then
    if ! cmp -s "$RAG_CONFIG_DIR/jwt_public.pem" "permission-service/config/jwt_public.pem"; then
        cp "$RAG_CONFIG_DIR/jwt_public.pem" permission-service/config/jwt_public.pem
        cp "$RAG_CONFIG_DIR/jwt_private.pem" permission-service/config/jwt_private.pem
        chmod 600 permission-service/config/jwt_private.pem
        warn "检测到 RAG JWT 密钥已轮换，已重新同步到权限侧（permission-service 将强制重建使 Docker secret 生效）"
        NEED_RECREATE_PERM="1"
    fi
fi

# ── 4. 起基础设施 + Keycloak，等待 healthy ───────────────────────
info "启动基础设施 + Keycloak（$INFRA_SERVICES）..."
$COMPOSE up -d $INFRA_SERVICES
# 等待全部 healthy
local_waited=0
while (( local_waited < INFRA_TIMEOUT )); do
    bad="$($COMPOSE ps --format json 2>/dev/null | python3 -c "
import json,sys
bad=[]
for line in sys.stdin:
    line=line.strip()
    if not line: continue
    r=json.loads(line)
    h=(r.get('Health') or '').lower(); s=(r.get('State') or '').lower()
    if s=='running' and (h=='healthy' or h==''): continue
    bad.append(r.get('Service') or r.get('Name') or '?')
print(' '.join(bad))
" || echo UNPARSE)"
    if [ -z "$bad" ]; then ok "基础设施全部就绪"; break; fi
    sleep 3; local_waited=$((local_waited+3))
done
if (( local_waited >= INFRA_TIMEOUT )); then
    $COMPOSE ps; fail "基础设施启动超时（${INFRA_TIMEOUT}s）：$bad"
fi

# 硬校验：keycloak 容器实际口令必须与 secret 一致（防 env 未透传导致弱口令 admin123 固化）
_KCN="$(docker exec perm-keycloak sh -c 'printf "%s" "$KEYCLOAK_ADMIN_PASSWORD"' 2>/dev/null || true)"
if [ -n "$_KCN" ] && [ "$_KCN" != "$KC_ADMIN_PASSWORD" ]; then
    fail "keycloak 容器口令与 secret 不一致（容器 ${#_KCN} 字符 vs secret ${#KC_ADMIN_PASSWORD} 字符）。\n    KEYCLOAK_ADMIN_PASSWORD 未正确注入 compose——请修复后删除 keycloak 数据卷重新部署。"
fi
ok "keycloak 口令已注入（与 secret 一致）"

# ── 5. Keycloak realm/client/roles/mappers 幂等导入 ───────────────
info "Keycloak realm/client 自动导入（kcadm）..."
bash scripts/keycloak_bootstrap.sh || fail "keycloak_bootstrap 失败"

# ── 6. alembic 迁移（先于服务启动） ───────────────────────────────
info "数据库迁移（alembic upgrade head）..."
$COMPOSE run --rm --no-deps permission-service alembic upgrade head || fail "alembic 迁移失败"
ok "数据库迁移完成"

# ── 7. 起 permission-service + admin-console ──────────────────────
build_flag=""; [ "$BUILD" = "1" ] && build_flag="--build"
# JWT 轮换同步后需强制重建 permission-service（Docker secret 在容器创建时快照）
[ "$NEED_RECREATE_PERM" = "1" ] && build_flag="$build_flag --force-recreate permission-service"
info "启动 permission-service / admin-console..."
$COMPOSE up -d $build_flag permission-service admin-console

# 验证
local_waited=0
while (( local_waited < APP_TIMEOUT )); do
    if curl -fsS -m 3 "http://localhost:${PERMISSION_SERVICE_HOST_PORT:-18080}/healthz" >/dev/null 2>&1; then
        ok "permission-service :${PERMISSION_SERVICE_HOST_PORT:-18080}/healthz 就绪"; break
    fi
    sleep 3; local_waited=$((local_waited+3))
done
if (( local_waited >= APP_TIMEOUT )); then
    warn "permission-service 未在 ${APP_TIMEOUT}s 内就绪，请查日志: $0 logs permission-service"
fi

# service_api_key 读取（供 RAG 侧对接；不回显明文，只提示）
if [ -f "permission-service/config/service_api_key" ]; then
    ok "service_api_key 已就绪（RAG 侧 AUTHZ_CLIENT_CREDENTIAL 需与此一致；同主机 deploy 会自动读取）"
fi

echo ""
echo "══════ 权限系统已部署 ══════"
echo "  permission-service   http://${EXTERNAL_HOST}:${PERMISSION_SERVICE_HOST_PORT:-18080}  (/healthz)"
echo "  管理台                http://${EXTERNAL_HOST}:${ADMIN_CONSOLE_HOST_PORT:-3002}"
echo "  Keycloak             http://${EXTERNAL_HOST}:${KEYCLOAK_HOST_PORT:-8080}"
echo "  PRODUCTION=${PRODUCTION}"
echo ""
echo "  与 RAG 对接（deploy RAG 会自动读取）："
echo "    AUTHZ_CLIENT_CREDENTIAL = permission-service/config/service_api_key"
echo "    JWT 公钥               = permission-service/config/jwt_public.pem"
echo "    事件流 Redis           = perm-redis :${PERM_REDIS_HOST_PORT:-16380}"
echo "  ⚠ TLS 由外部 LB/Ingress 终结；本机直接部署请确保网络安全边界"
echo "═══════════════════════════"
