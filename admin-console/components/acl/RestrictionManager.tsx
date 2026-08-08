/**
 * RestrictionManager — 封禁与限制管理组件。
 *
 * 设计依据：docs/外部系统设计.md §2.4.4 限制管理 + §2.3.1 restrictions 表
 *          + docs/权限管理系统架构设计.md §5.2 授权管理 — 限制管理
 *
 * 型一（subject_ban）：封禁主体 → prefilter 返回 suspended=true
 * 型二（resource_restriction）：限制资源 → filter 端点 pre-deny
 */

"use client";

import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { showConfirm } from "@/components/shared/Toast";
import { useAuthStore } from "@/stores/useAuthStore";
import { useToast } from "@/components/shared/Toast";
import { useResourceNames } from "@/lib/useResourceNames";

// ── 类型 ──

interface Restriction {
  id: string;
  tenant_id: string;
  restriction_type: "subject_ban" | "resource_restriction";
  principal: string | null;
  resource_type: string | null;
  resource_id: string | null;
  reason: string | null;
  created_by: string;
  created_at: string;
  removed: boolean;
}

interface PrincipalOption {
  value: string;
  label: string;
}

export default function RestrictionManager() {
  const { user, currentProjectId } = useAuthStore();
  const { showToast } = useToast();
  const { formatResource } = useResourceNames();

  // ── 状态 ──
  const [items, setItems] = useState<Restriction[]>([]);
  const [loading, setLoading] = useState(true);
  const [showAddForm, setShowAddForm] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  // 添加表单
  const [restrictionType, setRestrictionType] = useState<"subject_ban" | "resource_restriction">("subject_ban");
  const [banPrincipal, setBanPrincipal] = useState("");
  const [banPrincipalSearch, setBanPrincipalSearch] = useState("");
  const [resType, setResType] = useState("");
  const [resId, setResId] = useState("");
  const [availableResTypes, setAvailableResTypes] = useState<string[]>([]);
  const [reason, setReason] = useState("");

  // 筛选
  const [filterText, setFilterText] = useState("");
  const [filterType, setFilterType] = useState("");
  const [showRemoved, setShowRemoved] = useState(false);

  // 主体建议
  const [principalOptions, setPrincipalOptions] = useState<PrincipalOption[]>([]);
  const [filteredPrincipals, setFilteredPrincipals] = useState<PrincipalOption[]>([]);

  // ── 加载 ──
  const loadItems = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.get<Restriction[]>("/api/v1/restrictions");
      setItems(res.data);
    } catch {
      setItems([]);
    }
    setLoading(false);
  }, []);

  // 加载可用资源类型
  useEffect(() => {
    api.get("/api/v1/resources").then(r => {
      const types = new Set((r.data as Array<{resource_type: string}>).map(x => x.resource_type));
      const typeList = Array.from(types).sort();
      setAvailableResTypes(typeList);
      if (typeList.length > 0 && !resType) setResType(typeList[0]);
    }).catch(() => setAvailableResTypes(["kb", "document"]));
  }, []);

  const loadPrincipals = useCallback(async () => {
    const opts: PrincipalOption[] = [];
    try {
      const usersRes = await api.get("/api/v1/auth/users");
      const users = usersRes.data as Array<{ user_id: string; groups: string[] }>;
      for (const u of users) {
        if (u.user_id) opts.push({ value: `user:${u.user_id}`, label: `🧑 ${u.user_id}` });
        for (const g of u.groups || []) {
          if (g && !opts.find(o => o.value === `group:${g}`)) {
            opts.push({ value: `group:${g}`, label: `👥 ${g}` });
          }
        }
      }
    } catch { /* ignore */ }
    setPrincipalOptions(opts);
  }, []);

  useEffect(() => { loadItems(); }, [loadItems]);
  useEffect(() => { loadPrincipals(); }, [loadPrincipals]);

  // ── 过滤 ──
  useEffect(() => {
    const q = banPrincipalSearch.toLowerCase();
    setFilteredPrincipals(
      principalOptions.filter(o => o.label.toLowerCase().includes(q) || o.value.toLowerCase().includes(q)).slice(0, 10)
    );
  }, [banPrincipalSearch, principalOptions]);

  // ── 添加封禁 ──
  const handleAdd = async () => {
    if (restrictionType === "subject_ban" && !banPrincipal.trim()) {
      return showToast("error", "请输入被封禁的主体");
    }
    if (restrictionType === "resource_restriction" && (!resType.trim() || !resId.trim())) {
      return showToast("error", "请输入资源类型和资源 ID");
    }

    setSubmitting(true);
    try {
      const pid = currentProjectId && currentProjectId !== "__all__" ? currentProjectId : "rag-v14";
      await api.post("/api/v1/restrictions/add", {
        tenant_id: user?.tenant_id || "tenant-dev",
        restriction_type: restrictionType,
        principal: restrictionType === "subject_ban" ? banPrincipal.trim() : null,
        resource_type: restrictionType === "resource_restriction" ? resType.trim() : null,
        resource_id: restrictionType === "resource_restriction" ? resId.trim() : null,
        reason: reason.trim() || null,
        created_by: user?.user_id || "admin",
        project_id: pid,
      });

      const typeLabel = restrictionType === "subject_ban" ? "主体封禁" : "资源限制";
      showToast("success", `${typeLabel}已生效`);
      setShowAddForm(false);
      setBanPrincipal("");
      setResType(availableResTypes[0] || "");
      setResId("");
      setReason("");
      loadItems();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      showToast("error", err?.response?.data?.detail || "添加封禁失败");
    } finally {
      setSubmitting(false);
    }
  };

  // ── 解除封禁 ──
  const handleRemove = async (item: Restriction) => {
    const label = item.restriction_type === "subject_ban"
      ? `${item.principal} 的封禁`
      : `${item.resource_type}:${item.resource_id} 的限制`;
    if (!showConfirm(`确认解除 ${label}？`)) return;

    try {
      await api.post(`/api/v1/restrictions/remove?restriction_id=${item.id}`);
      showToast("success", "已解除");
      loadItems();
    } catch {
      showToast("error", "解除失败");
    }
  };

  // ── 筛选 ──
  const filteredItems = items.filter(item => {
    if (!showRemoved && item.removed) return false;
    if (filterType && item.restriction_type !== filterType) return false;
    if (filterText) {
      const q = filterText.toLowerCase();
      const s = [item.principal, item.resource_id, item.resource_type, item.reason, item.created_by]
        .filter(Boolean).join(" ").toLowerCase();
      if (!s.includes(q)) return false;
    }
    return true;
  });

  // ── 统计 ──
  const activeBans = items.filter(i => i.restriction_type === "subject_ban" && !i.removed).length;
  const activeRestrictions = items.filter(i => i.restriction_type === "resource_restriction" && !i.removed).length;

  return (
    <div>
      {/* 统计卡片 */}
      <div className="grid grid-cols-2 gap-3 mb-5">
        <div className="bg-red-50 border border-red-200 rounded-lg p-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-red-600 font-medium">型一：主体封禁</p>
              <p className="text-xs text-red-400 mt-1">prefilter 返回 suspended=true</p>
            </div>
            <span className="text-3xl font-bold text-red-600">{activeBans}</span>
          </div>
        </div>
        <div className="bg-orange-50 border border-orange-200 rounded-lg p-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-orange-600 font-medium">型二：资源限制</p>
              <p className="text-xs text-orange-400 mt-1">filter 端点 pre-deny</p>
            </div>
            <span className="text-3xl font-bold text-orange-600">{activeRestrictions}</span>
          </div>
        </div>
      </div>

      {/* 操作栏 */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex gap-2">
          <input
            type="text"
            value={filterText}
            onChange={e => setFilterText(e.target.value)}
            placeholder="搜索主体/资源/原因..."
            className="border border-gray-300 rounded-lg px-3 py-2 text-sm w-56"
          />
          <select
            value={filterType}
            onChange={e => setFilterType(e.target.value)}
            className="border border-gray-300 rounded-lg px-3 py-2 text-sm"
          >
            <option value="">全部类型</option>
            <option value="subject_ban">型一：主体封禁</option>
            <option value="resource_restriction">型二：资源限制</option>
          </select>
          <label className="flex items-center gap-2 text-sm text-gray-600 px-2">
            <input
              type="checkbox"
              checked={showRemoved}
              onChange={e => setShowRemoved(e.target.checked)}
              className="rounded"
            />
            显示已解除
          </label>
        </div>
        <button
          onClick={() => setShowAddForm(!showAddForm)}
          className="bg-red-600 text-white px-4 py-2 rounded-lg hover:bg-red-700 text-sm font-medium"
        >
          + 添加封禁
        </button>
      </div>

      {/* 添加表单 */}
      {showAddForm && (
        <div className="bg-white rounded-lg border border-red-200 shadow-sm p-5 mb-4">
          <h3 className="text-base font-semibold text-gray-900 mb-4">🚫 添加封禁/限制</h3>

          {/* 类型选择 */}
          <div className="flex gap-2 mb-4">
            <button
              onClick={() => setRestrictionType("subject_ban")}
              className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                restrictionType === "subject_ban"
                  ? "bg-red-600 text-white"
                  : "bg-gray-100 text-gray-600 hover:bg-gray-200"
              }`}
            >
              🚫 型一：主体封禁
            </button>
            <button
              onClick={() => setRestrictionType("resource_restriction")}
              className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                restrictionType === "resource_restriction"
                  ? "bg-orange-600 text-white"
                  : "bg-gray-100 text-gray-600 hover:bg-gray-200"
              }`}
            >
              ⛔ 型二：资源限制
            </button>
          </div>

          {/* 表单字段 */}
          <div className="grid grid-cols-2 gap-4">
            {restrictionType === "subject_ban" ? (
              <div className="col-span-2">
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  被禁主体 <span className="text-red-500">*</span>
                </label>
                <input
                  type="text"
                  value={banPrincipalSearch}
                  onChange={e => setBanPrincipalSearch(e.target.value)}
                  placeholder="搜索用户/组..."
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm mb-1"
                />
                <input
                  type="text"
                  value={banPrincipal}
                  onChange={e => setBanPrincipal(e.target.value)}
                  placeholder="或直接输入: user:xxx / group:xxx"
                  className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono text-gray-600"
                />
                {banPrincipalSearch && filteredPrincipals.length > 0 && (
                  <div className="mt-1 border rounded-lg max-h-32 overflow-y-auto bg-white shadow-sm">
                    {filteredPrincipals.map(p => (
                      <button
                        key={p.value}
                        onClick={() => { setBanPrincipal(p.value); setBanPrincipalSearch(""); }}
                        className="w-full text-left px-3 py-1.5 text-sm hover:bg-red-50"
                      >
                        {p.label}
                      </button>
                    ))}
                  </div>
                )}
                <p className="text-xs text-gray-400 mt-1">
                  被封禁主体的所有请求将返回 suspended=true，检索直接跳过
                </p>
              </div>
            ) : (
              <>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    资源类型 <span className="text-red-500">*</span>
                  </label>
                  <select
                    value={resType}
                    onChange={e => setResType(e.target.value)}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
                  >
                    {availableResTypes.map(rt => (
                      <option key={rt} value={rt}>{rt}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">
                    资源 ID <span className="text-red-500">*</span>
                  </label>
                  <input
                    type="text"
                    value={resId}
                    onChange={e => setResId(e.target.value)}
                    placeholder={`${resType}-uuid`}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono"
                  />
                </div>
              </>
            )}
            <div className="col-span-2">
              <label className="block text-sm font-medium text-gray-700 mb-1">原因（可选）</label>
              <textarea
                value={reason}
                onChange={e => setReason(e.target.value)}
                placeholder="封禁原因说明..."
                rows={2}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
              />
            </div>
          </div>

          <div className="flex gap-3 mt-4">
            <button
              onClick={handleAdd}
              disabled={submitting}
              className="bg-red-600 text-white px-5 py-2 rounded-lg hover:bg-red-700 text-sm disabled:opacity-50"
            >
              {submitting ? "处理中..." : "确认添加"}
            </button>
            <button
              onClick={() => setShowAddForm(false)}
              className="bg-gray-200 text-gray-700 px-5 py-2 rounded-lg hover:bg-gray-300 text-sm"
            >
              取消
            </button>
          </div>
        </div>
      )}

      {/* 列表 */}
      {loading ? (
        <div className="text-center py-12 text-gray-400">加载中...</div>
      ) : filteredItems.length === 0 ? (
        <div className="text-center py-12 bg-white rounded-lg border border-gray-200">
          <p className="text-gray-400 text-lg mb-2">🚫</p>
          <p className="text-gray-500">暂无封禁/限制</p>
        </div>
      ) : (
        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          <table className="w-full">
            <thead className="bg-gray-50 border-b">
              <tr>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">类型</th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">主体</th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">资源</th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">原因</th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">创建者</th>
                <th className="text-left px-4 py-3 text-xs font-semibold text-gray-500 uppercase">状态</th>
                <th className="text-right px-4 py-3 text-xs font-semibold text-gray-500 uppercase">操作</th>
              </tr>
            </thead>
            <tbody>
              {filteredItems.map(item => (
                <tr key={item.id} className="border-t hover:bg-gray-50 transition-colors">
                  <td className="px-4 py-3">
                    <span className={`inline-flex px-2 py-0.5 rounded-full text-xs font-medium ${
                      item.restriction_type === "subject_ban"
                        ? "bg-red-100 text-red-700"
                        : "bg-orange-100 text-orange-700"
                    }`}>
                      {item.restriction_type === "subject_ban" ? "型一封禁" : "型二限制"}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    {item.principal ? (
                      <span className="text-sm font-mono text-gray-800">{item.principal}</span>
                    ) : (
                      <span className="text-gray-300">—</span>
                    )}
                  </td>
                  <td className="px-4 py-3 max-w-[180px] truncate" title={item.resource_id ?? ""}>
                    {item.resource_type ? (
                      <span className="text-sm text-gray-600">
                        {formatResource(item.resource_type, item.resource_id ?? "")}
                      </span>
                    ) : (
                      <span className="text-gray-300">—</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-sm text-gray-500 max-w-[200px] truncate" title={item.reason || ""}>
                    {item.reason || "—"}
                  </td>
                  <td className="px-4 py-3 text-sm text-gray-500">{item.created_by}</td>
                  <td className="px-4 py-3">
                    {item.removed ? (
                      <span className="text-xs text-gray-400 bg-gray-100 px-2 py-0.5 rounded-full">已解除</span>
                    ) : (
                      <span className="text-xs text-green-700 bg-green-100 px-2 py-0.5 rounded-full">生效中</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-right">
                    {!item.removed && (
                      <button
                        onClick={() => handleRemove(item)}
                        className="text-green-600 hover:text-green-800 text-sm font-medium hover:underline"
                      >
                        解除
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="mt-3 text-xs text-gray-400">
        共 {filteredItems.length} 条（生效中 {filteredItems.filter(i => !i.removed).length} 条）
      </div>
    </div>
  );
}
