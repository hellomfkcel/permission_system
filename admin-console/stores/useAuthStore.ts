/**
 * Auth Store — 管理台登录状态管理 + Token 自动刷新。
 *
 * 设计依据：docs/外部系统设计.md §3 + docs/frontend-design.md §0 认证与租户。
 */

import { create } from "zustand";

interface UserInfo {
  user_id: string;
  tenant_id: string;
  roles: string[];
  groups: string[];
  principals: string[];
}

interface ProjectInfo {
  id: string;
  name: string;
  description: string;
  status: string;
}

interface AuthState {
  token: string | null;
  user: UserInfo | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  /** 当前选中的项目 ID */
  currentProjectId: string | null;
  /** 可用的项目列表 */
  availableProjects: ProjectInfo[];
  /** 初始化：从 localStorage 恢复会话 */
  initFromStorage: () => void;
  setAuth: (token: string, user: UserInfo) => void;
  logout: () => void;
  /** 设置当前项目 */
  setCurrentProject: (projectId: string) => void;
  /** 加载可用项目列表 */
  loadProjects: () => Promise<void>;
  /** 尝试 refresh token（仅在过期前 5 分钟触发） */
  tryRefreshToken: () => Promise<boolean>;
}

/** Keycloak OIDC token endpoint — 从环境变量读取，支持不同环境部署。
 *
 * 开发环境 (.env.local):
 *   NEXT_PUBLIC_KEYCLOAK_URL=http://192.168.1.127:8080
 *   NEXT_PUBLIC_KEYCLOAK_REALM=rag-v14
 *   NEXT_PUBLIC_KEYCLOAK_CLIENT_ID=admin-console
 *
 * 生产环境 (Docker Compose / K8s):
 *   通过 docker-compose.yml environment 或 K8s ConfigMap 注入。
 */
const KEYCLOAK_URL =
  process.env.NEXT_PUBLIC_KEYCLOAK_URL || "http://localhost:8080";
const KEYCLOAK_REALM =
  process.env.NEXT_PUBLIC_KEYCLOAK_REALM || "rag-v14";
const KEYCLOAK_CLIENT_ID =
  process.env.NEXT_PUBLIC_KEYCLOAK_CLIENT_ID || "admin-console";

export const useAuthStore = create<AuthState>((set, get) => ({
  token: null,
  user: null,
  isAuthenticated: false,
  isLoading: true,
  currentProjectId: null,
  availableProjects: [],

  initFromStorage: () => {
    if (typeof window === "undefined") {
      set({ isLoading: false });
      return;
    }
    try {
      const token = localStorage.getItem("admin_token");
      const userStr = localStorage.getItem("admin_user");
      if (token && userStr) {
        const user = JSON.parse(userStr) as UserInfo;
        // 检查 JWT 是否过期
        try {
          const payload = JSON.parse(atob(token.split(".")[1]));
          if (payload.exp && payload.exp * 1000 < Date.now()) {
            // token 已过期 — 尝试 refresh
            localStorage.removeItem("admin_token");
            localStorage.removeItem("admin_user");
            set({ token: null, user: null, isAuthenticated: false, isLoading: false });
            return;
          }
        } catch {
          // 无法解析 JWT，保留 token 但标记未认证
        }
        set({ token, user, isAuthenticated: true, isLoading: false });
      } else {
        set({ isLoading: false });
      }
    } catch {
      set({ isLoading: false });
    }
  },

  setAuth: (token: string, user: UserInfo) => {
    if (typeof window !== "undefined") {
      localStorage.setItem("admin_token", token);
      localStorage.setItem("admin_user", JSON.stringify(user));
    }
    set({ token, user, isAuthenticated: true });
  },

  logout: () => {
    if (typeof window !== "undefined") {
      localStorage.removeItem("admin_token");
      localStorage.removeItem("admin_user");
      localStorage.removeItem("admin_refresh_token");
      localStorage.removeItem("admin_token_expires_at");
      localStorage.removeItem("admin_current_project");
      document.cookie = "admin_session=; path=/; max-age=0";
    }
    set({ token: null, user: null, isAuthenticated: false, currentProjectId: null, availableProjects: [] });
  },

  setCurrentProject: (projectId: string) => {
    if (typeof window !== "undefined") {
      localStorage.setItem("admin_current_project", projectId);
    }
    set({ currentProjectId: projectId });
  },

  loadProjects: async () => {
    const { token } = get();
    if (!token) return;
    try {
      const resp = await fetch(
        `${process.env.NEXT_PUBLIC_PERMISSION_SERVICE_URL || "http://localhost:18080"}/api/v1/projects`,
        { headers: { Authorization: `Bearer ${token}` } }
      );
      if (!resp.ok) return;
      const projects: ProjectInfo[] = await resp.json();
      const activeProjects = projects.filter(p => p.status === "active");

      // Restore saved project selection or default to first
      let savedProject = typeof window !== "undefined"
        ? localStorage.getItem("admin_current_project")
        : null;
      if (savedProject && !activeProjects.find(p => p.id === savedProject)) {
        savedProject = null; // Saved project no longer available
      }
      const defaultProject = savedProject || activeProjects[0]?.id || null;

      set({
        availableProjects: activeProjects,
        currentProjectId: defaultProject,
      });
      if (defaultProject && typeof window !== "undefined") {
        localStorage.setItem("admin_current_project", defaultProject);
      }
    } catch {
      // Silently fail - project list is not critical for login
    }
  },

  tryRefreshToken: async () => {
    const refreshToken = typeof window !== "undefined"
      ? localStorage.getItem("admin_refresh_token")
      : null;
    if (!refreshToken) return false;

    try {
      const resp = await fetch(
        `${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/token`,
        {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({
            grant_type: "refresh_token",
            client_id: KEYCLOAK_CLIENT_ID,
            refresh_token: refreshToken,
          }),
        }
      );
      if (!resp.ok) return false;

      const data = await resp.json();
      const newToken = data.access_token as string;
      const newRefresh = data.refresh_token as string;

      const payload = JSON.parse(atob(newToken.split(".")[1]));
      const user = {
        user_id: payload.sub || get().user?.user_id || "unknown",
        tenant_id: payload.tenant || get().user?.tenant_id || "",
        roles: payload.realm_access?.roles || get().user?.roles || [],
        groups: payload.groups || get().user?.groups || [],
        principals: [
          `user:${payload.sub || get().user?.user_id || "unknown"}`,
          ...(payload.groups || get().user?.groups || []).map((g: string) => `group:${g}`),
        ],
      };

      localStorage.setItem("admin_token", newToken);
      if (newRefresh) localStorage.setItem("admin_refresh_token", newRefresh);
      localStorage.setItem("admin_user", JSON.stringify(user));

      set({ token: newToken, user, isAuthenticated: true });
      return true;
    } catch {
      return false;
    }
  },
}));
