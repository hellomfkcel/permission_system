/**
 * Platform Permissions Store — 管理台功能访问权限。
 *
 * 登录后调用 GET /api/v1/auth/me/access 获取当前管理员的平台功能权限。
 * 侧边栏使用此 store 决定显示哪些导航项及读写状态。
 *
 * 设计依据：平台级权限管理 Phase 3a。
 */

import { create } from "zustand";

interface PlatformAccess {
  user_id: string;
  roles: string[];
  is_platform_admin: boolean;
  features: Record<string, string>;       // feature_id → display_name
  permissions: Record<string, string[]>;  // feature_id → [actions]
  project_ids: string[];                  // 可访问的项目 ID (空=全部)
}

interface PlatformPermissionsState {
  access: PlatformAccess | null;
  isLoading: boolean;
  error: string | null;

  /** 从后端加载平台访问权限 */
  loadAccess: (token: string) => Promise<void>;

  /** 检查是否可以访问指定平台功能（至少 read） */
  canAccess: (featureId: string) => boolean;

  /** 检查是否可以修改指定平台功能（write） */
  canWrite: (featureId: string) => boolean;

  /** 检查是否有任何平台管理权限 */
  hasAnyAccess: () => boolean;

  /** 重置状态 */
  reset: () => void;
}

export const usePlatformPermissions = create<PlatformPermissionsState>(
  (set, get) => ({
    access: null,
    isLoading: false,
    error: null,

    loadAccess: async (token: string) => {
      set({ isLoading: true, error: null });
      try {
        const baseUrl =
          process.env.NEXT_PUBLIC_PERMISSION_SERVICE_URL ||
          "http://localhost:18080";
        const resp = await fetch(`${baseUrl}/api/v1/auth/me/access`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!resp.ok) {
          const errData = await resp.json().catch(() => ({}));
          throw new Error(
            (errData as Record<string, string>).detail || `HTTP ${resp.status}`
          );
        }
        const data: PlatformAccess = await resp.json();
        set({ access: data, isLoading: false });
      } catch (err: unknown) {
        const msg =
          err instanceof Error ? err.message : "Failed to load permissions";
        set({ error: msg, isLoading: false });
        console.warn("[usePlatformPermissions] loadAccess failed:", msg);
      }
    },

    canAccess: (featureId: string) => {
      const { access } = get();
      if (!access) return false;
      // platform_admin / system_admin → 全部可访问
      if (access.is_platform_admin) return true;
      const actions = access.permissions[featureId] || [];
      return (
        actions.includes("platform:read") ||
        actions.includes("platform:write")
      );
    },

    canWrite: (featureId: string) => {
      const { access } = get();
      if (!access) return false;
      // platform_admin / system_admin → 全部可写
      if (access.is_platform_admin) return true;
      const actions = access.permissions[featureId] || [];
      return actions.includes("platform:write");
    },

    hasAnyAccess: () => {
      const { access } = get();
      if (!access) return false;
      if (access.is_platform_admin) return true;
      return Object.keys(access.permissions).length > 0;
    },

    reset: () => {
      set({ access: null, isLoading: false, error: null });
    },
  })
);
