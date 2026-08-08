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

// ── 资源类型标签（从后台 /auth/config 动态获取，此处仅作 SSR 兜底）──
const RESOURCE_TYPE_LABELS_FALLBACK: Record<string, string> = {
  kb: "📚 知识库",
  document: "📄 文档",
  platform: "🔧 平台功能",
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
  const { user, currentProjectId } = useAuthStore();
  const { showToast } = useToast();

  // ── 表单状态 ──
  const [principalType, setPrincipalType] = useState<"user" | "group" | "role">("user");
  const [principalSearch, setPrincipalSearch] = useState("");
  const [principal, setPrincipal] = useState("");
  const [resourceType, setResourceType] = useState(defaultResourceType || "");
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

  // ── 动态数据：可用资源类型 + 动作 + 平台功能列表 ──
  const [availableResourceTypes, setAvailableResourceTypes] = useState<string[]>([]);
  const [actionsByResourceType, setActionsByResourceType] = useState<Record<string, string[]>>({});
  const [resourceTypeLabels, setResourceTypeLabels] = useState<Record<string, string>>({});
  const [platformFeatureIds, setPlatformFeatureIds] = useState<string[]>([]);

  // ── 加载主体列表（稳定引用，无外部依赖）──
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

  // ── 加载配置：资源类型 + 动作 + 平台功能（一次 API 调用，稳定引用）──
  //     接收 defaultRT 参数而非从闭包读取，避免 useCallback 依赖 defaultResourceType。
  //     资源动作、标签均从 /auth/config 动态获取，无硬编码。
  const loadConfig = useCallback(async (defaultRT?: string) => {
    try {
      const [resourcesRes, configRes] = await Promise.all([
        api.get<Resource[]>("/api/v1/resources"),
        api.get("/api/v1/auth/config"),
      ]);
      // 资源类型：合并两个来源
      // 1. resource_registry 表中存在实例的类型
      const types = new Set(resourcesRes.data.map(r => r.resource_type));
      // 2. Cerbos YAML 中定义的资源类型（如 OA 系统的 oa_leave_request 等）
      const resourceActions: Record<string, string[]> = configRes.data.resource_actions || {};
      for (const rt of Object.keys(resourceActions)) {
        types.add(rt);
      }
      types.add("platform");
      const typeList = Array.from(types).sort();
      setAvailableResourceTypes(typeList);
      // 选定资源类型：优先使用外部传入的 defaultRT，否则用列表第一项
      const resolvedType = defaultRT && typeList.includes(defaultRT)
        ? defaultRT
        : (typeList[0] || "");
      if (typeList.length > 0) {
        setResourceType(resolvedType);
      }
      // 动作按资源类型分组（从 /auth/config 的 resource_actions 直接获取，无需本地解析前缀）
      const byType: Record<string, string[]> = configRes.data.resource_actions || {};
      setActionsByResourceType(byType);
      // 资源类型标签（从 /auth/config 动态获取）
      const labels: Record<string, string> = configRes.data.resource_type_labels || {};
      setResourceTypeLabels(labels);
      // 平台功能 ID 列表（从 /auth/config 的 platform_features 获取）
      const features: Record<string, string> = configRes.data.platform_features || {};
      setPlatformFeatureIds(Object.keys(features).sort());
    } catch (err: unknown) {
      // 后端不可达：设置空状态，UI 将显示错误提示
      console.error("[PermissionGrantDialog] loadConfig failed:", err);
      setAvailableResourceTypes([]);
      setActionsByResourceType({});
      setResourceTypeLabels({});
      setPlatformFeatureIds([]);
    }
  }, []);

  // ── 加载资源列表（稳定引用，rt + featureIds 由调用方传入）──
  const loadResourcesForType = useCallback(async (rt: string, featureIds: string[]) => {
    if (!rt) return;
    setLoadingResources(true);
    try {
      if (rt === "platform") {
        // 平台功能资源不在 resource_registry 中，从后端配置获取
        setResources(featureIds.map(id => ({
          id: `platform-${id}`, resource_type: "platform", resource_id: id,
          tenant_id: "", owner: "system", retired: false,
        } as Resource)));
      } else {
        // 尝试从 resource_registry 查询（对于 YAML 定义的自定义类型可能返回空）
        try {
          const res = await api.get<Resource[]>(`/api/v1/resources?type=${rt}`);
          setResources(res.data.filter((r: Resource) => !r.retired));
        } catch {
          // 自定义资源类型（如 OA 系统资源）没有 DB 实例，允许手动输入
          setResources([]);
        }
      }
    } catch {
      setResources([]);
    }
    setLoadingResources(false);
  }, []);

  // ── 初始化：打开 Dialog 时加载配置和主体列表 ──
  //     仅依赖 open（loadConfig/loadPrincipals 为稳定引用，无需列入 deps）。
  //     React 18 自动批处理 loadConfig 内部的多次 setState，
  //     资源列表由下方的 resourceType 变化 effect 负责加载。
  useEffect(() => {
    if (!open) return;

    loadConfig(defaultResourceType);
    loadPrincipals();

    // 重置表单
    setPrincipal(defaultResourceId ? `user:${user?.user_id || ""}` : "");
    setResourceId(defaultResourceId || "");
    setSelectedActions([]);
    setExpiresIn("never");
    setNotes("");
    setSubmitting(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

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

  // ── 资源类型切换或初始化完成后加载资源列表 ──
  //     loadResourcesForType 为稳定引用，无需列入 deps。
  useEffect(() => {
    if (!open || !resourceType) return;
    loadResourcesForType(resourceType, platformFeatureIds);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resourceType, open, platformFeatureIds]);

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
    const pid = currentProjectId && currentProjectId !== "__all__" ? currentProjectId : "rag-v14";

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
          project_id: pid,
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
            project_id: pid,
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

  const actionList = actionsByResourceType[resourceType] || [];
  const availableActions = actionList.map(a => ({
    value: a,
    label: a,
    desc: "",
  }));

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
            <div className="flex gap-2 mb-2 flex-wrap">
              {availableResourceTypes.map(rt => (
                <button
                  key={rt}
                  onClick={() => { setResourceType(rt); setResourceId(""); }}
                  className={`px-3 py-1 rounded-full text-xs font-medium transition-colors ${
                    resourceType === rt ? "bg-green-600 text-white" : "bg-gray-100 text-gray-600 hover:bg-gray-200"
                  }`}
                >
                  {resourceTypeLabels[rt] || RESOURCE_TYPE_LABELS_FALLBACK[rt] || rt}
                </button>
              ))}
            </div>
            <input
              type="text"
              value={resourceSearch}
              onChange={e => setResourceSearch(e.target.value)}
              placeholder={`搜索${resourceTypeLabels[resourceType] || RESOURCE_TYPE_LABELS_FALLBACK[resourceType] || resourceType}...`}
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
                    <span>{r.resource_id}</span>
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
