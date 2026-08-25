#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
# Keycloak realm/client/roles/mappers 幂等自动导入（kcadm）
#
# 背景：docs/keycloak-realm-setup.md 的 realm 配置原本全靠 Admin UI 人工操作，
# 导致全新部署不可复现、且 keycloak_client_secret 与 permission-service client 易失配。
# 本脚本把该文档的结构化配置转成 kcadm 幂等导入，deploy 时自动执行。
#
# 用法（仓库根目录执行）：
#   bash scripts/keycloak_bootstrap.sh
#
# 依赖：
#   - keycloak 容器已启动（docker compose up -d keycloak），本脚本经
#     docker compose exec 调用容器内 kcadm.sh
#   - KEYCLOAK_ADMIN_PASSWORD（env 或 permission-service/config/keycloak_admin_password）
#   - permission-service/config/keycloak_client_secret（permission-service client 的 secret）
#   - EXTERNAL_HOST（浏览器可达地址，用于 client redirect/webOrigins；默认 localhost）
#
# 幂等：realm/client/role/mapper 已存在则跳过创建、仅按需补齐配置；可反复运行。
# ══════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

info() { echo -e "\033[36m[i]\033[0m $*"; }
ok()   { echo -e "\033[32m[✓]\033[0m $*"; }
warn() { echo -e "\033[33m[!]\033[0m $*"; }
fail() { echo -e "\033[31m[x]\033[0m $*" >&2; exit 1; }

# ── 参数解析 ─────────────────────────────────────────────────────
set -a; [ -f .env ] && source .env; set +a
EXTERNAL_HOST="${EXTERNAL_HOST:-localhost}"
KC_SERVER="${KC_SERVER:-http://localhost:8080}"
KC_REALM="${KEYCLOAK_REALM:-rag-v14}"
KC_ADMIN_USER="${KEYCLOAK_ADMIN_USERNAME:-admin}"

KC_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-}"
if [ -z "$KC_ADMIN_PASSWORD" ] && [ -f "permission-service/config/keycloak_admin_password" ]; then
    KC_ADMIN_PASSWORD="$(cat permission-service/config/keycloak_admin_password)"
fi
[ -n "$KC_ADMIN_PASSWORD" ] || fail "缺 KEYCLOAK_ADMIN_PASSWORD（env 或 permission-service/config/keycloak_admin_password）"

# ── kcadm 入口 ────────────────────────────────────────────────────
command -v docker >/dev/null || fail "缺少 docker"
KCADM="docker compose exec -T keycloak /opt/keycloak/bin/kcadm.sh"
if ! docker compose ps --format json 2>/dev/null | python3 -c "
import json,sys
for line in sys.stdin:
    line=line.strip()
    if not line: continue
    r=json.loads(line)
    if r.get('Service')=='keycloak' and r.get('State')=='running':
        sys.exit(0)
sys.exit(1)
"; then
    fail "keycloak 容器未运行——请先 docker compose up -d keycloak"
fi

# Keycloak start-dev 初始化较慢，TCP 就绪 ≠ admin API 就绪；重试登录直到可用
info "等待 Keycloak admin API 就绪并登录（最多 90s）..."
KC_READY=0
for _i in $(seq 1 30); do
    if $KCADM config credentials \
        --server "$KC_SERVER" --realm master \
        --user "$KC_ADMIN_USER" --password "$KC_ADMIN_PASSWORD" >/dev/null 2>&1; then
        KC_READY=1; break
    fi
    sleep 3
done
[ "$KC_READY" = "1" ] || fail "Keycloak admin API 未在 90s 内就绪或口令错误（KEYCLOAK_ADMIN_PASSWORD）"
ok "kcadm 登录成功"

# ── helpers ───────────────────────────────────────────────────────
client_uuid() {
    # 取 clientId 对应的内部 UUID（不存在返回空）
    $KCADM get clients -r "$KC_REALM" -q clientId="$1" \
        --format csv --fields id 2>/dev/null | tail -1 | tr -d '"'
}

# 1. realm ─────────────────────────────────────────────────────────
# sslRequired=none：HTTP-only 部署（TLS 由外部 LB 终结，本机不提供 https）。默认 external 会让
# keycloak-js 适配器对非 localhost 客户端强制升级 https → http 部署下管理台/RAG 登录全挂。
# 若将来 TLS 由本机 nginx 终结，再把这里改回 external/all。
if ! $KCADM get "realms/$KC_REALM" >/dev/null 2>&1; then
    info "创建 realm $KC_REALM..."
    $KCADM create realms -s realm="$KC_REALM" -s enabled=true -s registrationAllowed=false -s sslRequired=none
    ok "realm $KC_REALM 已创建（sslRequired=none）"
