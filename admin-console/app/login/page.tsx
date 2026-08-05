/** 登录页 — 开发模式（dev-login）+ 生产模式（Keycloak SSO）。

设计依据：docs/外部系统设计.md §4.1 + docs/frontend-design.md §0 认证与租户。
*/

"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useAuthStore } from "@/stores/useAuthStore";

// 开发模式登录端点：优先使用权限服务自带的 dev-login，失败时回退到 RAG API
const PERM_DEV_LOGIN = (process.env.NEXT_PUBLIC_PERMISSION_SERVICE_URL || "http://localhost:18080") + "/api/v1/auth/dev-login";
const RAG_DEV_LOGIN = process.env.NEXT_PUBLIC_RAG_API_URL
  ? `${process.env.NEXT_PUBLIC_RAG_API_URL}/api/v1/auth/dev-login`
  : "http://localhost:8000/api/v1/auth/dev-login";

const KEYCLOAK_URL = process.env.NEXT_PUBLIC_KEYCLOAK_URL || "http://192.168.1.127:8080";
const KEYCLOAK_REALM = process.env.NEXT_PUBLIC_KEYCLOAK_REALM || "rag-v14";
const KEYCLOAK_CLIENT_ID = process.env.NEXT_PUBLIC_KEYCLOAK_CLIENT_ID || "admin-console";
const REDIRECT_URI =
  typeof window !== "undefined"
    ? `${window.location.origin}/auth/callback`
    : "http://192.168.1.127:3002/auth/callback";

export default function LoginPage() {
  const router = useRouter();
  const { setAuth } = useAuthStore();

  // Dev mode state
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [tenant, setTenant] = useState("tenant-dev");
  const [devLoading, setDevLoading] = useState(false);
  const [devError, setDevError] = useState("");

  // SSO state
  const [ssoLoading, setSsoLoading] = useState(false);

  const handleDevLogin = async () => {
    if (!username.trim()) {
      setDevError("请输入用户名");
      return;
    }
    setDevLoading(true);
    setDevError("");

    try {
      // 优先使用权限服务自带的 dev-login，失败时回退到 RAG dev-login
      let resp = await fetch(PERM_DEV_LOGIN, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username.trim(), password, tenant: tenant.trim() }),
      });

      // 回退到 RAG dev-login
      if (!resp.ok) {
        resp = await fetch(RAG_DEV_LOGIN, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username: username.trim(), password, tenant: tenant.trim() }),
        });
      }

      if (!resp.ok) {
        const errData = await resp.json().catch(() => ({}));
        throw new Error((errData as Record<string, string>).detail || `HTTP ${resp.status}`);
      }

      const data = await resp.json();
      const token = data.access_token as string;
      const user = {
        user_id: (data.user as Record<string, string>).id || username.trim(),
        tenant_id: (data.user as Record<string, string>).tenant_id || tenant.trim(),
        roles: ((data.user as Record<string, string[]>).roles) || ["user"],
        groups: ((data.user as Record<string, string[]>).groups) || [],
        principals: [`user:${(data.user as Record<string, string>).id || username.trim()}`],
      };

      localStorage.setItem("admin_token", token);
      localStorage.setItem("admin_user", JSON.stringify(user));
      localStorage.setItem("admin_token_expires_at", data.expires_at || "");
      document.cookie = `admin_session=1; path=/; max-age=86400; SameSite=Lax`;

      setAuth(token, user);
      router.push("/dashboard");
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "登录失败";
      setDevError(`登录失败: ${msg}`);
    } finally {
      setDevLoading(false);
    }
  };

  const handleSsoLogin = () => {
    setSsoLoading(true);
    const authUrl =
      `${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/auth` +
      `?client_id=${encodeURIComponent(KEYCLOAK_CLIENT_ID)}` +
      `&redirect_uri=${encodeURIComponent(REDIRECT_URI)}` +
      `&response_type=code` +
      `&scope=openid+profile+email`;
    window.location.href = authUrl;
  };

  const handleDevKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !devLoading) handleDevLogin();
  };

  return (
    <div className="flex items-center justify-center min-h-screen bg-gray-100">
      <div className="bg-white p-8 rounded-xl shadow-lg w-full max-w-md">
        <h1 className="text-2xl font-bold mb-2 text-center">🔐 权限管理台</h1>
        <p className="text-sm text-gray-500 mb-6 text-center">
          登录以管理 RAG v14 权限系统
        </p>

        {/* 开发模式 */}
        <div className="border border-gray-200 rounded-lg p-4 mb-4">
          <p className="text-xs font-medium text-gray-500 mb-3 uppercase tracking-wide">
            开发模式
          </p>
          <div className="space-y-3">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">用户名</label>
              <input
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                onKeyDown={handleDevKeyDown}
                placeholder="admin"
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
                disabled={devLoading}
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">密码</label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                onKeyDown={handleDevKeyDown}
                placeholder="开发模式密码"
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
                disabled={devLoading}
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">租户</label>
              <input
                type="text"
                value={tenant}
                onChange={(e) => setTenant(e.target.value)}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
                disabled={devLoading}
              />
            </div>

            {devError && (
              <div className="bg-red-50 border border-red-200 rounded-lg px-3 py-2 text-xs text-red-700">
                {devError}
              </div>
            )}

            <button
              onClick={handleDevLogin}
              disabled={devLoading}
              className="w-full bg-blue-600 text-white py-2 rounded-lg hover:bg-blue-700 transition-colors text-sm font-medium disabled:opacity-50"
            >
              {devLoading ? "登录中..." : "开发模式登录（RAG dev-login）"}
            </button>
          </div>
        </div>

        {/* 生产模式 SSO */}
        <div className="border-t pt-4">
          <button
            onClick={handleSsoLogin}
            disabled={ssoLoading}
            className="w-full border-2 border-gray-300 text-gray-700 py-2.5 rounded-lg hover:bg-gray-50 transition-colors text-sm font-medium disabled:opacity-50"
          >
            {ssoLoading ? "跳转中..." : "🔑 通过 Keycloak SSO 登录"}
          </button>
          <p className="text-xs text-gray-400 text-center mt-2">
            {KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}
          </p>
        </div>
      </div>
    </div>
  );
}
