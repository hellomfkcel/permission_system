/**
 * PolicySimulator — 策略模拟器 (Playground) 组件。
 *
 * 设计依据：docs/外部系统设计.md §3.4.2 策略模拟器 (Playground)
 *          + docs/权限管理系统架构设计.md §2.1 动词目录（16 个 action）
 *
 * 功能：
 * - JSON 编辑 Principal / Resource 属性
 * - 全部 16 个 action 选择（按资源类型分组）
 * - 8 个场景预设快速加载
 * - 匹配规则展示 + Principal/Resource 摘要
 * - JWT Credential 模式（真实用户身份模拟）
 */

"use client";

import { useState } from "react";
import api from "@/lib/api";

// ── 场景预设 ──

interface ScenarioPreset {
  name: string;
  description: string;
  principal: object;
  action: string;
  resource: object;
}

const SCENARIOS: ScenarioPreset[] = [
  {
    name: "管理员查看自己的 KB",
    description: "system_admin 读取知识库 → ALLOW",
    principal: { id: "user:admin", roles: ["system_admin", "user"], attr: { tenant_id: "tenant-dev", granted_actions: { "kb-001": ["read", "write", "manage"] } } },
    action: "kb:read",
    resource: { kind: "kb", id: "kb-001", attr: { retired: false } },
  },
  {
    name: "普通用户读取 KB",
    description: "user + kb_reader → ALLOW",
    principal: { id: "user:alice", roles: ["user"], attr: { tenant_id: "tenant-dev", granted_actions: { "kb-public": ["read"] } } },
    action: "kb:read",
    resource: { kind: "kb", id: "kb-public", attr: { retired: false } },
  },
  {
    name: "无权限用户写入 KB",
    description: "user 但无 kb:write → DENY",
    principal: { id: "user:bob", roles: ["user"], attr: { tenant_id: "tenant-dev", granted_actions: { "kb-public": ["read"] } } },
    action: "kb:write",
    resource: { kind: "kb", id: "kb-public", attr: { retired: false } },
  },
  {
    name: "文档查看 (doc:view)",
    description: "kb_reader 查看启用文档 → ALLOW",
    principal: { id: "user:alice", roles: ["user"], attr: { tenant_id: "tenant-dev", granted_actions: { "kb-docs": ["read"] } } },
    action: "doc:view",
    resource: { kind: "document", id: "doc-001", attr: { retired: false, is_enabled: true, kb_id: "kb-docs" } },
  },
  {
    name: "已退役文档查看",
    description: "文档 retired=true → DENY",
    principal: { id: "user:admin", roles: ["system_admin"], attr: { tenant_id: "tenant-dev", granted_actions: {} } },
    action: "doc:view",
    resource: { kind: "document", id: "doc-retired", attr: { retired: true, is_enabled: true, kb_id: "kb-docs" } },
  },
  {
    name: "文档下载 (doc:download)",
    description: "allow_download=true → ALLOW",
    principal: { id: "user:alice", roles: ["user"], attr: { tenant_id: "tenant-dev", granted_actions: { "kb-docs": ["read"] } } },
    action: "doc:download",
    resource: { kind: "document", id: "doc-001", attr: { retired: false, is_enabled: true, allow_download: true, kb_id: "kb-docs" } },
  },
  {
    name: "型一封禁用户检索",
    description: "被封禁用户 → DENY (pre-deny)",
    principal: { id: "user:mallory", roles: ["user"], attr: { tenant_id: "tenant-dev", granted_actions: { "kb-public": ["read"] } } },
    action: "doc:retrieve",
    resource: { kind: "document", id: "doc-001", attr: { retired: false, is_enabled: true, kb_id: "kb-public" } },
  },
  {
    name: "KB 管理 (kb:manage)",
    description: "kb_admin 派生 → ALLOW",
    principal: { id: "user:admin", roles: ["user"], attr: { tenant_id: "tenant-dev", granted_actions: { "kb-001": ["read", "write", "manage"] } } },
    action: "kb:manage",
    resource: { kind: "kb", id: "kb-001", attr: { retired: false } },
  },
];

// ── Action 分组 ──

const ACTION_GROUPS: Record<string, string[]> = {
  "KB 资源": ["kb:read", "kb:write", "kb:manage", "kb:grant"],
  "Document 资源": ["doc:view", "doc:download", "doc:retrieve", "doc:unmount", "doc:purge", "doc:share"],
};

// ── 组件 ──

interface SimulateResult {
  decision: string;
  decision_id?: string;
  resource_id?: string;
  action?: string;
  matched_rules?: string[];
  cerbos_verdict?: string;
  principal_summary?: Record<string, unknown>;
  resource_summary?: Record<string, unknown>;
}

