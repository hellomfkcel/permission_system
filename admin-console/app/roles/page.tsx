/** 角色管理 — 角色列表 + 权限矩阵表格。
 *
 * 设计依据：docs/manage_role_design.md §3.4 管理台页面。
 */

"use client";

import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { Shield, Plus, Loader2, Trash2, Lock } from "lucide-react";
import api from "@/lib/api";

interface RoleDef {
  id: string;
  name: string;
  description: string;
  parent_keycloak_roles: string[];
  is_system: boolean;
  is_keycloak_role: boolean;
  permissions: string[];
  binding_count: number;
  created_at: string;
}

interface PermMatrix {
  roles: {
    name: string;
    parent_keycloak_roles: string[];
    permissions: string[];
  }[];
}

export default function RolesPage() {
  const router = useRouter();
  const [roles, setRoles] = useState<RoleDef[]>([]);
  const [matrix, setMatrix] = useState<PermMatrix | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // 创建 Dialog
  const [createOpen, setCreateOpen] = useState(false);
  const [createName, setCreateName] = useState("");
  const [createDesc, setCreateDesc] = useState("");
  const [createParentRole, setCreateParentRole] = useState("user");
  const [createError, setCreateError] = useState("");
  const [creating, setCreating] = useState(false);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [rolesRes, matrixRes] = await Promise.all([
        api.get("/api/v1/roles/definitions"),
        api.get("/api/v1/roles/permissions"),
      ]);
      setRoles(rolesRes.data);
      setMatrix(matrixRes.data);
    } catch {
      setError("加载角色数据失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  const handleCreate = async () => {
    setCreateError("");
    if (!createName.trim()) { setCreateError("角色名不能为空"); return; }
    setCreating(true);
    try {
      await api.post("/api/v1/roles/definitions", {
        name: createName.trim(),
        description: createDesc.trim(),
        parent_keycloak_roles: [createParentRole],
      });
      setCreateOpen(false);
      setCreateName("");
      setCreateDesc("");
      setCreateParentRole("user");
      loadData();
    } catch (e: any) {
      setCreateError(e.response?.data?.detail || "创建失败");
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (name: string) => {
    if (!confirm(`确认删除角色「${name}」？`)) return;
    try {
      await api.delete(`/api/v1/roles/definitions/${name}`);
      loadData();
    } catch (e: any) {
      alert(e.response?.data?.detail || "删除失败");
    }
  };

  // 构建权限矩阵表：所有 action → 各角色是否有权限
  const allActions = matrix
    ? [...new Set(matrix.roles.flatMap(r => r.permissions))].sort()
    : [];

  const rolePermMap: Record<string, Set<string>> = {};
  if (matrix) {
    for (const r of matrix.roles) {
      rolePermMap[r.name] = new Set(r.permissions);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16 text-gray-400">
        <Loader2 size={24} className="animate-spin mr-2" /> 加载中...
      </div>
    );
  }

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">🔑 角色管理</h1>
          <p className="text-sm text-gray-500 mt-1">管理系统角色定义与权限映射</p>
        </div>
        <button
          onClick={() => setCreateOpen(true)}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700"
        >
          <Plus size={16} /> 创建角色
        </button>
      </div>

      {error && (
        <div className="p-4 mb-4 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">
          {error} <button onClick={loadData} className="ml-2 underline">重试</button>
        </div>
      )}

      {/* 角色列表卡片 */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-8">
        {roles.map((r) => {
          const perms = rolePermMap[r.name] || new Set();
          return (
            <div
              key={r.id}
              className="bg-white rounded-lg border border-gray-200 p-5 hover:shadow-md transition-shadow cursor-pointer"
              onClick={() => router.push(`/roles/${r.name}`)}
            >
              <div className="flex items-start justify-between">
                <div>
                  <div className="flex items-center gap-2">
                    <h3 className="text-lg font-semibold text-gray-900">{r.name}</h3>
                    {r.is_keycloak_role && (
                      <span className="flex items-center gap-1 text-xs text-purple-500 bg-purple-50 px-2 py-0.5 rounded-full">
                        <Lock size={10} /> Keycloak
                      </span>
                    )}
                    {r.is_system && !r.is_keycloak_role && (
                      <span className="flex items-center gap-1 text-xs text-gray-400 bg-gray-100 px-2 py-0.5 rounded-full">
                        <Lock size={10} /> 系统内置
                      </span>
                    )}
                  </div>
                  <p className="text-sm text-gray-500 mt-1">{r.description}</p>
                </div>
                {!r.is_system && (
                  <button
                    onClick={(e) => { e.stopPropagation(); handleDelete(r.name); }}
                    className="p-1.5 text-gray-400 hover:text-red-500 hover:bg-red-50 rounded"
                    title="删除"
                  >
                    <Trash2 size={14} />
                  </button>
                )}
              </div>
              <div className="flex items-center gap-4 mt-3 text-xs text-gray-400">
                <span>父角色: {r.parent_keycloak_roles?.join(", ") || "无"}</span>
                <span>{r.binding_count} 个绑定</span>
                <span>{perms.size} 个权限</span>
              </div>
            </div>
          );
        })}
      </div>

      {/* 权限矩阵表格 */}
      {matrix && allActions.length > 0 && (
        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          <h2 className="px-6 py-4 text-lg font-semibold border-b border-gray-100">
            📊 角色-权限矩阵
          </h2>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="bg-gray-50">
                <tr>
                  <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase sticky left-0 bg-gray-50">Action</th>
                  {matrix.roles.map((r) => (
                    <th key={r.name} className="text-center px-3 py-3 text-xs font-semibold text-gray-500 uppercase">
                      <button
                        onClick={() => router.push(`/roles/${r.name}`)}
                        className="hover:text-blue-600 hover:underline"
                      >
                        {r.name}
                      </button>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {allActions.map((action) => (
                  <tr key={action} className="hover:bg-gray-50">
                    <td className="px-4 py-2.5 text-sm font-mono text-gray-700 sticky left-0 bg-white">{action}</td>
                    {matrix.roles.map((r) => (
                      <td key={r.name} className="text-center px-3 py-2.5">
                        {rolePermMap[r.name]?.has(action) ? (
                          <span className="text-green-600 font-bold">✅</span>
                        ) : (
                          <span className="text-gray-300">—</span>
                        )}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* 创建 Dialog */}
      {createOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="bg-white rounded-xl shadow-xl w-full max-w-md p-6">
            <h2 className="text-lg font-semibold text-gray-900 mb-4">创建自定义角色</h2>
            {createError && (
              <div className="mb-3 p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">{createError}</div>
            )}
            <div className="space-y-3">
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">角色名 *</label>
                <input type="text" value={createName} onChange={(e) => setCreateName(e.target.value)}
                  placeholder="custom_role" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm font-mono" />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">描述</label>
                <textarea value={createDesc} onChange={(e) => setCreateDesc(e.target.value)}
                  rows={2} placeholder="角色描述" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm" />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">父级 Keycloak 角色</label>
                <select value={createParentRole} onChange={(e) => setCreateParentRole(e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm">
                  <option value="user">user</option>
                  <option value="system_admin">system_admin</option>
                </select>
                <p className="text-xs text-gray-400 mt-1">决定哪些用户可以获得此角色</p>
              </div>
            </div>
            <div className="flex justify-end gap-3 mt-6">
              <button onClick={() => { setCreateOpen(false); setCreateError(""); }} className="px-4 py-2 text-sm text-gray-600 hover:bg-gray-100 rounded-lg">取消</button>
              <button onClick={handleCreate} disabled={creating}
                className="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 flex items-center gap-2">
                {creating && <Loader2 size={14} className="animate-spin" />} 创建
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