else
    ok "realm $KC_REALM 已存在（按需补齐 sslRequired=none）"
    $KCADM update "realms/$KC_REALM" -s sslRequired=none >/dev/null 2>&1 \
        && ok "  sslRequired=none 已确认" \
        || warn "  sslRequired=none 更新失败——管理台/RAG 登录可能仍强制 https"
fi

# 1b. master realm（admin 控制台所在）同样 sslRequired=none ──
$KCADM update "realms/master" -s sslRequired=none >/dev/null 2>&1 \
    && ok "master realm sslRequired=none（admin 控制台 http 可访问）" \
    || warn "master realm sslRequired 更新失败——admin 控制台可能仍强制 https"

# 2. rag-frontend（public，浏览器 SSO 登录，redirect 覆盖 :3001 直连 与 nginx :80 入口）──
uuid=$(client_uuid rag-frontend)
if [ -z "$uuid" ]; then
    info "创建 client rag-frontend..."
    $KCADM create clients -r "$KC_REALM" \
        -s clientId=rag-frontend -s protocol=openid-connect \
        -s publicClient=true -s standardFlowEnabled=true \
        -s directAccessGrantsEnabled=false \
        -s "redirectUris=[\"http://$EXTERNAL_HOST:3001/*\",\"http://$EXTERNAL_HOST/*\",\"https://$EXTERNAL_HOST:3001/*\",\"https://$EXTERNAL_HOST/*\",\"http://localhost:3001/*\",\"http://localhost/*\",\"https://localhost:3001/*\",\"https://localhost/*\"]" \
        -s "webOrigins=[\"http://$EXTERNAL_HOST:3001\",\"http://$EXTERNAL_HOST\",\"https://$EXTERNAL_HOST:3001\",\"https://$EXTERNAL_HOST\",\"http://localhost:3001\",\"http://localhost\"]"
    uuid=$(client_uuid rag-frontend)
else
    ok "client rag-frontend 已存在（补齐 redirect/webOrigins）"
    $KCADM update "clients/$uuid" -r "$KC_REALM" \
        -s "redirectUris=[\"http://$EXTERNAL_HOST:3001/*\",\"http://$EXTERNAL_HOST/*\",\"https://$EXTERNAL_HOST:3001/*\",\"https://$EXTERNAL_HOST/*\",\"http://localhost:3001/*\",\"http://localhost/*\",\"https://localhost:3001/*\",\"https://localhost/*\"]" \
        -s "webOrigins=[\"http://$EXTERNAL_HOST:3001\",\"http://$EXTERNAL_HOST\",\"https://$EXTERNAL_HOST:3001\",\"https://$EXTERNAL_HOST\",\"http://localhost:3001\",\"http://localhost\"]"
fi

# 3. admin-console（public，管理台 SSO）──
# 浏览器 SPA 必须 public：前端回调直接向 Keycloak 换码、无法持有 client_secret。
# 设 confidential 会令 token exchange 401 unauthorized_client（Invalid client credentials）。
uuid=$(client_uuid admin-console)
if [ -z "$uuid" ]; then
    info "创建 client admin-console（public）..."
    $KCADM create clients -r "$KC_REALM" \
        -s clientId=admin-console -s protocol=openid-connect \
        -s publicClient=true -s standardFlowEnabled=true \
        -s directAccessGrantsEnabled=false \
        -s "redirectUris=[\"http://$EXTERNAL_HOST:3002/*\",\"https://$EXTERNAL_HOST:3002/*\",\"http://$EXTERNAL_HOST:18081/*\",\"https://$EXTERNAL_HOST:18081/*\",\"http://localhost:3002/*\",\"https://localhost:3002/*\"]" \
        -s "webOrigins=[\"http://$EXTERNAL_HOST:3002\",\"https://$EXTERNAL_HOST:3002\",\"http://$EXTERNAL_HOST:18081\",\"https://$EXTERNAL_HOST:18081\",\"http://localhost:3002\",\"https://localhost:3002\"]"
    uuid=$(client_uuid admin-console)
else
    ok "client admin-console 已存在（补齐 publicClient/redirect/webOrigins）"
    $KCADM update "clients/$uuid" -r "$KC_REALM" \
        -s publicClient=true \
        -s "redirectUris=[\"http://$EXTERNAL_HOST:3002/*\",\"https://$EXTERNAL_HOST:3002/*\",\"http://$EXTERNAL_HOST:18081/*\",\"https://$EXTERNAL_HOST:18081/*\",\"http://localhost:3002/*\",\"https://localhost:3002/*\"]" \
        -s "webOrigins=[\"http://$EXTERNAL_HOST:3002\",\"https://$EXTERNAL_HOST:3002\",\"http://$EXTERNAL_HOST:18081\",\"https://$EXTERNAL_HOST:18081\",\"http://localhost:3002\",\"https://localhost:3002\"]"
