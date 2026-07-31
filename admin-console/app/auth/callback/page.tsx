/** OAuth2 回调处理 — Keycloak SSO code → token 交换。

设计依据：docs/外部系统设计.md §4.1 Keycloak 集成 + frontend-design.md §0 认证与租户。
*/

"use client";

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useAuthStore } from "@/stores/useAuthStore";

const KEYCLOAK_URL = process.env.NEXT_PUBLIC_KEYCLOAK_URL || "http://192.168.1.127:8080";
const KEYCLOAK_REALM = process.env.NEXT_PUBLIC_KEYCLOAK_REALM || "rag-v14";
const KEYCLOAK_CLIENT_ID = process.env.NEXT_PUBLIC_KEYCLOAK_CLIENT_ID || "admin-console";
const REDIRECT_URI =
  typeof window !== "undefined"
    ? `${window.location.origin}/auth/callback`
    : "http://192.168.1.127:3002/auth/callback";

export default function AuthCallbackPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { setAuth } = useAuthStore();
  const [error, setError] = useState("");
  const [status, setStatus] = useState("正在验证身份...");

  useEffect(() => {
    const code = searchParams.get("code");
    const errorParam = searchParams.get("error");

    if (errorParam) {
      setError(`Keycloak 认证失败: ${errorParam}`);
      return;
    }

    if (!code) {
      setError("未收到授权码，请重新登录");
      return;
    }

    exchangeCode(code);
  }, [searchParams]);

  const exchangeCode = async (code: string) => {
    try {
      setStatus("正在交换授权码...");

      const resp = await fetch(
        `${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/token`,
        {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({
            grant_type: "authorization_code",
            client_id: KEYCLOAK_CLIENT_ID,
            code,
            redirect_uri: REDIRECT_URI,
          }),
        }
      );

      if (!resp.ok) {
        const errText = await resp.text();
        throw new Error(`Token exchange failed (${resp.status}): ${errText.slice(0, 200)}`);
      }

      const data = await resp.json();
      const accessToken = data.access_token as string;
      const refreshToken = data.refresh_token as string;

      // 解析 JWT 获取用户信息
      const payload = JSON.parse(atob(accessToken.split(".")[1]));
      const user = {
        user_id: payload.sub || "unknown",
        tenant_id: payload.tenant || payload.tenant_id || "",
        roles: payload.realm_access?.roles || [],
        groups: payload.groups || [],
        principals: [
          `user:${payload.sub || "unknown"}`,
          ...(payload.groups || []).map((g: string) => `group:${g}`),
        ],
      };

      // 存储 token + 用户信息
      localStorage.setItem("admin_token", accessToken);
      if (refreshToken) localStorage.setItem("admin_refresh_token", refreshToken);
      localStorage.setItem("admin_user", JSON.stringify(user));
      const expMs = payload.exp ? payload.exp * 1000 : Date.now() + 3600000;
      localStorage.setItem("admin_token_expires_at", new Date(expMs).toISOString());
      document.cookie = `admin_session=1; path=/; max-age=86400; SameSite=Lax`;

      setAuth(accessToken, user);
      setStatus("登录成功，正在跳转...");
      setTimeout(() => router.push("/dashboard"), 500);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "未知错误";
      setError(`Token 交换失败: ${msg}`);
    }
  };

  if (error) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-gray-100">
        <div className="bg-white p-8 rounded-xl shadow-lg w-full max-w-md text-center">
          <div className="text-red-500 text-4xl mb-4">⚠️</div>
          <h1 className="text-xl font-bold mb-2">登录失败</h1>
          <p className="text-sm text-red-600 mb-4">{error}</p>
          <button
            onClick={() => router.push("/login")}
            className="bg-blue-600 text-white px-6 py-2 rounded-lg hover:bg-blue-700 transition-colors text-sm"
          >
            返回登录页
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-gray-100">
      <div className="bg-white p-8 rounded-xl shadow-lg w-full max-w-md text-center">
        <div className="animate-spin rounded-full h-10 w-10 border-b-2 border-blue-600 mx-auto mb-4" />
        <h1 className="text-lg font-semibold mb-2">认证处理中</h1>
        <p className="text-sm text-gray-500">{status}</p>
      </div>
    </div>
  );
}
