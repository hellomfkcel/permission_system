/**
 * PermissionGrantDialog — 权限授予 Dialog 组件。
 *
 * 设计依据：docs/外部系统设计.md §3.4.1 权限授予 Dialog
 *          + docs/权限管理系统架构设计.md §2.1 动词目录（16 个 action）
 *
 * 支持：主体搜索、资源搜索、多 action 勾选、过期时间、批量授予。
 */

"use client";

import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { useAuthStore } from "@/stores/useAuthStore";
import { useToast } from "@/components/shared/Toast";
import {
  ACTIONS_BY_RESOURCE,
  ACTION_LABELS,
  type Action,
} from "@/lib/constants";

// ── 类型定义 ──

interface Resource {
  id: string;
  resource_type: string;
  resource_id: string;
  tenant_id: string;
  owner: string;
  retired: boolean;
}

interface PrincipalOption {
  value: string;       // e.g. "user:alice"
  label: string;       // e.g. "🧑 alice (user:alice)"
  type: "user" | "group" | "role";
}

// ── 动词按资源类型分组 ──
// 权威源: lib/constants.ts（单一来源，与设计文档 §2.1 对齐）
const ACTIONS_BY_RESOURCE_TYPE: Record<string, { value: string; label: string; desc: string }[]> = {
  kb: ACTIONS_BY_RESOURCE.kb.map((a: Action) => ({ value: a, label: a, desc: ACTION_LABELS[a] })),
  document: ACTIONS_BY_RESOURCE.document.map((a: Action) => ({ value: a, label: a, desc: ACTION_LABELS[a] })),
};

interface PermissionGrantDialogProps {
  open: boolean;
  onClose: () => void;
  onGranted: () => void;  // 授予成功后的回调（刷新列表）
  /** 预填资源信息（从资源详情页打开时传入） */
  defaultResourceType?: string;
  defaultResourceId?: string;
}