fi

# 4. permission-service（confidential + service accounts + 写回 client secret）──
uuid=$(client_uuid permission-service)
if [ -z "$uuid" ]; then
    info "创建 client permission-service（service accounts）..."
    $KCADM create clients -r "$KC_REALM" \
        -s clientId=permission-service -s protocol=openid-connect \
        -s publicClient=false -s serviceAccountsEnabled=true \
        -s standardFlowEnabled=false -s directAccessGrantsEnabled=false \
        -s clientAuthenticatorType=client-secret
    uuid=$(client_uuid permission-service)
else
    ok "client permission-service 已存在"
fi
# 同步 permission-service client secret：以 Keycloak 为权威（confidential client 创建时自动生成），
# 读回并写回配置文件，确保 permission-service 启动时用的 secret 与 Keycloak 一致。
_KC_SECRET_JSON="$($KCADM get "clients/$uuid/client-secret" -r "$KC_REALM" 2>/dev/null || true)"
_KC_SECRET="$(printf '%s' "$_KC_SECRET_JSON" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("value",""))
except Exception:
    print("")' 2>/dev/null || true)"
if [ -n "$_KC_SECRET" ]; then
    mkdir -p permission-service/config
    umask 077
    printf '%s' "$_KC_SECRET" > permission-service/config/keycloak_client_secret
    ok "permission-service client secret 已从 Keycloak 同步到配置文件"
else
    info "未能读取 permission-service client secret（若 client 已存在且已有 secret，权限服务将用自身配置值）"
fi

# 授予 permission-service service account 的 realm-management 角色（用户同步/列表必需）。
# 缺失时 /admin/realms/{realm}/users 返回 403 → 用户同步失败、admin-console 无法建用户/看用户。
# 幂等：add-roles 重复授予无副作用。注：该 realm 无 view-groups 角色，只授存在的。
info "授予 permission-service service account realm-management 角色（view-users/query-users/query-groups/view-realm）..."
if $KCADM add-roles -r "$KC_REALM" \
    --uusername service-account-permission-service \
    --cclientid realm-management \
    --rolename view-users --rolename query-users --rolename query-groups --rolename view-realm >/dev/null 2>&1; then
    ok "service account 已授权 realm-management 角色（用户同步可用）"
else
    warn "授权 realm-management 角色失败——可稍后在 Keycloak 管理台手动授权（Users→permission-service service account→Role Mappings）"
fi

# 5. realm 角色 ────────────────────────────────────────────────────
for role in system_admin user; do
    if ! $KCADM get "roles/$role" -r "$KC_REALM" >/dev/null 2>&1; then
        info "创建 realm 角色 $role..."
        $KCADM create roles -r "$KC_REALM" -s name="$role"
        ok "角色 $role 已创建"
    else
        ok "角色 $role 已存在"
    fi
done

# 5b. 默认角色：把 user 加入 default-roles-<realm>（RESET 后新用户自动获得 user 角色，
#     否则授权链路静默断裂——新用户 JWT realm_access.roles 无 user，匹配不上任何授权）──
if $KCADM get "roles/default-roles-$KC_REALM" -r "$KC_REALM" >/dev/null 2>&1; then
    $KCADM add-roles -r "$KC_REALM" --rname "default-roles-$KC_REALM" --rolename user >/dev/null 2>&1 \
        && ok "user 已加入 default-roles-$KC_REALM（新用户默认角色）" \
        || warn "user 加入 default-roles 失败——新用户将拿不到 user 角色（可手工在管理台补）"
else
    warn "default-roles-$KC_REALM 不存在，跳过（可手工补 user 默认角色）"
fi

