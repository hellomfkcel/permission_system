/** 资源管理 — KB/文档列表 + 详情面板（ACL/角色绑定）。
 *
 * 设计依据：docs/外部系统设计.md §3.3 页面结构 + §3.4.1 权限授予 Dialog
 *          + docs/frontend-design.md §3.4.3 管理台跳转入口对接。
 */

"use client";

import { useEffect, useState, useMemo } from "react";
import { useSearchParams } from "next/navigation";
import { ACTION_LABELS, type Action } from "@/lib/constants";
import api from "@/lib/api";
import PermissionGrantDialog from "@/components/acl/PermissionGrantDialog";

/** 复制文本到剪贴板。返回 true 表示成功。
 *
 * 优先使用现代 Clipboard API（需 HTTPS 或 localhost），
 * 不可用时回退到 document.execCommand('copy')（兼容 HTTP）。 */
function copyToClipboard(text: string): boolean {
  if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).catch(() => fallbackCopy(text));
    return true;
  }
  return fallbackCopy(text);
}

function fallbackCopy(text: string): boolean {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); return true; } catch { return false; }
  finally { document.body.removeChild(ta); }
}

interface Resource {
  id: string;
  resource_type: string;
  resource_id: string;
  name: string | null;
  tenant_id: string;
  owner: string;
  retired: boolean;
  created_at: string;
  updated_at: string;
}

interface ACLEntry {
  id: string;
  tenant_id: string;
  principal: string;
  resource_type: string;
  resource_id: string;
  action: string;
  granted_by: string;
  granted_at: string;
  expires_at: string | null;
  revoked: boolean;
}

interface RoleBinding {
  id: string;
  tenant_id: string;
  principal: string;
  role: string;
  resource_type: string;
  resource_id: string;
  granted_by: string;
  granted_at: string;
  revoked: boolean;
}

/** 选中资源的详情数据 */
interface ResourceDetail {
  acls: ACLEntry[];
  roleBindings: RoleBinding[];
}

