/** 角色管理 — 三级角色作用域：平台级 | 身份角色 | 项目级。
 *
 * 平台模式：显示全部角色（平台级 + 身份角色 + 各项目派生角色）。
 * 项目模式：显示身份角色 + 该项目派生角色，且**权限按该项目作用域解析**。
 *
 * 语义（见 docs/permission_model_v2.md）：
 * - 身份角色（user / system_admin）是"入场资格"，在项目层不持有任何权限；
 * - 派生角色才是权限持有者，由 granted_actions 或资源 ACL 激活；
 * - 因此"父角色"一词改称"激活角色" —— 它是激活条件，不是权限继承。
 *   界面上不再出现"子角色继承父角色权限"的暗示。
 */

"use client";

import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { Shield, Plus, Loader2, Trash2, Lock, Globe, Box, KeyRound, FileLock2 } from "lucide-react";
import api from "@/lib/api";
import { useAuthStore } from "@/stores/useAuthStore";

interface RoleDef {
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

interface MatrixRole {
  name: string;
  kind: "derived" | "identity";
  activation: "identity" | "grant" | "acl";
  activated_by: string[];
  permissions: string[];
  conditional_permissions: string[];
  project_id: string;
}

interface PermMatrix {
  roles: MatrixRole[];
}

/** 判断角色所属的作用域等级。
 *
 * 身份角色（Keycloak 侧的入场资格）单独成组：它们既不是平台功能角色，
 * 也不属于任何项目，混进"平台级"会让人以为它们持有平台权限。
 */
function roleScope(r: RoleDef): "platform" | "identity" | "project" {
  if (r.project_id !== null) return "project";
  if (r.kind === "identity") return "identity";
  return "platform";
}

const SCOPE_LABELS: Record<string, string> = {
  platform: "平台级",
  identity: "身份角色",
  project: "项目级",
};

const SCOPE_HINTS: Record<string, string> = {
  platform: "所有项目可见",
  identity: "入场资格 · 本身不持项目权限",
  project: "本项目专属",
};

const SCOPE_COLORS: Record<string, string> = {
  platform: "text-orange-600 bg-orange-50 border-orange-200",
  identity: "text-gray-500 bg-gray-100 border-gray-200",
  project: "text-blue-600 bg-blue-50 border-blue-200",
};

/** 激活方式 → 展示文案。回答"这个角色什么时候生效"。 */
const ACTIVATION_LABELS: Record<string, string> = {
  identity: "持有身份角色即激活",
  grant: "需授权记录激活",
  acl: "资源 ACL 动态激活",
};

const ACTIVATION_COLORS: Record<string, string> = {
  identity: "text-gray-500 bg-gray-50 border-gray-200",
  grant: "text-emerald-600 bg-emerald-50 border-emerald-200",
  acl: "text-violet-600 bg-violet-50 border-violet-200",
};

export default function RolesPage() {
  const router = useRouter();
  const { currentProjectId, availableProjects } = useAuthStore();
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
  // 自定义角色必须归属某个项目：平台层按设计只有两个策略文件、不随项目增减，
  // 没有平台级自定义派生角色这一形态。平台模式下由此下拉框显式选目标项目。
  const [createProjectId, setCreateProjectId] = useState("");
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
      // 矩阵必须与角色列表用同一个作用域取数：此前矩阵固定拉全局，
      // 项目模式下角色卡显示的是"全平台并集"的权限数，与项目内实际可用的
      // 权限对不上（在 demo2 里看 user 显示 14 个权限，实际只有 6 个）。
      const [rolesRes, matrixRes] = await Promise.all([
        api.get("/api/v1/roles/definitions", { params }),
        api.get("/api/v1/roles/permissions", { params }),
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
    const targetProject = isPlatformMode ? createProjectId : currentProjectId;
    if (!targetProject) { setCreateError("请选择角色所属的项目"); return; }
    setCreating(true);
    try {
      await api.post("/api/v1/roles/definitions", {
        name: createName.trim(),
        description: createDesc.trim(),
        parent_keycloak_roles: [createParentRole],
        project_id: targetProject,
      });
      setCreateOpen(false);
      setCreateName("");
      setCreateDesc("");
      setCreateParentRole("user");
      setCreateProjectId("");
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
    const groups: Record<string, RoleDef[]> = { platform: [], identity: [], project: [] };
    for (const r of roles) {
      groups[roleScope(r)].push(r);
    }
    return groups;
  }, [roles]);

  // ── 权限矩阵 ──
  // 后端已按角色名去重（同一角色不再拆成 cerbos / keycloak 两个条目），
  // 这里直接建映射即可，不会出现后写覆盖前写的重复列。
  const allActions = matrix
    ? Array.from(new Set(matrix.roles.flatMap(r => r.permissions))).sort()
    : [];

  const rolePermMap: Record<string, Set<string>> = {};
  const roleCondMap: Record<string, Set<string>> = {};
  if (matrix) {
    for (const r of matrix.roles) {
      rolePermMap[r.name] = new Set(r.permissions);
      roleCondMap[r.name] = new Set(r.conditional_permissions);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16 text-gray-400">
        <Loader2 size={24} className="animate-spin mr-2" /> 加载中...
      </div>
    );
  }

  const scopeOrder: Array<"platform" | "identity" | "project"> = ["platform", "identity", "project"];

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
            setCreateProjectId(isPlatformMode ? "" : (currentProjectId ?? ""));
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
              {scope === "identity" && <Shield size={16} className="text-gray-400" />}
              {scope === "project" && <Box size={16} className="text-blue-500" />}
              <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wide">
                {SCOPE_LABELS[scope]} ({groupRoles.length})
              </h2>
              <span className={`text-[10px] px-1.5 py-0.5 rounded border ${SCOPE_COLORS[scope]}`}>
                {SCOPE_HINTS[scope]}
              </span>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {groupRoles.map((r) => {
                // 权限计数取自 /definitions（与矩阵同一作用域、同一份策略解析），
                // 不再从全局矩阵取数 —— 那是"卡片显示 14、详情显示 4"的来源。
                const permCount = r.permissions.length;
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
                          {/* 激活方式：回答"这个角色什么时候生效" */}
                          <span
                            className={`flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full border ${ACTIVATION_COLORS[r.activation]}`}
                            title={ACTIVATION_LABELS[r.activation]}
                          >
                            {r.activation === "acl" ? <FileLock2 size={10} /> : <KeyRound size={10} />}
                            {ACTIVATION_LABELS[r.activation]}
                          </span>
                          {!r.policy_synced && (
                            <span className="text-[10px] text-amber-600 bg-amber-50 border border-amber-200 px-1.5 py-0.5 rounded-full">
                              策略中无此角色
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
                      <span title="激活该角色的身份角色，不是权限来源">
                        激活角色: {(r.activated_by?.length ? r.activated_by : r.parent_keycloak_roles)?.join(", ") || "无"}
                      </span>
                      <span>{r.binding_count} 个绑定</span>
                      <span>
                        {permCount} 个权限
                        {r.kind === "identity" && permCount === 0 && "（入场资格，不持权）"}
                      </span>
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
          <div className="px-6 py-4 border-b border-gray-100">
            <h2 className="text-lg font-semibold">📊 角色-权限矩阵</h2>
            <p className="text-xs text-gray-400 mt-1">
              作用域：{isPlatformMode ? "全平台" : `项目 ${currentProjectId}`} ·
              ✅ 策略直授 · 🔑 需授权记录激活 · 🔒 资源 ACL 动态授予
            </p>
          </div>
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
                      <div className="text-[9px] font-normal normal-case text-gray-400 mt-0.5">
                        {r.kind === "identity" ? "身份角色" : "派生角色"}
                      </div>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {allActions.map((action) => (
                  <tr key={action} className="hover:bg-gray-50">
                    <td className="px-4 py-2.5 text-sm font-mono text-gray-700 sticky left-0 bg-white">{action}</td>
                    {matrix.roles.map((r) => {
                      if (!rolePermMap[r.name]?.has(action)) {
                        return <td key={r.name} className="text-center px-3 py-2.5"><span className="text-gray-300">—</span></td>;
                      }
                      // 区分"策略直授"与"要有授权记录 / ACL 才生效"：
                      // 一律画 ✅ 会让 user 看起来天生拥有全部权限。
                      const conditional = roleCondMap[r.name]?.has(action);
                      const acl = r.activation === "acl";
                      return (
                        <td key={r.name} className="text-center px-3 py-2.5">
                          <span
                            className="font-bold"
                            title={acl ? "由资源实例上的 ACL 动态授予" : conditional || r.activation === "grant" ? "需要对应的授权记录才生效" : "策略直接授予"}
                          >
                            {acl ? "🔒" : conditional || r.activation === "grant" ? "🔑" : "✅"}
                          </span>
                        </td>
                      );
                    })}
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
                <label className="block text-xs font-medium text-gray-600 mb-1">所属项目 *</label>
                {isPlatformMode ? (
                  <select
                    value={createProjectId}
                    onChange={(e) => setCreateProjectId(e.target.value)}
                    className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
                  >
                    <option value="">— 请选择项目 —</option>
                    {availableProjects.map((p) => (
                      <option key={p.id} value={p.id}>{p.name}（{p.id}）</option>
                    ))}
                  </select>
                ) : (
                  <div className="px-3 py-2 bg-blue-50 border border-blue-200 rounded-lg text-sm text-blue-700">
                    {currentProjectId}
                  </div>
                )}
                <p className="text-xs text-gray-400 mt-1">
                  自定义角色必须归属一个项目：平台层只有 platform.yaml 与
                  project_permission.yaml 两个文件、不随项目增减。平台权限请通过
                  平台角色绑定或按功能的授权记录下发。
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
                <label className="block text-xs font-medium text-gray-600 mb-1">激活角色（入场资格）</label>
                <select value={createParentRole} onChange={(e) => setCreateParentRole(e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm">
                  {["user", "system_admin", "platform_admin"].map(r => (
                    <option key={r} value={r}>{r}</option>
                  ))}
                </select>
                <p className="text-xs text-gray-400 mt-1">
                  决定哪些用户**有资格**被激活为此角色。新角色不会继承它的任何权限 ——
                  权限来自你在策略中为此角色声明的动作。
                </p>
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
