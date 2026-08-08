"""perm-service-client — 通用权限平台 Python SDK。

使用示例:

    from perm_service_client import PermissionClient

    client = PermissionClient(
        base_url="https://perm.example.com",
        api_key="psk_xxx",
        client_id="project-b-backend",
    )

    result = client.check("order:read", "order", "order-123")
    if result["decision"] == "allow":
        ...

上下文令牌（异步 worker 场景）:

    ctx_token = client.mint_ctx_token(credential=jwt_token, audience="worker-1")
    # 将 ctx_token 传给 worker，worker 中:
    credential = client.verify_ctx_token(ctx_token, "worker-1")

生命周期端口（资源登记/挂载/回收）:

    client.register_resource("kb", "kb-123", owner="user:alice", tenant_id="t1")
    client.link_resource("doc-456", "kb-123", tenant_id="t1")
    client.retire_resource("kb", "kb-123", tenant_id="t1")
"""

from perm_service_client.client import PermissionClient

__all__ = ["PermissionClient"]
__version__ = "1.0.0"
