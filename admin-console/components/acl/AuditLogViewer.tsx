/**
 * AuditLogViewer — 审计日志查询与导出组件。
 */

"use client";

import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { useAuthStore } from "@/stores/useAuthStore";
import { useResourceNames } from "@/lib/useResourceNames";

// ── 类型 ──

interface AuditEntry {
  id: string;
  event_type: string;
  resource_type: string | null;
  resource_id: string | null;
  kb_id: string | null;
  tenant_id: string;
  change_detail: Record<string, unknown>;
  version: number;
  created_at: string;
}

// ── 事件类型标签 ──

const EVENT_LABELS: Record<string, { label: string; color: string }> = {
  RESOURCE_REGISTERED:    { label: "资源注册", color: "bg-green-100 text-green-800" },
  RESOURCE_LINKED:        { label: "挂载建立", color: "bg-blue-100 text-blue-800" },
  RESOURCE_UNLINKED:      { label: "挂载解除", color: "bg-orange-100 text-orange-800" },
  RESOURCE_RETIRED:       { label: "资源退役", color: "bg-red-100 text-red-800" },
  RESOURCE_ATTR_UPDATED:  { label: "属性更新", color: "bg-purple-100 text-purple-800" },
  ACL_GRANTED:            { label: "ACL 授予", color: "bg-green-100 text-green-800" },
  ACL_REVOKED:            { label: "ACL 回收", color: "bg-yellow-100 text-yellow-800" },
  ACL_BATCH_GRANTED:      { label: "ACL 批量授予", color: "bg-emerald-100 text-emerald-800" },
  ROLE_BOUND:             { label: "角色绑定", color: "bg-indigo-100 text-indigo-800" },
  ROLE_UNBOUND:           { label: "角色解绑", color: "bg-yellow-100 text-yellow-800" },
  RESTRICTION_ADDED:      { label: "封禁添加", color: "bg-red-100 text-red-800" },
  RESTRICTION_REMOVED:    { label: "封禁解除", color: "bg-green-100 text-green-800" },
  OWNERSHIP_TRANSFERRED:  { label: "所有权转移", color: "bg-purple-100 text-purple-800" },
  RESOURCE_REGISTERED_KB: { label: "KB 注册", color: "bg-blue-100 text-blue-800" },
  VisibilityChanged:      { label: "可见性变更", color: "bg-cyan-100 text-cyan-800" },
};

const PAGE_SIZE = 50;

