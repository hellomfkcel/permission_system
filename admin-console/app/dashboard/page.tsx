/** Dashboard — 数据驱动的权限概览统计 + 最近变更时间线。

平台模式：按 resource_type 动态展示全部项目的资源统计 + 项目总数。
项目模式：按 resource_type 动态展示该项目的资源统计。

所有统计来自后端 GROUP BY 查询，新增资源类型无需修改前端代码。
*/

"use client";

import { useEffect, useState } from "react";
import {
  FolderOpen, FileText, Users, Key, History, Ban,
  AlertTriangle, Clock, Boxes, Shield, Globe,
} from "lucide-react";
import api from "@/lib/api";
import { useAuthStore } from "@/stores/useAuthStore";

// ── 数据驱动统计模型 ──

interface ResourceStat {
  resource_type: string;
  label: string;
  count: number;
}

interface RestrictionStat {
  restriction_type: string;
  label: string;
  count: number;
}

interface DashboardStats {
  project_count: number;
  resource_stats: ResourceStat[];
  user_count: number;
  acl_count: number;
  restriction_stats: RestrictionStat[];
  recent_changes: number;
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

// ── 资源类型 → 图标/颜色映射（按 resource_type 动态匹配）──

const RESOURCE_ICONS: Record<string, React.ReactNode> = {
  kb: <FolderOpen size={32} />,
  document: <FileText size={32} />,
  platform: <Shield size={32} />,
};

const RESOURCE_COLORS: Record<string, string> = {
  kb: "text-blue-500",
  document: "text-purple-500",
  platform: "text-orange-500",
};

const RESTRICTION_ICONS: Record<string, React.ReactNode> = {
  subject_ban: <Ban size={32} />,
  resource_restriction: <AlertTriangle size={32} />,
};

const RESTRICTION_COLORS: Record<string, string> = {
  subject_ban: "text-red-500",
  resource_restriction: "text-yellow-500",
};

// ── 通用统计卡片组件 ──

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
          {description && (
            <p className="text-xs text-gray-400 mt-1">{description}</p>
          )}
        </div>
        <div className={color || "text-blue-500"}>{icon}</div>
      </div>
    </div>
  );
}

// ── 事件标签 ──

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

// ══════════════════════════════════════════════════════════════

