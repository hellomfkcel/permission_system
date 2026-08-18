"""Prometheus 兼容指标收集器 — 线程安全的 in-memory 计数器。"""

import threading
import time

_start_time = time.time()

# ── Counter 指标（单调递增）──
_counters: dict[str, int] = {}
_counters_lock = threading.Lock()


def inc_counter(name: str, value: int = 1) -> None:
    """递增计数器。"""
    with _counters_lock:
        _counters[name] = _counters.get(name, 0) + value


# ── 便捷方法 ──


def _project_label(project_id: str | None) -> str:
    """项目标签值。未知来源统一记为 unknown，避免标签基数因空值分裂。"""
    return project_id or "unknown"


def record_authz_decision(
    endpoint: str, decision: str, project_id: str | None = None,
) -> None:
    """记录一次权限判定结果。

    Prometheus metric: authz_decision_total{endpoint, decision, project}
    project 维度用于区分多项目共用一个服务实例时各项目的判定量与拒绝率。
    """
    inc_counter(
        f"authz_decision_total;endpoint={endpoint};decision={decision}"
        f";project={_project_label(project_id)}"
    )


def record_authz_call_failed(
    endpoint: str, kind: str, project_id: str | None = None,
) -> None:
    """记录一次权限服务调用失败。

    Prometheus metric: authz_call_failed_total{endpoint, kind, project}
    kind ∈ {timeout, connection, http_error}
    """
    inc_counter(
        f"authz_call_failed_total;endpoint={endpoint};kind={kind}"
        f";project={_project_label(project_id)}"
    )


def record_authz_obligation_unknown() -> None:
    """记录一次未知 obligation key。恒应为 0；非零即契约演进信号。

    Prometheus metric: authz_obligation_unknown_total
    """
    inc_counter("authz_obligation_unknown_total")


def record_event_published() -> None:
    """记录一次 VisibilityChanged 事件发布。"""
    inc_counter("visibility_events_published_total")


def record_event_publish_failed() -> None:
    """记录一次事件发布失败。"""
    inc_counter("visibility_events_publish_failed_total")


# Keycloak 同步指标


def record_keycloak_sync_success(
    created: int = 0, updated: int = 0, deleted: int = 0,
) -> None:
    """记录一次成功的 Keycloak 用户/组同步。

    Prometheus metrics:
      keycloak_sync_total — 同步总次数（按 outcome=success 标签）
      keycloak_sync_users_created — 本次同步创建的用户数
      keycloak_sync_users_updated — 本次同步更新的用户数
      keycloak_sync_users_deleted — 本次同步删除的用户数
    """
    inc_counter("keycloak_sync_total;outcome=success")
    inc_counter("keycloak_sync_users_created_total", created)
    inc_counter("keycloak_sync_users_updated_total", updated)
    inc_counter("keycloak_sync_users_deleted_total", deleted)


def record_keycloak_sync_failed() -> None:
    """记录一次失败的 Keycloak 用户/组同步。

    Prometheus metric: keycloak_sync_total{outcome="failed"}
    """
    inc_counter("keycloak_sync_total;outcome=failed")


# ── Prometheus text format 输出 ──


def get_prometheus_metrics() -> str:
    """生成 Prometheus text format 指标输出。"""
    lines: list[str] = []

    # Uptime
    uptime = time.time() - _start_time
    lines.append("# HELP permission_service_uptime_seconds Service uptime in seconds")
    lines.append("# TYPE permission_service_uptime_seconds gauge")
    lines.append(f"permission_service_uptime_seconds {uptime:.0f}")

    # Counter metrics
    counter_helps = {
        "authz_decision_total": "Authorization decisions by endpoint, outcome (allow/deny/indeterminate) and project",
        "authz_call_failed_total": "Failed authorization calls by endpoint, failure kind and project",
        "authz_obligation_unknown_total": "Unknown obligation keys encountered (should be 0)",
        "visibility_events_published_total": "VisibilityChanged events published to Redis",
        "visibility_events_publish_failed_total": "VisibilityChanged event publishing failures",
        "keycloak_sync_total": "Keycloak user/group sync runs by outcome (success/failed)",
        "keycloak_sync_users_created_total": "Total users created via Keycloak sync",
        "keycloak_sync_users_updated_total": "Total users updated via Keycloak sync",
        "keycloak_sync_users_deleted_total": "Total users deleted via Keycloak sync",
    }

    with _counters_lock:
        for metric_name, help_text in counter_helps.items():
            prefix = f"{metric_name};"
            matching = {
                k: v for k, v in _counters.items() if k.startswith(prefix)
            }
            if not matching:
                continue

            lines.append(f"# HELP {metric_name} {help_text}")
            lines.append(f"# TYPE {metric_name} counter")
            for full_key, value in sorted(matching.items()):
                labels_str = full_key[len(prefix):]
                # 内部键形如 name;k1=v1;k2=v2，输出需为 Prometheus 文本格式
                # name{k1="v1",k2="v2"}：标签值必须加引号并转义，否则无法被采集端解析。
                label_parts = []
                for part in labels_str.split(";"):
                    if "=" not in part:
                        continue
                    label_name, _, label_value = part.partition("=")
                    escaped = (
                        label_value.replace("\\", "\\\\")
                        .replace('"', '\\"')
                        .replace("\n", "\\n")
                    )
                    label_parts.append(f'{label_name}="{escaped}"')
                if label_parts:
                    lines.append(f"{metric_name}{{{','.join(label_parts)}}} {value}")
                else:
                    lines.append(f"{metric_name} {value}")

        # Non-prefixed counters
        for k, v in sorted(_counters.items()):
            if ";" in k:
                continue  # Already handled above
            lines.append(f"# HELP {k} Counter")
            lines.append(f"# TYPE {k} counter")
            lines.append(f"{k} {v}")

    return "\n".join(lines) + "\n"