export default function ResourcesPage() {
  const searchParams = useSearchParams();

  // URL 参数支持：?resource_type=kb&resource_id=xxx 自动展开详情
  // 设计依据：docs/外部系统设计.md §3.4.3 RAG 系统跳转入口对接 —
  //   RAG 前端跳转到 {ADMIN_CONSOLE_URL}/resources/kb/{kb_id} 时，
  //   管理台直接定位到该资源的权限详情。
  const urlResourceType = searchParams.get("resource_type") || "";
  const urlResourceId = searchParams.get("resource_id") || "";

  const [resources, setResources] = useState<Resource[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [filterType, setFilterType] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");

  // 详情面板状态
  const [selectedResource, setSelectedResource] = useState<Resource | null>(null);
  const [detail, setDetail] = useState<ResourceDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");

  // 权限授予 Dialog 状态
  const [showGrantDialog, setShowGrantDialog] = useState(false);
  const [grantResourceType, setGrantResourceType] = useState("kb");
  const [grantResourceId, setGrantResourceId] = useState("");

  // 复制成功提示
  const [copyToast, setCopyToast] = useState("");

  const handleCopy = (text: string) => {
    if (copyToClipboard(text)) {
      setCopyToast("已复制");
      setTimeout(() => setCopyToast(""), 1800);
    }
  };

  // Debounce search input (300ms)
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(searchQuery), 300);
    return () => clearTimeout(timer);
  }, [searchQuery]);

  const loadResources = () => {
    setLoading(true);
    setError("");

    const params = new URLSearchParams();
    if (filterType) params.set("type", filterType);

    api
      .get<Resource[]>(`/api/v1/resources?${params.toString()}`)
      .then((res) => setResources(res.data))
      .catch((err) => {
        setError(
          err?.response?.data?.detail || err?.message || "加载失败，请确认权限服务后端正常运行"
        );
      })
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    loadResources();
  }, [filterType]);

  // URL 参数自动展开详情（resources 加载完成后触发一次）
  useEffect(() => {
    if (!urlResourceType || !urlResourceId || resources.length === 0) return;
    const target = resources.find(
      (r) =>
        r.resource_type === urlResourceType &&
        r.resource_id === urlResourceId
    );
    if (target) {
      openResourceDetail(target);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resources, urlResourceType, urlResourceId]);

  /** 点击资源行 → 加载该资源的 ACL + 角色绑定详情 */
  const openResourceDetail = async (resource: Resource) => {
    if (selectedResource?.id === resource.id) {
      // 再次点击 → 关闭面板
      setSelectedResource(null);
      setDetail(null);
      return;
    }

    setSelectedResource(resource);
    setDetail(null);
    setDetailLoading(true);
    setDetailError("");

    try {
      const [aclRes, roleRes] = await Promise.all([
        api.get<ACLEntry[]>("/api/v1/acl", {
          params: { resource_type: resource.resource_type, resource_id: resource.resource_id },
        }),
        api.get<RoleBinding[]>("/api/v1/roles/bindings", {
          params: { resource_type: resource.resource_type, resource_id: resource.resource_id },
        }),
      ]);
      setDetail({
        acls: aclRes.data || [],
        roleBindings: roleRes.data || [],
      });
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "加载详情失败";
      setDetailError(msg);
    } finally {
      setDetailLoading(false);
    }
  };

  // 客户端搜索过滤
  const filtered = useMemo(() => {
    if (!debouncedSearch.trim()) return resources;
    const q = debouncedSearch.toLowerCase();
    return resources.filter(
      (r) =>
        (r.name && r.name.toLowerCase().includes(q)) ||
        r.resource_id.toLowerCase().includes(q) ||
        r.tenant_id.toLowerCase().includes(q) ||
        r.owner.toLowerCase().includes(q)
    );
  }, [resources, debouncedSearch]);

  const typeLabel = (t: string) =>
    t === "kb" ? "知识库" : t === "document" ? "文档" : t;

  const actionLabel = (action: string) => ACTION_LABELS[action as Action] || action;

  // 统计指标
  const stats = useMemo(() => {
    const kbCount = resources.filter((r) => r.resource_type === "kb" && !r.retired).length;
    const docCount = resources.filter((r) => r.resource_type === "document" && !r.retired).length;
    const retiredCount = resources.filter((r) => r.retired).length;
    return { kbCount, docCount, retiredCount, total: resources.length };
  }, [resources]);

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-bold">📁 资源管理</h1>
        <button
          onClick={loadResources}
          disabled={loading}
          className="text-sm text-blue-600 hover:text-blue-800 disabled:opacity-50"
        >
          {loading ? "刷新中..." : "🔄 刷新"}
        </button>
      </div>

      {/* 统计卡片 */}
      <div className="grid grid-cols-4 gap-4 mb-6">
        <div className="bg-white rounded-lg shadow p-4">
          <p className="text-xs text-gray-500">总计</p>
          <p className="text-2xl font-bold">{stats.total}</p>
        </div>
        <div className="bg-white rounded-lg shadow p-4">
          <p className="text-xs text-gray-500">知识库</p>
          <p className="text-2xl font-bold text-blue-600">{stats.kbCount}</p>
        </div>
        <div className="bg-white rounded-lg shadow p-4">
          <p className="text-xs text-gray-500">文档</p>
          <p className="text-2xl font-bold text-purple-600">{stats.docCount}</p>
        </div>
        <div className="bg-white rounded-lg shadow p-4">
          <p className="text-xs text-gray-500">已退役</p>
          <p className="text-2xl font-bold text-red-600">{stats.retiredCount}</p>
        </div>
      </div>

      {/* 搜索 + 过滤器 */}
      <div className="flex gap-3 mb-4">
        <div className="flex-1">
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="🔍 搜索资源名称 / ID / 租户 / 所有者..."
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
          />
        </div>
        <select
          value={filterType}
          onChange={(e) => setFilterType(e.target.value)}
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm"
        >
          <option value="">全部类型</option>
          <option value="kb">知识库 (kb)</option>
          <option value="document">文档 (document)</option>
        </select>
      </div>

      {debouncedSearch && (
        <p className="text-xs text-gray-400 mb-3">
          搜索 &quot;{debouncedSearch}&quot; — 找到 {filtered.length} 条结果
        </p>
      )}

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg px-4 py-3 mb-4 text-sm text-red-700">
          {error}
          <button onClick={loadResources} className="ml-4 underline">重试</button>
        </div>
      )}

      {/* 主内容区：表格 + 详情面板 */}
      <div className="flex gap-4">
        {/* 左侧：资源表格 */}
        <div className={`bg-white rounded-lg shadow overflow-hidden ${selectedResource ? "w-3/5" : "w-full"}`}>
          {loading ? (
            <div className="p-8 text-center text-gray-500">
              <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600 mx-auto mb-3" />
              加载资源列表...
            </div>
          ) : (
            <table className="w-full">
              <thead className="bg-gray-50">
                <tr>
                  <th className="text-left px-4 py-3 text-sm font-medium text-gray-600">类型</th>
                  <th className="text-left px-4 py-3 text-sm font-medium text-gray-600">名称</th>
                  <th className="text-left px-4 py-3 text-sm font-medium text-gray-600">资源 ID</th>
                  <th className="text-left px-4 py-3 text-sm font-medium text-gray-600">租户</th>
                  <th className="text-left px-4 py-3 text-sm font-medium text-gray-600">所有者</th>
                  <th className="text-left px-4 py-3 text-sm font-medium text-gray-600">创建时间</th>
                  <th className="text-left px-4 py-3 text-sm font-medium text-gray-600">状态</th>
                </tr>
              </thead>
              <tbody>
                {filtered.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="px-4 py-8 text-center text-gray-400">
                      {resources.length === 0
                        ? "暂无资源（资源通过 RAG 系统生命周期端口自动注册）"
                        : "无匹配结果"}
                    </td>
                  </tr>
                ) : (
                  filtered.map((r) => {
                    const isSelected = selectedResource?.id === r.id;
                    return (
                      <tr
                        key={r.id}
                        onClick={() => openResourceDetail(r)}
                        className={`border-t cursor-pointer transition-colors ${
                          isSelected
                            ? "bg-blue-50 hover:bg-blue-100"
                            : "hover:bg-gray-50"
                        }`}
                      >
                        <td className="px-4 py-3 text-sm">
                          <span
                            className={`px-2 py-0.5 rounded text-xs font-medium ${
                              r.resource_type === "kb"
                                ? "bg-blue-100 text-blue-800"
                                : "bg-purple-100 text-purple-800"
                            }`}
                          >
                            {typeLabel(r.resource_type)}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-sm font-medium text-gray-800 max-w-[200px] truncate"
                            title={r.name || r.resource_id}>
                          {r.name || <span className="text-gray-400 italic">未命名</span>}
                        </td>
                        <td
                          className="px-4 py-3 text-xs font-mono text-gray-500"
                        >
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              handleCopy(r.resource_id);
                            }}
                            className="text-left hover:text-blue-600 hover:underline cursor-pointer transition-colors"
                            title={`点击复制: ${r.resource_id}`}
                          >
                            {r.resource_id}
                          </button>
                        </td>
                        <td className="px-4 py-3 text-sm text-gray-600">{r.tenant_id}</td>
                        <td className="px-4 py-3 text-sm font-mono text-gray-600">{r.owner}</td>
                        <td className="px-4 py-3 text-sm text-gray-500">
                          {r.created_at ? new Date(r.created_at).toLocaleDateString() : "-"}
                        </td>
                        <td className="px-4 py-3 text-sm">
                          {r.retired ? (
                            <span className="text-red-600 text-xs bg-red-50 px-2 py-0.5 rounded">
                              已退役
                            </span>
                          ) : (
                            <span className="text-green-600 text-xs bg-green-50 px-2 py-0.5 rounded">
                              活跃
                            </span>
                          )}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          )}
        </div>

        {/* 右侧：详情面板 */}
        {selectedResource && (
          <div className="w-2/5 bg-white rounded-lg shadow p-4">
            <div className="flex items-center justify-between mb-3">
              <h2 className="font-semibold text-gray-800">
                {typeLabel(selectedResource.resource_type)} 详情
              </h2>
              <button
                onClick={() => { setSelectedResource(null); setDetail(null); }}
                className="text-gray-400 hover:text-gray-600 text-lg leading-none"
              >
                ✕
              </button>
            </div>

            {/* 资源基本信息 */}
            <div className="bg-gray-50 rounded-lg p-3 mb-4 text-sm space-y-1">
              {selectedResource.name && (
                <div className="flex justify-between">
                  <span className="text-gray-500">名称</span>
                  <span className="font-medium text-gray-800 truncate max-w-[200px]" title={selectedResource.name}>
                    {selectedResource.name}
                  </span>
                </div>
              )}
              <div className="flex justify-between">
                <span className="text-gray-500">资源 ID</span>
                <button
                  onClick={() => handleCopy(selectedResource.resource_id)}
                  className="font-mono text-gray-700 text-xs hover:text-blue-600 hover:underline cursor-pointer transition-colors text-right max-w-[240px] break-all"
                  title="点击复制"
                >
                  {selectedResource.resource_id}
                </button>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">类型</span>
                <span>{typeLabel(selectedResource.resource_type)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">租户</span>
                <span>{selectedResource.tenant_id}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">所有者</span>
                <span className="font-mono">{selectedResource.owner}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">创建时间</span>
                <span>{selectedResource.created_at ? new Date(selectedResource.created_at).toLocaleString() : "-"}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">状态</span>
                <span className={selectedResource.retired ? "text-red-600" : "text-green-600"}>
                  {selectedResource.retired ? "已退役" : "活跃"}
                </span>
              </div>
            </div>

            {/* ACL + 角色绑定详情 */}
            {detailLoading ? (
              <div className="text-center py-8 text-gray-400">
                <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600 mx-auto mb-2" />
                加载权限详情...
              </div>
            ) : detailError ? (
              <div className="bg-red-50 border border-red-200 rounded-lg px-3 py-2 text-xs text-red-700 mb-3">
                {detailError}
              </div>
            ) : detail ? (
              <div className="space-y-4">
                {/* ACL 列表 */}
                <div>
                  <h3 className="text-sm font-medium text-gray-700 mb-2">
                    🔑 ACL 权限 ({detail.acls.filter((a) => !a.revoked).length} 活跃)
                  </h3>
                  {detail.acls.length === 0 ? (
                    <p className="text-xs text-gray-400 py-2">暂无 ACL 记录</p>
                  ) : (
                    <div className="max-h-48 overflow-y-auto space-y-1">
                      {detail.acls.map((acl) => (
                        <div
                          key={acl.id}
                          className={`text-xs p-2 rounded border ${
                            acl.revoked
                              ? "bg-gray-50 border-gray-200 text-gray-400 line-through"
                              : "bg-blue-50 border-blue-100"
                          }`}
                        >
                          <div className="flex justify-between items-start">
                            <span className="font-mono font-medium">{acl.principal}</span>
                            <span className={`px-1.5 py-0.5 rounded text-[10px] ${
                              acl.revoked ? "bg-gray-200 text-gray-500" : "bg-blue-200 text-blue-800"
                            }`}>
                              {actionLabel(acl.action)}
                            </span>
                          </div>
                          <div className="text-gray-500 mt-0.5">
                            授予者: {acl.granted_by}
                            {acl.expires_at && ` · 过期: ${new Date(acl.expires_at).toLocaleDateString()}`}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* 角色绑定列表 */}
                <div>
                  <h3 className="text-sm font-medium text-gray-700 mb-2">
                    🎭 角色绑定 ({detail.roleBindings.filter((rb) => !rb.revoked).length} 活跃)
                  </h3>
                  {detail.roleBindings.length === 0 ? (
                    <p className="text-xs text-gray-400 py-2">暂无角色绑定</p>
                  ) : (
                    <div className="max-h-48 overflow-y-auto space-y-1">
                      {detail.roleBindings.map((rb) => (
                        <div
                          key={rb.id}
                          className={`text-xs p-2 rounded border ${
                            rb.revoked
                              ? "bg-gray-50 border-gray-200 text-gray-400 line-through"
                              : "bg-purple-50 border-purple-100"
                          }`}
                        >
                          <div className="flex justify-between items-start">
                            <span className="font-mono font-medium">{rb.principal}</span>
                            <span className={`px-1.5 py-0.5 rounded text-[10px] ${
                              rb.revoked ? "bg-gray-200 text-gray-500" : "bg-purple-200 text-purple-800"
                            }`}>
                              {rb.role}
                            </span>
                          </div>
                          <div className="text-gray-500 mt-0.5">
                            授予者: {rb.granted_by}
                            {rb.resource_id && ` · 范围: ${rb.resource_id}`}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            ) : null}

            {/* 快捷操作 */}
            {!selectedResource.retired && (
              <div className="mt-4 pt-3 border-t">
                <p className="text-xs text-gray-500 mb-2">快捷操作</p>
                <div className="flex gap-2">
                  <button
                    onClick={() => {
                      setGrantResourceType(selectedResource.resource_type);
                      setGrantResourceId(selectedResource.resource_id);
                      setShowGrantDialog(true);
                    }}
                    className="text-xs bg-blue-600 text-white px-3 py-1.5 rounded hover:bg-blue-700 transition-colors"
                  >
                    + 授予权限
                  </button>
                  {selectedResource.resource_type === "kb" && (
                    <a
                      href={`/restrictions?resource_type=kb&resource_id=${encodeURIComponent(selectedResource.resource_id)}`}
                      className="text-xs border border-gray-300 text-gray-700 px-3 py-1.5 rounded hover:bg-gray-50 transition-colors"
                    >
                      封禁管理
                    </a>
                  )}
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      <div className="mt-4 text-xs text-gray-400">
        资源通过 RAG 系统 B-DOC 写路径自动注册到权限服务（register/link/unlink/retire 生命周期端口）。
        点击资源行可查看其 ACL 权限和角色绑定详情。
      </div>

      {/* 权限授予 Dialog — 预填选中资源 */}
      <PermissionGrantDialog
        open={showGrantDialog}
        onClose={() => setShowGrantDialog(false)}
        onGranted={loadResources}
        defaultResourceType={grantResourceType}
        defaultResourceId={grantResourceId}
      />

      {/* 复制成功提示 — 2 秒后自动消失 */}
      {copyToast && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50
                        bg-gray-800 text-white text-sm px-5 py-2.5 rounded-lg
                        shadow-lg transition-opacity duration-300 animate-pulse">
          ✅ {copyToast}
        </div>
      )}
    </div>
  );
}
