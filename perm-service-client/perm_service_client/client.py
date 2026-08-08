"""权限平台通用 HTTP 客户端。

Phase 3: 从 RAG v14 PermissionServiceClient 提取的独立 SDK。
零 RAG 依赖，纯 httpx + 可选 OTel tracing。

设计依据：docs/外部系统设计.md §2.6 + 独立权限平台升级方案 Phase 3。
"""

import hashlib
import hmac
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx


def _build_traceparent() -> str | None:
    """从当前 OTel span context 构建 W3C traceparent 头。fail-open。"""
    try:
        from opentelemetry import trace as _otel_trace
        span_context = _otel_trace.get_current_span().get_span_context()
        if span_context.is_valid:
            trace_id = format(span_context.trace_id, "032x")
            span_id = format(span_context.span_id, "016x")
            return f"00-{trace_id}-{span_id}-{span_context.trace_flags:02x}"
    except Exception:
        pass
    return None


class PermissionClient:
    """通用权限平台 HTTP 客户端。

    新项目接入只需提供 base_url + api_key + client_id。
    所有方法通过 REST API 与权限服务后端通信。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        client_id: str = "interactive-backend",
        timeout: float = 15.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_client_id = client_id
        self._client = httpx.Client(timeout=timeout)

    def __repr__(self) -> str:
        return f"PermissionClient(base_url={self.base_url}, client_id={self.default_client_id})"

    # ══════════════════════════════════════════════════════════════
    # HTTP 工具
    # ══════════════════════════════════════════════════════════════

    def _headers(self, request_id: str, client_id: str | None = None) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-Request-Id": request_id,
            "X-Client-Id": client_id or self.default_client_id,
        }
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        traceparent = _build_traceparent()
        if traceparent:
            headers["traceparent"] = traceparent
        return headers

    def close(self) -> None:
        self._client.close()

    # ══════════════════════════════════════════════════════════════
    # 决策面
    # ══════════════════════════════════════════════════════════════

    def check(
        self,
        credential: str,
        action: str,
        resource_type: str,
        resource_id: str,
        channel_kb: str | None = None,
        request_id: str | None = None,
        client_id: str | None = None,
    ) -> Dict[str, Any]:
        """单条权限判定 → POST /v1/check。

        Args:
            credential: JWT 原文
            action: 动词 (kb:read, order:approve, ...)
            resource_type: 资源类型 (kb, document, order, ...)
            resource_id: 资源 ID
            channel_kb: 通道类动词必带 (如 doc:unmount)
        """
        if request_id is None:
            request_id = str(uuid.uuid4())

        body: Dict[str, Any] = {
            "request_id": request_id,
            "credential": credential,
            "action": action,
            "resource": {"type": resource_type, "id": resource_id},
        }
        if channel_kb:
            body["channel"] = {"kb": channel_kb}

        try:
            resp = self._client.post(
                f"{self.base_url}/v1/check",
                json=body,
                headers=self._headers(request_id, client_id),
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {
                "decision": "deny",
                "decision_id": request_id,
                "reasons": ["perm_service_unavailable"],
            }

    def check_batch(
        self,
        credential: str,
        action: str,
        resources: List[Dict[str, Any]],
        request_id: str | None = None,
        client_id: str | None = None,
    ) -> Dict[str, Dict[str, Any]]:
        """批量判定 → POST /v1/check/batch。

        Args:
            resources: [{"type": "kb", "id": "kb-1", "channel_kb": "kb-1"}, ...]
        """
        if request_id is None:
            request_id = str(uuid.uuid4())
        if not resources:
            return {}

        resources = resources[:200]  # 安全上限
        items = []
        for r in resources:
            item = {"action": action, "resource": {"type": r["type"], "id": r["id"]}}
            if r.get("channel_kb"):
                item["channel"] = {"kb": r["channel_kb"]}
            items.append(item)

        try:
            resp = self._client.post(
                f"{self.base_url}/v1/check/batch",
                json={"request_id": request_id, "credential": credential, "items": items},
                headers=self._headers(request_id, client_id),
            )
            resp.raise_for_status()
            data = resp.json()
            result: Dict[str, Dict[str, Any]] = {}
            for item in data.get("results", []):
                rid = item["resource_id"]
                result[rid] = {
                    "decision": item["decision"],
                    "decision_id": item.get("decision_id", request_id),
                    "reasons": [],
                }
            return result
        except Exception:
            return {}

    def filter_items(
        self,
        credential: str,
        items: List[Tuple[str, str]],
        request_id: str | None = None,
        client_id: str | None = None,
    ) -> List[Tuple[str, str]]:
        """检索后逐条复核 → POST /v1/filter。

        Args:
            items: [(resource_type, resource_id), ...]
        """
        if request_id is None:
            request_id = str(uuid.uuid4())
        if not items:
            return items

        body_items = [
            {"resource_type": rt, "resource_id": rid, "channel": {"kb": rid}}
            for rt, rid in items
        ]

        try:
            resp = self._client.post(
                f"{self.base_url}/v1/filter",
                json={"request_id": request_id, "credential": credential, "items": body_items},
                headers=self._headers(request_id, client_id),
            )
            resp.raise_for_status()
            data = resp.json()
            allowed = set(data.get("allowed", []))
            return [(rt, rid) for rt, rid in items if rid in allowed]
        except Exception:
            return []

    def get_prefilter(
        self,
        credential: str,
        request_id: str | None = None,
        client_id: str | None = None,
    ) -> Dict[str, Any]:
        """检索前编译 → GET /v1/prefilter。"""
        if request_id is None:
            request_id = str(uuid.uuid4())

        try:
            resp = self._client.get(
                f"{self.base_url}/v1/prefilter",
                params={"credential": credential},
                headers=self._headers(request_id, client_id),
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {"suspended": True}

    def get_visibility(
        self,
        tenant: str,
        doc_id: str,
        kb_id: str,
        client_id: str = "ingest",
    ) -> Dict[str, Any]:
        """取可见性戳记 → POST /v1/visibility。"""
        try:
            resp = self._client.post(
                f"{self.base_url}/v1/visibility",
                json={"tenant": tenant, "doc_id": doc_id, "channel": {"kb": kb_id}},
                headers=self._headers("stamp", client_id),
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {"unmounted": True}

    # ══════════════════════════════════════════════════════════════
    # ctx_token
    # ══════════════════════════════════════════════════════════════

    def mint_ctx_token(
        self,
        credential: str,
        audience: str = "retrieval-worker",
        ttl_s: int = 600,
        request_id: str | None = None,
        client_id: str | None = None,
    ) -> str:
        """铸造异步上下文令牌 → POST /v1/context。"""
        if request_id is None:
            request_id = str(uuid.uuid4())

        resp = self._client.post(
            f"{self.base_url}/v1/context",
            json={
                "request_id": request_id,
                "credential": credential,
                "audience": audience,
                "ttl_s": min(ttl_s, 600),
            },
            headers=self._headers(request_id, client_id),
        )
        resp.raise_for_status()
        return resp.json()["ctx_token"]

    # ══════════════════════════════════════════════════════════════
    # 生命周期端口
    # ══════════════════════════════════════════════════════════════

    def register_resource(
        self,
        resource_type: str,
        resource_id: str,
        owner: str,
        tenant_id: str,
        name: str | None = None,
        kb_id: str | None = None,
        idempotency_key: str | None = None,
        project_id: str = "rag-v14",
    ) -> str:
        """资源登记 → POST /v1/resources/register。

        project_id: 所属项目 ID，必填（权限服务 v2 新增要求）。
        """
        request_id = str(uuid.uuid4())
        if idempotency_key is None:
            idempotency_key = f"sdk-{tenant_id}-{resource_id}-v1"

        body: Dict[str, Any] = {
            "request_id": request_id,
            "idempotency_key": idempotency_key,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "owner": owner,
            "tenant_id": tenant_id,
            "project_id": project_id,
        }
        if name:
            body["name"] = name
        if kb_id:
            body["kb_id"] = kb_id

        resp = self._client.post(
            f"{self.base_url}/v1/resources/register",
            json=body,
            headers=self._headers(request_id, "interactive-backend"),
        )
        resp.raise_for_status()
        return resp.json()["change_id"]

    def link_resource(
        self,
        doc_id: str,
        kb_id: str,
        tenant_id: str,
        idempotency_key: str | None = None,
        project_id: str = "rag-v14",
    ) -> str:
        """挂载建立 → POST /v1/resources/link。

        project_id: 所属项目 ID，必填（权限服务 v2 新增要求）。
        """
        request_id = str(uuid.uuid4())
        if idempotency_key is None:
            idempotency_key = f"sdk-{tenant_id}-{doc_id}-{kb_id}-v1"

        resp = self._client.post(
            f"{self.base_url}/v1/resources/link",
            json={
                "request_id": request_id,
                "idempotency_key": idempotency_key,
                "resource_type": "document",
                "resource_id": doc_id,
                "kb_id": kb_id,
                "tenant_id": tenant_id,
                "project_id": project_id,
            },
            headers=self._headers(request_id, "interactive-backend"),
        )
        resp.raise_for_status()
        return resp.json()["change_id"]

    def unlink_resource(
        self,
        doc_id: str,
        kb_id: str,
        tenant_id: str,
        idempotency_key: str | None = None,
        project_id: str = "rag-v14",
    ) -> str:
        """解除挂载 → POST /v1/resources/unlink。

        project_id: 所属项目 ID，必填（权限服务 v2 新增要求）。
        """
        request_id = str(uuid.uuid4())
        if idempotency_key is None:
            idempotency_key = f"sdk-{tenant_id}-{doc_id}-{kb_id}-v1"

        resp = self._client.post(
            f"{self.base_url}/v1/resources/unlink",
            json={
                "request_id": request_id,
                "idempotency_key": idempotency_key,
                "resource_type": "document",
                "resource_id": doc_id,
                "kb_id": kb_id,
                "tenant_id": tenant_id,
                "project_id": project_id,
            },
            headers=self._headers(request_id, "interactive-backend"),
        )
        resp.raise_for_status()
        return resp.json()["change_id"]

    def retire_resource(
        self,
        resource_type: str,
        resource_id: str,
        tenant_id: str,
        idempotency_key: str | None = None,
        project_id: str = "rag-v14",
    ) -> str:
        """资源退役 → POST /v1/resources/retire。

        project_id: 所属项目 ID，必填（权限服务 v2 新增要求）。
        """
        request_id = str(uuid.uuid4())
        if idempotency_key is None:
            idempotency_key = f"sdk-{tenant_id}-{resource_id}-v1"

        resp = self._client.post(
            f"{self.base_url}/v1/resources/retire",
            json={
                "request_id": request_id,
                "idempotency_key": idempotency_key,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "tenant_id": tenant_id,
                "project_id": project_id,
            },
            headers=self._headers(request_id, "interactive-backend"),
        )
        resp.raise_for_status()
        return resp.json()["change_id"]
