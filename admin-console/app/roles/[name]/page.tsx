/** 角色详情页 — 权限列表 + 已绑定用户/组。
 *
 * 设计依据：docs/manage_role_design.md §3.4 角色详情页。
 */

"use client";

import { useEffect, useState, useCallback } from "react";
import { useParams, useRouter } from "next/navigation";
import { ArrowLeft, Loader2, Lock, Shield, X } from "lucide-react";
import api from "@/lib/api";

interface RoleDetail {
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

interface RoleBindingInfo {
  id: string;
  tenant_id: string;
  principal: string;
  role: string;
  resource_type: string | null;
  resource_id: string | null;
  granted_by: string;
  granted_at: string;
  revoked: boolean;
}

export default function RoleDetailPage() {
  const params = useParams();
  const router = useRouter();
  const roleName = params.name as string;

  const [role, setRole] = useState<RoleDetail | null>(null);
  const [bindings, setBindings] = useState<RoleBindingInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // Unbind
  const [unbinding, setUnbinding] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [roleRes, bindingsRes] = await Promise.all([
        api.get(`/api/v1/roles/definitions/${roleName}`),
        api.get("/api/v1/roles/bindings", { params: { role: roleName } }),
      ]);
      setRole(roleRes.data);
      setBindings(bindingsRes.data.filter((b: RoleBindingInfo) => !b.revoked));
    } catch {
      setError("加载角色信息失败");
    } finally {
      setLoading(false);
    }
  }, [roleName]);

  useEffect(() => { loadData(); }, [loadData]);

  const handleUnbind = async (binding: RoleBindingInfo) => {
    if (!confirm(`确认解除 ${binding.principal} 的 ${roleName} 绑定？`)) return;
    setUnbinding(binding.id);
    try {
      await api.post("/api/v1/roles/unbind", {
        principal: binding.principal,
        role: roleName,
        resource_type: binding.resource_type || undefined,
        resource_id: binding.resource_id || undefined,
      });
      loadData();
    } catch (e: any) {
      alert(e.response?.data?.detail || "解绑失败");
    } finally {
      setUnbinding(null);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16 text-gray-400">
        <Loader2 size={24} className="animate-spin mr-2" /> 加载中...
      </div>
    );
  }

  if (error || !role) {
    return (
      <div className="p-6">
        <button onClick={() => router.push("/roles")} className="flex items-center gap-2 text-sm text-blue-600 hover:underline mb-4">
          <ArrowLeft size={16} /> 返回角色列表
        </button>
        <div className="p-4 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">{error || "角色不存在"}</div>
      </div>
    );
  }

  return (
    <div className="p-6">
      <button onClick={() => router.push("/roles")} className="flex items-center gap-2 text-sm text-blue-600 hover:underline mb-4">
        <ArrowLeft size={16} /> 返回角色列表
      </button>

      {/* 角色信息卡片 */}
      <div className="bg-white rounded-lg border border-gray-200 p-6 mb-6">
        <div className="flex items-start justify-between">
          <div className="flex items-center gap-3">
            <Shield size={28} className="text-blue-500" />
            <div>
              <h1 className="text-2xl font-bold text-gray-900">{role.name}</h1>
              {role.is_system && (
                <span className="inline-flex items-center gap-1 mt-1 text-xs text-gray-400 bg-gray-100 px-2 py-0.5 rounded-full">
                  <Lock size={10} /> 系统内置角色（不可删除）
                </span>
              )}
              <p className="text-sm text-gray-500 mt-2">{role.description}</p>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-6 mt-4 text-sm text-gray-500">
          <span>父级 Keycloak 角色: <span className="font-mono text-gray-700">{role.parent_keycloak_roles?.join(", ") || "无"}</span></span>
          <span>{role.binding_count} 个活跃绑定</span>
          <span>{role.permissions.length} 个权限</span>
        </div>
      </div>

      {/* 权限列表 */}
      <div className="bg-white rounded-lg border border-gray-200 p-6 mb-6">
        <h2 className="text-lg font-semibold mb-4">📋 拥有的权限</h2>
        {role.permissions.length === 0 ? (
          <p className="text-sm text-gray-400">此角色暂无任何权限</p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {role.permissions.map((p) => (
              <span key={p} className="px-3 py-1.5 bg-green-50 text-green-700 border border-green-200 rounded-lg text-sm font-mono">
                {p}
              </span>
            ))}
          </div>
        )}
      </div>

      {/* 已绑定用户/组 */}
      <div className="bg-white rounded-lg border border-gray-200">
        <div className="flex items-center justify-between px-6 py-4 border-b">
          <h2 className="text-lg font-semibold">👥 已绑定用户/组 ({bindings.length})</h2>
        </div>
        {bindings.length === 0 ? (
          <div className="py-12 text-center text-sm text-gray-400">
            暂无绑定。前往 <button onClick={() => router.push("/permissions")} className="text-blue-500 hover:underline">权限管理</button> 添加角色绑定。
          </div>
        ) : (
          <table className="w-full">
            <thead className="bg-gray-50">
              <tr>
                <th className="text-left px-6 py-3 text-xs font-semibold text-gray-500 uppercase">主体</th>
                <th className="text-left px-6 py-3 text-xs font-semibold text-gray-500 uppercase">租户</th>
                <th className="text-left px-6 py-3 text-xs font-semibold text-gray-500 uppercase">资源范围</th>
                <th className="text-left px-6 py-3 text-xs font-semibold text-gray-500 uppercase">授予者</th>
                <th className="text-right px-6 py-3 text-xs font-semibold text-gray-500 uppercase">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {bindings.map((b) => (
                <tr key={b.id} className="hover:bg-gray-50">
                  <td className="px-6 py-3 text-sm font-mono font-medium">{b.principal}</td>
                  <td className="px-6 py-3 text-sm text-gray-600">{b.tenant_id || "-"}</td>
                  <td className="px-6 py-3 text-sm text-gray-600">
                    {b.resource_type ? `${b.resource_type}:${b.resource_id?.slice(0, 12)}...` : "全局"}
                  </td>
                  <td className="px-6 py-3 text-sm text-gray-500 font-mono">{b.granted_by}</td>
                  <td className="px-6 py-3 text-right">
                    <button
                      onClick={() => handleUnbind(b)}
                      disabled={unbinding === b.id}
                      className="p-1.5 text-gray-400 hover:text-red-500 hover:bg-red-50 rounded transition-colors"
                      title="解除绑定"
                    >
                      {unbinding === b.id ? <Loader2 size={14} className="animate-spin" /> : <X size={14} />}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
