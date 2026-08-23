#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
# 权限系统 secret 初始化/校验脚本（幂等）
#
# 职责：确保 docker-compose.yml 引用的全部 secret 文件存在且非空。
#   - 已存在的文件一律不覆盖（幂等，可反复运行）
#   - 缺失的随机类 secret（API Key / ctx_token / TLS）自动生成
#   - 必须与外部一致的 secret（jwt 公/私钥、Keycloak admin 凭据）
#     需要提供来源，见下方说明
#
# 用法：
#   scripts/init_secrets.sh                          # 校验 + 补缺
#   SECRETS_DIR=/etc/permission-platform/secrets \
#     scripts/init_secrets.sh                        # 自定义 secret 目录
#   RAG_CONFIG_DIR=/path/to/proj_rag_dev/config \
#     scripts/init_secrets.sh                        # 指定 RAG 密钥来源
#   KEYCLOAK_ADMIN_PASSWORD='...' scripts/init_secrets.sh
# ══════════════════════════════════════════════════════════════════
set -euo pipefail

# ── 路径 ───────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SECRETS_DIR="${SECRETS_DIR:-$REPO_ROOT/permission-service/config}"

# RAG 系统密钥目录（jwt 公/私钥来源；两系统共用同一密钥对）
RAG_CONFIG_DIR="${RAG_CONFIG_DIR:-/home/mfkcel/proj_rag_dev/config}"

# Keycloak admin 凭据（必须与 Keycloak 部署一致，绝不随机生成）
KC_ADMIN_USERNAME="${KEYCLOAK_ADMIN_USERNAME:-}"
KC_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-}"

mkdir -p "$SECRETS_DIR"
umask 077

info()  { echo -e "\033[36m[i]\033[0m $*"; }
ok()    { echo -e "\033[32m[✓]\033[0m $*"; }
warn()  { echo -e "\033[33m[!]\033[0m $*"; }
fail()  { echo -e "\033[31m[x]\033[0m $*" >&2; exit 1; }

have_file() { [[ -s "$SECRETS_DIR/$1" ]]; }

gen_hex() { openssl rand -hex "$1"; }

# ── 1. JWT 密钥（与 RAG 共用，从 RAG 仓库拷贝，绝不新生成） ────────
for k in jwt_public.pem jwt_private.pem; do
    if have_file "$k"; then
        ok "$k 已存在（跳过）"
    else
        if [[ -f "$RAG_CONFIG_DIR/$k" ]]; then
            cp "$RAG_CONFIG_DIR/$k" "$SECRETS_DIR/$k"
            chmod 600 "$SECRETS_DIR/$k"
            ok "$k 已从 RAG 仓库拷贝"
        else
            fail "缺少 $k 且未找到来源: $RAG_CONFIG_DIR/$k（可用 RAG_CONFIG_DIR 指定）"
        fi
    fi
done

# ── 2. 服务间 API Key（RAG 的 AUTHZ_CLIENT_CREDENTIAL 必须与此一致） ─
if have_file service_api_key; then
    ok "service_api_key 已存在（跳过）"
else
    # printf '%s'（无末尾换行）：换行会让 deploy.sh 的 sha256sum 与权限服务 sha256(key) 失配 → 401
    printf 'psk_%s' "$(gen_hex 32)" > "$SECRETS_DIR/service_api_key"
    chmod 600 "$SECRETS_DIR/service_api_key"
    ok "service_api_key 已生成（需同步到 RAG 的 AUTHZ_CLIENT_CREDENTIAL）"
fi

# ── 3. ctx_token HMAC 密钥（≥32 字节随机） ─────────────────────────
if have_file ctx_token_secret; then
    ok "ctx_token_secret 已存在（跳过）"
else
    gen_hex 32 > "$SECRETS_DIR/ctx_token_secret"
    chmod 600 "$SECRETS_DIR/ctx_token_secret"
    ok "ctx_token_secret 已生成"
fi

# ── 4. Keycloak client secret ─────────────────────────────────────
if have_file keycloak_client_secret; then
    ok "keycloak_client_secret 已存在（跳过）"
else
    gen_hex 16 > "$SECRETS_DIR/keycloak_client_secret"
    chmod 600 "$SECRETS_DIR/keycloak_client_secret"
    ok "keycloak_client_secret 已生成（需与 Keycloak 中 permission-service client 的 secret 一致）"
fi

# ── 5. Keycloak admin 凭据（不随机生成，必须显式提供） ─────────────
if have_file keycloak_admin_username && have_file keycloak_admin_password; then
    ok "keycloak_admin_username/password 已存在（跳过）"
else
    if [[ -z "$KC_ADMIN_USERNAME" || -z "$KC_ADMIN_PASSWORD" ]]; then
        fail "缺少 Keycloak admin 凭据。请通过环境变量提供：KEYCLOAK_ADMIN_USERNAME=... KEYCLOAK_ADMIN_PASSWORD=..."
    fi
    printf '%s' "$KC_ADMIN_USERNAME" > "$SECRETS_DIR/keycloak_admin_username"
    printf '%s' "$KC_ADMIN_PASSWORD" > "$SECRETS_DIR/keycloak_admin_password"
    chmod 600 "$SECRETS_DIR/keycloak_admin_username" "$SECRETS_DIR/keycloak_admin_password"
    ok "Keycloak admin 凭据已写入"
fi

# ── 6. TLS 自签证书（permission-service HTTPS） ────────────────────
if have_file tls_cert.pem && have_file tls_key.pem; then
    ok "tls_cert.pem / tls_key.pem 已存在（跳过）"
else
    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout "$SECRETS_DIR/tls_key.pem" \
        -out "$SECRETS_DIR/tls_cert.pem" \
        -days 3650 -subj "/CN=permission-service" 2>/dev/null
    chmod 600 "$SECRETS_DIR/tls_key.pem" "$SECRETS_DIR/tls_cert.pem"
    ok "TLS 自签证书已生成（生产建议替换为正式证书）"
fi

# ── 汇总 ──────────────────────────────────────────────────────────
echo ""
echo "═══ secret 目录校验完成: $SECRETS_DIR ═══"
missing=0
for f in jwt_public.pem jwt_private.pem service_api_key ctx_token_secret \
         keycloak_client_secret keycloak_admin_username keycloak_admin_password \
         tls_cert.pem tls_key.pem; do
    if have_file "$f"; then
        ok "  $f"
    else
        warn "  缺少 $f"
        missing=1
    fi
done
exit "$missing"
