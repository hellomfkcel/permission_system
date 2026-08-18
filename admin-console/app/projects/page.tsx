/** 项目管理 — 注册新项目、管理 client_id / API Key / audience。

Phase 1: 替代硬编码注册表。系统管理员可在此页面管理所有已注册项目。

设计依据：独立权限平台升级方案 Phase 1。
*/

"use client";

import { useCallback, useEffect, useState } from "react";
import api from "@/lib/api";
import { useToast } from "@/components/shared/Toast";
import { apiErrorMessage } from "@/lib/apiError";

interface Project {
  id: string; name: string; description: string; status: string;
  client_count: number; audience_count: number; api_key_count: number;
  created_at: string;
}
interface Client { id: string; project_id: string; client_id: string; description: string; }
interface ApiKey { id: string; project_id: string; key_prefix: string; description: string; revoked: boolean; created_at: string; }
interface Audience { id: string; project_id: string; audience: string; }
interface Member { id: string; project_id: string; user_id: string; role: string; granted_by: string; created_at: string; }

export default function ProjectsPage() {
  const { showToast: toast } = useToast();
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);

  // Create dialog
  const [showCreate, setShowCreate] = useState(false);
  const [newId, setNewId] = useState("");
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [newClients, setNewClients] = useState("");  // comma-separated client IDs
  const [newAudiences, setNewAudiences] = useState("");  // comma-separated audiences

  // Detail panel
  const [selected, setSelected] = useState<Project | null>(null);
  const [clients, setClients] = useState<Client[]>([]);
  const [apiKeys, setApiKeys] = useState<ApiKey[]>([]);
  const [audiences, setAudiences] = useState<Audience[]>([]);
  const [detailLoading, setDetailLoading] = useState(false);

  // Add client/audience dialogs
  const [showAddClient, setShowAddClient] = useState(false);
  const [newClientId, setNewClientId] = useState("");
  const [showAddAudience, setShowAddAudience] = useState(false);
  const [newAudience, setNewAudience] = useState("");

  // Members
  const [members, setMembers] = useState<Member[]>([]);
  const [showAddMember, setShowAddMember] = useState(false);
  const [newMemberId, setNewMemberId] = useState("");
  const [newMemberRole, setNewMemberRole] = useState("project_admin");
  const [availableUsers, setAvailableUsers] = useState<{user_id: string; username: string; display_name: string}[]>([]);
  const [userSearch, setUserSearch] = useState("");

  // API Key creation result
  const [newApiKey, setNewApiKey] = useState<string | null>(null);

  const loadProjects = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await api.get("/api/v1/projects");
      setProjects(data);
    } catch { toast("error", "加载项目列表失败"); }
    finally { setLoading(false); }
  }, [toast]);

  const loadDetail = async (projectId: string) => {
    setDetailLoading(true);
    try {
      const [cRes, kRes, aRes, mRes] = await Promise.all([
        api.get(`/api/v1/projects/${projectId}/clients`),
        api.get(`/api/v1/projects/${projectId}/api-keys`),
        api.get(`/api/v1/projects/${projectId}/audiences`),
        api.get(`/api/v1/projects/${projectId}/members`),
      ]);
      setClients(cRes.data);
      setApiKeys(kRes.data);
      setAudiences(aRes.data);
      setMembers(mRes.data);
    } catch { toast("error", "加载项目详情失败"); }
    finally { setDetailLoading(false); }
  };

  const selectProject = (p: Project) => {
    setSelected(p);
    setNewApiKey(null);
    loadDetail(p.id);
  };

  const handleCreate = async () => {
    if (!newId.trim() || !newName.trim()) return;
    try {
      await api.post("/api/v1/projects", { id: newId.trim(), name: newName.trim(), description: newDesc });
      // Auto-create initial clients
      const clientList = newClients.split(",").map(s => s.trim()).filter(Boolean);
      for (const cid of clientList) {
        try { await api.post(`/api/v1/projects/${newId.trim()}/clients`, { client_id: cid }); } catch {}
      }
      // Auto-create initial audiences
      const audList = newAudiences.split(",").map(s => s.trim()).filter(Boolean);
      for (const aud of audList) {
        try { await api.post(`/api/v1/projects/${newId.trim()}/audiences`, { audience: aud }); } catch {}
      }
      toast("success", `项目 ${newId} 创建成功`);
      setShowCreate(false); setNewId(""); setNewName(""); setNewDesc(""); setNewClients(""); setNewAudiences("");
      loadProjects();
    } catch (e) { toast("error", apiErrorMessage(e, "创建失败")); }
  };

  const handleAddClient = async () => {
    if (!selected || !newClientId.trim()) return;
    try {
      await api.post(`/api/v1/projects/${selected.id}/clients`, { client_id: newClientId.trim() });
      toast("success", `Client ${newClientId} 已注册`);
      setShowAddClient(false); setNewClientId("");
      loadDetail(selected.id); loadProjects();
    } catch (e) { toast("error", apiErrorMessage(e, "添加失败")); }
  };

  const handleRemoveClient = async (clientId: string) => {
    if (!selected || !confirm(`确认删除 client "${clientId}"？`)) return;
    try {
      await api.delete(`/api/v1/projects/${selected.id}/clients/${clientId}`);
      toast("success", `Client ${clientId} 已移除`);
      loadDetail(selected.id); loadProjects();
    } catch (e) { toast("error", apiErrorMessage(e, "删除失败")); }
  };

  const handleCreateApiKey = async () => {
    if (!selected) return;
    try {
      const { data } = await api.post(`/api/v1/projects/${selected.id}/api-keys`, { description: "管理台创建" });
      setNewApiKey(data.api_key);
      toast("success", "API Key 已创建 — 请立即保存");
      loadDetail(selected.id); loadProjects();
    } catch (e) { toast("error", apiErrorMessage(e, "创建失败")); }
  };

  const handleRevokeApiKey = async (keyId: string) => {
    if (!selected || !confirm("确认吊销此 API Key？吊销后立即失效。")) return;
    try {
      await api.post(`/api/v1/projects/${selected.id}/api-keys/${keyId}/revoke`);
      toast("success", "API Key 已吊销");
      loadDetail(selected.id); loadProjects();
    } catch (e) { toast("error", apiErrorMessage(e, "吊销失败")); }
  };

  const handleAddAudience = async () => {
    if (!selected || !newAudience.trim()) return;
    try {
      await api.post(`/api/v1/projects/${selected.id}/audiences`, { audience: newAudience.trim() });
      toast("success", `Audience ${newAudience} 已注册`);
      setShowAddAudience(false); setNewAudience("");
      loadDetail(selected.id); loadProjects();
    } catch (e) { toast("error", apiErrorMessage(e, "添加失败")); }
  };

  const handleRemoveAudience = async (audience: string) => {
    if (!selected || !confirm(`确认删除 audience "${audience}"？`)) return;
    try {
      await api.delete(`/api/v1/projects/${selected.id}/audiences/${audience}`);
      toast("success", `Audience ${audience} 已移除`);
      loadDetail(selected.id); loadProjects();
    } catch (e) { toast("error", apiErrorMessage(e, "删除失败")); }
  };

  const downloadSdkConfig = () => {
    if (!selected || apiKeys.length === 0) return;
    const activeKey = apiKeys.find(k => !k.revoked);
    if (!activeKey) { toast("error", "没有可用的 API Key，请先签发一个"); return; }

    // Fetch the full SDK config from backend
    api.get(`/api/v1/projects/${selected.id}/sdk-config`).then(({ data }) => {
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = `perm-config-${selected.id}.json`;
      a.click(); URL.revokeObjectURL(url);
      toast("success", "SDK 配置已下载");
    }).catch(() => {
      // Fallback: build config from available data
      const config = {
        project_id: selected.id,
        base_url: api.defaults.baseURL || "http://localhost:18080",
        api_key: "(fetch from admin console)",
        client_ids: clients.map(c => c.client_id),
        audiences: audiences.map(a => a.audience),
        sdk_install: "pip install perm-service-client",
        usage_example: `from perm_service_client import PermissionClient\n\nclient = PermissionClient(\n    base_url="${api.defaults.baseURL || 'http://localhost:18080'}",\n    api_key="(your-api-key)",\n    client_id="${clients[0]?.client_id || 'your-client-id'}",\n)\nresult = client.check("your-action", "your-resource-type", "resource-id")`,
      };
      const blob = new Blob([JSON.stringify(config, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = `perm-config-${selected.id}.json`;
      a.click(); URL.revokeObjectURL(url);
      toast("success", "SDK 配置已下载");
    });
  };

  const loadAvailableUsers = async () => {
    try {
      const { data } = await api.get("/api/v1/auth/users");
      setAvailableUsers(data.filter((u: { user_id?: string }) => !!u.user_id));
    } catch { setAvailableUsers([]); }
  };

  const filteredUsers = userSearch
    ? availableUsers.filter(u =>
        u.user_id.toLowerCase().includes(userSearch.toLowerCase()) ||
        (u.username || "").toLowerCase().includes(userSearch.toLowerCase()) ||
        (u.display_name || "").toLowerCase().includes(userSearch.toLowerCase()))
    : availableUsers.slice(0, 20);

  const handleAddMember = async () => {
    if (!selected || !newMemberId.trim()) return;
    try {
      await api.post(`/api/v1/projects/${selected.id}/members`, { user_id: newMemberId.trim(), role: newMemberRole });
      toast("success", `成员 ${newMemberId} 已添加`);
      setShowAddMember(false); setNewMemberId(""); setNewMemberRole("project_admin");
      loadDetail(selected.id);
    } catch (e) { toast("error", apiErrorMessage(e, "添加失败")); }
  };

  const handleRemoveMember = async (userId: string) => {
    if (!selected || !confirm(`确认移除成员 "${userId}"？`)) return;
    try {
      await api.delete(`/api/v1/projects/${selected.id}/members/${encodeURIComponent(userId)}`);
      toast("success", `成员 ${userId} 已移除`);
      loadDetail(selected.id);
    } catch (e) { toast("error", apiErrorMessage(e, "移除失败")); }
  };

  useEffect(() => { loadProjects(); }, [loadProjects]);

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">🏗️ 项目管理</h1>
        <button onClick={() => setShowCreate(true)} className="text-sm bg-blue-600 text-white px-3 py-1.5 rounded hover:bg-blue-700">
          + 注册新项目
        </button>
      </div>

      {/* Create Dialog */}
      {showCreate && (
        <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50" onClick={() => setShowCreate(false)}>
          <div className="bg-white rounded-xl shadow-xl p-6 w-full max-w-md" onClick={e => e.stopPropagation()}>
            <h3 className="text-lg font-semibold mb-4">注册新项目</h3>
            <input value={newId} onChange={e => setNewId(e.target.value)} placeholder="项目ID (小写字母+数字+连字符)" className="w-full border rounded-lg px-3 py-2 mb-3 text-sm" autoFocus />
            <input value={newName} onChange={e => setNewName(e.target.value)} placeholder="项目名称" className="w-full border rounded-lg px-3 py-2 mb-3 text-sm" />
            <input value={newDesc} onChange={e => setNewDesc(e.target.value)} placeholder="描述 (可选)" className="w-full border rounded-lg px-3 py-2 mb-3 text-sm" />
            <input value={newClients} onChange={e => setNewClients(e.target.value)} placeholder="Client IDs (逗号分隔, 如: my-backend,my-worker)" className="w-full border rounded-lg px-3 py-2 mb-3 text-sm" />
            <input value={newAudiences} onChange={e => setNewAudiences(e.target.value)} placeholder="Audiences (逗号分隔, 如: my-worker)" className="w-full border rounded-lg px-3 py-2 mb-4 text-sm" />
            <div className="flex justify-end gap-3">
              <button onClick={() => setShowCreate(false)} className="px-4 py-2 text-sm text-gray-500 hover:text-gray-700">取消</button>
              <button onClick={handleCreate} className="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700">创建</button>
            </div>
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Project List */}
        <div className="lg:col-span-1 space-y-2">
          {loading ? (
            <div className="text-center py-8 text-gray-400"><div className="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600 mx-auto" /></div>
          ) : projects.length === 0 ? (
            <p className="text-gray-400 text-sm text-center py-8">暂无注册项目</p>
          ) : (
            projects.map(p => (
              <button key={p.id} onClick={() => selectProject(p)}
                className={`w-full text-left p-3 rounded-lg border transition ${selected?.id === p.id ? "border-blue-500 bg-blue-50" : "border-gray-200 bg-white hover:border-gray-300"}`}>
                <div className="font-medium text-sm">{p.name}</div>
                <code className="text-xs text-gray-500">{p.id}</code>
                <div className="flex gap-3 mt-1 text-xs text-gray-400">
                  <span>🔑 {p.client_count} clients</span>
                  <span>🔐 {p.api_key_count} keys</span>
                  <span>📡 {p.audience_count} aud</span>
                </div>
              </button>
            ))
          )}
        </div>

        {/* Detail Panel */}
        <div className="lg:col-span-2">
          {!selected ? (
            <div className="text-center py-12 text-gray-400">选择左侧项目查看详情</div>
          ) : detailLoading ? (
            <div className="text-center py-8"><div className="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600 mx-auto" /></div>
          ) : (
            <div className="space-y-6">
              {/* API 调用信息 — 直接可见的 curl/Python 示例 */}
              <div className="bg-gray-900 rounded-lg p-4 text-gray-100 text-xs font-mono">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-gray-400 text-xs font-sans font-semibold">📡 API 调用信息</span>
                  <button onClick={() => {
                    const text = `# ${selected.name} — API 调用示例\n# Base URL: ${api.defaults.baseURL || 'http://localhost:18080'}\n# Client IDs: ${clients.map(c => c.client_id).join(', ')}\n\n# curl 示例\ncurl -X POST ${api.defaults.baseURL || 'http://localhost:18080'}/v1/check \\\\\n  -H "Content-Type: application/json" \\\\\n  -H "X-Client-Id: ${clients[0]?.client_id || 'your-client-id'}" \\\\\n  -H "X-Api-Key: (your-api-key)" \\\\\n  -d \'{"request_id":"t","credential":"<JWT>","action":"your-action","resource":{"type":"your-type","id":"your-id"}}\'\n\n# Python SDK\nfrom perm_service_client import PermissionClient\nclient = PermissionClient(\n    base_url="${api.defaults.baseURL || 'http://localhost:18080'}",\n    api_key="(your-api-key)",\n    client_id="${clients[0]?.client_id || 'your-client-id'}",\n)\nresult = client.check("your-action", "your-type", "your-id")`;
                    navigator.clipboard.writeText(text);
                    toast("success", "已复制到剪贴板");
                  }} className="text-gray-500 hover:text-gray-300 text-xs font-sans">📋 复制</button>
                </div>
                <div className="space-y-1">
                  <div><span className="text-blue-400">Base URL</span>: {api.defaults.baseURL || "http://localhost:18080"}</div>
                  <div>
                    <span className="text-green-400">Client IDs</span>: {clients.length > 0
                      ? clients.map((c, i) => <span key={c.id} className="text-green-300">{c.client_id}{i < clients.length - 1 ? ", " : ""}</span>)
                      : <span className="text-red-400">(未配置)</span>}
                  </div>
                  <div className="mt-2 pt-2 border-t border-gray-700">
                    <span className="text-gray-500"># curl</span>
                  </div>
                  <div>curl -X POST {api.defaults.baseURL || "http://localhost:18080"}<span className="text-yellow-300">/v1/check</span> \</div>
                  <div>  -H <span className="text-green-300">{'"Content-Type: application/json"'}</span> \</div>
                  <div>  -H <span className="text-green-300">{`"X-Client-Id: ${clients[0]?.client_id || 'your-client-id'}"`}</span> \</div>
                  <div>  -H <span className="text-green-300">{'"X-Api-Key: (your-api-key)"'}</span> \</div>
                  <div>  -d <span className="text-orange-300">{`'{"request_id":"t","credential":"<JWT>","action":"...","resource":{"type":"...","id":"..."}}'`}</span></div>
                  <div className="mt-2 pt-2 border-t border-gray-700">
                    <span className="text-gray-500"># Python</span>
                  </div>
                  <div><span className="text-purple-400">from</span> perm_service_client <span className="text-purple-400">import</span> PermissionClient</div>
                  <div>client = PermissionClient(</div>
                  <div>  base_url=<span className="text-green-300">{`"${api.defaults.baseURL || 'http://localhost:18080'}"`}</span>,</div>
                  <div>  api_key=<span className="text-green-300">{'"(your-api-key)"'}</span>,</div>
                  <div>  client_id=<span className="text-green-300">{`"${clients[0]?.client_id || 'your-client-id'}"`}</span>,</div>
                  <div>)</div>
                  <div>result = client.check(<span className="text-green-300">{'"your-action"'}</span>, <span className="text-green-300">{'"your-type"'}</span>, <span className="text-green-300">{'"your-id"'}</span>)</div>
                </div>
              </div>

              {/* SDK Config Download */}
              <div className="p-3 bg-blue-50 border border-blue-200 rounded-lg flex items-center justify-between">
                <div>
                  <strong className="text-sm text-blue-800">📦 下载 SDK JSON 配置</strong>
                  <p className="text-xs text-blue-600 mt-0.5">含 base_url、client_ids、audiences，不含 API Key</p>
                </div>
                <button onClick={downloadSdkConfig}
                  className="text-xs bg-blue-600 text-white px-3 py-1.5 rounded hover:bg-blue-700 shrink-0">
                  ⬇ 下载
                </button>
              </div>

              {newApiKey && (
                <div className="p-4 bg-green-50 border border-green-300 rounded-lg">
                  <strong className="text-green-800 text-sm">🔐 新 API Key（仅显示一次）:</strong>
                  <code className="block mt-1 p-2 bg-green-100 rounded text-xs font-mono break-all select-all">{newApiKey}</code>
                  <button onClick={() => { navigator.clipboard.writeText(newApiKey); toast("success", "已复制"); }}
                    className="mt-2 text-xs text-green-700 hover:underline">📋 复制到剪贴板</button>
                </div>
              )}

              {/* Clients */}
              <div className="bg-white border rounded-lg p-4">
                <div className="flex justify-between items-center mb-3">
                  <h3 className="font-semibold text-sm">🔑 客户端 (Client IDs)</h3>
                  <button onClick={() => setShowAddClient(true)} className="text-xs bg-blue-600 text-white px-2 py-1 rounded hover:bg-blue-700">+ 添加</button>
                </div>
                {showAddClient && (
                  <div className="flex gap-2 mb-3">
                    <input value={newClientId} onChange={e => setNewClientId(e.target.value)} placeholder="client_id" className="flex-1 border rounded px-2 py-1 text-xs" autoFocus
                      onKeyDown={e => e.key === "Enter" && handleAddClient()} />
                    <button onClick={handleAddClient} className="text-xs bg-green-600 text-white px-2 py-1 rounded">确定</button>
                    <button onClick={() => setShowAddClient(false)} className="text-xs bg-gray-200 px-2 py-1 rounded">取消</button>
                  </div>
                )}
                {clients.map(c => (
                  <div key={c.id} className="flex justify-between items-center py-1 text-sm border-b border-gray-50 last:border-0">
                    <code className="text-xs">{c.client_id}</code>
                    <button onClick={() => handleRemoveClient(c.client_id)} className="text-xs text-red-500 hover:underline">移除</button>
                  </div>
                ))}
                {clients.length === 0 && <p className="text-xs text-gray-400">暂无注册客户端</p>}
              </div>

              {/* API Keys */}
              <div className="bg-white border rounded-lg p-4">
                <div className="flex justify-between items-center mb-3">
                  <h3 className="font-semibold text-sm">🔐 API Keys</h3>
                  <button onClick={handleCreateApiKey} className="text-xs bg-blue-600 text-white px-2 py-1 rounded hover:bg-blue-700">+ 签发新 Key</button>
                </div>
                {apiKeys.map(k => (
                  <div key={k.id} className="flex justify-between items-center py-1 text-sm border-b border-gray-50 last:border-0">
                    <div>
                      <code className="text-xs">{k.key_prefix}...</code>
                      <span className="text-xs text-gray-400 ml-2">{k.description}</span>
                    </div>
                    <div className="flex items-center gap-2">
                      {k.revoked ? (
                        <span className="text-xs text-red-500">已吊销</span>
                      ) : (
                        <button onClick={() => handleRevokeApiKey(k.id)} className="text-xs text-red-500 hover:underline">吊销</button>
                      )}
                    </div>
                  </div>
                ))}
                {apiKeys.length === 0 && <p className="text-xs text-gray-400">暂无 API Key</p>}
              </div>

              {/* Audiences */}
              <div className="bg-white border rounded-lg p-4">
                <div className="flex justify-between items-center mb-3">
                  <h3 className="font-semibold text-sm">📡 ctx_token 受众 (Audiences)</h3>
                  <button onClick={() => setShowAddAudience(true)} className="text-xs bg-blue-600 text-white px-2 py-1 rounded hover:bg-blue-700">+ 添加</button>
                </div>
                {showAddAudience && (
                  <div className="flex gap-2 mb-3">
                    <input value={newAudience} onChange={e => setNewAudience(e.target.value)} placeholder="audience name" className="flex-1 border rounded px-2 py-1 text-xs" autoFocus
                      onKeyDown={e => e.key === "Enter" && handleAddAudience()} />
                    <button onClick={handleAddAudience} className="text-xs bg-green-600 text-white px-2 py-1 rounded">确定</button>
                    <button onClick={() => setShowAddAudience(false)} className="text-xs bg-gray-200 px-2 py-1 rounded">取消</button>
                  </div>
                )}
                {audiences.map(a => (
                  <div key={a.id} className="flex justify-between items-center py-1 text-sm border-b border-gray-50 last:border-0">
                    <code className="text-xs">{a.audience}</code>
                    <button onClick={() => handleRemoveAudience(a.audience)} className="text-xs text-red-500 hover:underline">移除</button>
                  </div>
                ))}
                {audiences.length === 0 && <p className="text-xs text-gray-400">暂无注册受众</p>}
              </div>

              {/* Members */}
              <div className="bg-white border rounded-lg p-4">
                <div className="flex justify-between items-center mb-3">
                  <h3 className="font-semibold text-sm">👥 项目成员</h3>
                  <button onClick={() => setShowAddMember(true)} className="text-xs bg-blue-600 text-white px-2 py-1 rounded hover:bg-blue-700">+ 添加成员</button>
                </div>
                {showAddMember && (
                  <div className="mb-3 p-3 bg-gray-50 rounded-lg border space-y-2">
                    <div className="flex gap-2 items-center">
                      <input value={userSearch} onChange={e => { setUserSearch(e.target.value); if (!availableUsers.length) loadAvailableUsers(); }}
                        placeholder="搜索用户 (ID / 用户名)…" className="flex-1 border rounded px-2 py-1 text-xs" autoFocus
                        onFocus={() => { if (!availableUsers.length) loadAvailableUsers(); }} />
                      <select value={newMemberRole} onChange={e => setNewMemberRole(e.target.value)} className="border rounded px-2 py-1 text-xs">
                        <option value="project_admin">管理员</option>
                        <option value="project_viewer">只读</option>
                      </select>
                      <button onClick={handleAddMember} disabled={!newMemberId} className="text-xs bg-green-600 text-white px-2 py-1 rounded disabled:opacity-40">添加</button>
                      <button onClick={() => { setShowAddMember(false); setUserSearch(""); setNewMemberId(""); }} className="text-xs bg-gray-200 px-2 py-1 rounded">取消</button>
                    </div>
                    {userSearch && filteredUsers.length > 0 && (
                      <div className="max-h-32 overflow-y-auto border rounded bg-white">
                        {filteredUsers.map(u => (
                          <button key={u.user_id}
                            onClick={() => { setNewMemberId(u.user_id); setUserSearch(u.user_id + (u.username ? ` (${u.username})` : "")); }}
                            className={`w-full text-left px-2 py-1 text-xs hover:bg-blue-50 ${newMemberId === u.user_id ? "bg-blue-100" : ""}`}>
                            <span className="font-medium">{u.user_id}</span>
                            {u.username && <span className="text-gray-400 ml-1">({u.username})</span>}
                            {u.display_name && <span className="text-gray-400 ml-1">{u.display_name}</span>}
                          </button>
                        ))}
                      </div>
                    )}
                    {userSearch && filteredUsers.length === 0 && (
                      <p className="text-xs text-gray-400">未找到用户，可直接输入用户ID后点添加</p>
                    )}
                    {!userSearch && (
                      <p className="text-xs text-gray-400">输入关键字搜索 Keycloak 用户，或直接输入用户ID</p>
                    )}
                  </div>
                )}
                {members.map(m => (
                  <div key={m.id} className="flex justify-between items-center py-1.5 text-sm border-b border-gray-50 last:border-0">
                    <div>
                      <span className="font-medium text-gray-700">{m.user_id}</span>
                      <span className={`ml-2 text-xs px-1.5 py-0.5 rounded-full ${m.role === "project_admin" ? "bg-purple-100 text-purple-700" : "bg-gray-100 text-gray-500"}`}>
                        {m.role === "project_admin" ? "管理员" : "只读"}
                      </span>
                    </div>
                    <button onClick={() => handleRemoveMember(m.user_id)} className="text-xs text-red-500 hover:underline">移除</button>
                  </div>
                ))}
                {members.length === 0 && <p className="text-xs text-gray-400">暂无成员</p>}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
