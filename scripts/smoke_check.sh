#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
# 权限系统 — 部署后自检（smoke check）
#
# 目标：验证"起来"≠"可用"。覆盖：
#   [存活]     permission-service /healthz
#   [IdP]      Keycloak master 登录 / realm ssl-required=none / 业务账号存在
#   [认证]     X-Api-Key 被权限服务接受（service_api_key 去换行 sha256 需与 DB 一致）
#   [对账]     service_api_key 文件==DB==RAG .env；ctx_token_secret 文件==RAG .env；
#              keycloak_client_secret 文件==Keycloak；JWT 公钥 权限==RAG
#
# 用法：bash scripts/smoke_check.sh [--strict]
#   --strict  任一失败即 exit 1（严格门禁）；默认有失败也 exit 1（供 deploy/start 摘要）
# ══════════════════════════════════════════════════════════════════
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

set -a; [ -f .env ] && source .env; set +a
PORT="${PERMISSION_SERVICE_HOST_PORT:-18080}"
KC_PORT="${KEYCLOAK_HOST_PORT:-8080}"
KC_REALM="${KEYCLOAK_REALM:-rag-v14}"
KC_ADMIN_USER="${KEYCLOAK_ADMIN_USERNAME:-admin}"

# RAG .env 定位（共享密钥对账用；同主机优先 sibling 目录，其次旧路径）
RAG_ENV="${RAG_ENV:-}"
if [ -z "$RAG_ENV" ] && [ -f "$REPO_ROOT/../proj_rag_dev/.env" ]; then
    RAG_ENV="$(cd "$REPO_ROOT/../proj_rag_dev" && pwd)/.env"
elif [ -z "$RAG_ENV" ] && [ -f "/home/mfkcel/proj_rag_dev/.env" ]; then
    RAG_ENV="/home/mfkcel/proj_rag_dev/.env"
fi

FAIL=0
ok()   { echo -e "\033[32m[✓]\033[0m $*"; }
warn() { echo -e "\033[33m[!]\033[0m $*"; }
fail() { echo -e "\033[31m[x]\033[0m $*"; FAIL=1; }

echo "══════ 权限系统 smoke 检查开始 ══════"

# ── [1] 服务存活 ────────────────────────────────────────────────
code="$(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://127.0.0.1:${PORT}/healthz" 2>/dev/null)"
[ "$code" = "200" ] && ok "存活: permission-service /healthz → 200" || fail "存活: permission-service /healthz → ${code:-不可达}"

# ── [1b] SSO 入口（permission-nginx :18081 /realms）可达 ────────
NGINX_PORT="${PERMISSION_NGINX_PORT:-18081}"
sso_code="$(curl -s -o /dev/null -w '%{http_code}' -m 5 \
    "http://127.0.0.1:${NGINX_PORT}/realms/${KC_REALM}/.well-known/openid-configuration" 2>/dev/null)"
[ "$sso_code" = "200" ] && ok "存活: SSO 入口 permission-nginx :${NGINX_PORT}/realms → 200" || fail "存活: SSO 入口 permission-nginx :${NGINX_PORT} → ${sso_code:-不可达}（登录跳转会不可访问；start.sh/deploy.sh 必须拉起 permission-nginx）"

# ── [2] Keycloak master 登录 ────────────────────────────────────
KC_PWD="$(cat permission-service/config/keycloak_admin_password 2>/dev/null || true)"
KC_TOKEN=""
if [ -n "$KC_PWD" ]; then
    KC_TOKEN="$(curl -fsS -m 10 -X POST "http://127.0.0.1:${KC_PORT}/realms/master/protocol/openid-connect/token" \
        -d "grant_type=password&client_id=admin-cli&username=${KC_ADMIN_USER}&password=${KC_PWD}" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null || true)"
fi
[ -n "$KC_TOKEN" ] && ok "IdP: Keycloak master 登录 OK" || fail "IdP: Keycloak master 登录失败（检查 permission-service/config/keycloak_admin_password 与容器口令）"

# ── [3/4] realm 配置与业务账号 ──────────────────────────────────
if [ -n "$KC_TOKEN" ]; then
    SR="$(curl -fsS -m 8 -H "Authorization: Bearer $KC_TOKEN" \
        "http://127.0.0.1:${KC_PORT}/admin/realms/${KC_REALM}" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("sslRequired",""))' 2>/dev/null || true)"
    [ "$SR" = "none" ] && ok "IdP: realm ssl-required=none（http 部署登录不会被强制 https）" || fail "IdP: realm ssl-required=${SR:-?}（应为 none，否则管理台/RAG 登录跳 https 失败）"
    for _u in admin testuser; do
        if curl -fsS -m 8 -H "Authorization: Bearer $KC_TOKEN" \
            "http://127.0.0.1:${KC_PORT}/admin/realms/${KC_REALM}/users?username=${_u}" 2>/dev/null \
            | grep -q '"username"'; then
            ok "IdP: 业务账号 ${_u} 存在"
        else
            fail "IdP: 业务账号 ${_u} 缺失——RESET 后未 seed（需 keycloak_seed_users + 重跑 keycloak_bootstrap）"
        fi
    done
else
    fail "IdP: 跳过 realm 配置/账号检查（无 admin token）"
fi

