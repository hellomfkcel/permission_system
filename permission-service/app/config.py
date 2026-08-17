"""权限服务后端 — 统一配置入口。"""

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── 数据库 ──
    database_url: str = (
        "postgresql+asyncpg://perm_user:perm_pass@localhost:25433/permission_db"
    )
    database_url_sync: str = (
        "postgresql://perm_user:perm_pass@localhost:25433/permission_db"
    )

    # ── Cerbos PDP ──
    cerbos_pdp_url: str = "http://localhost:13592"

    # Cerbos 策略文件目录 — 相对于本项目的 cerbos/policies/
    # 可通过环境变量 CERBOS_POLICIES_DIR 覆盖（如 Docker 部署中映射到容器内路径）
    cerbos_policies_dir: str = ""

    # ── Redis ──
    # 默认 URL（开发环境无密码）。
    # 生产环境通过 REDIS_URL 或 REDIS_PASSWORD 环境变量覆盖。
    # docker-compose 中 Redis 设了密码，通过 REDIS_PASSWORD 传入。
    redis_url: str = "redis://localhost:16380/0"
    redis_password: str = ""  # 独立密码字段，若设置则优先拼入 redis_url

    def get_redis_url(self) -> str:
        """返回最终 Redis URL（独立 REDIS_PASSWORD 配置优先于 URL 内嵌密码）。

        优先级：
        1. REDIS_PASSWORD 环境变量 → 重新构建含该密码的 URL
        2. REDIS_URL 环境变量（可能已含密码）
        3. 默认值
        """
        if self.redis_password:
            import re
            # 替换或插入密码到 redis URL 中
            # 匹配: redis://[user][:old_pwd]@host:port/db
            if re.search(r"redis://[^@]*@", self.redis_url):
                # 已有认证段 → 替换密码
                return re.sub(
                    r"redis://(?::[^@]*)?@",
                    f"redis://:{self.redis_password}@",
                    self.redis_url,
                )
            else:
                # 无认证段 → 插入密码
                return re.sub(
                    r"redis://",
                    f"redis://:{self.redis_password}@",
                    self.redis_url,
                )
        return self.redis_url

    # ── Keycloak ──
    keycloak_server_url: str = "http://localhost:8080"
    # Realm 名与具体接入项目无关；历史默认值为 rag-v14，现改为中性名。
    # 已有部署通过 KEYCLOAK_REALM 环境变量保留原值即可。
    keycloak_realm: str = "permission-platform"
    keycloak_client_id: str = "permission-service"
    keycloak_client_secret: str = ""
    # Keycloak client secret 文件路径（Docker secrets / K8s Secret 挂载）。
    # 若设置且文件存在，优先读取文件内容覆盖 keycloak_client_secret。
    # 这允许在 Docker Compose 中通过 secrets 机制安全传递凭据。
    keycloak_client_secret_file: str = ""
    # master realm 管理员凭据（仅在 service account 不可用时作为回退）。
    # 生产环境通过 K8s Secret 或 Vault 注入，禁止硬编码。
    keycloak_admin_username: str = ""
    keycloak_admin_password: str = ""
    # Docker secrets / K8s Secret 挂载路径（优先于明文值）
    keycloak_admin_username_file: str = ""
    keycloak_admin_password_file: str = ""

    # ── JWT ──
    jwt_public_key_path: str = "./config/jwt_public.pem"
    jwt_private_key_path: str = "./config/jwt_private.pem"
    jwt_algorithm: str = "RS256"
    jwt_expire_seconds: int = 3600  # dev-login token 有效期
    # ── 开发模式登录密码（安全底线：开发环境也不应无密码登录）──
    # 生产环境务必通过环境变量覆盖默认值。
    # 当 PRODUCTION=true 时，dev-login 端点完全禁用（返回 501）。
    dev_login_password: str = "dev_password_2026"
    # JWT Issuer 白名单（逗号分隔）。开发模式不校验（留空），
    # 生产模式配置为已知 issuer 的列表，如 "rag-v14,permission-service"。
    jwt_allowed_issuers: str = ""

    # ── 服务间认证 ──
    # RAG 系统调用权限服务时携带的 API Key。
    # 生产环境通过环境变量 SERVICE_API_KEY 注入，开发环境可留空跳过校验。
    service_api_key: str = ""
    # Docker secrets / K8s Secret 挂载路径（优先于 service_api_key 明文值）
    service_api_key_file: str = ""

    # ── ctx_token 签名密钥 ──
    # 独立密钥，不与 Redis URL 或其他配置共享。
    # 生产环境通过 K8s Secret 或 Vault 注入。
    ctx_token_secret: str = ""
    # Docker secrets / K8s Secret 挂载路径（优先于 ctx_token_secret 明文值）
    ctx_token_secret_file: str = ""

    # ── 服务 ──
    host: str = "0.0.0.0"
    port: int = 18080
    log_level: str = "INFO"

    # ── CORS ──
    # 允许的来源列表，逗号分隔。
    # 默认允许管理台开发端口和管理台生产地址。
    # 可通过 ALLOWED_ORIGINS 环境变量覆盖（K8s ConfigMap / .env）。
    allowed_origins: str = (
        "http://192.168.1.127:3002,http://localhost:3002"
    )

    # 限流：按端点差异化配置
    check_rate_limit: int = 1000       # /v1/check req/s
    check_batch_rate_limit: int = 500  # /v1/check/batch req/s
    filter_rate_limit: int = 500       # /v1/filter req/s
    prefilter_rate_limit: int = 500    # /v1/prefilter req/s
    visibility_rate_limit: int = 200   # /v1/visibility req/s — 盖戳管道高频调用

    # TLS / HTTPS
    # 开发环境默认不启用 TLS。生产环境通过环境变量配置：
    #   TLS_ENABLED=true
    #   TLS_CERT_FILE=/path/to/fullchain.pem
    #   TLS_KEY_FILE=/path/to/privkey.pem
    tls_enabled: bool = False
    tls_cert_file: str = ""
    tls_key_file: str = ""

    # ── 首次启动引导 ──
    # 首次启动（projects 表为空）时自动建档的项目 ID 与显示名。
    # 这是平台从单项目形态演进而来的兼容逻辑：把 SERVICE_API_KEY 与内置
    # client_id / audience 落到一个具体项目上，使旧接入方无需改配置即可继续工作。
    # 全新部署可设 BOOTSTRAP_PROJECT_ENABLED=false 跳过，改为在管理台手工建项目。
    bootstrap_project_enabled: bool = True
    bootstrap_project_id: str = "rag-v14"
    bootstrap_project_name: str = "RAG v14 知识库系统"
    # 内置 client_id 与 audience，逗号分隔
    bootstrap_client_ids: str = "interactive-backend,retrieval,ingest"
    bootstrap_audiences: str = "retrieval-worker,ingestion-worker,stamping-worker"
    bootstrap_admin_user: str = "admin"

    # ── 生产模式标记 ──
    # production=true 时启用额外安全检查：
    #   - ctx_token_secret 必须显式配置（禁止回退到 Redis URL hash）
    #   - 数据库凭据禁止使用默认值
    #   - Redis 必须配置密码
    production: bool = False

    model_config = {
        "env_file": ".env",
        "extra": "allow",
    }


