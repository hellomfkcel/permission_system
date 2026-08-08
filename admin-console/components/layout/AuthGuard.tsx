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

export default function AuthGuard({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading, initFromStorage } = useAuthStore();
  const pathname = usePathname();
  const router = useRouter();

  useEffect(() => {
    initFromStorage();
  }, [initFromStorage]);

  useEffect(() => {
    if (isLoading) return;
    const isPublic = PUBLIC_PATHS.includes(pathname);
    if (!isAuthenticated && !isPublic) {
      router.replace("/login");
    }
    if (isAuthenticated && isPublic) {
      router.replace("/dashboard");
    }
  }, [isAuthenticated, isLoading, pathname, router]);

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

  if (PUBLIC_PATHS.includes(pathname)) {
    return <>{children}</>;
  }

  if (!isAuthenticated) {
    return null;
  }

  // 鉴权由后端 API 负责，前端不再检查角色
  return <>{children}</>;
}
