/**
 * PolicySimulator — 策略模拟器 (Playground) 组件。
 *
 * 场景预设、资源类型、动作全部从后端实际数据动态生成。
 * 平台模式生成平台功能场景；项目模式生成该项目资源的场景。
 */

"use client";

import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { useAuthStore } from "@/stores/useAuthStore";

// ── 资源类型标签（从后台 /auth/config 动态获取，此处仅作 SSR 兜底）──
const RESOURCE_TYPE_LABELS_FALLBACK: Record<string, string> = {
  kb: "知识库", document: "文档", platform: "平台功能",
};

interface ScenarioPreset {
  name: string;
  description: string;
  principal: object;
  action: string;
  resource: object;
}

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
  const { currentProjectId } = useAuthStore();
  const isPlatformMode = !currentProjectId || currentProjectId === "__all__";

  const [mode, setMode] = useState<"json" | "credential">("json");
  const [principal, setPrincipal] = useState("{}");
  const [credential, setCredential] = useState("");
  const [action, setAction] = useState("");
  const [resourceKind, setResourceKind] = useState("");
  const [resourceId, setResourceId] = useState("");
  const [resourceAttr, setResourceAttr] = useState("{}");
  const [result, setResult] = useState<SimulateResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [history, setHistory] = useState<SimulateResult[]>([]);

  // ── 动态数据 ──
  const [actionGroups, setActionGroups] = useState<Record<string, string[]>>({});
  const [availableResourceTypes, setAvailableResourceTypes] = useState<string[]>([]);
  const [resourceTypeLabels, setResourceTypeLabels] = useState<Record<string, string>>({});
  const [scenarios, setScenarios] = useState<ScenarioPreset[]>([]);
  const [sampleUsers, setSampleUsers] = useState<Array<{id: string; roles: string[]}>>([]);

  // ── 加载所有数据 ──
  //     resource_actions 和 resource_type_labels 均从 /auth/config 动态获取，无硬编码。
  const loadAllData = useCallback(async () => {
    try {
      const [configRes, resourcesRes, rolesRes, usersRes] = await Promise.all([
        api.get("/api/v1/auth/config"),
        api.get("/api/v1/resources"),
        api.get("/api/v1/roles/definitions"),
        api.get("/api/v1/auth/users"),
      ]);

      // 从 /auth/config 获取动态配置
      const resourceActions: Record<string, string[]> = configRes.data.resource_actions || {};
      const typeLabels: Record<string, string> = configRes.data.resource_type_labels || {};
      setResourceTypeLabels(typeLabels);

      // 动作分组（用于 action 下拉 optgroup）
      const byType: Record<string, string[]> = {};
      // 平台功能分组始终存在
      byType["平台功能"] = resourceActions["platform"] || ["platform:read", "platform:write"];

      // 项目模式：添加该项目实际存在的资源类型对应的动作
      if (!isPlatformMode) {
        const existingTypes = new Set(
          (resourcesRes.data as Array<{resource_type: string}>).map(r => r.resource_type)
        );
        for (const [rt, actions] of Object.entries(resourceActions)) {
          if (rt === "platform") continue;
          if (existingTypes.has(rt)) {
            const label = typeLabels[rt] || rt;
            const group = `${label} 资源`;
            byType[group] = actions;
          }
        }
      }
      setActionGroups(byType);

      // 资源类型
      const types = new Set((resourcesRes.data as Array<{resource_type: string}>).map(r => r.resource_type));
      if (isPlatformMode) types.add("platform");
      const typeList = Array.from(types).sort();
      setAvailableResourceTypes(typeList);

      // 角色列表
      const roleDefs = rolesRes.data as Array<{name: string; is_system: boolean}>;

      // 用户列表（取前5个做样本）
      const users = (usersRes.data as Array<{user_id: string; roles: string[]}>).slice(0, 5);
      setSampleUsers(users.map(u => ({ id: u.user_id, roles: u.roles || [] })));

      // ── 数据驱动生成场景 ──
      const generated: ScenarioPreset[] = [];

      // 平台场景
      if (isPlatformMode) {
        generated.push({
          name: "管理员访问平台功能",
          description: "platform_admin → ALLOW",
          principal: { id: "user:admin", roles: ["system_admin", "platform_admin"], attr: { tenant_id: "tenant-dev" } },
          action: "platform:read",
          resource: { kind: "platform", id: "audit_mgmt", attr: { retired: false } },
        });
        generated.push({
          name: "观察者修改平台功能",
          description: "platform_viewer → DENY",
          principal: { id: "user:viewer", roles: ["platform_viewer"], attr: { tenant_id: "tenant-dev" } },
          action: "platform:write",
          resource: { kind: "platform", id: "audit_mgmt", attr: { retired: false } },
        });
      }

      // 为每种实际存在的资源类型生成场景（从 resource_actions 直接取对应类型的动作列表）
      for (const rt of typeList) {
        if (rt === "platform") continue;

        const rtLabel = typeLabels[rt] || rt;
        const actions = resourceActions[rt] || [];
        if (actions.length === 0) continue;

        const sampleAction = actions[0];
        const sampleAction2 = actions.length > 1 ? actions[1] : actions[0];
        const adminRoles = users.find(u => u.roles?.includes("system_admin"))?.roles || ["system_admin"];
        const sampleUser = users.find(u => !u.roles?.includes("system_admin")) || { id: "alice", roles: ["user"] };

        // 管理员场景
        generated.push({
          name: `管理员操作${rtLabel}`,
          description: `system_admin ${sampleAction} → ALLOW`,
          principal: { id: "user:admin", roles: adminRoles, attr: { tenant_id: "tenant-dev" } },
          action: sampleAction,
          resource: { kind: rt, id: `${rt}-sample`, attr: { retired: false } },
        });

        // 无权限场景
        generated.push({
          name: `普通用户操作${rtLabel}`,
          description: `${sampleUser.id} ${sampleAction2} → DENY`,
          principal: { id: `user:${sampleUser.id}`, roles: sampleUser.roles, attr: { tenant_id: "tenant-dev" } },
          action: sampleAction2,
          resource: { kind: rt, id: `${rt}-sample`, attr: { retired: false } },
        });

        // 退役资源场景
        generated.push({
          name: `退役${rtLabel}访问`,
          description: "retired=true → DENY",
          principal: { id: "user:admin", roles: adminRoles, attr: { tenant_id: "tenant-dev" } },
          action: sampleAction,
          resource: { kind: rt, id: `${rt}-retired`, attr: { retired: true } },
        });

        if (generated.length > 15) break;
      }

      setScenarios(generated);

      // 设置默认值
      if (typeList.length > 0) setResourceKind(typeList[0]);
      const firstGroup = Object.keys(byType)[0];
      if (firstGroup && byType[firstGroup].length > 0) setAction(byType[firstGroup][0]);
      if (users.length > 0) {
        const u = users[0];
        setPrincipal(JSON.stringify({
          id: `user:${u.user_id}`, roles: u.roles, attr: { tenant_id: "tenant-dev" },
        }, null, 2));
      }
      setResourceAttr(JSON.stringify({ retired: false }, null, 2));
    } catch (err: unknown) {
      console.error("[PolicySimulator] loadAllData failed:", err);
      setActionGroups({});
      setAvailableResourceTypes([]);
      setResourceTypeLabels({});
      setScenarios([]);
      setSampleUsers([]);
    }
  }, [isPlatformMode]);

  useEffect(() => { loadAllData(); }, [loadAllData]);

  const loadScenario = (s: ScenarioPreset) => {
    setPrincipal(JSON.stringify(s.principal, null, 2));
    setAction(s.action);
    const res = s.resource as Record<string, unknown>;
    setResourceKind((res.kind as string) || "");
    setResourceId((res.id as string) || "");
    setResourceAttr(JSON.stringify(res.attr || {}, null, 2));
    setResult(null);
    setError("");
  };

  const handleSimulate = async () => {
    setLoading(true); setError("");
    try {
      let body: Record<string, unknown>;
      if (mode === "credential" && credential.trim()) {
        body = { credential: credential.trim(), action, resource: { kind: resourceKind, id: resourceId, attr: JSON.parse(resourceAttr || "{}") } };
      } else {
        body = { principal: JSON.parse(principal), action, resource: { kind: resourceKind, id: resourceId, attr: JSON.parse(resourceAttr || "{}") } };
      }
      const res = await api.post<SimulateResult>("/api/v1/simulate", body);
      setResult(res.data);
      setHistory(prev => [res.data, ...prev].slice(0, 20));
    } catch (err: unknown) {
      setError(err instanceof SyntaxError ? "JSON 格式错误" : ((err as {response?:{data?:{detail?:string}}})?.response?.data?.detail || "模拟执行失败"));
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
      {/* 数据驱动场景预设 */}
      <div className="mb-6">
        <h2 className="text-sm font-semibold text-gray-600 mb-3">
          场景预设（{isPlatformMode ? "平台 + 项目" : "项目"} — 从实际数据生成，点击加载）
        </h2>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-2">
          {scenarios.map(s => (
            <button key={s.name} onClick={() => loadScenario(s)}
              className="text-left p-3 bg-white border border-gray-200 rounded-lg hover:border-blue-400 hover:shadow-sm transition-all text-sm">
              <div className="font-medium truncate text-xs">{s.name}</div>
              <div className="text-[11px] text-gray-500 mt-1 line-clamp-2">{s.description}</div>
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-white rounded-lg shadow p-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="font-semibold text-gray-800">输入</h2>
            <div className="flex gap-1">
              <button onClick={() => setMode("json")} className={`px-2.5 py-1 rounded text-xs font-medium ${mode === "json" ? "bg-blue-600 text-white" : "bg-gray-100 text-gray-600"}`}>JSON</button>
              <button onClick={() => setMode("credential")} className={`px-2.5 py-1 rounded text-xs font-medium ${mode === "credential" ? "bg-blue-600 text-white" : "bg-gray-100 text-gray-600"}`}>JWT</button>
            </div>
          </div>
          <div className="space-y-4">
            {mode === "json" ? (
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Principal (JSON)</label>
                <textarea value={principal} onChange={e => setPrincipal(e.target.value)}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono" rows={5} spellCheck={false} />
              </div>
            ) : (
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">JWT Credential</label>
                <textarea value={credential} onChange={e => setCredential(e.target.value)}
                  placeholder="粘贴 JWT token" className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono" rows={3} spellCheck={false} />
              </div>
            )}
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Action</label>
              <select value={action} onChange={e => setAction(e.target.value)} className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
                {Object.entries(actionGroups).map(([group, actions]) => (
                  <optgroup key={group} label={group}>{actions.map(a => <option key={a} value={a}>{a}</option>)}</optgroup>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">资源类型</label>
              <select value={resourceKind} onChange={e => setResourceKind(e.target.value)} className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm">
                {availableResourceTypes.map(rt => (
                  <option key={rt} value={rt}>{rt}（{resourceTypeLabels[rt] || RESOURCE_TYPE_LABELS_FALLBACK[rt] || rt}）</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">资源 ID</label>
              <input type="text" value={resourceId} onChange={e => setResourceId(e.target.value)}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono" />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Resource Attr (JSON)</label>
              <input type="text" value={resourceAttr} onChange={e => setResourceAttr(e.target.value)}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono" />
            </div>
            {error && <div className="bg-red-50 border border-red-200 rounded-lg px-3 py-2 text-xs text-red-700">{error}</div>}
            <button onClick={handleSimulate} disabled={loading}
              className="w-full bg-blue-600 text-white py-2.5 rounded-lg hover:bg-blue-700 font-medium disabled:opacity-50">
              {loading ? "⏳ 执行中..." : "🔬 执行模拟"}
            </button>
          </div>
        </div>

        <div className="bg-white rounded-lg shadow p-6">
          <h2 className="font-semibold text-gray-800 mb-4">判定结果</h2>
          {result ? (
            <div className="space-y-4">
              <div className={`p-4 rounded-lg border text-center ${decisionStyle(result.decision)}`}>
                <div className="text-2xl font-bold">
                  {result.decision === "allow" ? "✅ ALLOW" : result.decision === "deny" ? "❌ DENY" : "⚠️ INDETERMINATE"}
                </div>
                {result.cerbos_verdict && <div className="text-xs mt-1 opacity-75">Cerbos: {result.cerbos_verdict}</div>}
              </div>
              {result.decision_id && (
                <div className="text-xs"><span className="text-gray-500">Decision ID: </span>
                  <code className="bg-gray-100 px-2 py-0.5 rounded text-[11px] break-all">{result.decision_id}</code></div>
              )}
              {result.matched_rules && result.matched_rules.length > 0 && (
                <div><h3 className="text-sm font-medium text-gray-700 mb-2">匹配规则</h3>
                  <ul className="space-y-1">{result.matched_rules.map((rule, i) => (
                    <li key={i} className="text-xs bg-blue-50 border border-blue-100 rounded px-2 py-1.5 text-blue-800">{rule}</li>
                  ))}</ul>
                </div>
              )}
              {result.principal_summary && (
                <details className="text-xs"><summary className="cursor-pointer text-gray-500 hover:text-gray-700 font-medium">Principal 详情</summary>
                  <pre className="mt-1 bg-gray-50 p-2 rounded text-[11px] overflow-auto max-h-40 border">{JSON.stringify(result.principal_summary, null, 2)}</pre></details>
              )}
              {result.resource_summary && (
                <details className="text-xs"><summary className="cursor-pointer text-gray-500 hover:text-gray-700 font-medium">Resource 详情</summary>
                  <pre className="mt-1 bg-gray-50 p-2 rounded text-[11px] overflow-auto max-h-40 border">{JSON.stringify(result.resource_summary, null, 2)}</pre></details>
              )}
            </div>
          ) : (
            <div className="text-center py-16 text-gray-400"><div className="text-4xl mb-3">🔬</div><p>选择场景或填写参数后</p><p>点击&ldquo;执行模拟&rdquo;</p></div>
          )}
          {history.length > 1 && (
            <div className="mt-6 pt-4 border-t">
              <h3 className="text-xs font-semibold text-gray-500 uppercase mb-2">历史 ({history.length})</h3>
              <div className="space-y-1 max-h-32 overflow-y-auto">
                {history.slice(1, 11).map((h, i) => (
                  <div key={i} className="flex items-center justify-between text-xs py-1">
                    <span className="font-mono text-gray-600">{h.action}</span>
                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${h.decision === "allow" ? "bg-green-100 text-green-700" : "bg-red-100 text-red-700"}`}>{h.decision}</span>
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