# ── [5] X-Api-Key 认证 ─────────────────────────────────────────
KEY="$(tr -d '\n\r' < permission-service/config/service_api_key 2>/dev/null || true)"
key_code="$(curl -s -o /dev/null -w '%{http_code}' -m 5 -H "X-Api-Key: $KEY" "http://127.0.0.1:${PORT}/v1/health" 2>/dev/null)"
case "$key_code" in
    200|403) ok "认证: X-Api-Key 被权限服务接受（/v1/health → $key_code）" ;;
    401)     fail "认证: X-Api-Key 被拒绝（401）。修复：project_api_keys.key_hash 必须 = sha256(service_api_key 去换行)；重跑 deploy.sh 同步" ;;
    *)       fail "认证: /v1/health → ${key_code:-不可达}" ;;
esac

# ── [6] service_api_key 三方一致（文件==DB==RAG .env）──────────
if [ -n "$KEY" ]; then
    FILE_HASH="$(printf '%s' "$KEY" | sha256sum | awk '{print $1}')"
    DB_HASH="$(docker compose exec -T perm-postgres psql -U perm_user -d permission_db -tAc 'SELECT key_hash FROM project_api_keys' 2>/dev/null | tr -d ' \n')"
    [ "$FILE_HASH" = "$DB_HASH" ] && ok "对账: service_api_key 文件 == DB key_hash" || fail "对账: service_api_key 文件与 DB key_hash 不一致（${FILE_HASH:0:8}... vs ${DB_HASH:0:8}...）"
    if [ -n "$RAG_ENV" ]; then
        RAG_KEY="$(grep -E '^AUTHZ_CLIENT_CREDENTIAL=' "$RAG_ENV" | head -1 | cut -d= -f2- | tr -d '\n\r')"
        if [ -n "$RAG_KEY" ]; then
            [ "$RAG_KEY" = "$KEY" ] && ok "对账: service_api_key 文件 == RAG .env AUTHZ_CLIENT_CREDENTIAL" || fail "对账: RAG .env AUTHZ_CLIENT_CREDENTIAL 与权限文件不一致（RAG 需重跑 deploy）"
        else
            warn "对账: RAG .env 无 AUTHZ_CLIENT_CREDENTIAL，跳过（跨主机场景需手工保证）"
        fi
    fi
fi

# ── [7] ctx_token_secret 两侧一致（文件==RAG .env）─────────────
CTS="$(cat permission-service/config/ctx_token_secret 2>/dev/null || true)"
if [ -n "$CTS" ] && [ -n "$RAG_ENV" ]; then
    RAG_CTS="$(grep -E '^CTX_TOKEN_SECRET=' "$RAG_ENV" | head -1 | cut -d= -f2- | tr -d '\n\r')"
    [ "$RAG_CTS" = "$CTS" ] && ok "对账: ctx_token_secret 文件 == RAG .env" || fail "对账: RAG .env CTX_TOKEN_SECRET 与权限文件不一致（RAG 需重跑 deploy）"
fi

# ── [8] keycloak_client_secret 文件==Keycloak 实际 ─────────────
if [ -n "$KC_TOKEN" ]; then
    KC_SECRET_FILE="$(cat permission-service/config/keycloak_client_secret 2>/dev/null || true)"
    KC_CLIENT_ID="$(docker compose exec -T keycloak /opt/keycloak/bin/kcadm.sh get clients -r "$KC_REALM" -q clientId=permission-service --format csv --fields id 2>/dev/null | tail -1 | tr -d '"')"
    KC_SECRET_KC=""
    if [ -n "$KC_CLIENT_ID" ]; then
        KC_SECRET_KC="$(docker compose exec -T keycloak /opt/keycloak/bin/kcadm.sh get "clients/$KC_CLIENT_ID/client-secret" -r "$KC_REALM" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("value",""))' 2>/dev/null || true)"
    fi
    if [ -n "$KC_SECRET_KC" ]; then
        [ "$KC_SECRET_FILE" = "$KC_SECRET_KC" ] && ok "对账: keycloak_client_secret 文件==Keycloak" || fail "对账: keycloak_client_secret 文件与 Keycloak 不一致（重跑 keycloak_bootstrap 回写后需 --force-recreate permission-service）"
    else
        warn "对账: 未能读取 Keycloak 侧 client secret，跳过"
    fi
fi

# ── [9] JWT 公钥 权限==RAG ─────────────────────────────────────
RAG_JWT=""
if [ -f "$REPO_ROOT/../proj_rag_dev/config/jwt_public.pem" ]; then
    RAG_JWT="$REPO_ROOT/../proj_rag_dev/config/jwt_public.pem"
elif [ -f "/home/mfkcel/proj_rag_dev/config/jwt_public.pem" ]; then
    RAG_JWT="/home/mfkcel/proj_rag_dev/config/jwt_public.pem"
fi
if [ -f permission-service/config/jwt_public.pem ] && [ -n "$RAG_JWT" ]; then
    cmp -s permission-service/config/jwt_public.pem "$RAG_JWT" \
        && ok "对账: JWT 公钥 权限==RAG" \
        || fail "对账: JWT 公钥与 RAG 不一致（RAG 轮换后需重跑权限 deploy 同步）"
elif [ -f permission-service/config/jwt_public.pem ]; then
    ok "对账: JWT 公钥存在（RAG config 不可定位，跳过比对）"
fi

echo ""
if [ "$FAIL" = "0" ]; then
    ok "══════ smoke 检查全部通过 ══════"
    exit 0
else
    warn "══════ smoke 检查存在失败项（按上方 [x] 修复；密钥类问题重跑对应侧 deploy 即可收敛）══════"
    exit 1
fi
