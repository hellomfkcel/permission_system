/**
 * 侧边导航栏 — 含用户信息、项目选择器和条件渲染的导航项。
 *
 * 设计依据：docs/外部系统设计.md §3.3 页面结构 + Phase 3c 平台级权限管理。
 */

"use client";

import { useState, useEffect } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useAuthStore } from "@/stores/useAuthStore";
import { usePlatformPermissions } from "@/stores/usePlatformPermissions";
import {
  LayoutDashboard,
  FolderOpen,
  Users,
  Key,
  Ban,
  Shield,
  History,
  FlaskConical,
  Settings,
  LogOut,
  ExternalLink,
  Building2,
  FileCode,
  Boxes,
  Eye,
} from "lucide-react";

/** 接入系统入口 URL（可配置） */
const EXTERNAL_SYSTEM_URL =
  process.env.NEXT_PUBLIC_EXTERNAL_SYSTEM_URL || process.env.NEXT_PUBLIC_RAG_SYSTEM_URL || "http://localhost:3000";

/** 导航项定义 — 每个导航项映射到一个平台功能资源 ID */
/** 导航项定义 — 每个导航项映射到一个平台功能资源 ID。
 *
 * platformOnly=true 的项仅在"平台管理"模式下显示，
 * 在具体项目视图中隐藏（如项目管理、租户管理、系统设置）。
 */
const NAV_ITEMS = [
  { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard, feature: "dashboard", platformOnly: false },
  { href: "/projects", label: "项目管理", icon: Boxes, feature: "project_mgmt", platformOnly: true },
  { href: "/tenants", label: "租户管理", icon: Building2, feature: "tenant_mgmt", platformOnly: true },
  { href: "/resources", label: "资源管理", icon: FolderOpen, feature: "resource_mgmt", platformOnly: false },
  { href: "/users-groups", label: "用户与组", icon: Users, feature: "user_mgmt", platformOnly: false },
  { href: "/roles", label: "角色管理", icon: Shield, feature: "role_mgmt", platformOnly: false },
  { href: "/permissions", label: "权限管理", icon: Key, feature: "permission_mgmt", platformOnly: false },
  { href: "/restrictions", label: "封禁管理", icon: Ban, feature: "restriction_mgmt", platformOnly: false },
  { href: "/policies", label: "策略管理", icon: FileCode, feature: "policy_mgmt", platformOnly: false },
  { href: "/audit", label: "审计日志", icon: History, feature: "audit_mgmt", platformOnly: false },
  { href: "/playground", label: "策略模拟", icon: FlaskConical, feature: "playground", platformOnly: false },
  { href: "/settings", label: "系统设置", icon: Settings, feature: "settings", platformOnly: true },
];

export default function Sidebar() {
  const pathname = usePathname();
  const router = useRouter();
  const { user, logout, currentProjectId, availableProjects, setCurrentProject, loadProjects, token } = useAuthStore();
  const { access, canAccess, canWrite, loadAccess } = usePlatformPermissions();

  const handleLogout = () => {
    logout();
    if (typeof window !== "undefined") {
      document.cookie = "admin_session=; path=/; max-age=0";
    }
    router.push("/login");
  };

  // Load projects and platform permissions on mount
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    if (!loaded) {
      loadProjects();
      // Load platform permissions
      const storedToken = token || (typeof window !== "undefined" ? localStorage.getItem("admin_token") : null);
      if (storedToken) {
        loadAccess(storedToken);
      }
      setLoaded(true);
    }
  }, [loaded, loadProjects, loadAccess, token]);

  const currentProject = availableProjects.find(p => p.id === currentProjectId);
  const isPlatformMode = !currentProjectId || currentProjectId === "__all__" || currentProjectId === "";

  return (
    <aside className="w-56 min-h-screen bg-gray-900 text-white flex flex-col">
      <div className="px-4 py-4 border-b border-gray-700">
        <h1 className="text-lg font-bold">🔐 权限管理台</h1>
        {/* 项目选择器 */}
        <div className="mt-2">
          <select
            value={currentProjectId || "__all__"}
            onChange={(e) => {
              const val = e.target.value;
              if (val === "__all__") {
                setCurrentProject("__all__");
              } else {
                setCurrentProject(val);
              }
            }}
            className="w-full bg-gray-800 border border-gray-600 rounded px-2 py-1 text-xs text-gray-200 focus:border-blue-500 focus:outline-none"
          >
            <option value="__all__">🌐 平台管理</option>
            {availableProjects.length === 0 && (
              <option value="" disabled>无可用项目</option>
            )}
            {availableProjects.map((p) => (
              <option key={p.id} value={p.id}>
                📦 {p.name} ({p.id})
              </option>
            ))}
          </select>
        </div>
        {/* 当前视图提示 */}
        <div className="mt-1 text-xs text-gray-500">
          {isPlatformMode ? "平台级视图 — 全部项目" : `项目视图 — ${currentProject?.name || currentProjectId}`}
        </div>
      </div>

      <nav className="flex-1 py-4">
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon;
          const isActive = pathname === item.href;
          const hasAccess = canAccess(item.feature);
          const hasWrite = canWrite(item.feature);

          // 平台专属项在项目模式下隐藏
          if (item.platformOnly && !isPlatformMode) return null;
          // 无访问权限 → 不显示
          if (access && !hasAccess) return null;

          return (
            <Link
              key={item.href}
              href={hasAccess ? item.href : "#"}
              className={`flex items-center gap-3 px-4 py-2.5 text-sm transition-colors ${
                isActive
                  ? "bg-blue-600 text-white"
                  : hasAccess
                    ? "text-gray-300 hover:bg-gray-800 hover:text-white"
                    : "text-gray-600 cursor-not-allowed"
              }`}
              onClick={(e) => {
                if (!hasAccess) e.preventDefault();
              }}
              title={hasWrite ? `${item.label} (可修改)` : hasAccess ? `${item.label} (只读)` : "无权限"}
            >
              <Icon size={18} />
              {item.label}
              {access && hasAccess && !hasWrite && (
                <span title="只读"><Eye size={12} className="ml-auto text-gray-500" /></span>
              )}
            </Link>
          );
        })}
      </nav>

      {/* 接入系统跳转入口 */}
      <div className="px-4 py-2 border-t border-gray-700">
        <a
          href={EXTERNAL_SYSTEM_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-3 px-3 py-2 text-sm text-gray-400 hover:text-white hover:bg-gray-800 rounded transition-colors"
          title="打开接入系统"
        >
          <ExternalLink size={16} />
          打开接入系统
        </a>
      </div>

      {/* 用户信息 + 退出 */}
      <div className="px-4 py-3 border-t border-gray-700">
        {user && (
          <div className="mb-2">
            <p className="text-sm text-gray-300 truncate" title={user.user_id}>
              👤 {user.user_id}
            </p>
            <p className="text-xs text-gray-500 truncate">{user.tenant_id}</p>
          </div>
        )}
        <button
          onClick={handleLogout}
          className="flex items-center gap-2 w-full px-3 py-2 text-sm text-gray-400 hover:text-white hover:bg-gray-800 rounded transition-colors"
        >
          <LogOut size={16} />
          退出登录
        </button>
      </div>
    </aside>
  );
}
