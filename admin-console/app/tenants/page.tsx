/** 租户管理页 — 列表 + 创建 + 搜索。
 */

"use client";

import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { Building2, Plus, Search, Loader2, Users, Trash2 } from "lucide-react";
import api from "@/lib/api";

interface TenantInfo {
  id: string;
  name: string;
  description: string;
  status: "active" | "suspended" | "deleted";
  member_count: number;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export default function TenantsPage() {
  const router = useRouter();
  const [tenants, setTenants] = useState<TenantInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("active");

  // 创建 Dialog 状态
  const [createOpen, setCreateOpen] = useState(false);
  const [createId, setCreateId] = useState("");
  const [createName, setCreateName] = useState("");
  const [createDesc, setCreateDesc] = useState("");
  const [createError, setCreateError] = useState("");
  const [creating, setCreating] = useState(false);

  // 删除确认
  const [deleteTarget, setDeleteTarget] = useState<TenantInfo | null>(null);
  const [deleting, setDeleting] = useState(false);

  const fetchTenants = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const params: Record<string, string | number> = { limit: 100, offset: 0 };
      if (statusFilter) params.status = statusFilter;
      if (search.trim()) params.search = search.trim();
      const { data } = await api.get("/api/v1/tenants", { params });
      setTenants(data.tenants);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "加载租户列表失败";
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [search, statusFilter]);

  useEffect(() => {
    fetchTenants();
  }, [fetchTenants]);