export default function PermissionGrantDialog({
  open,
  onClose,
  onGranted,
  defaultResourceType,
  defaultResourceId,
}: PermissionGrantDialogProps) {
  const { user } = useAuthStore();
  const { showToast } = useToast();

  // ── 表单状态 ──
  const [principalType, setPrincipalType] = useState<"user" | "group" | "role">("user");
  const [principalSearch, setPrincipalSearch] = useState("");
  const [principal, setPrincipal] = useState("");
  const [resourceType, setResourceType] = useState<"kb" | "document">(
    (defaultResourceType as "kb" | "document") || "kb"
  );
  const [resourceSearch, setResourceSearch] = useState("");
  const [resourceId, setResourceId] = useState(defaultResourceId || "");
  const [selectedActions, setSelectedActions] = useState<string[]>([]);
  const [expiresIn, setExpiresIn] = useState("never");
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);

  // ── 数据列表 ──
  const [principalOptions, setPrincipalOptions] = useState<PrincipalOption[]>([]);
  const [filteredPrincipals, setFilteredPrincipals] = useState<PrincipalOption[]>([]);
  const [resources, setResources] = useState<Resource[]>([]);
  const [filteredResources, setFilteredResources] = useState<Resource[]>([]);
  const [loadingPrincipals, setLoadingPrincipals] = useState(false);
  const [loadingResources, setLoadingResources] = useState(false);

  // ── 加载主体列表 ──
  const loadPrincipals = useCallback(async () => {
    setLoadingPrincipals(true);
    const opts: PrincipalOption[] = [];
    try {
      // 从 user_cache 加载用户
      const usersRes = await api.get("/api/v1/auth/users");
      const users = usersRes.data as Array<{
        user_id: string; tenant_id: string; roles: string[]; groups: string[]; principals: string[];
      }>;
      for (const u of users) {
        if (u.user_id) {
          opts.push({ value: `user:${u.user_id}`, label: `🧑 ${u.user_id}`, type: "user" });
        }
        for (const g of u.groups || []) {
          if (g && !opts.find(o => o.value === `group:${g}`)) {
            opts.push({ value: `group:${g}`, label: `👥 ${g}`, type: "group" });
          }
        }
        for (const r of u.roles || []) {
          if (r && !opts.find(o => o.value === `role:${r}`)) {
            opts.push({ value: `role:${r}`, label: `🎭 ${r}`, type: "role" });
          }
        }
      }

      // 补充从 role_bindings 获取的主体
      const bindingsRes = await api.get("/api/v1/roles/bindings");
      const bindings = bindingsRes.data as Array<{ principal: string }>;
      for (const b of bindings) {
        if (b.principal && !opts.find(o => o.value === b.principal)) {
          const prefix = b.principal.split(":")[0];
          const icon = prefix === "user" ? "🧑" : prefix === "group" ? "👥" : "🎭";
          opts.push({ value: b.principal, label: `${icon} ${b.principal}`, type: prefix as "user" | "group" | "role" });
        }
      }
    } catch {
      // 加载失败时使用空列表
    }
    setPrincipalOptions(opts);
    setLoadingPrincipals(false);
  }, []);

  // ── 加载资源列表 ──
  const loadResources = useCallback(async () => {
    setLoadingResources(true);
    try {
      const res = await api.get<Resource[]>(`/api/v1/resources?type=${resourceType}`);
      setResources(res.data.filter((r: Resource) => !r.retired));
    } catch {
      setResources([]);
    }
    setLoadingResources(false);
  }, [resourceType]);

  // ── 初始化 ──
  useEffect(() => {
    if (open) {
      loadPrincipals();
      loadResources();
      setPrincipal(defaultResourceId ? `user:${user?.user_id || ""}` : "");
      setResourceId(defaultResourceId || "");
      setSelectedActions([]);
      setExpiresIn("never");
      setNotes("");
      setSubmitting(false);
    }
  }, [open, loadPrincipals, loadResources, defaultResourceId, user]);

  // ── 主体过滤 ──
  useEffect(() => {
    const q = principalSearch.toLowerCase();
    const filtered = principalOptions.filter(
      o =>
        o.type === principalType &&
        (o.value.toLowerCase().includes(q) || o.label.toLowerCase().includes(q))
    );
    setFilteredPrincipals(filtered.slice(0, 15));
  }, [principalSearch, principalType, principalOptions]);

  // ── 资源过滤 ──
  useEffect(() => {
    const q = resourceSearch.toLowerCase();
    setFilteredResources(
      resources
        .filter(r => r.resource_id.toLowerCase().includes(q) || r.owner.toLowerCase().includes(q))
        .slice(0, 15)
    );
  }, [resourceSearch, resources]);

  // ── 资源类型切换时重新加载 ──
  useEffect(() => {
    if (open) loadResources();
  }, [resourceType, open, loadResources]);

  // ── Action 勾选切换 ──
  const toggleAction = (action: string) => {
    setSelectedActions(prev =>
      prev.includes(action) ? prev.filter(a => a !== action) : [...prev, action]
    );
  };

  // ── 提交授予 ──
  const handleSubmit = async () => {
    if (!principal.trim()) return showToast("error", "请选择主体");
    if (!resourceId.trim()) return showToast("error", "请选择资源");
    if (selectedActions.length === 0) return showToast("error", "请至少选择一个权限");

    setSubmitting(true);
    const tenantId = user?.tenant_id || "tenant-dev";
    const grantedBy = principal.startsWith("user:") ? principal : `user:${user?.user_id || "admin"}`;

    try {
      if (selectedActions.length === 1) {
        // 单条授予
        await api.post("/api/v1/acl/grant", {
          tenant_id: tenantId,
          principal: principal,
          resource_type: resourceType,
          resource_id: resourceId,
          action: selectedActions[0],
          granted_by: grantedBy,
          expires_at: expiresIn === "never" ? null : new Date(Date.now() + parseInt(expiresIn) * 1000).toISOString(),
        });
      } else {
        // 批量授予
        await api.post("/api/v1/acl/batch-grant", {
          grants: selectedActions.map(action => ({
            tenant_id: tenantId,
            principal: principal,
            resource_type: resourceType,
            resource_id: resourceId,
            action: action,
            granted_by: grantedBy,
            expires_at: expiresIn === "never" ? null : new Date(Date.now() + parseInt(expiresIn) * 1000).toISOString(),
          })),
        });
      }

      showToast("success", `成功授予 ${selectedActions.length} 项权限`);
      onGranted();
      onClose();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      showToast("error", err?.response?.data?.detail || "授予失败，请确认权限服务后端正常运行");
    } finally {
      setSubmitting(false);
    }
  };

  const availableActions = ACTIONS_BY_RESOURCE_TYPE[resourceType] || [];

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-2xl max-h-[85vh] overflow-y-auto mx-4">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b">
          <h2 className="text-lg font-bold text-gray-900">授予权限</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none">&times;</button>
        </div>

        {/* Body */}
        <div className="px-6 py-4 space-y-5">
          {/* ── 主体选择 ── */}
          <div>
            <label className="block text-sm font-semibold text-gray-700 mb-2">主体</label>
            <div className="flex gap-2 mb-2">
              {(["user", "group", "role"] as const).map(t => (
                <button
                  key={t}
                  onClick={() => { setPrincipalType(t); setPrincipalSearch(""); }}
                  className={`px-3 py-1 rounded-full text-xs font-medium transition-colors ${
                    principalType === t
                      ? "bg-blue-600 text-white"
                      : "bg-gray-100 text-gray-600 hover:bg-gray-200"
                  }`}
                >
                  {t === "user" ? "🧑 用户" : t === "group" ? "👥 组" : "🎭 角色"}
                </button>
              ))}
            </div>
            <input
              type="text"
              value={principalSearch}
              onChange={e => setPrincipalSearch(e.target.value)}
              placeholder={`搜索${principalType === "user" ? "用户" : principalType === "group" ? "组" : "角色"}...`}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              disabled={loadingPrincipals}
            />
            {/* 手动输入 */}
            <input
              type="text"
              value={principal}
              onChange={e => setPrincipal(e.target.value)}
              placeholder={`或直接输入: ${principalType}:xxx`}
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm mt-1 font-mono text-gray-600"
            />
            {/* 建议列表 */}
            {principalSearch && filteredPrincipals.length > 0 && (
              <div className="mt-1 border border-gray-200 rounded-lg max-h-40 overflow-y-auto bg-white shadow-sm">
                {filteredPrincipals.map(p => (
                  <button
                    key={p.value}
                    onClick={() => { setPrincipal(p.value); setPrincipalSearch(""); }}
                    className="w-full text-left px-3 py-2 text-sm hover:bg-blue-50 flex items-center gap-2"
                  >
                    <span>{p.label}</span>
                    <span className="text-gray-400 text-xs font-mono ml-auto">{p.value}</span>
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* ── 资源选择 ── */}
          <div>
            <label className="block text-sm font-semibold text-gray-700 mb-2">资源</label>
            <div className="flex gap-2 mb-2">
              <button
                onClick={() => { setResourceType("kb"); setResourceId(""); }}
                className={`px-3 py-1 rounded-full text-xs font-medium transition-colors ${
                  resourceType === "kb" ? "bg-green-600 text-white" : "bg-gray-100 text-gray-600 hover:bg-gray-200"
                }`}
              >
                📚 知识库
              </button>
              <button
                onClick={() => { setResourceType("document"); setResourceId(""); }}
                className={`px-3 py-1 rounded-full text-xs font-medium transition-colors ${
                  resourceType === "document" ? "bg-green-600 text-white" : "bg-gray-100 text-gray-600 hover:bg-gray-200"
                }`}
              >
                📄 文档
              </button>
            </div>
            <input
              type="text"
              value={resourceSearch}
              onChange={e => setResourceSearch(e.target.value)}
              placeholder={`搜索${resourceType === "kb" ? "知识库" : "文档"}...`}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-green-500 focus:border-green-500"
              disabled={loadingResources}
            />
            <input
              type="text"
              value={resourceId}
              onChange={e => setResourceId(e.target.value)}
              placeholder={`或直接输入 ${resourceType} ID`}
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm mt-1 font-mono text-gray-600"
            />
            {resourceSearch && filteredResources.length > 0 && (
              <div className="mt-1 border border-gray-200 rounded-lg max-h-40 overflow-y-auto bg-white shadow-sm">
                {filteredResources.map(r => (
                  <button
                    key={r.resource_id}
                    onClick={() => { setResourceId(r.resource_id); setResourceSearch(""); }}
                    className="w-full text-left px-3 py-2 text-sm hover:bg-green-50 flex items-center justify-between"
                  >
                    <span>{r.resource_type === "kb" ? "📚" : "📄"} {r.resource_id}</span>
                    <span className="text-gray-400 text-xs">{r.owner}</span>
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* ── 权限勾选 ── */}
          <div>
            <label className="block text-sm font-semibold text-gray-700 mb-2">
              权限 <span className="text-red-500">*</span>
            </label>
            <div className="grid grid-cols-2 gap-2">
              {availableActions.map(action => (
                <label
                  key={action.value}
                  className={`flex items-start gap-2 p-3 rounded-lg border cursor-pointer transition-colors ${
                    selectedActions.includes(action.value)
                      ? "border-blue-400 bg-blue-50"
                      : "border-gray-200 hover:border-gray-300"
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={selectedActions.includes(action.value)}
                    onChange={() => toggleAction(action.value)}
                    className="mt-0.5 h-4 w-4 text-blue-600 rounded"
                  />
                  <div>
                    <span className="text-sm font-medium text-gray-800">{action.label}</span>
                    <p className="text-xs text-gray-500 mt-0.5">{action.desc}</p>
                  </div>
                </label>
              ))}
            </div>
          </div>

          {/* ── 过期时间 ── */}
          <div>
            <label className="block text-sm font-semibold text-gray-700 mb-2">过期时间</label>
            <select
              value={expiresIn}
              onChange={e => setExpiresIn(e.target.value)}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
            >
              <option value="never">永不过期</option>
              <option value="3600">1 小时后</option>
              <option value="86400">24 小时后</option>
              <option value="604800">7 天后</option>
              <option value="2592000">30 天后</option>
              <option value="31536000">1 年后</option>
            </select>
          </div>

          {/* ── 备注 ── */}
          <div>
            <label className="block text-sm font-semibold text-gray-700 mb-2">备注（可选）</label>
            <textarea
              value={notes}
              onChange={e => setNotes(e.target.value)}
              placeholder="可选备注..."
              rows={2}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
            />
          </div>
        </div>

        {/* Footer */}
        <div className="flex justify-end gap-3 px-6 py-4 border-t bg-gray-50 rounded-b-xl">
          <button
            onClick={onClose}
            className="px-5 py-2.5 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-50"
          >
            取消
          </button>
          <button
            onClick={handleSubmit}
            disabled={submitting}
            className="px-5 py-2.5 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {submitting ? "授予中..." : "授予权限"}
          </button>
        </div>
      </div>
    </div>
  );
}