settings = Settings()


def get_cerbos_policies_dir() -> Path:
    """解析 Cerbos 策略文件目录。

    优先级:
    1. 环境变量 CERBOS_POLICIES_DIR
    2. settings.cerbos_policies_dir
    3. <permission-service>/../cerbos/policies/（项目内相对路径）

    不硬编码任何特定项目的绝对路径。
    """
    import os
    env_val = os.getenv("CERBOS_POLICIES_DIR", "")
    if env_val:
        return Path(env_val)
    if settings.cerbos_policies_dir:
        return Path(settings.cerbos_policies_dir)
    return Path(__file__).parent.parent.parent / "cerbos" / "policies"


# ── Secret file loading ──
# Docker secrets / K8s Secret 挂载为文件，优先于环境变量中的明文值。
# 凭据禁止在 .env 中明文存放。

if settings.keycloak_client_secret_file:
    _secret_path = Path(settings.keycloak_client_secret_file)
    if _secret_path.is_file():
        settings.keycloak_client_secret = _secret_path.read_text().strip()

# ctx_token_secret: Docker secret 文件优先
if settings.ctx_token_secret_file:
    _ctx_secret_path = Path(settings.ctx_token_secret_file)
    if _ctx_secret_path.is_file():
        settings.ctx_token_secret = _ctx_secret_path.read_text().strip()