# 5c. realm 组（文档定义的 Engineering/Product/Admin；RESET 后可重建，组授权不静默失效）──
# 用 admin REST API（每次现取 token）：kcadm get groups 在长流程中偶发空返回，REST 更稳。
kc_admin_token() { # → master admin access token
    curl -fsS -m 10 -X POST "http://127.0.0.1:${KEYCLOAK_HOST_PORT:-8080}/realms/master/protocol/openid-connect/token" \
        -d "grant_type=password&client_id=admin-cli&username=${KC_ADMIN_USER}&password=${KC_ADMIN_PASSWORD}" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null || true
}
ensure_group() { # $1=组名
    local _t _gid _code
    _t="$(kc_admin_token)"
    [ -n "$_t" ] || { warn "组 $1：获取 admin token 失败"; return; }
    _gid="$(curl -fsS -m 8 -H "Authorization: Bearer $_t" \
        "http://127.0.0.1:${KEYCLOAK_HOST_PORT:-8080}/admin/realms/$KC_REALM/groups?search=$1&max=50" 2>/dev/null \
        | python3 -c "import json,sys; print(next((g['id'] for g in json.load(sys.stdin) if g.get('name')=='$1'),''))" 2>/dev/null || true)"
    if [ -n "$_gid" ]; then
        ok "组 $1 已存在"
    else
        _code="$(curl -s -o /dev/null -w '%{http_code}' -m 8 -X POST -H "Authorization: Bearer $_t" \
            -H "Content-Type: application/json" -d "{\"name\":\"$1\"}" \
            "http://127.0.0.1:${KEYCLOAK_HOST_PORT:-8080}/admin/realms/$KC_REALM/groups" 2>/dev/null)"
        case "$_code" in
            201) ok "组 $1 已创建" ;;
            409) ok "组 $1 已存在（创建返回冲突）" ;;
            *)   warn "组 $1 创建失败（HTTP $_code）" ;;
        esac
    fi
}
for _g in Engineering Product Admin; do ensure_group "$_g"; done