export default function PolicySimulator() {
  const [mode, setMode] = useState<"json" | "credential">("json");
  const [principal, setPrincipal] = useState(
    JSON.stringify({ id: "user:alice", roles: ["user"], attr: {} }, null, 2)
  );
  const [credential, setCredential] = useState("");
  const [action, setAction] = useState("kb:read");
  const [resourceKind, setResourceKind] = useState("kb");
  const [resourceId, setResourceId] = useState("test-kb");
  const [resourceAttr, setResourceAttr] = useState('{"retired":false}');
  const [result, setResult] = useState<SimulateResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [history, setHistory] = useState<SimulateResult[]>([]);

  // 加载场景预设
  const loadScenario = (scenario: ScenarioPreset) => {
    setPrincipal(JSON.stringify(scenario.principal, null, 2));
    setAction(scenario.action);
    const res = scenario.resource as Record<string, unknown>;
    setResourceKind((res.kind as string) || "kb");
    setResourceId((res.id as string) || "test");
    setResourceAttr(JSON.stringify(res.attr || {}, null, 2));
    setResult(null);
    setError("");
  };

  // 执行模拟
  const handleSimulate = async () => {
    setLoading(true);
    setError("");
    try {
      let body: Record<string, unknown>;

      if (mode === "credential" && credential.trim()) {
        // JWT Credential 模式：用真实用户身份
        body = { credential: credential.trim(), action, resource: { kind: resourceKind, id: resourceId, attr: JSON.parse(resourceAttr) } };
      } else {
        // JSON Principal 模式
        const principalObj = JSON.parse(principal);
        body = { principal: principalObj, action, resource: { kind: resourceKind, id: resourceId, attr: JSON.parse(resourceAttr) } };
      }

      const res = await api.post<SimulateResult>("/api/v1/simulate", body);
      const simResult = res.data;
      setResult(simResult);
      setHistory(prev => [simResult, ...prev].slice(0, 20));
    } catch (err: unknown) {
      if (err instanceof SyntaxError) {
        setError("JSON 格式错误 — 请检查 Principal 或 Resource 属性的 JSON 语法");
      } else {
        const e = err as { response?: { data?: { detail?: string } } };
        setError(e?.response?.data?.detail || "模拟执行失败，请确认权限服务后端正常运行");
      }
    }
    setLoading(false);
  };

  const decisionStyle = (d: string) => {
    switch (d) {
      case "allow": return "bg-green-100 text-green-800 border-green-300";
      case "deny": return "bg-red-100 text-red-800 border-red-300";
      default: return "bg-yellow-100 text-yellow-800 border-yellow-300";
    }
  };

  return (
    <div>
      {/* 场景预设 */}
      <div className="mb-6">
        <h2 className="text-sm font-semibold text-gray-600 mb-3">场景预设（点击加载）</h2>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-2">
          {SCENARIOS.map(s => (
            <button
              key={s.name}
              onClick={() => loadScenario(s)}
              className="text-left p-3 bg-white border border-gray-200 rounded-lg hover:border-blue-400 hover:shadow-sm transition-all text-sm"
            >
              <div className="font-medium truncate text-xs">{s.name}</div>
              <div className="text-[11px] text-gray-500 mt-1 line-clamp-2">{s.description}</div>
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* 输入面板 */}
        <div className="bg-white rounded-lg shadow p-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="font-semibold text-gray-800">输入</h2>
            <div className="flex gap-1">
              <button
                onClick={() => setMode("json")}
                className={`px-2.5 py-1 rounded text-xs font-medium ${mode === "json" ? "bg-blue-600 text-white" : "bg-gray-100 text-gray-600"}`}
              >
                JSON
              </button>
              <button
                onClick={() => setMode("credential")}
                className={`px-2.5 py-1 rounded text-xs font-medium ${mode === "credential" ? "bg-blue-600 text-white" : "bg-gray-100 text-gray-600"}`}
              >
                JWT
              </button>
            </div>
          </div>

          <div className="space-y-4">
            {mode === "json" ? (
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Principal (JSON)</label>
                <textarea
                  value={principal}
                  onChange={e => setPrincipal(e.target.value)}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono focus:ring-2 focus:ring-blue-500"
                  rows={5}
                  spellCheck={false}
                />
              </div>
            ) : (
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">JWT Credential</label>
                <textarea
                  value={credential}
                  onChange={e => setCredential(e.target.value)}
                  placeholder="粘贴 JWT token（eyJhbGci...）或 ctx_token（ctx.eyJ...）"
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono focus:ring-2 focus:ring-blue-500"
                  rows={3}
                  spellCheck={false}
                />
                <p className="text-xs text-gray-400 mt-1">
                  使用真实 JWT 模拟特定用户的权限判定结果
                </p>
              </div>
            )}

            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Action</label>
              <select
                value={action}
                onChange={e => setAction(e.target.value)}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
              >
                {Object.entries(ACTION_GROUPS).map(([group, actions]) => (
                  <optgroup key={group} label={group}>
                    {actions.map(a => <option key={a} value={a}>{a}</option>)}
                  </optgroup>
                ))}
              </select>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">资源类型</label>
                <select
                  value={resourceKind}
                  onChange={e => setResourceKind(e.target.value)}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
                >
                  <option value="kb">kb（知识库）</option>
                  <option value="document">document（文档）</option>
                </select>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">资源 ID</label>
                <input
                  type="text"
                  value={resourceId}
                  onChange={e => setResourceId(e.target.value)}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono"
                />
              </div>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Resource Attr (JSON)</label>
              <input
                type="text"
                value={resourceAttr}
                onChange={e => setResourceAttr(e.target.value)}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono"
              />
              <p className="text-xs text-gray-400 mt-1">
                {resourceKind === "kb" ? '例: {"retired":false}' : '例: {"retired":false,"is_enabled":true,"kb_id":"kb-1"}'}
              </p>
            </div>

            {error && (
              <div className="bg-red-50 border border-red-200 rounded-lg px-3 py-2 text-xs text-red-700">{error}</div>
            )}

            <button
              onClick={handleSimulate}
              disabled={loading}
              className="w-full bg-blue-600 text-white py-2.5 rounded-lg hover:bg-blue-700 font-medium disabled:opacity-50 transition-colors"
            >
              {loading ? "⏳ 执行中..." : "🔬 执行模拟"}
            </button>
          </div>
        </div>

        {/* 结果面板 */}
        <div className="bg-white rounded-lg shadow p-6">
          <h2 className="font-semibold text-gray-800 mb-4">判定结果</h2>

          {result ? (
            <div className="space-y-4">
              {/* 判决 */}
              <div className={`p-4 rounded-lg border text-center ${decisionStyle(result.decision)}`}>
                <div className="text-2xl font-bold">
                  {result.decision === "allow" ? "✅ ALLOW" : result.decision === "deny" ? "❌ DENY" : "⚠️ INDETERMINATE"}
                </div>
                {result.cerbos_verdict && (
                  <div className="text-xs mt-1 opacity-75">Cerbos: {result.cerbos_verdict}</div>
                )}
              </div>

              {/* Decision ID */}
              {result.decision_id && (
                <div className="text-xs">
                  <span className="text-gray-500">Decision ID: </span>
                  <code className="bg-gray-100 px-2 py-0.5 rounded text-[11px] break-all">{result.decision_id}</code>
                </div>
              )}

              {/* 匹配规则 */}
              {result.matched_rules && result.matched_rules.length > 0 && (
                <div>
                  <h3 className="text-sm font-medium text-gray-700 mb-2">匹配规则</h3>
                  <ul className="space-y-1">
                    {result.matched_rules.map((rule, i) => (
                      <li key={i} className="text-xs bg-blue-50 border border-blue-100 rounded px-2 py-1.5 text-blue-800">
                        {rule}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {/* Principal 摘要 */}
              {result.principal_summary && (
                <details className="text-xs">
                  <summary className="cursor-pointer text-gray-500 hover:text-gray-700 font-medium">Principal 详情</summary>
                  <pre className="mt-1 bg-gray-50 p-2 rounded text-[11px] overflow-auto max-h-40 border">
                    {JSON.stringify(result.principal_summary, null, 2)}
                  </pre>
                </details>
              )}

              {/* Resource 摘要 */}
              {result.resource_summary && (
                <details className="text-xs">
                  <summary className="cursor-pointer text-gray-500 hover:text-gray-700 font-medium">Resource 详情</summary>
                  <pre className="mt-1 bg-gray-50 p-2 rounded text-[11px] overflow-auto max-h-40 border">
                    {JSON.stringify(result.resource_summary, null, 2)}
                  </pre>
                </details>
              )}
            </div>
          ) : (
            <div className="text-center py-16 text-gray-400">
              <div className="text-4xl mb-3">🔬</div>
              <p>选择场景预设或填写参数后</p>
              <p>点击&ldquo;执行模拟&rdquo;查看 Cerbos PDP 判定</p>
            </div>
          )}

          {/* 历史记录 */}
          {history.length > 1 && (
            <div className="mt-6 pt-4 border-t">
              <h3 className="text-xs font-semibold text-gray-500 uppercase mb-2">历史 ({history.length})</h3>
              <div className="space-y-1 max-h-32 overflow-y-auto">
                {history.slice(1, 11).map((h, i) => (
                  <div key={i} className="flex items-center justify-between text-xs py-1">
                    <span className="font-mono text-gray-600">{h.action}</span>
                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
                      h.decision === "allow" ? "bg-green-100 text-green-700" : "bg-red-100 text-red-700"
                    }`}>{h.decision}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