if not settings.ctx_token_secret:
    # 若未显式设置 CTX_TOKEN_SECRET，尝试从默认路径读取
    _ctx_default = Path("config/ctx_token_secret")
    if _ctx_default.is_file():
        settings.ctx_token_secret = _ctx_default.read_text().strip()

# service_api_key: Docker secret 文件优先
if settings.service_api_key_file:
    _api_key_path = Path(settings.service_api_key_file)
    if _api_key_path.is_file():
        settings.service_api_key = _api_key_path.read_text().strip()

if not settings.service_api_key:
    # 若未显式设置，尝试从默认路径读取
    _api_key_default = Path("config/service_api_key")
    if _api_key_default.is_file():
        settings.service_api_key = _api_key_default.read_text().strip()

# Keycloak admin 凭据: Docker secret 文件优先
if settings.keycloak_admin_username_file:
    _kc_user_path = Path(settings.keycloak_admin_username_file)
    if _kc_user_path.is_file():
        settings.keycloak_admin_username = _kc_user_path.read_text().strip()

if settings.keycloak_admin_password_file:
    _kc_pass_path = Path(settings.keycloak_admin_password_file)
    if _kc_pass_path.is_file():
        settings.keycloak_admin_password = _kc_pass_path.read_text().strip()


# ══════════════════════════════════════════════════════════════
# 生产安全启动检查
# ══════════════════════════════════════════════════════════════

def validate_production_secrets() -> list[str]:
    """在服务启动时检查关键安全配置，返回警告列表。

    开发环境：仅发出警告（warnings），不阻止启动。
    生产环境（PRODUCTION=true）：关键项缺失抛出 RuntimeError。

    生产环境要求：
    1. ctx_token_secret 必须显式配置（禁止回退到 Redis URL hash）
    2. database_url 禁止使用默认开发凭据（perm_user:perm_pass）
    3. Redis 必须配置密码
    4. 建议启用 TLS

    Returns:
        警告信息列表，为空表示全部通过。
    """
    warnings: list[str] = []
    errors: list[str] = []

    # 检查 ctx_token_secret
    if not settings.ctx_token_secret:
        msg = (
            "CTX_TOKEN_SECRET is not set. "
            "Falling back to REDIS_URL hash for ctx_token signing. "
            "In production, set CTX_TOKEN_SECRET to a strong random value (min 32 chars)."
        )
        if settings.production:
            errors.append(msg)
        else:
            warnings.append(msg)

    # 检查数据库凭据
    if "perm_user:perm_pass" in settings.database_url:
        msg = (
            "DATABASE_URL contains default development credentials (perm_user:perm_pass). "
            "Override DATABASE_URL environment variable for production."
        )
        if settings.production:
            errors.append(msg)
        else:
            warnings.append(msg)

    # 检查 Redis 密码
    has_redis_auth = bool(settings.redis_password) or ("@" in settings.redis_url and "redis://:" in settings.redis_url)
    if not has_redis_auth and settings.redis_url == "redis://localhost:16380/0":
        msg = (
            "REDIS_URL uses default development URL without password. "
            "Set REDIS_PASSWORD or use a password-protected Redis URL."
        )
        if settings.production:
            errors.append(msg)
        else:
            warnings.append(msg)

    # 检查 JWT 密钥路径
    _jwt_public = Path(settings.jwt_public_key_path)
    if not _jwt_public.is_file():
        msg = f"JWT public key not found at {settings.jwt_public_key_path}."
        errors.append(msg)  # 无论开发还是生产，JWT 密钥缺失都是致命错误

    # 生产模式：TLS 建议
    if settings.production and not settings.tls_enabled:
        warnings.append(
            "TLS is not enabled in production mode. "
            "Set TLS_ENABLED=true and configure TLS_CERT_FILE/TLS_KEY_FILE."
        )

    # 生产模式：service_api_key 检查
    if settings.production and not settings.service_api_key:
        msg = (
            "SERVICE_API_KEY is not set. "
            "In production, RAG system calls must be authenticated via a pre-shared API key. "
            "Set SERVICE_API_KEY or SERVICE_API_KEY_FILE in environment."
        )
        warnings.append(msg)  # 当前为警告（向后兼容），后续版本将升级为错误

    # 生产模式：错误阻止启动
    if errors:
        raise RuntimeError(
            "Production security checks failed:\n- " + "\n- ".join(errors)
        )

    return warnings
