/** 角色详情页 — 权限列表 + 已绑定用户/组。 */

"use client";

import { useEffect, useState, useCallback } from "react";
import { useParams, useRouter } from "next/navigation";
import { ArrowLeft, Loader2, Lock, Shield, X } from "lucide-react";
import api from "@/lib/api";
import { useAuthStore } from "@/stores/useAuthStore";

interface RoleDetail {
  id: string;
  name: string;
  description: string;
  parent_keycloak_roles: string[];
  activated_by: string[];
  kind: "derived" | "identity";
  activation: "identity" | "grant" | "acl";
  is_system: boolean;
  is_keycloak_role: boolean;
  permissions: string[];
  conditional_permissions: string[];
  binding_count: number;
  project_id: string | null;
  policy_synced: boolean;
  created_at: string;
}

const ACTIVATION_LABELS: Record<string, string> = {
  identity: "持有身份角色即激活",
  grant: "需要授权记录（granted_actions）激活",
  acl: "由资源实例上的 ACL 动态激活",
};

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
  const { currentProjectId } = useAuthStore();
  const isPlatformMode = !currentProjectId || currentProjectId === "__all__";

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
      // 带上当前项目作用域，权限口径与列表页 / 矩阵一致
      const roleParams: Record<string, string> = {};
      if (!isPlatformMode) roleParams.project_id = currentProjectId;
      const [roleRes, bindingsRes] = await Promise.all([
        api.get(`/api/v1/roles/definitions/${roleName}`, { params: roleParams }),
        api.get("/api/v1/roles/bindings", { params: { role: roleName } }),
      ]);
      setRole(roleRes.data);
      setBindings(bindingsRes.data.filter((b: RoleBindingInfo) => !b.revoked));
    } catch {
      setError("加载角色信息失败");
    } finally {
      setLoading(false);
    }
  }, [roleName, currentProjectId, isPlatformMode]);

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
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2 mt-4 text-sm text-gray-500">
          <span title="激活该角色的身份角色。这是激活条件，不是权限继承 —— 本角色不会获得它们的权限。">
            激活角色: <span className="font-mono text-gray-700">
              {(role.activated_by?.length ? role.activated_by : role.parent_keycloak_roles)?.join(", ") || "无"}
            </span>
          </span>
          <span>激活方式: <span className="text-gray-700">{ACTIVATION_LABELS[role.activation]}</span></span>
          <span>{role.binding_count} 个活跃绑定</span>
          <span>{role.permissions.length} 个权限</span>
        </div>
        <p className="mt-3 text-xs text-gray-400">
          权限作用域：{isPlatformMode ? "全平台（所有项目的策略并集）" : `项目 ${currentProjectId}`}
          {role.kind === "identity" && " · 身份角色只是入场资格，项目层的权限由派生角色持有"}
        </p>
      </div>

      {/* 权限列表 */}
      <div className="bg-white rounded-lg border border-gray-200 p-6 mb-6">
        <h2 className="text-lg font-semibold mb-1">📋 策略授予的权限</h2>
        <p className="text-xs text-gray-400 mb-4">
          取自 Cerbos 策略文件（唯一权威源）。🔑 标记的动作还需要对应的授权记录
          或资源 ACL 才会在判定期生效。
        </p>
        {role.permissions.length === 0 ? (
          <p className="text-sm text-gray-400">
            {role.kind === "identity"
              ? "此角色是入场资格，本身不持有任何权限 —— 权限由它激活的派生角色持有。"
              : "此角色在当前作用域内没有任何权限。"}
          </p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {role.permissions.map((p) => {
              const conditional =
                role.conditional_permissions?.includes(p) || role.activation !== "identity";
              return (
                <span
                  key={p}
                  className={`px-3 py-1.5 rounded-lg text-sm font-mono border ${
                    conditional
                      ? "bg-emerald-50 text-emerald-700 border-emerald-200"
                      : "bg-green-50 text-green-700 border-green-200"
                  }`}
                  title={conditional ? "需要授权记录 / 资源 ACL 才生效" : "策略直接授予"}
                >
                  {conditional ? "🔑 " : ""}{p}
                </span>
              );
            })}
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
