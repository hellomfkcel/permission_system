/** Dashboard — 权限概览统计 + 最近变更时间线 + 待处理告警（P1-7 补全） */
"use client";

import { useEffect, useState } from "react";
import { FolderOpen, FileText, Users, Key, History, Ban, AlertTriangle, Clock } from "lucide-react";
import api from "@/lib/api";

interface DashboardStats {
  kb_count: number;
  document_count: number;
  user_count: number;
  acl_count: number;
  recent_changes: number;
  active_restrictions: number;
}

interface RecentChangeItem {
  event_type: string;
  resource_type: string;
  resource_id: string;
  tenant_id: string;
  version: number;
  change_summary: string;
  created_at: string;
}

interface RecentChangesData {
  changes: RecentChangeItem[];
  orphan_acl_count: number;
  expired_acl_count: number;
}

interface StatCardProps {
  title: string;
  value: string | number;
  icon: React.ReactNode;
  description?: string;
  color?: string;
}

function StatCard({ title, value, icon, description, color }: StatCardProps) {
  return (
    <div className="bg-white rounded-lg shadow p-6">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-gray-500">{title}</p>
          <p className="text-3xl font-bold mt-1">{value}</p>
          {description && <p className="text-xs text-gray-400 mt-1">{description}</p>}
        </div>
        <div className={color || "text-blue-500"}>{icon}</div>
      </div>
    </div>
  );
}

const EVENT_LABELS: Record<string, string> = {
  ACL_GRANTED: "权限授予",
  ACL_REVOKED: "权限回收",
  ROLE_BOUND: "角色绑定",
  ROLE_UNBOUND: "角色解绑",
  RESTRICTION_ADDED: "封禁添加",
  RESTRICTION_REMOVED: "封禁解除",
  RESOURCE_REGISTERED: "资源登记",
  RESOURCE_LINKED: "挂载建立",
  RESOURCE_UNLINKED: "挂载解除",
  RESOURCE_RETIRED: "资源退役",
  RESOURCE_ATTR_UPDATED: "属性更新",
  OWNERSHIP_TRANSFERRED: "所有权转移",
};

function formatTime(isoString: string): string {
  try {
    const d = new Date(isoString);
    const now = new Date();
    const diffMin = Math.floor((now.getTime() - d.getTime()) / 60000);
    if (diffMin < 1) return "刚刚";
    if (diffMin < 60) return `${diffMin} 分钟前`;
    const diffHr = Math.floor(diffMin / 60);
    if (diffHr < 24) return `${diffHr} 小时前`;
    return d.toLocaleDateString("zh-CN");
  } catch {
    return isoString;
  }
}

