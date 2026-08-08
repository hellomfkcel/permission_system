/** 角色管理 — 三级角色作用域：平台级 | 基础内置 | 项目级。

平台模式：显示全部角色（平台级 + 基础内置 + 各项目自定义）。
项目模式：显示基础内置角色 + 该项目自定义角色（不显示其他项目的角色）。
*/

"use client";

import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { Shield, Plus, Loader2, Trash2, Lock, Globe, Box } from "lucide-react";
import api from "@/lib/api";
import { useAuthStore } from "@/stores/useAuthStore";

interface RoleDef {
  id: string;
  name: string;
  description: string;
  parent_keycloak_roles: string[];
  is_system: boolean;
  is_keycloak_role: boolean;
  permissions: string[];
  binding_count: number;
  project_id: string | null;
  created_at: string;
}

interface PermMatrix {
  roles: {
    name: string;
    parent_keycloak_roles: string[];
    permissions: string[];
  }[];
}

/** 内置角色：仅 Keycloak 身份角色 + 超级管理员。
 *
 * 设计依据：kb_admin/kb_writer/kb_reader 是 RAG 项目 Cerbos 派生角色，
 * 已归属到 project_id='rag-v14'（项目级），不再是全局内置角色。
 */
const BUILT_IN_ROLES = new Set(["system_admin", "user", "admin"]);

/** 判断角色所属的作用域等级 */
function roleScope(r: RoleDef): "platform" | "base" | "project" {
  if (r.project_id !== null) return "project";
  if (r.name.startsWith("platform_")) return "platform";
  if (BUILT_IN_ROLES.has(r.name)) return "base";
  // 兜底：project_id=NULL 但不属于内置/平台 → 视为平台级
  return "platform";
}

const SCOPE_LABELS: Record<string, string> = {
  platform: "平台级",
  base: "内置",
  project: "项目级",
};

const SCOPE_COLORS: Record<string, string> = {
  platform: "text-orange-600 bg-orange-50 border-orange-200",
  base: "text-gray-500 bg-gray-100 border-gray-200",
  project: "text-blue-600 bg-blue-50 border-blue-200",
};

