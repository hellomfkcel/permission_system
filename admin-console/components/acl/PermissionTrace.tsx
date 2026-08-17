/**
 * PermissionTrace — 权限继承可视化组件（增强版）。
 */

"use client";

import { useState, useCallback } from "react";
import api from "@/lib/api";
import { useToast } from "@/components/shared/Toast";

interface EffectivePerm {
  principal: string;
  resource_type: string;
  resource_id: string;
  effective_actions: string[];
  source: string;
}

interface PermTreeNode {
  label: string;
  children: PermTreeNode[];
  detail?: string;
}

const SOURCE_CONFIG: Record<string, { bg: string; text: string; label: string; icon: string }> = {
  acl:           { bg: "bg-green-100",  text: "text-green-800",  label: "ACL 直接授予", icon: "🔑" },
  role_binding:  { bg: "bg-blue-100",   text: "text-blue-800",   label: "角色绑定继承", icon: "🎭" },
  combined:      { bg: "bg-purple-100", text: "text-purple-800", label: "ACL + 角色叠加", icon: "🔗" },
};

export default function PermissionTrace() {
  const { showToast } = useToast();
  const [principal, setPrincipal] = useState("");
  const [loading, setLoading] = useState(false);
  const [results, setResults] = useState<EffectivePerm[]>([]);
  const [viewMode, setViewMode] = useState<"list" | "tree">("list");
  const [expandedItems, setExpandedItems] = useState<Set<number>>(new Set());

  const handleTrace = useCallback(async () => {
    if (!principal.trim()) {
      showToast("warning", "请输入要追踪的主体");
      return;
    }
    setLoading(true);
    try {
      const res = await api.get<EffectivePerm[]>("/api/v1/acl/effective", {
        params: { principal: principal.trim() },
      });
      setResults(res.data);
      setExpandedItems(new Set());
      if (res.data.length === 0) {
        showToast("info", `未找到 ${principal.trim()} 的有效权限`);
      }
    } catch {
      showToast("error", "获取有效权限失败，请确认权限服务后端正常运行");
    } finally {
      setLoading(false);
    }
  }, [principal, showToast]);

  // 构建权限推导树
  const buildTree = (item: EffectivePerm): PermTreeNode => {
    const root: PermTreeNode = {
      label: `${item.resource_type}:${item.resource_id}`,
      children: [],
    };

    // 用户/组节点
    const identityNode: PermTreeNode = {
      label: item.principal,
      detail: `主体标识: ${item.principal}`,
      children: [],
    };

    if (item.source === "acl") {
      identityNode.children.push({
        label: "ACL 表直接授予",
        detail: `授予了 ${item.effective_actions.length} 个权限: ${item.effective_actions.join(", ")}`,
        children: item.effective_actions.map(a => ({
          label: a, detail: `权限: ${a}（直接授予）`, children: [],
        })),
      });
    } else if (item.source === "role_binding") {
      const roleNode: PermTreeNode = {
        label: "角色绑定",
        detail: "通过 Cerbos Derived Roles 获得隐式权限",
        children: [],
      };
      roleNode.children.push({
        label: "派生角色展开",
        detail: `Cerbos 根据 granted_actions 匹配派生角色条件`,
        children: item.effective_actions.map(a => ({
          label: a, detail: `权限: ${a}（角色继承）`, children: [],
        })),
      });
      identityNode.children.push(roleNode);
    } else if (item.source === "combined") {
      const aclNode: PermTreeNode = {
        label: "ACL 直接授予",
        detail: "部分权限来自 ACL 表",
        children: [],
      };
      const roleNode: PermTreeNode = {
        label: "角色绑定",
        detail: "部分权限来自角色绑定",
        children: [],
      };
      identityNode.children.push(aclNode, roleNode);
    }

    root.children.push(identityNode);
    return root;
  };

  const toggleExpand = (idx: number) => {
    setExpandedItems(prev => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx); else next.add(idx);
      return next;
    });
  };

  const renderTreeNode = (node: PermTreeNode, depth: number = 0): React.ReactNode => (
    <div className="ml-4 border-l-2 border-gray-200 pl-3 py-1">
      <div className="flex items-center gap-2">
        <span className="text-xs text-gray-400">{node.label.includes(":") ? "📦" : depth === 1 ? "👤" : depth === 2 ? "🔗" : "▸"}</span>
        <span className="text-sm font-medium text-gray-700">{node.label}</span>
      </div>
      {node.detail && (
        <p className="text-[11px] text-gray-400 mt-0.5 ml-5">{node.detail}</p>
      )}
      {node.children.map((child, i) => (
        <div key={i}>{renderTreeNode(child, depth + 1)}</div>
      ))}
    </div>
  );

  return (
    <div className="bg-white rounded-lg shadow p-6">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold">🔍 权限继承追踪</h2>
        <div className="flex gap-1">
          <button
            onClick={() => setViewMode("list")}
            className={`px-2.5 py-1 rounded text-xs font-medium ${viewMode === "list" ? "bg-blue-600 text-white" : "bg-gray-100 text-gray-600"}`}
          >
            列表
          </button>
          <button
            onClick={() => setViewMode("tree")}
            className={`px-2.5 py-1 rounded text-xs font-medium ${viewMode === "tree" ? "bg-blue-600 text-white" : "bg-gray-100 text-gray-600"}`}
          >
            树形
          </button>
        </div>
      </div>
      <p className="text-sm text-gray-500 mb-4">
        输入主体标识（如 <code className="bg-gray-100 px-1 rounded">user:alice</code> 或 <code className="bg-gray-100 px-1 rounded">group:eng</code>），追踪其有效权限的来源链路。
      </p>

      <div className="flex gap-3 mb-6">
        <input
          type="text"
          value={principal}
          onChange={e => setPrincipal(e.target.value)}
          onKeyDown={e => e.key === "Enter" && handleTrace()}
          placeholder="user:xxx | group:xxx | role:xxx"
          className="flex-1 border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono focus:ring-2 focus:ring-blue-500"
        />
        <button
          onClick={handleTrace}
          disabled={loading}
          className="bg-blue-600 text-white px-5 py-2 rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm font-medium transition-colors"
        >
          {loading ? "查询中..." : "追踪"}
        </button>
      </div>

      {results.length > 0 && (
        <div>
          {/* 统计摘要 */}
          <div className="grid grid-cols-4 gap-3 mb-4">
            <div className="bg-gray-50 rounded-lg p-3 text-center">
              <div className="text-xl font-bold text-gray-700">{results.length}</div>
              <div className="text-xs text-gray-500">有效权限</div>
            </div>
            {(["acl", "role_binding", "combined"] as const).map(source => {
              const count = results.filter(r => r.source === source).length;
              if (count === 0) return null;
              const cfg = SOURCE_CONFIG[source];
              return (
                <div key={source} className={`rounded-lg p-3 text-center ${cfg.bg}`}>
                  <div className={`text-xl font-bold ${cfg.text}`}>{count}</div>
                  <div className={`text-xs ${cfg.text} opacity-75`}>{cfg.label}</div>
                </div>
              );
            })}
          </div>

          {/* 权限条目 */}
          <div className="space-y-3">
            {results.map((item, idx) => {
              const cfg = SOURCE_CONFIG[item.source] || SOURCE_CONFIG.acl;
              const isExpanded = expandedItems.has(idx);
              return (
                <div key={idx} className="border border-gray-200 rounded-lg overflow-hidden">
                  {/* 摘要行 */}
                  <div
                    onClick={() => toggleExpand(idx)}
                    className="flex items-center justify-between p-3 bg-gray-50 cursor-pointer hover:bg-gray-100 transition-colors"
                  >
                    <div className="flex items-center gap-3">
                      <span className="text-lg">{cfg.icon}</span>
                      <div>
                        <code className="text-sm font-mono text-gray-700">
                          {item.resource_type}:{item.resource_id.slice(0, 24)}{item.resource_id.length > 24 ? "..." : ""}
                        </code>
                        <div className="flex gap-1 mt-1">
                          {item.effective_actions.map(a => (
                            <span key={a} className="bg-gray-200 text-gray-600 px-1.5 py-0.5 rounded text-[10px] font-mono">{a}</span>
                          ))}
                        </div>
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <span className={`${cfg.bg} ${cfg.text} px-2 py-0.5 rounded text-xs font-medium`}>{cfg.label}</span>
                      <span className="text-gray-400 text-sm">{isExpanded ? "▾" : "▸"}</span>
                    </div>
                  </div>

                  {/* 展开详情 */}
                  {isExpanded && (
                    <div className="p-4 border-t">
                      {viewMode === "tree" ? (
                        renderTreeNode(buildTree(item))
                      ) : (
                        <div className="space-y-3 text-sm">
                          <div className="grid grid-cols-2 gap-2 text-xs">
                            <div><span className="text-gray-500">主体：</span><code className="font-mono">{item.principal}</code></div>
                            <div><span className="text-gray-500">来源：</span><span className={`${cfg.text} font-medium`}>{cfg.label}</span></div>
                            <div><span className="text-gray-500">资源类型：</span><code>{item.resource_type}</code></div>
                            <div><span className="text-gray-500">资源 ID：</span><code className="font-mono">{item.resource_id}</code></div>
                          </div>
                          <div>
                            <h4 className="text-xs font-semibold text-gray-500 uppercase mb-1">权限推导路径</h4>
                            <div className="bg-gray-50 border rounded-lg p-3">
                              {item.source === "acl" && (
                                <div className="flex items-center gap-2 text-xs">
                                  <span className="bg-green-100 text-green-700 px-1.5 py-0.5 rounded font-mono">{item.principal}</span>
                                  <span className="text-gray-400">→</span>
                                  <span className="bg-gray-200 text-gray-700 px-1.5 py-0.5 rounded">ACL 表</span>
                                  <span className="text-gray-400">→</span>
                                  <span className="bg-gray-200 text-gray-700 px-1.5 py-0.5 rounded">{item.effective_actions.length} 个权限</span>
                                </div>
                              )}
                              {item.source === "role_binding" && (
                                <div className="flex items-center gap-2 text-xs flex-wrap">
                                  <span className="bg-blue-100 text-blue-700 px-1.5 py-0.5 rounded font-mono">{item.principal}</span>
                                  <span className="text-gray-400">→</span>
                                  <span className="bg-purple-100 text-purple-700 px-1.5 py-0.5 rounded">角色绑定</span>
                                  <span className="text-gray-400">→</span>
                                  <span className="bg-purple-100 text-purple-700 px-1.5 py-0.5 rounded">Cerbos 派生角色</span>
                                  <span className="text-gray-400">→</span>
                                  <span className="bg-purple-100 text-purple-700 px-1.5 py-0.5 rounded">隐式权限</span>
                                </div>
                              )}
                              {item.source === "combined" && (
                                <div className="space-y-1 text-xs">
                                  <div className="flex items-center gap-2">
                                    <span className="text-green-600 font-medium">ACL 路径:</span>
                                    <span className="font-mono">{item.principal}</span>
                                    <span className="text-gray-400">→</span>
                                    <span>直接授予</span>
                                  </div>
                                  <div className="flex items-center gap-2">
                                    <span className="text-blue-600 font-medium">角色路径:</span>
                                    <span className="font-mono">{item.principal}</span>
                                    <span className="text-gray-400">→</span>
                                    <span>角色绑定</span>
                                    <span className="text-gray-400">→</span>
                                    <span>派生角色展开</span>
                                  </div>
                                  <p className="text-gray-400 mt-1">两条路径权限合并去重后得到 {item.effective_actions.length} 个权限</p>
                                </div>
                              )}
                            </div>
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