export default function DashboardPage() {
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [changesData, setChangesData] = useState<RecentChangesData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const loadData = async () => {
    setLoading(true);
    setError("");
    try {
      const [statsRes, changesRes] = await Promise.all([
        api.get("/api/v1/auth/stats"),
        api.get("/api/v1/auth/recent-changes"),
      ]);
      setStats(statsRes.data);
      setChangesData(changesRes.data);
    } catch {
      setError("无法加载统计数据");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
  }, []);

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">📊 Dashboard</h1>
        <button
          onClick={loadData}
          className="text-sm text-blue-600 hover:text-blue-800 transition-colors"
        >
          🔄 刷新
        </button>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg px-4 py-3 mb-6 text-sm text-red-700">
          {error}
        </div>
      )}

      {loading && !stats ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {[...Array(6)].map((_, i) => (
            <div key={i} className="bg-white rounded-lg shadow p-6 animate-pulse">
              <div className="h-4 bg-gray-200 rounded w-1/2 mb-3" />
              <div className="h-8 bg-gray-200 rounded w-1/3" />
            </div>
          ))}
        </div>
      ) : (
        <>
          {/* P1-7: 待处理告警面板 */}
          {changesData && (changesData.expired_acl_count > 0 || changesData.orphan_acl_count > 0) && (
            <div className="mb-6 space-y-2">
              {changesData.expired_acl_count > 0 && (
                <div className="bg-yellow-50 border border-yellow-200 rounded-lg px-4 py-3 flex items-center gap-3">
                  <Clock size={18} className="text-yellow-600 flex-shrink-0" />
                  <span className="text-sm text-yellow-800">
                    <strong>{changesData.expired_acl_count}</strong> 条 ACL 已过期但未回收 — 建议清理
                  </span>
                </div>
              )}
              {changesData.orphan_acl_count > 0 && (
                <div className="bg-red-50 border border-red-200 rounded-lg px-4 py-3 flex items-center gap-3">
                  <AlertTriangle size={18} className="text-red-600 flex-shrink-0" />
                  <span className="text-sm text-red-800">
                    <strong>{changesData.orphan_acl_count}</strong> 条孤儿权限 — 资源已退役但 ACL 未清理
                  </span>
                </div>
              )}
            </div>
          )}

          {/* 统计卡片 */}
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            <StatCard
              title="知识库数"
              value={stats?.kb_count ?? "-"}
              icon={<FolderOpen size={32} />}
              description="活跃 KB"
              color="text-blue-500"
            />
            <StatCard
              title="文档数"
              value={stats?.document_count ?? "-"}
              icon={<FileText size={32} />}
              description="已注册文档"
              color="text-purple-500"
            />
            <StatCard
              title="用户数"
              value={stats?.user_count ?? "-"}
              icon={<Users size={32} />}
              description="同步自 Keycloak"
              color="text-green-500"
            />
            <StatCard
              title="ACL 条目"
              value={stats?.acl_count ?? "-"}
              icon={<Key size={32} />}
              description="活跃权限"
              color="text-yellow-500"
            />
            <StatCard
              title="最近变更"
              value={stats?.recent_changes ?? "-"}
              icon={<History size={32} />}
              description="24 小时内"
              color="text-indigo-500"
            />
            <StatCard
              title="活跃封禁"
              value={stats?.active_restrictions ?? "-"}
              icon={<Ban size={32} />}
              description="生效中"
              color="text-red-500"
            />
          </div>

          {/* P1-7: 最近变更时间线 */}
          <div className="mt-8 grid grid-cols-1 lg:grid-cols-2 gap-6">
            <div className="bg-white rounded-lg shadow p-6">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-lg font-semibold flex items-center gap-2">
                  <History size={20} className="text-gray-400" />
                  最近变更时间线
                </h2>
                {changesData && (
                  <span className="text-xs text-gray-400">
                    最近 {changesData.changes.length} 条
                  </span>
                )}
              </div>
              {changesData && changesData.changes.length > 0 ? (
                <div className="space-y-3 max-h-96 overflow-y-auto">
                  {changesData.changes.map((change) => (
                    <div
                      key={`${change.version}-${change.event_type}`}
                      className="flex items-start gap-3 pb-3 border-b border-gray-50 last:border-0"
                    >
                      <span className="inline-block w-2 h-2 mt-2 rounded-full bg-blue-400 flex-shrink-0" />
                      <div className="flex-1 min-w-0">
                        <p className="text-sm font-medium text-gray-700 truncate">
                          {EVENT_LABELS[change.event_type] || change.event_type}
                        </p>
                        <p className="text-xs text-gray-500 truncate">
                          {change.change_summary}
                        </p>
                        {change.resource_type && (
                          <p className="text-xs text-gray-400">
                            {change.resource_type}:{change.resource_id.slice(0, 12)}...
                          </p>
                        )}
                      </div>
                      <span className="text-xs text-gray-400 flex-shrink-0">
                        {formatTime(change.created_at)}
                      </span>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-gray-400 text-center py-8">
                  暂无变更记录
                </p>
              )}
            </div>

            {/* P1-7: 告警面板 */}
            <div className="bg-white rounded-lg shadow p-6">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-lg font-semibold flex items-center gap-2">
                  <AlertTriangle size={20} className="text-gray-400" />
                  待处理告警
                </h2>
              </div>
              <div className="space-y-3">
                <div className="flex items-center justify-between p-3 bg-gray-50 rounded-lg">
                  <div>
                    <p className="text-sm font-medium">过期 ACL</p>
                    <p className="text-xs text-gray-500">已过期但未自动回收的权限条目</p>
                  </div>
                  <span className={`text-lg font-bold ${(changesData?.expired_acl_count ?? 0) > 0 ? "text-yellow-600" : "text-green-600"}`}>
                    {changesData?.expired_acl_count ?? "—"}
                  </span>
                </div>
                <div className="flex items-center justify-between p-3 bg-gray-50 rounded-lg">
                  <div>
                    <p className="text-sm font-medium">孤儿权限</p>
                    <p className="text-xs text-gray-500">指向已退役资源的 ACL 条目</p>
                  </div>
                  <span className={`text-lg font-bold ${(changesData?.orphan_acl_count ?? 0) > 0 ? "text-red-600" : "text-green-600"}`}>
                    {changesData?.orphan_acl_count ?? "—"}
                  </span>
                </div>
                <div className="flex items-center justify-between p-3 bg-gray-50 rounded-lg">
                  <div>
                    <p className="text-sm font-medium">活跃封禁</p>
                    <p className="text-xs text-gray-500">当前生效中的封禁规则</p>
                  </div>
                  <span className="text-lg font-bold text-gray-600">
                    {stats?.active_restrictions ?? "—"}
                  </span>
                </div>
              </div>
            </div>
          </div>

          {/* 快捷入口 */}
          <div className="mt-8 grid grid-cols-1 md:grid-cols-3 gap-6">
            <a
              href="/permissions"
              className="block p-5 bg-white rounded-lg shadow hover:shadow-md transition-shadow border border-gray-100"
            >
              <h3 className="font-medium">🔑 权限管理</h3>
              <p className="text-sm text-gray-500 mt-1">授予/回收用户和组的访问权限</p>
            </a>
            <a
              href="/resources"
              className="block p-5 bg-white rounded-lg shadow hover:shadow-md transition-shadow border border-gray-100"
            >
              <h3 className="font-medium">📁 资源浏览</h3>
              <p className="text-sm text-gray-500 mt-1">查看所有已注册的 KB 和文档资源</p>
            </a>
            <a
              href="/audit"
              className="block p-5 bg-white rounded-lg shadow hover:shadow-md transition-shadow border border-gray-100"
            >
              <h3 className="font-medium">🔍 审计日志</h3>
              <p className="text-sm text-gray-500 mt-1">查看权限变更历史和判定记录</p>
            </a>
          </div>
        </>
      )}
    </div>
  );
}
