/**
 * API 客户端 — Axios 实例，baseURL 指向权限服务后端。
 *
 * 设计依据：docs/外部系统设计.md §3.2 管理台技术选型。
 */

import axios from "axios";

const api = axios.create({
  baseURL:
    process.env.NEXT_PUBLIC_PERMISSION_SERVICE_URL || "http://localhost:18080",
  timeout: 30000,
  headers: {
    "Content-Type": "application/json",
  },
});

// 请求拦截器：自动注入 JWT token + 过期检查 + 项目上下文
api.interceptors.request.use((config) => {
  if (typeof window !== "undefined") {
    const token = localStorage.getItem("admin_token");
    // 自动附加项目上下文（如果已选择具体项目且不是"平台管理"模式）
    const currentProject = localStorage.getItem("admin_current_project");
    // __all__ 或空字符串 = 平台管理模式，不自动注入 project_id
    if (
      currentProject &&
      currentProject !== "__all__" &&
      currentProject !== "" &&
      !config.params?.project_id
    ) {
      if (!config.params) config.params = {};
      if (!config.params.project_id) {
        config.params.project_id = currentProject;
      }
    }
    if (token) {
      // JWT 过期检查（提前 5 分钟警告）
      try {
        const payload = JSON.parse(atob(token.split(".")[1]));
        if (payload.exp) {
          const expMs = payload.exp * 1000;
          const now = Date.now();
          if (expMs < now) {
            // Token 已过期
            localStorage.removeItem("admin_token");
            localStorage.removeItem("admin_user");
            document.cookie = "admin_session=; path=/; max-age=0";
            window.location.href = "/login";
            return Promise.reject(new Error("Token expired"));
          } else if (expMs - now < 300_000) {
            // 5 分钟内过期 → 控制台警告
            console.warn(
              `JWT token expires in ${Math.round((expMs - now) / 1000)}s. Please refresh your token.`
            );
          }
        }
      } catch {
        // 无法解析 JWT，继续（可能不是标准 JWT 格式）
      }
      config.headers.Authorization = `Bearer ${token}`;
    }
  }
  return config;
});

/** 显示用户可见的错误提示。
 *
 * 通过全局 Toast（ToastProvider 注册）展示，避免在拦截器中引入 React 依赖。
 * 403/503 等错误对用户可见，帮助用户理解当前状态并采取行动。
 */
/** 扩展 Window 类型以支持全局 Toast。 */
declare global {
  interface Window {
    __globalToast?: (type: "success" | "error" | "warning" | "info", message: string) => void;
  }
}

function showUserError(message: string) {
  if (typeof window !== "undefined" && window.__globalToast) {
    // 全局 Toast（React ToastProvider 已挂载）
    window.__globalToast("error", message);
  } else {
    // 回退：ToastProvider 未挂载时使用 console
    console.warn("[api] Toast unavailable, fallback:", message);
  }
}

// 响应拦截器：401 → 跳登录，403 → Toast 提示，503 → Toast 提示
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (typeof window !== "undefined") {
      if (error.response?.status === 401) {
        localStorage.removeItem("admin_token");
        localStorage.removeItem("admin_user");
        window.location.href = "/login";
      } else if (error.response?.status === 403) {
        const detail = error.response?.data?.detail || error.response?.data?.message || "";
        const resourceInfo = error.config?.url
          ? ` (${error.config.method?.toUpperCase() || "GET"} ${error.config.url})`
          : "";
        showUserError(
          detail
            ? `权限不足: ${detail}`
            : `您没有执行此操作的权限${resourceInfo}。如需申请权限，请前往管理台设置页面。`
        );
      } else if (error.response?.status === 503) {
        showUserError(
          "服务暂时不可用，请稍后重试。如持续出现此问题，请联系系统管理员。"
        );
      }
    }
    return Promise.reject(error);
  }
);

export default api;
