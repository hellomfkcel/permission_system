"""VisibilityChanged 事件发布 + permission_changes 持久化。

双通道并行，互不阻塞：
    Pub/Sub  低延迟实时通知，fire-and-forget
    Stream   持久化消息，订阅方重启后可补消费

Stream 配置：key=visibility_changed_stream，MAXLEN≈100,000（近似裁剪），
消费组 rag-visibility-consumers。
"""

import json
import uuid
from datetime import datetime, timezone

import redis.asyncio as redis

from app.config import settings
from app.metrics_collector import record_event_published, record_event_publish_failed


class EventPublisher:
    """Redis 事件发布器 — 持久化 + Pub/Sub + Stream 三通道。

    Outbox 模式：
    1. write_change_log()  在当前 DB 事务内写 permission_changes，调用方负责 commit
    2. publish_to_redis()  事务提交后发布 Pub/Sub
    3. publish_to_stream() 事务提交后追加 Stream
    """
    CHANNEL = "visibility_changed"
    STREAM_KEY = "visibility_changed_stream"
    STREAM_MAXLEN = 100_000  # 近似裁剪上限

    def __init__(self) -> None:
        self._redis: redis.Redis | None = None

    async def _get_redis(self) -> redis.Redis:
        if self._redis is None:
            self._redis = redis.from_url(settings.get_redis_url())
        return self._redis

    async def write_change_log(
        self,
        db,  # AsyncSession — 由调用方传入，与业务操作共享同一事务
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        project_id: str | None,
        event_type: str = "VisibilityChanged",
        kb_id: str | None = None,
        change_detail: dict | None = None,
    ) -> tuple[int, uuid.UUID]:
        """在同一事务内写入 permission_changes 记录 + 递增版本号。

        调用方负责 await db.commit() 提交事务。
        版本号在同一事务内递增，确保业务变更与事件日志原子提交。

        project_id 由本方法统一写进 change_detail —— permission_changes 没有独立的
        项目列，审计查询按 change_detail->>'project_id' 做隔离。设为必填参数以保证
        新增事件类型不会漏标。平台级变更传 None。

        Returns:
            (version, change_entry_id) — 供 publish_to_redis / publish_to_stream 使用。
        """
        from sqlalchemy import text
        from models.change_log import PermissionChange

        result = await db.execute(
            text("SELECT nextval('global_permission_version')")
        )
        version = result.scalar()

        detail = dict(change_detail or {})
        detail["project_id"] = project_id

        change_id = uuid.uuid4()
        change_entry = PermissionChange(
            id=change_id,
            event_type=event_type,
            resource_type=resource_type,
            resource_id=resource_id if resource_id else None,
            kb_id=kb_id,
            tenant_id=tenant_id,
            change_detail=detail,
            version=version,
        )
        db.add(change_entry)
        return version, change_id

    def _build_event(
        self,
        change_id: uuid.UUID,
        version: int,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        event_type: str,
        kb_id: str | None,
        change_detail: dict | None,
        unmounted: bool,
    ) -> dict:
        """构造事件 payload（Pub/Sub 和 Stream 共用）。"""
        event = {
            "event_id": str(change_id),
            "event_type": event_type,
            "resource": {"type": resource_type, "id": resource_id},
            "tenant": tenant_id,
            "version": version,
            "unmounted": unmounted,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        }
        if kb_id:
            event["channel"] = {"kb": kb_id}
        if change_detail:
            event["change_detail"] = change_detail
        return event

    async def publish_to_redis(
        self,
        change_id: uuid.UUID,
        version: int,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        event_type: str = "VisibilityChanged",
        kb_id: str | None = None,
        change_detail: dict | None = None,
        unmounted: bool = False,
    ) -> None:
        """发布 VisibilityChanged 到 Redis Pub/Sub（事务提交后调用）。

        Redis 发布失败不影响已提交的业务事务 — 事件已持久化在 permission_changes 表中，
        可通过对账或重放恢复。

        Args:
            unmounted: 资源是否已解除挂载/退役。unlink/retire 操作时应设为 True。
        """
        r = await self._get_redis()

        event = self._build_event(
            change_id, version, tenant_id, resource_type, resource_id,
            event_type, kb_id, change_detail, unmounted,
        )

        try:
            await r.publish(self.CHANNEL, json.dumps(event))
            record_event_published()
        except Exception:
            record_event_publish_failed()
            raise  # 调用方决定如何处理（不影响已提交的 DB 事务）

    async def publish_to_stream(
        self,
        change_id: uuid.UUID,
        version: int,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        event_type: str = "VisibilityChanged",
        kb_id: str | None = None,
        change_detail: dict | None = None,
        unmounted: bool = False,
    ) -> str | None:
        """发布 VisibilityChanged 到 Redis Stream（持久化，支持断点续消费）。

        Stream 保留最近约 100,000 条消息，订阅方通过 Consumer Group + XREADGROUP
        实现可靠消费。

        Returns:
            Stream entry ID，失败返回 None。
        """
        r = await self._get_redis()

        event = self._build_event(
            change_id, version, tenant_id, resource_type, resource_id,
            event_type, kb_id, change_detail, unmounted,
        )

        try:
            entry_id = await r.xadd(
                self.STREAM_KEY,
                {"event": json.dumps(event)},
                maxlen=self.STREAM_MAXLEN,
                approximate=True,
            )
            return entry_id
        except Exception:
            # Stream 写入失败不阻塞 Pub/Sub 路径
            # 事件已持久化在 permission_changes 表中
            return None

    async def publish_event(
        self,
        change_id: uuid.UUID,
        version: int,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        event_type: str = "VisibilityChanged",
        kb_id: str | None = None,
        change_detail: dict | None = None,
        unmounted: bool = False,
    ) -> None:
        """双通道发布：Pub/Sub 实时通知 + Stream 持久化。

        推荐的发布入口。两个通道独立失败，互不阻塞。
        """
        # Pub/Sub 路径（实时通知，低延迟）
        try:
            await self.publish_to_redis(
                change_id, version, tenant_id, resource_type, resource_id,
                event_type, kb_id, change_detail, unmounted,
            )
        except Exception:
            pass  # Pub/Sub 发布失败不影响 Stream

        # Stream 路径（持久化，可靠消费）
        await self.publish_to_stream(
            change_id, version, tenant_id, resource_type, resource_id,
            event_type, kb_id, change_detail, unmounted,
        )

    # 便捷方法：同时写 change_log + 发布 Redis（向后兼容）
    async def publish_visibility_changed(
        self,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        project_id: str | None,
        event_type: str = "VisibilityChanged",
        kb_id: str | None = None,
        change_detail: dict | None = None,
    ) -> int:
        """一站式发布事件（向后兼容接口）。

        内部在独立事务中写 change_log 后双通道发布（Pub/Sub + Stream）。
        新代码应使用 write_change_log + publish_event 两步模式。

        project_id 必填，与 write_change_log 同一口径：审计的项目隔离靠它。
        """
        from app.database import async_session

        async with async_session() as db:
            version, change_id = await self.write_change_log(
                db, tenant_id, resource_type, resource_id, project_id,
                event_type, kb_id, change_detail,
            )
            await db.commit()

        await self.publish_event(
            change_id, version, tenant_id, resource_type, resource_id,
            event_type, kb_id, change_detail,
        )
        return version

    async def publish_tenant_created(
        self,
        db,  # AsyncSession — 由调用方传入
        tenant_id: str,
        tenant_name: str,
        created_by: str,
    ) -> None:
        """发布 TenantCreated 事件。

        在创建租户的事务内写入 change_log。
        调用方负责 await db.commit()。
        """
        import uuid as _uuid
        from sqlalchemy import text as _text
        from models.change_log import PermissionChange

        # Increment global version and insert change log in same transaction
        result = await db.execute(
            _text("SELECT nextval('global_permission_version')")
        )
        version = result.scalar()

        change_entry = PermissionChange(
            id=_uuid.uuid4(),
            event_type="TenantCreated",
            resource_type="tenant",
            resource_id=tenant_id,
            tenant_id=tenant_id,
            change_detail={
                "action": "tenant_created",
                "tenant_name": tenant_name,
                "created_by": created_by,
            },
            version=version,
        )
        db.add(change_entry)
        # Note: caller must await db.commit() to persist

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


# 全局单例
_event_publisher: EventPublisher | None = None


def get_event_publisher() -> EventPublisher:
    global _event_publisher
    if _event_publisher is None:
        _event_publisher = EventPublisher()
    return _event_publisher