# 5d. 初始业务账号 seed（幂等；口令仅在创建时写入，不覆盖运行期已改口令）──
# 清单来自 permission-service/config/keycloak_seed_users（deploy 生成，运维可编辑）：
#   每行 username|password|realm_roles(逗号)|tenant_id；# 开头为注释；password 为空则跳过设口令。
user_uuid() { # $1=username → 内部 UUID
    $KCADM get users -r "$KC_REALM" -q username="$1" --format csv --fields id 2>/dev/null | tail -1 | tr -d '"'
}
ensure_user() { # $1=username $2=password $3=realm_roles $4=tenant_id
    local _u="$1" _pwd="$2" _roles="$3" _tid="$4" _uid
    _uid="$(user_uuid "$_u")"
    if [ -z "$_uid" ]; then
        info "创建用户 $_u..."
        $KCADM create users -r "$KC_REALM" -s username="$_u" -s enabled=true >/dev/null 2>&1
        _uid="$(user_uuid "$_u")"
        if [ -n "$_pwd" ] && [ -n "$_uid" ]; then
            $KCADM set-password -r "$KC_REALM" --username "$_u" --new-password "$_pwd" >/dev/null 2>&1 \
                && ok "  $_u 已创建并设口令" \
                || warn "  $_u 创建成功但设口令失败"
        elif [ -n "$_uid" ]; then
            warn "  $_u 已创建但 seed 未提供口令（跳过 set-password，需在管理台设初始口令）"
        fi
    else
        ok "用户 $_u 已存在（不覆盖口令）"
    fi
    if [ -n "$_uid" ] && [ -n "$_roles" ]; then
        for _r in ${_roles//,/ }; do
            $KCADM add-roles -r "$KC_REALM" --uusername "$_u" --rolename "$_r" >/dev/null 2>&1 || true
        done
        ok "  $_u 角色已确保: $_roles"
    fi
    if [ -n "$_uid" ] && [ -n "$_tid" ]; then
        $KCADM update "users/$_uid" -r "$KC_REALM" -s "attributes.tenant_id=[\"$_tid\"]" >/dev/null 2>&1 \
            && ok "  $_u tenant_id 已确保" \
            || warn "  $_u tenant_id 设置失败"
    fi
}
if [ -f "permission-service/config/keycloak_seed_users" ]; then
    while IFS='|' read -r _su _sp _sr _st; do
        [ -z "$_su" ] && continue
        [[ "$_su" == \#* ]] && continue
        ensure_user "$_su" "$_sp" "$_sr" "$_st" </dev/null
    done < "permission-service/config/keycloak_seed_users"
else
    warn "缺 permission-service/config/keycloak_seed_users——初始业务账号未 seed（deploy 会自动生成）"
fi

# 6. client mappers（tenant / groups → rag-frontend + admin-console）──
mapper_exists() { # $1=client_uuid $2=mapper_name
    local _out _rc
    _out="$($KCADM get "clients/$1/protocol-mappers/models" -r "$KC_REALM" 2>&1)"
    _rc=$?
    if [ "${_BOOTSTRAP_DEBUG:-0}" = "1" ]; then
        echo "[mapper_exists] rc=$_rc bytes=${#_out} target=$2" >&2
        printf '%s' "$_out" | head -c 300 >&2; echo >&2
    fi
    printf '%s' "$_out" | grep -q "\"name\" *: *\"$2\""
}
ensure_mapper() { # $1=client_uuid $2=mapper_name $3=protocolMapper $4=claim.name $5=extra_config...
    local cuuid="$1" name="$2" pm="$3" claim="$4"; shift 4
    if ! mapper_exists "$cuuid" "$name"; then
        info "  mapper $name → client $cuuid..."
        local args=(
            create "clients/$cuuid/protocol-mappers/models" -r "$KC_REALM"
            -s name="$name" -s protocol=openid-connect
            -s protocolMapper="$pm"
            -s "config.\"claim.name\"=$claim"
            -s "config.\"access.token.claim\"=true"
            -s "config.\"id.token.claim\"=true"
            -s "config.\"userinfo.token.claim\"=true"
        )
        for extra in "$@"; do
            args+=(-s "$extra")
        done
        # create 可能因「已存在」报错（幂等检测偶发瞬态），以创建后验证为准
        $KCADM "${args[@]}" >/dev/null 2>&1 || true
        if mapper_exists "$cuuid" "$name"; then
            ok "  mapper $name 就绪"
        else
            fail "  mapper $name 创建/校验失败"
        fi
    else
        ok "  mapper $name 已存在"
    fi
}

for cli in rag-frontend admin-console; do
    cuuid=$(client_uuid "$cli")
    [ -n "$cuuid" ] || { warn="client $cli 不存在，跳过 mapper"; info "$warn"; continue; }
    info "为 $cli 挂 mapper..."
    # tenant：User Attribute tenant_id → claim tenant（RAG/管理台用户上下文）
    ensure_mapper "$cuuid" tenant oidc-usermodel-attribute-mapper tenant \
        "config.\"user.attribute\"=tenant_id" "config.\"json.type.label\"=String"
    # groups：Group Membership → claim groups（角色/组级授权）
    ensure_mapper "$cuuid" groups oidc-group-membership-mapper groups
done

# 7. 用户 profile 声明 tenant_id 属性 ─────────────────────────────
# Keycloak 24 managed user-profile 默认只允许 username/email/firstName/lastName，
# 自定义属性（tenant_id）会被丢弃 → tenant mapper 无值可映射 → SSO id_token 缺 tenant claim。
# 此处幂等声明 tenant_id 进 user profile（保留 groups 字段，避免 PUT 校验失败）。
info "确保 user profile 声明 tenant_id 属性..."
KC_ADMIN_TOKEN="$(curl -fsS -m 10 -X POST \
    "http://127.0.0.1:${KEYCLOAK_HOST_PORT:-8080}/realms/master/protocol/openid-connect/token" \
    -d "grant_type=password&client_id=admin-cli&username=${KC_ADMIN_USER}&password=${KC_ADMIN_PASSWORD}" \
    2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("access_token",""))')"
if [ -n "$KC_ADMIN_TOKEN" ]; then
    python3 - "$KC_ADMIN_TOKEN" "$KC_REALM" "${KEYCLOAK_HOST_PORT:-8080}" <<'PYEOF'
import json, sys, urllib.request
token, realm, port = sys.argv[1], sys.argv[2], sys.argv[3]
base = f"http://127.0.0.1:{port}/admin/realms/{realm}"
def req(method, path, body=None):
    r = urllib.request.Request(base + path, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            data = resp.read()
            return resp.status, (json.loads(data) if data else None)
    except urllib.error.HTTPError as e:
        return e.code, None
prof = req("GET", "/users/profile")[1] or {"attributes": [], "groups": []}
names = [a.get("name") for a in prof.get("attributes", [])]
if "tenant_id" not in names:
    prof.setdefault("attributes", []).append({
        "name": "tenant_id", "displayName": "Tenant ID", "validations": {},
        "permissions": {"view": ["admin", "user"], "edit": ["admin", "user"]},
        "multivalued": False,
    })
    st, _ = req("PUT", "/users/profile", prof)
    if st == 200:
        print("[✓] tenant_id 已声明进 user profile")
    else:
        print(f"[!] user profile PUT 失败: {st}")
else:
    print("[✓] tenant_id 已在 user profile")
PYEOF
else
    info "未能获取 Keycloak admin token，跳过 user profile 声明（可后续手工补）"
fi

echo ""
echo "══════ Keycloak 引导完成 ══════"
echo "  realm:      $KC_REALM"
echo "  clients:    rag-frontend / admin-console / permission-service"
echo "  EXTERNAL_HOST=$EXTERNAL_HOST（redirect/webOrigins 已按此推导）"
echo "════════════════════════════════"
