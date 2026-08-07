/**
 * AuthGuard — 路由保护 + 管理员角色校验组件。
 *
 * 检查用户是否已登录（有效的 JWT token）且具有管理员角色（system_admin 或 admin）。
 * 未登录 → 重定向到 /login；已登录但非管理员 → 显示"无权限"页面。
 * 登录页本身 (/login, /auth/callback) 不需要保护。
 *
 * 设计依据：frontend-design.md §0 认证与租户
 *          + docs/权限管理系统架构设计.md §2.2 角色层级。
 */

"use client";

import { useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useAuthStore } from "@/stores/useAuthStore";

const PUBLIC_PATHS = ["/login", "/auth/callback"];
const ADMIN_ROLES = ["system_admin", "admin"];

export default function AuthGuard({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading, user, initFromStorage } = useAuthStore();
  const pathname = usePathname();
  const router = useRouter();

  // 初始化：从 localStorage 恢复会话
  useEffect(() => {
    initFromStorage();
  }, [initFromStorage]);

  // 路由保护：未登录 → 跳转 /login；已登录非管理员 → 显示无权限
  useEffect(() => {
    if (isLoading) return;

    const isPublic = PUBLIC_PATHS.includes(pathname);

    if (!isAuthenticated && !isPublic) {
      router.replace("/login");
    }

    // 已登录用户访问 /login → 跳转 dashboard
    if (isAuthenticated && isPublic) {
      router.replace("/dashboard");
    }
  }, [isAuthenticated, isLoading, pathname, router]);

  // 加载中不渲染（防止闪烁）
  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-gray-100">
        <div className="text-center">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600 mx-auto" />
          <p className="text-gray-500 mt-4 text-sm">加载中...</p>
        </div>
      </div>
    );
  }

  // 公开页面直接渲染
  if (PUBLIC_PATHS.includes(pathname)) {
    return <>{children}</>;
  }

  // 未认证则不渲染（将被重定向）
  if (!isAuthenticated) {
    return null;
  }

  // ★ 管理员角色检查：非管理员用户显示无权限页面
  const isAdminUser = user?.roles?.some((r: string) => ADMIN_ROLES.includes(r));
  if (!isAdminUser) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-gray-100">
        <div className="bg-white rounded-xl shadow-lg p-12 text-center max-w-md">
          <div className="text-6xl mb-4">🔒</div>
          <h1 className="text-2xl font-bold text-gray-900 mb-2">需要管理员权限</h1>
          <p className="text-gray-500 mb-6">
            权限管理台仅对系统管理员开放。如需访问，请联系管理员授予 system_admin 或 admin 角色。
          </p>
          <button
            onClick={() => {
              localStorage.removeItem("admin_token");
              localStorage.removeItem("admin_user");
              document.cookie = "admin_session=; path=/; max-age=0";
              router.replace("/login");
            }}
            className="inline-block px-6 py-2.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition font-medium"
          >
            返回登录 →
          </button>
        </div>
      </div>
    );
  }

  return <>{children}</>;
}