export default function DashboardPage() {
  const { currentProjectId } = useAuthStore();
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [changesData, setChangesData] = useState<RecentChangesData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const isPlatformMode =
    !currentProjectId || currentProjectId === "__all__";

  const loadData = async () => {
    setLoading(true);
    setError("");
    try {
      const params: Record<string, string> = {};
      if (!isPlatformMode) {
        params.project_id = currentProjectId;
      }
      const [statsRes, changesRes] = await Promise.all([
        api.get<DashboardStats>("/api/v1/auth/stats", { params }),
        api.get<RecentChangesData>("/api/v1/auth/recent-changes", { params }),
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
  }, [currentProjectId]);

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">
          📊 {isPlatformMode ? "平台概览" : "项目概览"}
        </h1>
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
            <div
              key={i}
              className="bg-white rounded-lg shadow p-6 animate-pulse"
            >
              <div className="h-4 bg-gray-200 rounded w-1/2 mb-3" />
              <div className="h-8 bg-gray-200 rounded w-1/3" />
            </div>
          ))}
        </div>
      ) : (
        <>
          {/* P1-7: 待处理告警 */}
          {changesData &&
            (changesData.expired_acl_count > 0 ||
              changesData.orphan_acl_count > 0) && (
              <div className="mb-6 space-y-2">
                {changesData.expired_acl_count > 0 && (
                  <div className="bg-yellow-50 border border-yellow-200 rounded-lg px-4 py-3 flex items-center gap-3">
                    <Clock size={18} className="text-yellow-600 flex-shrink-0" />
                    <span className="text-sm text-yellow-800">
                      <strong>{changesData.expired_acl_count}</strong>{" "}
                      条 ACL 已过期但未回收 — 建议清理
                    </span>
                  </div>
                )}
                {changesData.orphan_acl_count > 0 && (
                  <div className="bg-red-50 border border-red-200 rounded-lg px-4 py-3 flex items-center gap-3">
                    <AlertTriangle
                      size={18}
                      className="text-red-600 flex-shrink-0"
                    />
                    <span className="text-sm text-red-800">
                      <strong>{changesData.orphan_acl_count}</strong>{" "}
                      条孤儿权限 — 资源已退役但 ACL 未清理
                    </span>
                  </div>
                )}
              </div>
            )}

          {/* ══════════════════════════════════════════════════ */}
          {/* 数据驱动统计卡片 — 按后端返回动态渲染 */}
          {/* ══════════════════════════════════════════════════ */}
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {/* 平台模式：项目总数 */}
            {isPlatformMode && stats && stats.project_count > 0 && (
              <StatCard
                title="项目总数"
                value={stats.project_count}
                icon={<Boxes size={32} />}
                description="活跃项目"
                color="text-cyan-500"
              />
            )}

            {/* 资源统计：按 resource_type GROUP BY 动态渲染 */}
            {stats?.resource_stats.map((rs) => (
              <StatCard
                key={rs.resource_type}
                title={rs.label}
                value={rs.count}
                icon={RESOURCE_ICONS[rs.resource_type] ?? <Globe size={32} />}
                description={`活跃 ${rs.label}`}
                color={RESOURCE_COLORS[rs.resource_type] ?? "text-gray-500"}
              />
            ))}

            {/* 用户总数 */}
            <StatCard
              title="用户数"
              value={stats?.user_count ?? "-"}
              icon={<Users size={32} />}
              description={isPlatformMode ? "同步自 Keycloak" : "项目成员"}
              color="text-green-500"
            />

            {/* ACL 条目 */}
            <StatCard
              title="ACL 条目"
              value={stats?.acl_count ?? "-"}
              icon={<Key size={32} />}
              description="活跃权限"
              color="text-yellow-600"
            />

            {/* 封禁统计：按 restriction_type GROUP BY 动态渲染 */}
            {stats?.restriction_stats.map((rs) => (
              <StatCard
                key={rs.restriction_type}
                title={rs.label}
                value={rs.count}
                icon={
                  RESTRICTION_ICONS[rs.restriction_type] ?? (
                    <Ban size={32} />
                  )
                }
                description="生效中"
                color={
                  RESTRICTION_COLORS[rs.restriction_type] ?? "text-red-500"
                }
              />
            ))}

            {/* 24h 变更 */}
            <StatCard
              title="最近变更"
              value={stats?.recent_changes ?? "-"}
              icon={<History size={32} />}
              description="24 小时内"
              color="text-indigo-500"
            />
          </div>

          {/* ══════════════════════════════════════════════════ */}
          {/* 最近变更时间线 + 告警面板 */}
          {/* ══════════════════════════════════════════════════ */}
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
                            {change.resource_type}:
                            {change.resource_id.slice(0, 12)}...
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
                    <p className="text-xs text-gray-500">
                      已过期但未自动回收的权限条目
                    </p>
                  </div>
                  <span
                    className={`text-lg font-bold ${
                      (changesData?.expired_acl_count ?? 0) > 0
                        ? "text-yellow-600"
                        : "text-green-600"
                    }`}
                  >
                    {changesData?.expired_acl_count ?? "—"}
                  </span>
                </div>
                <div className="flex items-center justify-between p-3 bg-gray-50 rounded-lg">
                  <div>
                    <p className="text-sm font-medium">孤儿权限</p>
                    <p className="text-xs text-gray-500">
                      指向已退役资源的 ACL 条目
                    </p>
                  </div>
                  <span
                    className={`text-lg font-bold ${
                      (changesData?.orphan_acl_count ?? 0) > 0
                        ? "text-red-600"
                        : "text-green-600"
                    }`}
                  >
                    {changesData?.orphan_acl_count ?? "—"}
                  </span>
                </div>
                {/* 动态封禁告警 */}
                {stats?.restriction_stats.map((rs) => (
                  <div
                    key={`alert-${rs.restriction_type}`}
                    className="flex items-center justify-between p-3 bg-gray-50 rounded-lg"
                  >
                    <div>
                      <p className="text-sm font-medium">{rs.label}</p>
                      <p className="text-xs text-gray-500">
                        当前生效中的{rs.label}规则
                      </p>
                    </div>
                    <span className="text-lg font-bold text-gray-600">
                      {rs.count}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {/* 快捷入口 — 数据驱动：仅显示有统计的资源类型 */}
          <div className="mt-8 grid grid-cols-1 md:grid-cols-3 gap-6">
            <a
              href="/permissions"
              className="block p-5 bg-white rounded-lg shadow hover:shadow-md transition-shadow border border-gray-100"
            >
              <h3 className="font-medium">🔑 权限管理</h3>
              <p className="text-sm text-gray-500 mt-1">
                授予/回收用户和组的访问权限
              </p>
            </a>
            <a
              href="/resources"
              className="block p-5 bg-white rounded-lg shadow hover:shadow-md transition-shadow border border-gray-100"
            >
              <h3 className="font-medium">📁 资源浏览</h3>
              <p className="text-sm text-gray-500 mt-1">
                查看所有已注册的资源
              </p>
            </a>
            <a
              href="/audit"
              className="block p-5 bg-white rounded-lg shadow hover:shadow-md transition-shadow border border-gray-100"
            >
              <h3 className="font-medium">🔍 审计日志</h3>
              <p className="text-sm text-gray-500 mt-1">
                查看权限变更历史和判定记录
              </p>
            </a>
          </div>
        </>
      )}
    </div>
  );
}