export default function AuditLogViewer() {
  const { currentProjectId } = useAuthStore();

  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const { formatResource } = useResourceNames(currentProjectId);
  // 动态资源类型列表
  const [availableResTypes, setAvailableResTypes] = useState<string[]>([]);

  // 筛选
  const [filters, setFilters] = useState({
    resource_type: "",
    resource_id: "",
    principal: "",
    event_type: "",
    decision_id: "",
    from_time: "",
    to_time: "",
  });

  // 加载可用资源类型
  useEffect(() => {
    api.get("/api/v1/resources").then(r => {
      const types = new Set((r.data as Array<{resource_type: string}>).map(x => x.resource_type));
      setAvailableResTypes(["platform", ...Array.from(types)].sort());
    }).catch(() => {});
  }, []);

  const load = useCallback(() => {
    setLoading(true);
    setError("");
    const params = new URLSearchParams({ limit: String(PAGE_SIZE * 4) });
    if (filters.resource_type) params.set("resource_type", filters.resource_type);
    if (filters.resource_id) params.set("resource_id", filters.resource_id);
    if (filters.principal) params.set("principal", filters.principal);
    if (filters.decision_id) params.set("decision_id", filters.decision_id);
    if (filters.from_time) params.set("from_time", filters.from_time);
    if (filters.to_time) params.set("to_time", filters.to_time);

    api
      .get<AuditEntry[]>(`/api/v1/audit?${params.toString()}`)
      .then(r => setEntries(r.data || []))
      .catch(err => setError(err?.response?.data?.detail || err?.message || "加载审计日志失败"))
      .finally(() => setLoading(false));
  }, [filters.resource_type, filters.resource_id, filters.principal, filters.decision_id, filters.from_time, filters.to_time]);

  useEffect(() => { load(); }, [load]);

  // P2-3: principal 过滤改由后端处理，前端仅保留 event_type 客户端快速筛选
  const filtered = entries.filter(e => {
    if (filters.event_type && e.event_type !== filters.event_type) return false;
    return true;
  });

  const eventTypes = Array.from(new Set(entries.map(e => e.event_type))).sort();

  // 导出
  const handleExport = (format: "csv" | "json") => {
    const data = filtered.length > 0 ? filtered : entries;
    if (format === "json") {
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `audit-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
    } else {
      const header = "version,event_type,resource_type,resource_id,tenant_id,detail,created_at";
      const rows = data.map(e =>
        [e.version, e.event_type, e.resource_type || "", e.resource_id || "", e.tenant_id,
          `"${JSON.stringify(e.change_detail).replace(/"/g, '""')}"`, e.created_at].join(",")
      );
      const blob = new Blob([header + "\n" + rows.join("\n")], { type: "text/csv;charset=utf-8;" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `audit-${new Date().toISOString().slice(0, 10)}.csv`;
      a.click();
    }
  };

  return (
    <div>
      {/* 筛选栏 */}
      <div className="bg-white rounded-lg shadow p-4 mb-4">
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">资源类型</label>
            <select
              value={filters.resource_type}
              onChange={e => setFilters({ ...filters, resource_type: e.target.value })}
              className="w-full border rounded-lg px-3 py-2 text-sm"
            >
              <option value="">全部</option>
              {availableResTypes.map(rt => (
                <option key={rt} value={rt}>{rt}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">资源 ID</label>
            <input
              type="text"
              value={filters.resource_id}
              onChange={e => setFilters({ ...filters, resource_id: e.target.value })}
              placeholder="搜索资源 ID..."
              className="w-full border rounded-lg px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">关键词搜索</label>
            <input
              type="text"
              value={filters.principal}
              onChange={e => setFilters({ ...filters, principal: e.target.value })}
              placeholder="搜索主体/change_detail..."
              className="w-full border rounded-lg px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">事件类型</label>
            <select
              value={filters.event_type}
              onChange={e => setFilters({ ...filters, event_type: e.target.value })}
              className="w-full border rounded-lg px-3 py-2 text-sm"
            >
              <option value="">全部</option>
              {eventTypes.map(et => (
                <option key={et} value={et}>{EVENT_LABELS[et]?.label || et}</option>
              ))}
            </select>
          </div>
        </div>
        {/* P2-3: 高级过滤 — decision_id + 时间范围 */}
        <div className="grid grid-cols-2 lg:grid-cols-3 gap-3 mt-3">
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Decision ID (跨系统取证)</label>
            <input
              type="text"
              value={filters.decision_id}
              onChange={e => setFilters({ ...filters, decision_id: e.target.value })}
              placeholder="搜索 Cerbos decision_id..."
              className="w-full border rounded-lg px-3 py-2 text-sm font-mono"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">起始时间 (ISO 8601)</label>
            <input
              type="text"
              value={filters.from_time}
              onChange={e => setFilters({ ...filters, from_time: e.target.value })}
              placeholder="如 2026-07-31T00:00:00"
              className="w-full border rounded-lg px-3 py-2 text-sm font-mono"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">结束时间 (ISO 8601)</label>
            <input
              type="text"
              value={filters.to_time}
              onChange={e => setFilters({ ...filters, to_time: e.target.value })}
              placeholder="如 2026-07-31T23:59:59"
              className="w-full border rounded-lg px-3 py-2 text-sm font-mono"
            />
          </div>
        </div>
      </div>

      {/* 错误 */}
      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg px-4 py-3 mb-4 text-sm text-red-700">
          {error}
          <button onClick={load} className="ml-4 underline">重试</button>
        </div>
      )}

      {/* 加载/空态 */}
      {loading ? (
        <div className="bg-white rounded-lg shadow p-12 text-center text-gray-500">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600 mx-auto mb-3" />
          加载审计日志...
        </div>
      ) : filtered.length === 0 ? (
        <div className="bg-white rounded-lg shadow p-12 text-center text-gray-400">
          <div className="text-4xl mb-3">📋</div>
          <p>{entries.length === 0 ? "暂无审计记录（权限变更操作会自动生成）" : `无匹配结果（共 ${entries.length} 条记录）`}</p>
        </div>
      ) : (
        <div className="bg-white rounded-lg shadow overflow-hidden">
          <table className="w-full">
            <thead className="bg-gray-50 border-b">
              <tr>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase w-16">版本</th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase w-32">事件</th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">资源</th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">租户</th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase w-40">时间</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map(entry => {
                const isExpanded = expandedId === entry.id;
                const evLabel = EVENT_LABELS[entry.event_type];
                return (
                  <tr
                    key={entry.id}
                    onClick={() => setExpandedId(isExpanded ? null : entry.id)}
                    className="border-t hover:bg-gray-50 cursor-pointer transition-colors"
                  >
                    <td className="px-4 py-3 text-sm font-mono text-gray-400">v{entry.version}</td>
                    <td className="px-4 py-3">
                      <span className={`px-2 py-0.5 rounded-full text-[11px] font-medium ${evLabel?.color || "bg-gray-100 text-gray-700"}`}>
                        {evLabel?.label || entry.event_type}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-600 max-w-[200px] truncate"
                        title={entry.resource_type && entry.resource_id ? `${entry.resource_type}:${entry.resource_id}` : ""}>
                      {entry.resource_type && entry.resource_id
                        ? formatResource(entry.resource_type, entry.resource_id)
                        : <span className="text-gray-300">—</span>}
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-600">{entry.tenant_id}</td>
                    <td className="px-4 py-3 text-sm text-gray-500">{new Date(entry.created_at).toLocaleString()}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          {/* 展开详情 */}
          {expandedId && (() => {
            const entry = filtered.find(e => e.id === expandedId);
            if (!entry) return null;
            return (
              <div className="px-6 py-4 bg-gray-50 border-t">
                <div className="grid grid-cols-2 gap-4 text-sm">
                  <div>
                    <h4 className="font-medium text-gray-700 mb-2">变更详情 (change_detail)</h4>
                    <pre className="bg-white border rounded-lg p-3 text-xs font-mono text-gray-700 overflow-auto max-h-48">
                      {JSON.stringify(entry.change_detail, null, 2)}
                    </pre>
                  </div>
                  <div className="space-y-2">
                    <div><span className="text-gray-500">事件 ID：</span><span className="font-mono text-xs">{entry.id}</span></div>
                    <div><span className="text-gray-500">版本号：</span><span className="font-mono font-bold">v{entry.version}</span></div>
                    <div><span className="text-gray-500">事件类型：</span><span>{entry.event_type}</span></div>
                    {entry.kb_id && <div><span className="text-gray-500">关联 KB：</span><span className="text-xs">{formatResource("kb", entry.kb_id)}</span></div>}
                    <div><span className="text-gray-500">时间：</span><span>{new Date(entry.created_at).toLocaleString()}</span></div>
                  </div>
                </div>
              </div>
            );
          })()}
        </div>
      )}

      {/* 底部信息 + 导出按钮 */}
      <div className="mt-4 flex items-center justify-between">
        <div className="text-xs text-gray-400">
          共 {filtered.length} 条记录（总计 {entries.length} 条）
          &nbsp;·&nbsp;版本号全局单调递增
          &nbsp;·&nbsp;事件由 Outbox 模式持久化（permission_changes 表）
        </div>
        <div className="flex gap-2">
          <button onClick={() => handleExport("csv")} className="bg-green-600 text-white px-3 py-1.5 rounded-lg hover:bg-green-700 text-xs font-medium">
            📥 CSV
          </button>
          <button onClick={() => handleExport("json")} className="bg-blue-600 text-white px-3 py-1.5 rounded-lg hover:bg-blue-700 text-xs font-medium">
            📋 JSON
          </button>
          <button onClick={load} disabled={loading} className="bg-gray-600 text-white px-3 py-1.5 rounded-lg hover:bg-gray-700 text-xs font-medium disabled:opacity-50">
            🔄 刷新
          </button>
        </div>
      </div>
    </div>
  );
}
