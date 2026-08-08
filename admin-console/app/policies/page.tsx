/** Cerbos 策略管理 — 策略列表浏览 + YAML 内容实时查看 + 编辑/版本历史。
 *
 * 设计依据：docs/外部系统设计.md §3.3 页面结构 + §3.4.2 策略模拟器。
 *
 * 架构纪律（来自 docs/权限管理系统架构设计.md §1）：
 * - 管理台不直连 Cerbos PDP —— 所有策略操作经权限服务后端代理。
 * - 权限服务从文件系统读取 cerbos/policies/ 目录，Cerbos PDP 自动热加载。
 */

"use client";

import { useEffect, useState, useRef } from "react";
import api from "@/lib/api";
import { useAuthStore } from "@/stores/useAuthStore";
import { useToast } from "@/components/shared/Toast";

interface PolicyEntry {
  path: string;
  name: string;
  yaml_content: string;
}

interface VersionEntry {
  version_id: string;
  created_at: string;
  size_bytes: number;
  message: string;
}

export default function PoliciesPage() {
  const { currentProjectId } = useAuthStore();
  const isPlatformMode = !currentProjectId || currentProjectId === "__all__";
  const { showToast: toast } = useToast();
  const toastError = (msg: string) => toast("error", msg);
  const [policies, setPolicies] = useState<PolicyEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [pdpStatus, setPdpStatus] = useState<"checking" | "ok" | "down">("checking");

  const [activePolicy, setActivePolicy] = useState<PolicyEntry | null>(null);
  const [yamlContent, setYamlContent] = useState("");
  const [editMode, setEditMode] = useState(false);
  const [editContent, setEditContent] = useState("");
  const [saving, setSaving] = useState(false);
  const [editingMessage, setEditingMessage] = useState("");

  // 版本历史
  const [versions, setVersions] = useState<VersionEntry[]>([]);
  const [showVersions, setShowVersions] = useState(false);
  const [diffResult, setDiffResult] = useState<string | null>(null);

  // 新建策略（替代原生 prompt）
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [newPolicyPath, setNewPolicyPath] = useState("");

  // 上传策略文件
  const [showUploadDialog, setShowUploadDialog] = useState(false);
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadSubpath, setUploadSubpath] = useState("");
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // P2-6: 部署状态
  const [deployStatus, setDeployStatus] = useState<{status: string; policies_count: number; cerbos_version: string; message: string} | null>(null);
  const [checkingDeploy, setCheckingDeploy] = useState(false);

  const checkDeployStatus = async () => {
    setCheckingDeploy(true);
    try {
      const res = await api.get("/api/v1/policies/deploy-status");
      setDeployStatus(res.data);
    } catch {
      setDeployStatus({status: "unknown", policies_count: 0, cerbos_version: "", message: "无法获取部署状态"});
    } finally {
      setCheckingDeploy(false);
    }
  };

  // P2-2: YAML 校验状态
  const [validating, setValidating] = useState(false);
  const [validationResult, setValidationResult] = useState<{
    valid: boolean; errors: string[]; warnings: string[]; actions_count: number;
  } | null>(null);

  // ── 加载策略列表 ──
  const loadPolicies = async () => {
    setLoading(true);
    try {
      const params: Record<string, string> = {};
      if (!isPlatformMode) params.project_id = currentProjectId;
      const res = await api.get("/api/v1/policies", { params });
      const all: PolicyEntry[] = res.data;
      // 过滤掉 .versions/ 目录下的快照文件
      const filtered = all.filter((p) => !p.path.startsWith(".versions/"));
      setPolicies(filtered);
      setPdpStatus("ok");
    } catch {
      setPdpStatus("down");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadPolicies();
  }, [currentProjectId]);

  // ── 查看策略 YAML ──
  const viewPolicy = (policy: PolicyEntry) => {
    setActivePolicy(policy);
    setYamlContent(policy.yaml_content);
    setEditMode(false);
    setShowVersions(false);
    setDiffResult(null);
  };

  // ── 进入编辑模式 ──
  const startEdit = () => {
    setEditContent(yamlContent);
    setEditMode(true);
    setEditingMessage("");
  };

  const cancelEdit = () => {
    setEditMode(false);
    setEditingMessage("");
  };

  // ── 保存策略 ──
  const savePolicy = async () => {
    if (!activePolicy) return;
    setSaving(true);
    try {
      const params: Record<string, string> = {};
      if (!isPlatformMode && currentProjectId) params.project_id = currentProjectId;
      await api.put(`/api/v1/policies/${encodeURIComponent(activePolicy.path)}`, {
        yaml_content: editContent,
        message: editingMessage || undefined,
      }, { params });
      setYamlContent(editContent);
      setEditMode(false);
      // 重新加载列表刷新版本
      await loadPolicies();
      if (activePolicy) {
        const updated = policies.find((p) => p.path === activePolicy.path);
        if (updated) {
          setActivePolicy({ ...updated, yaml_content: editContent });
        }
      }
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      toastError(`保存失败: ${err?.response?.data?.detail || err.message}`);
    } finally {
      setSaving(false);
    }
  };

  // ── 加载版本历史 ──
  const loadVersions = async () => {
    if (!activePolicy) return;
    try {
      const res = await api.get(
        `/api/v1/policies/${encodeURIComponent(activePolicy.path)}/versions`
      );
      setVersions(res.data);
      setShowVersions(true);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      toastError(`加载版本历史失败: ${err?.response?.data?.detail || err.message}`);
    }
  };

  // ── 显示版本 Diff ──
  const showDiff = async (versionId: string) => {
    if (!activePolicy) return;
    try {
      const res = await api.get(
        `/api/v1/policies/${encodeURIComponent(activePolicy.path)}/diff`,
        { params: { from: versionId, to: "current" } }
      );
      const lines = res.data.diff_lines || [];
      setDiffResult(lines.join("\n"));
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      toastError(`加载 Diff 失败: ${err?.response?.data?.detail || err.message}`);
    }
  };

  // ── P2-2: YAML 校验 ──
  const validateYaml = async (content: string) => {
    setValidating(true);
    setValidationResult(null);
    try {
      const res = await api.post("/api/v1/policies/validate", { yaml_content: content });
      setValidationResult(res.data);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      toastError(`校验请求失败: ${err?.response?.data?.detail || (e as Error).message}`);
    } finally {
      setValidating(false);
    }
  };

  // ── 创建新策略 ──
  const createPolicy = async () => {
    if (!newPolicyPath.trim()) return;
    const path = newPolicyPath.trim();
    const name = path.split("/").pop()?.replace(/\.ya?ml$/, "") || path;
    try {
      const params: Record<string, string> = {};
      if (!isPlatformMode && currentProjectId) params.project_id = currentProjectId;
      await api.put(`/api/v1/policies/${encodeURIComponent(path)}`, {
        yaml_content: `apiVersion: api.cerbos.dev/v1\n# ${name}\n`,
        message: "创建新策略",
      }, { params });
      await loadPolicies();
      setShowCreateDialog(false);
      setNewPolicyPath("");
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      toastError(`创建失败: ${err?.response?.data?.detail || err.message}`);
    }
  };

  // ── 上传策略文件 ──
  const handleUpload = async () => {
    if (!uploadFile) return;
    setUploading(true);
    try {
      const formData = new FormData();
      formData.append("file", uploadFile);
      formData.append("project_id", isPlatformMode ? "" : (currentProjectId || ""));
      if (uploadSubpath.trim()) {
        formData.append("policy_subpath", uploadSubpath.trim());
      }
      formData.append("message", `上传策略文件: ${uploadFile.name}`);

      await api.post("/api/v1/policies/upload", formData, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      toast("success", `策略文件 "${uploadFile.name}" 上传成功`);
      setShowUploadDialog(false);
      setUploadFile(null);
      setUploadSubpath("");
      await loadPolicies();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      toastError(`上传失败: ${err?.response?.data?.detail || err.message}`);
    } finally {
      setUploading(false);
    }
  };

  // ── YAML 语法高亮 ──
  const highlightYaml = (yaml: string) => {
    return yaml
      .replace(/^(\s*#.*)$/gm, '<span class="text-gray-500">$1</span>')
      .replace(
        /^(\s*)([a-zA-Z_][a-zA-Z0-9_-]*)(\s*:)/gm,
        '$1<span class="text-blue-400">$2</span>$3'
      )
      .replace(
        /:(\s+)(["'].*?["']|true|false|\d+)/gm,
        ':<span class="text-orange-300">$1$2</span>'
      )
      .replace(/:(\s+)(>)/gm, ':<span class="text-yellow-400">$1$2</span>')
      .replace(
        /\b(EFFECT_ALLOW|EFFECT_DENY)\b/g,
        '<span class="text-green-400">$1</span>'
      )
      .replace(
        /\b(apiVersion|derivedRoles|resourcePolicy|rules|actions|effect|condition|match|expr)\b/g,
        '<span class="text-purple-400">$1</span>'
      );
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">📜 Cerbos 策略管理</h1>
        <div className="flex items-center gap-3">
          {/* PDP 状态（通过权限服务间接判定） */}
          <div className="flex items-center gap-2 text-sm">
            <span className="text-gray-500">PDP:</span>
            {pdpStatus === "checking" && (
              <span className="text-yellow-600">检查中...</span>
            )}
            {pdpStatus === "ok" && (
              <span className="text-green-600 flex items-center gap-1">
                <span className="w-2 h-2 bg-green-500 rounded-full inline-block" />
                SERVING ({policies.length} policies)
              </span>
            )}
            {pdpStatus === "down" && (
              <span className="text-red-600">权限服务不可达</span>
            )}
          </div>
          <button
            onClick={() => setShowUploadDialog(true)}
            className="text-sm bg-green-600 text-white px-3 py-1.5 rounded hover:bg-green-700"
          >
            📤 上传策略
          </button>
          <button
            onClick={() => setShowCreateDialog(true)}
            className="text-sm bg-blue-600 text-white px-3 py-1.5 rounded hover:bg-blue-700"
          >
            + 新建策略
          </button>
        </div>
        {/* 新建策略 Dialog */}
        {showCreateDialog && (
          <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50" onClick={() => setShowCreateDialog(false)}>
            <div className="bg-white rounded-xl shadow-xl p-6 w-full max-w-md" onClick={(e) => e.stopPropagation()}>
              <h3 className="text-lg font-semibold mb-4">新建策略文件</h3>
              <input
                type="text"
                value={newPolicyPath}
                onChange={(e) => setNewPolicyPath(e.target.value)}
                placeholder="如 resource_policies/new.yaml"
                className="w-full border rounded-lg px-3 py-2 mb-4 text-sm"
                onKeyDown={(e) => e.key === "Enter" && createPolicy()}
                autoFocus
              />
              <div className="flex justify-end gap-3">
                <button onClick={() => setShowCreateDialog(false)} className="px-4 py-2 text-sm text-gray-500 hover:text-gray-700">取消</button>
                <button onClick={createPolicy} className="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700">创建</button>
              </div>
            </div>
          </div>
        )}

        {/* 上传策略 Dialog */}
        {showUploadDialog && (
          <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50" onClick={() => !uploading && setShowUploadDialog(false)}>
            <div className="bg-white rounded-xl shadow-xl p-6 w-full max-w-md" onClick={(e) => e.stopPropagation()}>
              <h3 className="text-lg font-semibold mb-4">📤 上传策略文件</h3>
              <p className="text-xs text-gray-500 mb-4">
                上传到: <code className="bg-gray-100 px-1 rounded">cerbos/policies/{isPlatformMode ? "" : (currentProjectId || "")}</code>
              </p>

              {/* 文件选择 */}
              <div className="mb-4">
                <label className="block text-sm font-medium text-gray-700 mb-1">选择 .yaml/.yml 文件</label>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".yaml,.yml"
                  onChange={(e) => {
                    const f = e.target.files?.[0] || null;
                    setUploadFile(f);
                    if (f && !uploadSubpath) {
                      // 默认子路径
                      setUploadSubpath(`resource_policies/${f.name}`);
                    }
                  }}
                  className="w-full border rounded-lg p-2 text-sm"
                  disabled={uploading}
                />
                {uploadFile && (
                  <p className="text-xs text-green-600 mt-1">已选择: {uploadFile.name} ({(uploadFile.size / 1024).toFixed(1)} KB)</p>
                )}
              </div>

              {/* 目标路径 */}
              <div className="mb-4">
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  目标路径（可选，留空自动推导）
                </label>
                <input
                  type="text"
                  value={uploadSubpath}
                  onChange={(e) => setUploadSubpath(e.target.value)}
                  placeholder="如 resource_policies/my_policy.yaml"
                  className="w-full border rounded-lg px-3 py-2 text-sm"
                  disabled={uploading}
                />
                <p className="text-xs text-gray-400 mt-1">
                  留空时根据 YAML 内容自动推导: 含 derivedRoles → derived_roles/；否则 → resource_policies/
                </p>
              </div>

              <div className="flex justify-end gap-3">
                <button
                  onClick={() => { setShowUploadDialog(false); setUploadFile(null); setUploadSubpath(""); }}
                  disabled={uploading}
                  className="px-4 py-2 text-sm text-gray-500 hover:text-gray-700"
                >
                  取消
                </button>
                <button
                  onClick={handleUpload}
                  disabled={!uploadFile || uploading}
                  className="px-4 py-2 text-sm bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50"
                >
                  {uploading ? "上传中..." : "上传"}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* 策略列表 */}
        <div className="lg:col-span-1 space-y-3">
          {loading ? (
            <div className="text-center py-8 text-gray-400">
              <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600 mx-auto" />
              <p className="mt-2 text-sm">加载策略列表...</p>
            </div>
          ) : policies.length === 0 ? (
            <div className="text-center py-8 text-gray-400 text-sm">
              暂无策略文件
            </div>
          ) : (
            policies.map((policy) => (
              <button
                key={policy.path}
                onClick={() => viewPolicy(policy)}
                className={`w-full text-left p-4 rounded-lg border transition-all ${
                  activePolicy?.path === policy.path
                    ? "border-blue-500 bg-blue-50 shadow-sm"
                    : "border-gray-200 bg-white hover:border-gray-300 hover:shadow-sm"
                }`}
              >
                <h3 className="font-medium text-sm">{policy.name}</h3>
                <code className="text-xs text-gray-500 mt-1 block">
                  {policy.path}
                </code>
              </button>
            ))
          )}

          <div className="mt-4 p-4 bg-blue-50 border border-blue-200 rounded-lg text-xs text-blue-800">
            <strong>ℹ️ 策略管理说明</strong>
            <p className="mt-1">
              策略文件位于 <code className="bg-blue-100 px-1 rounded">cerbos/policies/</code>，
              通过权限服务后端管理。Cerbos PDP 自动热加载，修改策略后无需重启任何服务。
            </p>
            <p className="mt-1">
              所有变更经权限服务审计日志记录，版本历史可追溯。
            </p>
          </div>

          {/* P2-6: 策略部署状态面板 */}
          <div className="mt-4 p-4 bg-white border border-gray-200 rounded-lg">
            <div className="flex items-center justify-between mb-2">
              <strong className="text-sm">📡 策略部署状态</strong>
              <button onClick={checkDeployStatus} disabled={checkingDeploy} className="text-xs bg-blue-600 text-white px-3 py-1 rounded hover:bg-blue-700 disabled:opacity-50">
                {checkingDeploy ? "检查中..." : "刷新"}
              </button>
            </div>
            {deployStatus ? (
              <div className="space-y-1 text-xs">
                <div className="flex gap-2">
                  <span className="text-gray-500">状态:</span>
                  <span className={
                    deployStatus.status === "healthy" ? "text-green-600 font-medium" :
                    deployStatus.status === "degraded" ? "text-yellow-600 font-medium" : "text-gray-500"
                  }>
                    {deployStatus.status === "healthy" ? "✅ 健康" : deployStatus.status === "degraded" ? "⚠️ 降级" : "❓ 未知"}
                  </span>
                </div>
                {deployStatus.cerbos_version && (
                  <div className="flex gap-2">
                    <span className="text-gray-500">Cerbos 版本:</span>
                    <span className="font-mono">{deployStatus.cerbos_version}</span>
                  </div>
                )}
                <div className="flex gap-2">
                  <span className="text-gray-500">策略数:</span>
                  <span>{deployStatus.policies_count}</span>
                </div>
                {deployStatus.message && (
                  <div className="flex gap-2">
                    <span className="text-gray-500">信息:</span>
                    <span className="text-gray-600">{deployStatus.message}</span>
                  </div>
                )}
              </div>
            ) : (
              <p className="text-xs text-gray-400">点击"刷新"检查 Cerbos PDP 策略部署状态</p>
            )}
          </div>
        </div>

        {/* YAML 内容面板 */}
        <div className="lg:col-span-2">
          <div className="bg-white rounded-lg shadow p-6 min-h-[400px]">
            {!activePolicy ? (
              <div className="flex items-center justify-center h-full text-gray-400 py-12">
                <div className="text-center">
                  <svg
                    className="w-12 h-12 mx-auto text-gray-300"
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={1.5}
                      d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"
                    />
                  </svg>
                  <p className="mt-3">选择左侧策略查看 YAML 源码</p>
                </div>
              </div>
            ) : (
              <div>
                {/* 头部工具栏 */}
                <div className="flex items-center justify-between mb-4">
                  <div>
                    <h2 className="font-semibold">{activePolicy.name}</h2>
                    <code className="text-xs bg-gray-100 px-2 py-1 rounded">
                      cerbos/policies/{activePolicy.path}
                    </code>
                  </div>
                  <div className="flex gap-2">
                    {!editMode ? (
                      <>
                        <button
                          onClick={() => validateYaml(yamlContent)}
                          disabled={validating}
                          className="text-xs bg-yellow-500 text-white px-3 py-1 rounded hover:bg-yellow-600 disabled:opacity-50"
                        >
                          {validating ? "校验中..." : "🔍 校验"}
                        </button>
                        <button
                          onClick={startEdit}
                          className="text-xs bg-blue-600 text-white px-3 py-1 rounded hover:bg-blue-700"
                        >
                          编辑
                        </button>
                        <button
                          onClick={loadVersions}
                          className="text-xs bg-gray-100 text-gray-700 px-3 py-1 rounded hover:bg-gray-200"
                        >
                          版本历史
                        </button>
                      </>
                    ) : (
                      <>
                        <input
                          type="text"
                          placeholder="变更说明（可选）"
                          value={editingMessage}
                          onChange={(e) => setEditingMessage(e.target.value)}
                          className="text-xs border rounded px-2 py-1 w-40"
                        />
                        <button
                          onClick={savePolicy}
                          disabled={saving}
                          className="text-xs bg-green-600 text-white px-3 py-1 rounded hover:bg-green-700 disabled:opacity-50"
                        >
                          {saving ? "保存中..." : "保存"}
                        </button>
                        <button
                          onClick={cancelEdit}
                          className="text-xs bg-gray-200 text-gray-700 px-3 py-1 rounded hover:bg-gray-300"
                        >
                          取消
                        </button>
                      </>
                    )}
                  </div>
                </div>

                {/* YAML 内容 */}
                {editMode ? (
                  <textarea
                    value={editContent}
                    onChange={(e) => setEditContent(e.target.value)}
                    className="w-full h-[400px] bg-gray-900 text-green-400 font-mono text-xs p-4 rounded-lg resize-none focus:outline-none focus:ring-2 focus:ring-blue-500"
                    spellCheck={false}
                  />
                ) : (
                  <div className="bg-gray-900 rounded-lg p-4 overflow-auto max-h-[400px]">
                    <pre
                      className="text-xs leading-relaxed whitespace-pre-wrap"
                      dangerouslySetInnerHTML={{
                        __html: highlightYaml(yamlContent),
                      }}
                    />
                  </div>
                )}

                {/* 版本历史面板 */}
                {showVersions && versions.length > 0 && (
                  <div className="mt-4 border-t pt-4">
                    <h3 className="text-sm font-semibold mb-2">版本历史</h3>
                    <div className="max-h-40 overflow-auto space-y-1">
                      {versions.map((v) => (
                        <div
                          key={v.version_id}
                          className="flex items-center justify-between text-xs py-1 px-2 bg-gray-50 rounded"
                        >
                          <span className="font-mono text-gray-600">
                            {v.version_id}
                          </span>
                          <div className="flex gap-2">
                            <span className="text-gray-400">{v.created_at}</span>
                            <span className="text-gray-400">
                              {v.size_bytes} bytes
                            </span>
                            {v.version_id !== "current" && (
                              <button
                                onClick={() => showDiff(v.version_id)}
                                className="text-blue-600 hover:underline"
                              >
                                Diff
                              </button>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* P2-2: YAML 校验结果 */}
                {validationResult && (
                  <div className={`mt-4 border-t pt-4 ${validationResult.valid ? '' : ''}`}>
                    <div className="flex items-center justify-between mb-2">
                      <h3 className="text-sm font-semibold">
                        {validationResult.valid ? "✅ 校验通过" : "❌ 校验失败"}
                      </h3>
                      <button onClick={() => setValidationResult(null)} className="text-xs text-gray-500 hover:underline">
                        关闭
                      </button>
                    </div>
                    {validationResult.errors.length > 0 && (
                      <div className="mb-2">
                        {validationResult.errors.map((err, i) => (
                          <div key={i} className="text-xs text-red-600 bg-red-50 px-2 py-1 rounded mb-1">❌ {err}</div>
                        ))}
                      </div>
                    )}
                    {validationResult.warnings.length > 0 && (
                      <div className="mb-2">
                        {validationResult.warnings.map((warn, i) => (
                          <div key={i} className="text-xs text-yellow-600 bg-yellow-50 px-2 py-1 rounded mb-1">⚠️ {warn}</div>
                        ))}
                      </div>
                    )}
                    <div className="text-xs text-gray-500">
                      规则数: {validationResult.actions_count} |
                      有 apiVersion: {validationResult.valid ? "✅" : "—"}
                    </div>
                  </div>
                )}

                {/* Diff 结果 */}
                {diffResult && (
                  <div className="mt-4 border-t pt-4">
                    <div className="flex items-center justify-between mb-2">
                      <h3 className="text-sm font-semibold">版本差异 (Diff)</h3>
                      <button
                        onClick={() => setDiffResult(null)}
                        className="text-xs text-gray-500 hover:underline"
                      >
                        关闭
                      </button>
                    </div>
                    <pre className="bg-gray-900 text-xs p-4 rounded-lg overflow-auto max-h-[300px] whitespace-pre-wrap">
                      {diffResult.split("\n").map((line, i) => (
                        <div
                          key={i}
                          className={
                            line.startsWith("+")
                              ? "text-green-400"
                              : line.startsWith("-")
                              ? "text-red-400"
                              : line.startsWith("@@")
                              ? "text-blue-400"
                              : "text-gray-300"
                          }
                        >
                          {line}
                        </div>
                      ))}
                    </pre>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
