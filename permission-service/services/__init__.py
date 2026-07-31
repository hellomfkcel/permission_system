"""业务逻辑服务层。

- cerbos_adapter: Cerbos PDP HTTP 调用适配器
- acl_resolver: ACL/角色绑定/封禁的统一查询与权限解析
- jwt_parser: JWT 解析与 Principal 构建
- event_publisher: Redis Pub/Sub 事件发布 + permission_changes 持久化
- stamp_calculator: 可见性戳记计算（visibility 端点专用）
"""