  // 搜索防抖
  useEffect(() => {
    const timer = setTimeout(() => {
      fetchTenants();
    }, 300);
    return () => clearTimeout(timer);
  }, [search]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleCreate = async () => {
    setCreateError("");
    if (!createId.trim() || !createName.trim()) {
      setCreateError("租户 ID 和名称不能为空");
      return;
    }
    if (!/^[a-z][a-z0-9-]{1,63}$/.test(createId.trim())) {
      setCreateError("租户 ID 格式：小写字母开头，仅含小写字母/数字/连字符，2-64 字符");
      return;
    }
    setCreating(true);
    try {
      await api.post("/api/v1/tenants", {
        id: createId.trim(),
        name: createName.trim(),
        description: createDesc.trim(),
      });
      setCreateOpen(false);
      setCreateId("");
      setCreateName("");
      setCreateDesc("");
      fetchTenants();
    } catch (e: unknown) {
      if (e && typeof e === "object" && "response" in e) {
        const resp = (e as { response: { data?: { detail?: string } } }).response;
        setCreateError(resp.data?.detail || "创建失败");
      } else {
        setCreateError("创建租户失败");
      }
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await api.delete(`/api/v1/tenants/${deleteTarget.id}`);
      setDeleteTarget(null);
      fetchTenants();
    } catch (e: unknown) {
      if (e && typeof e === "object" && "response" in e) {
        const resp = (e as { response: { data?: { detail?: string }; status?: number } }).response;
        alert(resp.data?.detail || `删除失败 (HTTP ${resp.status})`);
      }
    } finally {
      setDeleting(false);
    }
  };

  const formatDate = (iso: string) => {
    if (!iso) return "-";
    try {
      return new Date(iso).toLocaleString("zh-CN");
    } catch {
      return iso;
    }
  };

  const statusBadge = (status: string) => {
    switch (status) {
      case "active": return <span className="px-2 py-0.5 text-xs rounded-full bg-green-100 text-green-700">活跃</span>;
      case "suspended": return <span className="px-2 py-0.5 text-xs rounded-full bg-yellow-100 text-yellow-700">已停用</span>;
      case "deleted": return <span className="px-2 py-0.5 text-xs rounded-full bg-red-100 text-red-700">已删除</span>;
      default: return <span className="px-2 py-0.5 text-xs rounded-full bg-gray-100 text-gray-600">{status}</span>;
    }
  };

  return (
    <div className="p-6">
      {/* 页头 */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">🏢 租户管理</h1>
          <p className="text-sm text-gray-500 mt-1">
            管理系统中的所有租户，创建、停用及管理成员
          </p>
        </div>
        <button
          onClick={() => setCreateOpen(true)}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 transition-colors"
        >
          <Plus size={16} />
          创建租户
        </button>
      </div>

      {/* 搜索 + 筛选 */}
      <div className="flex items-center gap-4 mb-4">
        <div className="relative flex-1 max-w-sm">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
          <input
            type="text"
            placeholder="搜索租户 ID 或名称..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full pl-9 pr-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          <option value="">全部状态</option>
          <option value="active">活跃</option>
          <option value="suspended">已停用</option>
          <option value="deleted">已删除</option>
        </select>
      </div>

      {/* 错误/加载/空状态 */}
      {error && (
        <div className="p-4 mb-4 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">
          {error}
          <button onClick={fetchTenants} className="ml-2 underline">重试</button>
        </div>
      )}

      {loading && (
        <div className="flex items-center justify-center py-16 text-gray-400">
          <Loader2 size={24} className="animate-spin mr-2" /> 加载中...
        </div>
      )}

      {!loading && !error && tenants.length === 0 && (
        <div className="flex flex-col items-center justify-center py-16 text-gray-400">
          <Building2 size={48} className="mb-3" />
          <p className="text-lg font-medium">暂无租户</p>
          <p className="text-sm mt-1">点击「创建租户」按钮创建第一个租户</p>
        </div>
      )}

      {/* 租户列表 */}
      {!loading && tenants.length > 0 && (
        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          <table className="w-full">
            <thead className="bg-gray-50">
              <tr>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">租户</th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">状态</th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">成员</th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">创建者</th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">创建时间</th>
                <th className="text-right px-4 py-3 text-xs font-semibold text-gray-500 uppercase">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {tenants.map((t) => (
                <tr
                  key={t.id}
                  className="hover:bg-gray-50 cursor-pointer"
                  onClick={() => router.push(`/tenants/${t.id}`)}
                >
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <Building2 size={16} className="text-blue-500" />
                      <div>
                        <p className="text-sm font-medium text-gray-900">{t.name}</p>
                        <p className="text-xs text-gray-500">{t.id}</p>
                      </div>
                    </div>
                  </td>
                  <td className="px-4 py-3">{statusBadge(t.status)}</td>
                  <td className="px-4 py-3 text-sm text-gray-600">
                    <Users size={14} className="inline mr-1" />
                    {t.member_count}
                  </td>
                  <td className="px-4 py-3 text-sm text-gray-500 font-mono">{t.created_by}</td>
                  <td className="px-4 py-3 text-sm text-gray-500">{formatDate(t.created_at)}</td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        setDeleteTarget(t);
                      }}
                      disabled={t.status === "deleted"}
                      className="p-1.5 text-gray-400 hover:text-red-500 hover:bg-red-50 rounded transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
                      title="删除租户"
                    >
                      <Trash2 size={16} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* 创建 Dialog */}
      {createOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="bg-white rounded-xl shadow-xl w-full max-w-md p-6">
            <h2 className="text-lg font-semibold text-gray-900 mb-4">创建新租户</h2>
            {createError && (
              <div className="mb-3 p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">{createError}</div>
            )}
            <div className="space-y-3">
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">租户 ID *</label>
                <input
                  type="text"
                  value={createId}
                  onChange={(e) => setCreateId(e.target.value)}
                  placeholder="例如: acme-corp"
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
                <p className="text-xs text-gray-400 mt-1">小写字母+数字+连字符，2-64字符，创建后不可修改</p>
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">显示名称 *</label>
                <input
                  type="text"
                  value={createName}
                  onChange={(e) => setCreateName(e.target.value)}
                  placeholder="例如: ACME 公司"
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">描述</label>
                <textarea
                  value={createDesc}
                  onChange={(e) => setCreateDesc(e.target.value)}
                  placeholder="可选描述信息"
                  rows={2}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
            </div>
            <div className="flex justify-end gap-3 mt-6">
              <button
                onClick={() => { setCreateOpen(false); setCreateError(""); }}
                className="px-4 py-2 text-sm text-gray-600 hover:bg-gray-100 rounded-lg transition-colors"
              >
                取消
              </button>
              <button
                onClick={handleCreate}
                disabled={creating}
                className="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors flex items-center gap-2"
              >
                {creating && <Loader2 size={14} className="animate-spin" />}
                创建
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 删除确认 Dialog */}
      {deleteTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="bg-white rounded-xl shadow-xl w-full max-w-sm p-6">
            <h2 className="text-lg font-semibold text-gray-900 mb-2">确认删除租户?</h2>
            <p className="text-sm text-gray-600 mb-4">
              将软删除租户「{deleteTarget.name}」({deleteTarget.id})。
              <br />
              <span className="text-red-600">前提：租户内不能有活跃资源。</span>
            </p>
            <div className="flex justify-end gap-3">
              <button
                onClick={() => setDeleteTarget(null)}
                className="px-4 py-2 text-sm text-gray-600 hover:bg-gray-100 rounded-lg"
              >
                取消
              </button>
              <button
                onClick={handleDelete}
                disabled={deleting}
                className="px-4 py-2 text-sm bg-red-600 text-white rounded-lg hover:bg-red-700 disabled:opacity-50 flex items-center gap-2"
              >
                {deleting && <Loader2 size={14} className="animate-spin" />}
                确认删除
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
