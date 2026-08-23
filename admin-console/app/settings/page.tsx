/** 系统设置 — 查看配置 + 健康检查（P1-6: 动态值替代硬编码） */
"use client";

import { useEffect, useState } from "react";
import api from "@/lib/api";

interface HealthStatus {
  permService: "ok" | "down" | "checking";
  cerbos: "ok" | "down" | "checking";
  keycloak: "ok" | "down" | "checking";
}

interface SystemConfig {
  service_port: number;
  cerbos_pdp_url: string;
  keycloak_server_url: string;
  keycloak_realm: string;
  rate_limits: Record<string, string>;
  derived_roles_count: number;
  resource_rules: string[];
}

export default function SettingsPage() {
  const serviceUrl = process.env.NEXT_PUBLIC_PERMISSION_SERVICE_URL || "http://localhost:18080";
  // Keycloak URL 由 build arg（deploy 注入，支持 https/域名）提供；不硬编码 IP
  const keycloakUrl = process.env.NEXT_PUBLIC_KEYCLOAK_URL || "";
  const keycloakRealm = process.env.NEXT_PUBLIC_KEYCLOAK_REALM || "rag-v14";

  const [health, setHealth] = useState<HealthStatus>({
    permService: "checking",
    cerbos: "checking",
    keycloak: "checking",
  });
  const [config, setConfig] = useState<SystemConfig | null>(null);

  useEffect(() => {
    // P1-6: 动态获取配置（替代硬编码）
    api.get("/api/v1/auth/config")
      .then((res) => setConfig(res.data))
      .catch(() => {}); // 获取失败时使用空值，页面显示占位符

    // 检查权限服务健康
    api.get("/healthz")
      .then(() => setHealth((h) => ({ ...h, permService: "ok" })))
      .catch(() => setHealth((h) => ({ ...h, permService: "down" })));

    // 检查 Cerbos PDP（通过 simulate 端点探测，用平台通用动作，不绑定任何项目）
    api.post("/api/v1/simulate", {
      principal: { id: "user:health-check", roles: ["user"], attr: {} },
      action: "platform:read",
      resource: { kind: "platform", id: "health-check", attr: { retired: false } },
    })
      .then(() => setHealth((h) => ({ ...h, cerbos: "ok" })))
      .catch(() => setHealth((h) => ({ ...h, cerbos: "down" })));

    // 检查 Keycloak（通过 OIDC discovery）
    fetch(`${keycloakUrl}/realms/${keycloakRealm}/.well-known/openid-configuration`)
      .then((r) => {
        if (r.ok) setHealth((h) => ({ ...h, keycloak: "ok" }));
        else setHealth((h) => ({ ...h, keycloak: "down" }));
      })
      .catch(() => setHealth((h) => ({ ...h, keycloak: "down" })));
  }, [keycloakUrl, keycloakRealm]);

  const statusBadge = (status: string) => {
    const map: Record<string, { color: string; text: string }> = {
      ok: { color: "bg-green-500", text: "运行中" },
      down: { color: "bg-red-500", text: "不可达" },
      checking: { color: "bg-yellow-500", text: "检查中..." },
    };
    const s = map[status] || map.checking;
    return (
      <span className="flex items-center gap-2 text-sm">
        <span className={`inline-block w-2 h-2 ${s.color} rounded-full`} />
        {s.text}
      </span>
    );
  };

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">⚙️ 系统设置</h1>

      <div className="space-y-6">
        {/* 权限服务后端 */}
        <div className="bg-white rounded-lg shadow p-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold">权限服务后端</h2>
            {statusBadge(health.permService)}
          </div>
          <div className="space-y-3 text-sm">
            <div>
              <label className="block text-xs text-gray-500">服务地址</label>
              <code className="bg-gray-100 px-3 py-1 rounded block mt-1">{serviceUrl}</code>
            </div>
            <div>
              <label className="block text-xs text-gray-500">API 版本</label>
              <code className="bg-gray-100 px-3 py-1 rounded block mt-1">v1</code>
            </div>
            <div>
              <label className="block text-xs text-gray-500">服务端口</label>
              <code className="bg-gray-100 px-3 py-1 rounded block mt-1">
                {config?.service_port ?? "—"}
              </code>
            </div>
            {/* P1-6: 动态限流配置（从 /api/v1/auth/config 获取） */}
            <div>
              <label className="block text-xs text-gray-500">限流配置</label>
              <code className="bg-gray-100 px-3 py-1 rounded block mt-1">
                {config
                  ? Object.entries(config.rate_limits)
                      .map(([ep, limit]) => `${ep}: ${limit}`)
                      .join(" | ")
                  : "加载中..."}
              </code>
            </div>
          </div>
        </div>

        {/* Keycloak IdP */}
        <div className="bg-white rounded-lg shadow p-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold">Keycloak IdP</h2>
            {statusBadge(health.keycloak)}
          </div>
          <div className="space-y-3 text-sm">
            <div>
              <label className="block text-xs text-gray-500">IdP 地址</label>
              <code className="bg-gray-100 px-3 py-1 rounded block mt-1">
                {config?.keycloak_server_url || keycloakUrl}
              </code>
            </div>
            <div>
              <label className="block text-xs text-gray-500">Realm</label>
              <code className="bg-gray-100 px-3 py-1 rounded block mt-1">
                {config?.keycloak_realm || keycloakRealm}
              </code>
            </div>
            <div>
              <label className="block text-xs text-gray-500">Client ID</label>
              <code className="bg-gray-100 px-3 py-1 rounded block mt-1">
                {process.env.NEXT_PUBLIC_KEYCLOAK_CLIENT_ID || "admin-console"}
              </code>
            </div>
          </div>
        </div>

        {/* Cerbos PDP */}
        <div className="bg-white rounded-lg shadow p-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold">Cerbos PDP</h2>
            {statusBadge(health.cerbos)}
          </div>
          <div className="space-y-3 text-sm">
            <p className="text-gray-500">
              Cerbos PDP 策略决策点 — RAG 系统与权限服务共用
            </p>
            <div>
              <label className="block text-xs text-gray-500">PDP 地址</label>
              <code className="bg-gray-100 px-3 py-1 rounded block mt-1">
                {config?.cerbos_pdp_url || "—"}
              </code>
            </div>
            <div>
              <label className="block text-xs text-gray-500">策略文件</label>
              <code className="bg-gray-100 px-3 py-1 rounded block mt-1">
                cerbos/policies/derived_roles/rag_roles.yaml + resource_policies/
              </code>
            </div>
            {/* P1-6: 动态策略规则（从 /api/v1/auth/config 获取） */}
            <div>
              <label className="block text-xs text-gray-500">策略规则</label>
              <code className="bg-gray-100 px-3 py-1 rounded block mt-1">
                {config
                  ? `${config.derived_roles_count} 派生角色 + ${config.resource_rules.length} 资源规则 (${config.resource_rules.join(", ")})`
                  : "加载中..."}
              </code>
            </div>
          </div>
        </div>

        {/* 部署信息 */}
        <div className="bg-white rounded-lg shadow p-6">
          <h2 className="text-lg font-semibold mb-4">部署拓扑</h2>
          <div className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <span className="text-gray-500">权限服务 (Docker):</span>
              <code className="block bg-gray-100 px-3 py-1 rounded mt-1">
                {serviceUrl}
              </code>
            </div>
            <div>
              <span className="text-gray-500">管理台:</span>
              <code className="block bg-gray-100 px-3 py-1 rounded mt-1">
                {typeof window !== "undefined" ? window.location.origin : "—"}
              </code>
            </div>
            <div>
              <span className="text-gray-500">Cerbos PDP:</span>
              <code className="block bg-gray-100 px-3 py-1 rounded mt-1">
                {config?.cerbos_pdp_url || "—"}
              </code>
            </div>
            <div>
              <span className="text-gray-500">Keycloak:</span>
              <code className="block bg-gray-100 px-3 py-1 rounded mt-1">
                {config?.keycloak_server_url || keycloakUrl}
              </code>
            </div>
            <div>
              <span className="text-gray-500">PostgreSQL:</span>
              <code className="block bg-gray-100 px-3 py-1 rounded mt-1">25433 (独立)</code>
            </div>
            <div>
              <span className="text-gray-500">Redis:</span>
              <code className="block bg-gray-100 px-3 py-1 rounded mt-1">16380 (独立)</code>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
