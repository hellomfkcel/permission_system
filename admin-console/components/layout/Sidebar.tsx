/**
 * 侧边导航栏 — 含用户信息和退出登录。
 *
 * 设计依据：docs/外部系统设计.md §3.3 页面结构 + frontend-design.md §0 全局 Header。
 */

"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useAuthStore } from "@/stores/useAuthStore";
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
} from "lucide-react";

/** RAG 系统入口 URL — 从环境变量读取，用于"返回 RAG 系统"跳转。
 *
 * 设计依据：docs/外部系统设计.md §3.4.3 RAG 系统跳转入口对接（双向跳转）
 *          + frontend-design.md §3.7 管理台跳转入口。
 */
const RAG_SYSTEM_URL =
  process.env.NEXT_PUBLIC_RAG_SYSTEM_URL || "http://192.168.1.127:3000";

const NAV_ITEMS = [
  { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { href: "/resources", label: "资源管理", icon: FolderOpen },
  { href: "/users-groups", label: "用户与组", icon: Users },
  { href: "/permissions", label: "权限管理", icon: Key },
  { href: "/restrictions", label: "封禁管理", icon: Ban },
  { href: "/policies", label: "策略管理", icon: Shield },
  { href: "/audit", label: "审计日志", icon: History },
  { href: "/playground", label: "策略模拟", icon: FlaskConical },
  { href: "/settings", label: "系统设置", icon: Settings },
];

export default function Sidebar() {
  const pathname = usePathname();
  const router = useRouter();
  const { user, logout } = useAuthStore();

  const handleLogout = () => {
    logout();
    // 清除 cookie
    if (typeof window !== "undefined") {
      document.cookie = "admin_session=; path=/; max-age=0";
    }
    router.push("/login");
  };

  return (
    <aside className="w-56 min-h-screen bg-gray-900 text-white flex flex-col">
      <div className="px-4 py-6 border-b border-gray-700">
        <h1 className="text-lg font-bold">🔐 权限管理台</h1>
        <p className="text-xs text-gray-400 mt-1">RAG v14 Permission</p>
      </div>

      <nav className="flex-1 py-4">
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon;
          const isActive = pathname === item.href;
          return (
            <Link
              key={item.href}
              href={item.href}
              className={`flex items-center gap-3 px-4 py-2.5 text-sm transition-colors ${
                isActive
                  ? "bg-blue-600 text-white"
                  : "text-gray-300 hover:bg-gray-800 hover:text-white"
              }`}
            >
              <Icon size={18} />
              {item.label}
            </Link>
          );
        })}
      </nav>

      {/* RAG 系统跳转入口 — 双向导航 */}
      <div className="px-4 py-2 border-t border-gray-700">
        <a
          href={RAG_SYSTEM_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-3 px-3 py-2 text-sm text-gray-400 hover:text-white hover:bg-gray-800 rounded transition-colors"
          title="打开 RAG 知识库管理系统"
        >
          <ExternalLink size={16} />
          返回 RAG 系统
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
