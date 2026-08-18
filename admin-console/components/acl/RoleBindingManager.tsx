/**
 * RoleBindingManager — 角色绑定管理组件。
 */

"use client";

import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { useAuthStore } from "@/stores/useAuthStore";
import { useToast, showConfirm } from "@/components/shared/Toast";
import { useResourceNames } from "@/lib/useResourceNames";

interface RoleBinding {
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

interface PrincipalOption {
  value: string;
  label: string;
  type: "user" | "group" | "role";
}

export default function RoleBindingManager() {
  const { user, currentProjectId } = useAuthStore();
  const { showToast } = useToast();
  const { formatResource } = useResourceNames();

  // ── 状态 ──
  const [bindings, setBindings] = useState<RoleBinding[]>([]);
  const [availableRoles, setAvailableRoles] = useState<{value: string; label: string; desc: string}[]>([]);
  const [loading, setLoading] = useState(true);
  const [showBindForm, setShowBindForm] = useState(false);

  // 绑定表单
  const [bindPrincipal, setBindPrincipal] = useState("");
  const [bindPrincipalSearch, setBindPrincipalSearch] = useState("");
  const [bindRole, setBindRole] = useState("");
  const [bindResourceType, setBindResourceType] = useState("");
  const [bindResourceId, setBindResourceId] = useState("");
  const [bindSubmitting, setBindSubmitting] = useState(false);
  // 动态资源类型列表
  const [availableResourceTypes, setAvailableResourceTypes] = useState<string[]>([]);

  // 筛选
  const [filterPrincipal, setFilterPrincipal] = useState("");
  const [filterRole, setFilterRole] = useState("");

  // 主体建议列表
  const [principalOptions, setPrincipalOptions] = useState<PrincipalOption[]>([]);
  const [filteredPrincipals, setFilteredPrincipals] = useState<PrincipalOption[]>([]);

  // ── 加载可用资源类型（合并 resource_registry + Cerbos YAML）──
  const loadResourceTypes = useCallback(async () => {
    try {
      const [res, configRes] = await Promise.all([
        api.get("/api/v1/resources"),
        api.get("/api/v1/auth/config"),
      ]);
      const types = new Set((res.data as Array<{resource_type: string}>).map(r => r.resource_type));
      // 合并 Cerbos YAML 中定义的资源类型（如 oa_leave_request）
      const resourceActions: Record<string, string[]> = configRes.data.resource_actions || {};
      for (const rt of Object.keys(resourceActions)) {
        types.add(rt);
      }
      setAvailableResourceTypes(Array.from(types).sort());
    } catch {
      setAvailableResourceTypes(["kb", "document", "platform"]);
    }
  }, []);

  // ── 加载角色定义（按项目范围）──
  const loadRoles = useCallback(async () => {
    try {
      const res = await api.get("/api/v1/roles/definitions");
      const defs = res.data as Array<{name: string; description: string; project_id: string | null}>;
      const roles = defs.map(d => ({
        value: d.name,
        label: d.name,
        desc: d.description || "",
      }));
      setAvailableRoles(roles);
      if (roles.length > 0 && !roles.find(r => r.value === bindRole)) {
        setBindRole(roles[0].value);
      }
    } catch {
      // keep defaults
    }
  }, [bindRole]);

  // ── 加载绑定 ──
  const loadBindings = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.get<RoleBinding[]>("/api/v1/roles/bindings");
      setBindings(res.data.filter(b => !b.revoked));
    } catch {
      setBindings([]);
    }
    setLoading(false);
  }, []);

  const loadPrincipals = useCallback(async () => {
    const opts: PrincipalOption[] = [];
    try {
      const usersRes = await api.get("/api/v1/auth/users");
      const users = usersRes.data as Array<{
        user_id: string; groups: string[]; roles: string[];
      }>;
      for (const u of users) {
        if (u.user_id) opts.push({ value: `user:${u.user_id}`, label: `🧑 ${u.user_id}`, type: "user" });
        for (const g of u.groups || []) {
          if (g && !opts.find(o => o.value === `group:${g}`)) {
            opts.push({ value: `group:${g}`, label: `👥 ${g}`, type: "group" });
          }
        }
      }

      // 从 bindings 补充
      for (const b of bindings) {
        if (b.principal && !opts.find(o => o.value === b.principal)) {
          const prefix = b.principal.split(":")[0];
          const icon = prefix === "user" ? "🧑" : prefix === "group" ? "👥" : "🎭";
          opts.push({ value: b.principal, label: `${icon} ${b.principal}`, type: prefix as "user" | "group" | "role" });
        }
      }
    } catch { /* ignore */ }
    setPrincipalOptions(opts);
  }, [bindings]);

  useEffect(() => { loadResourceTypes(); }, [loadResourceTypes, currentProjectId]);
  useEffect(() => { loadRoles(); }, [loadRoles, currentProjectId]);
  useEffect(() => { loadBindings(); }, [loadBindings, currentProjectId]);
  useEffect(() => { if (bindings.length > 0) loadPrincipals(); }, [bindings, loadPrincipals]);

  // ── 主体过滤 ──
  useEffect(() => {
    const q = bindPrincipalSearch.toLowerCase();
    setFilteredPrincipals(
      principalOptions.filter(o => o.label.toLowerCase().includes(q) || o.value.toLowerCase().includes(q)).slice(0, 10)
    );
  }, [bindPrincipalSearch, principalOptions]);

  // ── 绑定角色 ──
  const handleBind = async () => {
    if (!bindPrincipal.trim()) return showToast("error", "请输入或选择主体");
    setBindSubmitting(true);
    try {
      const pid = currentProjectId && currentProjectId !== "__all__" ? currentProjectId : "rag-v14";
      await api.post("/api/v1/roles/bind", {
        tenant_id: user?.tenant_id || "tenant-dev",
        principal: bindPrincipal.trim(),
        role: bindRole,
        resource_type: bindResourceType.trim() || null,
        resource_id: bindResourceId.trim() || null,
        granted_by: `user:${user?.user_id || "admin"}`,
        project_id: pid,
      });
      showToast("success", `已绑定角色 ${bindRole} 到 ${bindPrincipal}`);
      setShowBindForm(false);
      setBindPrincipal("");
      setBindRole(availableRoles[0]?.value || "");
      setBindResourceType("");
      setBindResourceId("");
      loadBindings();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      showToast("error", err?.response?.data?.detail || "角色绑定失败");
    } finally {
      setBindSubmitting(false);
    }
  };

  // ── 解除绑定 ──
  const handleUnbind = async (b: RoleBinding) => {
    if (!showConfirm(`确认解除 ${b.principal} 的 ${b.role} 角色？`)) return;
    try {
      await api.post("/api/v1/roles/unbind", {
        principal: b.principal,
        role: b.role,
        resource_type: b.resource_type,
        resource_id: b.resource_id,
      });
      showToast("success", `已解除 ${b.principal} 的 ${b.role}`);
      loadBindings();
    } catch {
      showToast("error", "解除绑定失败");
    }
  };

  // ── 筛选 ──
  const filteredBindings = bindings.filter(b => {
    if (filterRole && b.role !== filterRole) return false;
    if (filterPrincipal && !b.principal.toLowerCase().includes(filterPrincipal.toLowerCase())) return false;
    return true;
  });

  // ── 统计 ──
  const roleStats = availableRoles.map(r => ({
    ...r,
    count: bindings.filter(b => b.role === r.value).length,
  }));

  return (
    <div>
      {/* 统计卡片 */}
      <div className="grid grid-cols-4 gap-3 mb-5">
        {roleStats.map(r => (
          <div key={r.value} className="bg-white rounded-lg border border-gray-200 p-3 text-center">
            <div className="text-2xl font-bold text-blue-600">{r.count}</div>
            <div className="text-xs text-gray-500 mt-1">{r.label}</div>
          </div>
        ))}
      </div>

      {/* 操作栏 */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex gap-2">
          <input
            type="text"
            value={filterPrincipal}
            onChange={e => setFilterPrincipal(e.target.value)}
            placeholder="搜索主体..."
            className="border border-gray-300 rounded-lg px-3 py-2 text-sm w-48"
          />
          <select
            value={filterRole}
            onChange={e => setFilterRole(e.target.value)}
            className="border border-gray-300 rounded-lg px-3 py-2 text-sm"
          >
            <option value="">全部角色</option>
            {availableRoles.map(r => <option key={r.value} value={r.value}>{r.label}</option>)}
          </select>
        </div>
        <button
          onClick={() => setShowBindForm(!showBindForm)}
          className="bg-purple-600 text-white px-4 py-2 rounded-lg hover:bg-purple-700 text-sm font-medium"
        >
          + 绑定角色
        </button>
      </div>

      {/* 绑定表单 */}
      {showBindForm && (
        <div className="bg-white rounded-lg border border-purple-200 shadow-sm p-5 mb-4">
          <h3 className="text-base font-semibold text-gray-900 mb-4">🎭 角色绑定</h3>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">主体</label>
              <input
                type="text"
                value={bindPrincipalSearch}
                onChange={e => setBindPrincipalSearch(e.target.value)}
                placeholder="搜索用户/组..."
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm mb-1"
              />
              <input
                type="text"
                value={bindPrincipal}
                onChange={e => setBindPrincipal(e.target.value)}
                placeholder="或直接输入 user:xxx / group:xxx"
                className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono text-gray-600"
              />
              {bindPrincipalSearch && filteredPrincipals.length > 0 && (
                <div className="mt-1 border rounded-lg max-h-32 overflow-y-auto bg-white shadow-sm">
                  {filteredPrincipals.map(p => (
                    <button
                      key={p.value}
                      onClick={() => { setBindPrincipal(p.value); setBindPrincipalSearch(""); }}
                      className="w-full text-left px-3 py-1.5 text-sm hover:bg-purple-50"
                    >
                      {p.label}
                    </button>
                  ))}
                </div>
              )}
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">角色</label>
              <select
                value={bindRole}
                onChange={e => setBindRole(e.target.value)}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
              >
                {availableRoles.map(r => <option key={r.value} value={r.value}>{r.label} ({r.value})</option>)}
              </select>
              <p className="text-xs text-gray-500 mt-1">{availableRoles.find(r => r.value === bindRole)?.desc}</p>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">资源类型（可选）</label>
              <select
                value={bindResourceType}
                onChange={e => setBindResourceType(e.target.value)}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
              >
                <option value="">不限（全局）</option>
                {availableResourceTypes.map(rt => (
                  <option key={rt} value={rt}>{rt}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">资源 ID（可选）</label>
              <input
                type="text"
                value={bindResourceId}
                onChange={e => setBindResourceId(e.target.value)}
                placeholder={bindResourceType ? `${bindResourceType}-uuid` : "选择资源类型后填写"}
                disabled={!bindResourceType}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm disabled:bg-gray-100"
              />
            </div>
          </div>
          <div className="flex gap-3 mt-4">
            <button
              onClick={handleBind}
              disabled={bindSubmitting}
              className="bg-purple-600 text-white px-5 py-2 rounded-lg hover:bg-purple-700 text-sm disabled:opacity-50"
            >
              {bindSubmitting ? "绑定中..." : "确认绑定"}
            </button>
            <button
              onClick={() => setShowBindForm(false)}
              className="bg-gray-200 text-gray-700 px-5 py-2 rounded-lg hover:bg-gray-300 text-sm"
            >
              取消
            </button>
          </div>
        </div>
      )}

      {/* 绑定列表 */}
      {loading ? (
        <div className="text-center py-12 text-gray-400">加载中...</div>
      ) : filteredBindings.length === 0 ? (
        <div className="text-center py-12 bg-white rounded-lg border border-gray-200">
          <p className="text-gray-400 text-lg mb-2">🎭</p>
          <p className="text-gray-500">暂无角色绑定</p>
          <p className="text-gray-400 text-sm mt-1">点击&ldquo;绑定角色&rdquo;开始管理</p>
        </div>
      ) : (
        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          <table className="w-full">
            <thead className="bg-gray-50 border-b">
              <tr>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">主体</th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">角色</th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">资源范围</th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">授予者</th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">时间</th>
                <th className="text-right px-4 py-3 text-xs font-semibold text-gray-500 uppercase">操作</th>
              </tr>
            </thead>
            <tbody>
              {filteredBindings.map(b => (
                <tr key={b.id} className="border-t hover:bg-gray-50 transition-colors">
                  <td className="px-4 py-3">
                    <span className="text-sm font-mono text-gray-800">{b.principal}</span>
                  </td>
                  <td className="px-4 py-3">
                    <span className="inline-flex items-center gap-1 bg-purple-100 text-purple-800 px-2.5 py-1 rounded-full text-xs font-medium">
                      🎭 {b.role}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-sm text-gray-500 max-w-[180px] truncate"
                      title={b.resource_type && b.resource_id ? `${b.resource_type}:${b.resource_id}` : ""}>
                    {b.resource_type && b.resource_id
                      ? formatResource(b.resource_type, b.resource_id)
                      : <span className="text-gray-300">全局</span>}
                  </td>
                  <td className="px-4 py-3 text-sm text-gray-500">{b.granted_by}</td>
                  <td className="px-4 py-3 text-sm text-gray-400">
                    {new Date(b.granted_at).toLocaleDateString()}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => handleUnbind(b)}
                      className="text-red-600 hover:text-red-800 text-sm font-medium hover:underline"
                    >
                      解除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* 底部信息 */}
      <div className="mt-3 text-xs text-gray-400">
        共 {filteredBindings.length} 条绑定
        {filteredBindings.length !== bindings.length && `（已筛选，总计 ${bindings.length} 条）`}
      </div>
    </div>
  );
}