export default function RolesPage() {
  const router = useRouter();
  const { currentProjectId } = useAuthStore();
  const isPlatformMode = !currentProjectId || currentProjectId === "__all__";

  const [roles, setRoles] = useState<RoleDef[]>([]);
  const [matrix, setMatrix] = useState<PermMatrix | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // 创建 Dialog — 项目模式下只能创建项目级角色
  const [createOpen, setCreateOpen] = useState(false);
  const [createName, setCreateName] = useState("");
  const [createDesc, setCreateDesc] = useState("");
  const [createParentRole, setCreateParentRole] = useState("user");
  const [createScope, setCreateScope] = useState<"platform" | "project">(
    isPlatformMode ? "platform" : "project"
  );
  const [createError, setCreateError] = useState("");
  const [creating, setCreating] = useState(false);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const params: Record<string, string> = {};
      if (!isPlatformMode) {
        params.project_id = currentProjectId;
      }
      const [rolesRes, matrixRes] = await Promise.all([
        api.get("/api/v1/roles/definitions", { params }),
        api.get("/api/v1/roles/permissions"),
      ]);
      setRoles(rolesRes.data);
      setMatrix(matrixRes.data);
    } catch {
      setError("加载角色数据失败");
    } finally {
      setLoading(false);
    }
  }, [currentProjectId]);

  useEffect(() => { loadData(); }, [loadData]);

  const handleCreate = async () => {
    setCreateError("");
    if (!createName.trim()) { setCreateError("角色名不能为空"); return; }
    setCreating(true);
    try {
      const body: Record<string, unknown> = {
        name: createName.trim(),
        description: createDesc.trim(),
        parent_keycloak_roles: [createParentRole],
      };
      // 平台模式 + 平台级 → 不传 project_id（默认 NULL）
      // 项目模式 → 始终传当前项目 ID
      if (!isPlatformMode) {
        body.project_id = currentProjectId;
      }
      await api.post("/api/v1/roles/definitions", body);
      setCreateOpen(false);
      setCreateName("");
      setCreateDesc("");
      setCreateParentRole("user");
      setCreateScope("platform");
      loadData();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      setCreateError(err.response?.data?.detail || "创建失败");
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (name: string) => {
    if (!confirm(`确认删除角色「${name}」？`)) return;
    try {
      await api.delete(`/api/v1/roles/definitions/${name}`);
      loadData();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      alert(err.response?.data?.detail || "删除失败");
    }
  };

  // ── 按作用域分组 ──
  const grouped = useCallback(() => {
    const groups: Record<string, RoleDef[]> = { platform: [], base: [], project: [] };
    for (const r of roles) {
      groups[roleScope(r)].push(r);
    }
    return groups;
  }, [roles]);

  // ── 权限矩阵 ──
  const allActions = matrix
    ? Array.from(new Set(matrix.roles.flatMap(r => r.permissions))).sort()
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

  const scopeOrder: Array<"platform" | "base" | "project"> = ["platform", "base", "project"];

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">🔑 角色管理</h1>
          <p className="text-sm text-gray-500 mt-1">
            {isPlatformMode
              ? "管理系统所有角色定义与权限映射"
              : "管理基础角色与该项目自定义角色"}
          </p>
        </div>
        <button
          onClick={() => {
            setCreateScope(isPlatformMode ? "platform" : "project");
            setCreateOpen(true);
          }}
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

      {/* 按作用域分组显示角色卡片 */}
      {scopeOrder.map((scope) => {
        const groupRoles = grouped()[scope];
        if (groupRoles.length === 0) return null;
        return (
          <div key={scope} className="mb-6">
            <div className="flex items-center gap-2 mb-3">
              {scope === "platform" && <Globe size={16} className="text-orange-500" />}
              {scope === "base" && <Shield size={16} className="text-gray-400" />}
              {scope === "project" && <Box size={16} className="text-blue-500" />}
              <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wide">
                {SCOPE_LABELS[scope]}角色 ({groupRoles.length})
              </h2>
              <span className={`text-[10px] px-1.5 py-0.5 rounded border ${SCOPE_COLORS[scope]}`}>
                {scope === "platform" ? "所有项目可见" : scope === "base" ? "全局内置" : "本项目专属"}
              </span>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {groupRoles.map((r) => {
                const perms = rolePermMap[r.name] || new Set();
                const scopeBadge = roleScope(r);
                return (
                  <div
                    key={r.id}
                    className="bg-white rounded-lg border border-gray-200 p-4 hover:shadow-md transition-shadow cursor-pointer"
                    onClick={() => router.push(`/roles/${r.name}`)}
                  >
                    <div className="flex items-start justify-between">
                      <div>
                        <div className="flex items-center gap-2">
                          <h3 className="text-base font-semibold text-gray-900">{r.name}</h3>
                          {r.is_keycloak_role && (
                            <span className="flex items-center gap-1 text-[10px] text-purple-500 bg-purple-50 px-1.5 py-0.5 rounded-full">
                              <Lock size={10} /> Keycloak
                            </span>
                          )}
                          {r.is_system && !r.is_keycloak_role && (
                            <span className="flex items-center gap-1 text-[10px] text-gray-400 bg-gray-100 px-1.5 py-0.5 rounded-full">
                              <Lock size={10} /> 系统内置
                            </span>
                          )}
                          {/* 作用域标签 */}
                          <span className={`text-[10px] px-1.5 py-0.5 rounded-full border ${SCOPE_COLORS[scopeBadge]}`}>
                            {SCOPE_LABELS[scopeBadge]}
                          </span>
                          {r.project_id && (
                            <span className="text-[10px] text-blue-500 bg-blue-50 px-1.5 py-0.5 rounded-full font-mono">
                              {r.project_id}
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
          </div>
        );
      })}

      {/* 权限矩阵表格 */}
      {matrix && allActions.length > 0 && (
        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden mt-6">
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
                <label className="block text-xs font-medium text-gray-600 mb-1">作用域</label>
                {isPlatformMode ? (
                  <select
                    value={createScope}
                    onChange={(e) => setCreateScope(e.target.value as "platform" | "project")}
                    className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
                  >
                    <option value="platform">平台级（所有项目可见）</option>
                  </select>
                ) : (
                  <div className="px-3 py-2 bg-blue-50 border border-blue-200 rounded-lg text-sm text-blue-700">
                    项目级 — 仅在「{currentProjectId}」项目中可见
                  </div>
                )}
                <p className="text-xs text-gray-400 mt-1">
                  {isPlatformMode
                    ? "平台级角色整个权限平台可见"
                    : "具体项目中只能创建该项目专属角色"}
                </p>
              </div>
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
                  {["user", "system_admin", "platform_admin"].map(r => (
                    <option key={r} value={r}>{r}</option>
                  ))}
                </select>
                <p className="text-xs text-gray-400 mt-1">决定哪些 Keycloak 用户可以继承此角色</p>
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
