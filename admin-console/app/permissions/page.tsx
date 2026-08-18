/** 权限管理 — ACL 授予/回收 + 角色绑定。
 *
 * 使用 PermissionGrantDialog 和 RoleBindingManager 组件。
 */

"use client";

import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { useAuthStore } from "@/stores/useAuthStore";
import { showConfirm } from "@/components/shared/Toast";
import { useToast } from "@/components/shared/Toast";
import PermissionGrantDialog from "@/components/acl/PermissionGrantDialog";
import RoleBindingManager from "@/components/acl/RoleBindingManager";
import PermissionTrace from "@/components/acl/PermissionTrace";
import { useResourceNames } from "@/lib/useResourceNames";

interface ACLEntry {
  id: string; tenant_id: string; principal: string; resource_type: string;
  resource_id: string; action: string; granted_by: string; granted_at: string;
  expires_at: string | null; revoked: boolean;
}

export default function PermissionsPage() {
  const { showToast } = useToast();
  const { currentProjectId } = useAuthStore();
  const { formatResource } = useResourceNames(currentProjectId);

  const [activeTab, setActiveTab] = useState<"acl" | "roles">("acl");
  const [entries, setEntries] = useState<ACLEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [showGrantDialog, setShowGrantDialog] = useState(false);
  const [showImportDialog, setShowImportDialog] = useState(false); // P2-1: CSV 导入
  const [csvFile, setCsvFile] = useState<File | null>(null);
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState<{created: number; skipped: number; errors: string[]} | null>(null);

  // 筛选
  const [filterPrincipal, setFilterPrincipal] = useState("");
  const [filterResource, setFilterResource] = useState("");

  const loadACLs = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.get<ACLEntry[]>("/api/v1/acl");
      setEntries(res.data);
    } catch {
      setEntries([]);
    }
    setLoading(false);
  }, []);

  useEffect(() => { loadACLs(); }, [loadACLs, currentProjectId]);

  const handleRevoke = async (entry: ACLEntry) => {
    if (!showConfirm(`确认回收 ${entry.principal} 对 ${entry.resource_type}:${entry.resource_id} 的 ${entry.action} 权限？`)) return;
    try {
      await api.post("/api/v1/acl/revoke", {
        principal: entry.principal,
        resource_type: entry.resource_type,
        resource_id: entry.resource_id,
        action: entry.action,
      });
      showToast("success", "权限已回收");
      loadACLs();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      showToast("error", err?.response?.data?.detail || "回收失败");
    }
  };

  // P2-1: CSV 导入
  const handleImportCSV = async () => {
    if (!csvFile) return;
    setImporting(true);
    setImportResult(null);
    try {
      const formData = new FormData();
      formData.append("file", csvFile);
      const res = await api.post("/api/v1/acl/import-csv", formData, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      setImportResult(res.data);
      showToast("success", `CSV 导入完成：创建 ${res.data.created} 条，跳过 ${res.data.skipped} 条`);
      loadACLs();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      showToast("error", err?.response?.data?.detail || "CSV 导入失败");
    } finally {
      setImporting(false);
    }
  };

  const handleExportCSV = () => {
    const active = entries.filter(e => !e.revoked);
    const header = "principal,resource_type,resource_id,action,granted_by,granted_at,expires_at";
    const rows = active.map(e =>
      [e.principal, e.resource_type, e.resource_id, e.action, e.granted_by, e.granted_at, e.expires_at || ""].join(",")
    );
    const blob = new Blob([[header, ...rows].join("\n")], { type: "text/csv;charset=utf-8;" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `acl-export-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
  };

  const activeEntries = entries.filter(e => !e.revoked);
  const filteredEntries = activeEntries.filter(e => {
    if (filterPrincipal && !e.principal.toLowerCase().includes(filterPrincipal.toLowerCase())) return false;
    if (filterResource && !`${e.resource_type}:${e.resource_id}`.toLowerCase().includes(filterResource.toLowerCase())) return false;
    return true;
  });

  // 按 action 分组统计
  const actionCounts: Record<string, number> = {};
  for (const e of activeEntries) {
    actionCounts[e.action] = (actionCounts[e.action] || 0) + 1;
  }
  const topActions = Object.entries(actionCounts).sort(([,a], [,b]) => b - a).slice(0, 6);

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold">🔑 权限管理</h1>
          <div className="flex gap-4 mt-3">
            <button
              onClick={() => setActiveTab("acl")}
              className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                activeTab === "acl" ? "bg-blue-600 text-white" : "bg-gray-200 text-gray-700 hover:bg-gray-300"
              }`}
            >
              ACL 权限
            </button>
            <button
              onClick={() => setActiveTab("roles")}
              className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                activeTab === "roles" ? "bg-blue-600 text-white" : "bg-gray-200 text-gray-700 hover:bg-gray-300"
              }`}
            >
              角色绑定
            </button>
          </div>
        </div>
        <div className="flex gap-2">
          {activeTab === "acl" && (
            <>
              <button onClick={() => { setImportResult(null); setCsvFile(null); setShowImportDialog(true); }} className="bg-purple-600 text-white px-4 py-2 rounded-lg hover:bg-purple-700 text-sm font-medium">
                📤 导入 CSV
              </button>
              <button onClick={handleExportCSV} className="bg-green-600 text-white px-4 py-2 rounded-lg hover:bg-green-700 text-sm font-medium">
                📥 导出 CSV
              </button>
              <button
                onClick={() => setShowGrantDialog(true)}
                className="bg-blue-600 text-white px-4 py-2 rounded-lg hover:bg-blue-700 text-sm font-medium"
              >
                + 授予权限
              </button>
            </>
          )}
        </div>
      </div>

      {/* ACL Tab */}
      {activeTab === "acl" && (
        <>
          {/* 统计卡片 */}
          <div className="grid grid-cols-6 gap-3 mb-4">
            {topActions.map(([action, count]) => (
              <div key={action} className="bg-white rounded-lg border border-gray-200 p-3 text-center">
                <div className="text-xl font-bold text-blue-600">{count}</div>
                <div className="text-xs text-gray-500 mt-1 font-mono">{action}</div>
              </div>
            ))}
          </div>

          {/* 筛选栏 */}
          <div className="flex gap-2 mb-4">
            <input
              type="text"
              value={filterPrincipal}
              onChange={e => setFilterPrincipal(e.target.value)}
              placeholder="搜索主体..."
              className="border border-gray-300 rounded-lg px-3 py-2 text-sm w-48"
            />
            <input
              type="text"
              value={filterResource}
              onChange={e => setFilterResource(e.target.value)}
              placeholder="搜索资源..."
              className="border border-gray-300 rounded-lg px-3 py-2 text-sm w-48"
            />
          </div>

          {loading ? (
            <div className="text-center py-12 text-gray-400">加载中...</div>
          ) : filteredEntries.length === 0 ? (
            <div className="text-center py-12 bg-white rounded-lg border border-gray-200">
              <p className="text-gray-400 text-lg mb-2">🔑</p>
              <p className="text-gray-500">暂无权限条目</p>
              <p className="text-gray-400 text-sm mt-1">点击&ldquo;授予权限&rdquo;开始管理</p>
            </div>
          ) : (
            <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
              <table className="w-full">
                <thead className="bg-gray-50 border-b">
                  <tr>
                    <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">主体</th>
                    <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">资源</th>
                    <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">权限</th>
                    <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">授予者</th>
                    <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">过期</th>
                    <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">时间</th>
                    <th className="text-right px-4 py-3 text-xs font-semibold text-gray-500 uppercase">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredEntries.map(entry => (
                    <tr key={entry.id} className="border-t hover:bg-gray-50 transition-colors">
                      <td className="px-4 py-3">
                        <span className="text-sm font-mono text-gray-800">{entry.principal}</span>
                      </td>
                      <td className="px-4 py-3 text-sm text-gray-600 max-w-[220px] truncate"
                          title={`${entry.resource_type}:${entry.resource_id}`}>
                        {formatResource(entry.resource_type, entry.resource_id)}
                      </td>
                      <td className="px-4 py-3">
                        <span className="inline-flex bg-green-100 text-green-800 px-2 py-0.5 rounded-full text-xs font-medium">
                          {entry.action}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-sm text-gray-500">{entry.granted_by}</td>
                      <td className="px-4 py-3 text-sm text-gray-400">
                        {entry.expires_at ? new Date(entry.expires_at).toLocaleDateString() : "—"}
                      </td>
                      <td className="px-4 py-3 text-sm text-gray-400">
                        {new Date(entry.granted_at).toLocaleDateString()}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <button
                          onClick={() => handleRevoke(entry)}
                          className="text-red-600 hover:text-red-800 text-sm font-medium hover:underline"
                        >
                          回收
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="mt-3 text-xs text-gray-400">
            共 {filteredEntries.length} 条活跃权限
            {filteredEntries.length !== activeEntries.length && `（已筛选，总计 ${activeEntries.length} 条）`}
          </div>
        </>
      )}

      {/* Roles Tab — 使用 RoleBindingManager */}
      {activeTab === "roles" && <RoleBindingManager />}

      {/* 权限授予 Dialog */}
      <PermissionGrantDialog
        open={showGrantDialog}
        onClose={() => setShowGrantDialog(false)}
        onGranted={loadACLs}
      />

      {/* CSV 导入 Dialog — P2-1 */}
      {showImportDialog && (
        <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50" onClick={() => !importing && setShowImportDialog(false)}>
          <div className="bg-white rounded-xl shadow-xl p-6 w-full max-w-md" onClick={(e) => e.stopPropagation()}>
            <h3 className="text-lg font-semibold mb-4">📤 导入 CSV 权限</h3>
            <p className="text-xs text-gray-500 mb-4">CSV 格式: principal,resource_type,resource_id,action,granted_by,tenant_id[,expires_at]</p>
            <input
              type="file"
              accept=".csv"
              onChange={(e) => setCsvFile(e.target.files?.[0] || null)}
              className="w-full border rounded-lg p-2 text-sm mb-4"
              disabled={importing}
            />
            {importResult && (
              <div className={`mb-4 p-3 rounded-lg text-sm ${importResult.errors.length > 0 ? 'bg-yellow-50 border border-yellow-200' : 'bg-green-50 border border-green-200'}`}>
                <p>✅ 创建 {importResult.created} 条 | ⏭️ 跳过 {importResult.skipped} 条</p>
                {importResult.errors.length > 0 && (
                  <details className="mt-1">
                    <summary className="text-xs text-red-600 cursor-pointer">{importResult.errors.length} 个错误</summary>
                    <ul className="text-xs text-red-500 mt-1 list-disc pl-4">
                      {importResult.errors.map((err, i) => <li key={i}>{err}</li>)}
                    </ul>
                  </details>
                )}
              </div>
            )}
            <div className="flex justify-end gap-3">
              <button onClick={() => setShowImportDialog(false)} disabled={importing} className="px-4 py-2 text-sm text-gray-500 hover:text-gray-700">取消</button>
              <button onClick={handleImportCSV} disabled={!csvFile || importing} className="px-4 py-2 text-sm bg-purple-600 text-white rounded-lg hover:bg-purple-700 disabled:opacity-50">
                {importing ? "导入中..." : "开始导入"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 权限继承追踪 */}
      {activeTab === "acl" && (
        <div className="mt-6">
          <PermissionTrace />
        </div>
      )}
    </div>
  );
}
