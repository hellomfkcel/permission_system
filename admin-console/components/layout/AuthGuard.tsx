/**
 * AuthGuard — 路由保护组件。
 *
 * 检查用户是否已登录（有效的 JWT token），未登录则重定向到 /login。
 * 登录页本身 (/login) 不需要保护。
 *
 * 设计依据：frontend-design.md §0 认证与租户。
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

  // 初始化：从 localStorage 恢复会话
  useEffect(() => {
    initFromStorage();
  }, [initFromStorage]);

  // 路由保护：未登录 → 跳转 /login
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

  return <>{children}</>;
}
