/** 用户与组管理 — 只读视图，用户数据同步自 Keycloak IdP */
"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import api from "@/lib/api";

interface UserInfo {
  user_id: string;
  username: string;
  email: string;
  display_name: string;
  tenant_id: string;
  tenants: string[];
  roles: string[];
  groups: string[];
  principals: string[];
}

interface Group {
  id: string;
  name: string;
  path: string;
  member_count: number;
}

export default function UsersGroupsPage() {
  const [users, setUsers] = useState<UserInfo[]>([]);
  const [groups, setGroups] = useState<Group[]>([]);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [syncResult, setSyncResult] = useState("");
  const [activeTab, setActiveTab] = useState<"users" | "groups">("users");

  const loadData = async () => {
    setLoading(true);
    try {
      const [usersRes, groupsRes] = await Promise.all([
        api.get("/api/v1/auth/users"),
        api.get("/api/v1/auth/groups"),
      ]);
      setUsers(usersRes.data);
      setGroups(groupsRes.data);
    } catch {
      // 数据加载失败，保持空列表
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
  }, []);

  const handleSync = async () => {
    setSyncing(true);
    setSyncResult("");
    try {
      const res = await api.post("/api/v1/auth/sync/users");
      setSyncResult(
        `同步完成：创建 ${res.data.users_created}，更新 ${res.data.users_updated}，删除 ${res.data.users_deleted}`
      );
      await loadData();
    } catch (err: unknown) {
      const e = err as { response?: { data?: { detail?: string } } };
      setSyncResult(`同步失败：${e?.response?.data?.detail || "未知错误"}`);
    } finally {
      setSyncing(false);
    }
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">👥 用户与组管理</h1>
        <button
          onClick={handleSync}
          disabled={syncing}
          className="bg-blue-600 text-white px-4 py-2 rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors text-sm"
        >
          {syncing ? "同步中..." : "🔄 从 Keycloak 同步"}
        </button>
      </div>

      {syncResult && (
        <div className={`px-4 py-3 rounded-lg mb-4 text-sm ${
          syncResult.includes("失败")
            ? "bg-red-50 border border-red-200 text-red-700"
            : "bg-green-50 border border-green-200 text-green-700"
        }`}>
          {syncResult}
        </div>
      )}

      <div className="bg-white rounded-lg shadow overflow-hidden mb-4">
        <div className="border-b">
          <button
            onClick={() => setActiveTab("users")}
            className={`px-6 py-3 text-sm font-medium transition-colors ${
              activeTab === "users"
                ? "border-b-2 border-blue-600 text-blue-600"
                : "text-gray-500 hover:text-gray-700"
            }`}
          >
            用户列表 ({users.length})
          </button>
          <button
            onClick={() => setActiveTab("groups")}
            className={`px-6 py-3 text-sm font-medium transition-colors ${
              activeTab === "groups"
                ? "border-b-2 border-blue-600 text-blue-600"
                : "text-gray-500 hover:text-gray-700"
            }`}
          >
            组列表 ({groups.length})
          </button>
        </div>

        {loading ? (
          <div className="p-8 text-center text-gray-500">
            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600 mx-auto mb-3" />
            加载中...
          </div>
        ) : activeTab === "users" ? (
          users.length === 0 ? (
            <div className="p-12 text-center text-gray-400">
              <p className="mb-3">暂无用户数据</p>
              <p className="text-xs">
                点击 &ldquo;从 Keycloak 同步&rdquo; 按钮同步用户数据
                （需 Keycloak 可访问且 permission-service 的 client_secret 已配置）
              </p>
            </div>
          ) : (
            <table className="w-full">
              <thead className="bg-gray-50">
                <tr>
                  <th className="text-left px-4 py-3 text-sm font-medium text-gray-600">用户</th>
                  <th className="text-left px-4 py-3 text-sm font-medium text-gray-600">邮箱</th>
                  <th className="text-left px-4 py-3 text-sm font-medium text-gray-600">租户</th>
                  <th className="text-left px-4 py-3 text-sm font-medium text-gray-600">角色</th>
                  <th className="text-left px-4 py-3 text-sm font-medium text-gray-600">组</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <tr key={u.user_id} className="border-t hover:bg-gray-50">
                    <td className="px-4 py-3 text-sm">
                      <Link
                        href={`/users-groups/user/${encodeURIComponent(u.user_id)}`}
                        className="text-blue-600 hover:underline"
                      >
                        <span className="font-medium">{u.display_name || u.username || u.user_id.slice(0, 8) + "..."}</span>
                      </Link>
                      {u.username && u.username !== u.display_name && (
                        <span className="text-gray-400 text-xs ml-1.5">@{u.username}</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-600">{u.email || "-"}</td>
                    <td className="px-4 py-3 text-sm text-gray-600">
                      {u.tenants.length > 0
                        ? u.tenants.map((t) => (
                            <span key={t} className="bg-purple-100 text-purple-800 px-2 py-0.5 rounded text-xs mr-1">
                              {t}
                            </span>
                          ))
                        : (u.tenant_id || "-")}
                    </td>
                    <td className="px-4 py-3 text-sm">
                      {u.roles.length > 0
                        ? u.roles.map((r) => (
                            <span key={r} className="bg-blue-100 text-blue-800 px-2 py-0.5 rounded text-xs mr-1">
                              {r}
                            </span>
                          ))
                        : "-"}
                    </td>
                    <td className="px-4 py-3 text-sm">
                      {u.groups.length > 0
                        ? u.groups.map((g) => (
                            <span key={g} className="bg-green-100 text-green-800 px-2 py-0.5 rounded text-xs mr-1">
                              {g}
                            </span>
                          ))
                        : "-"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
        ) : groups.length === 0 ? (
          <div className="p-12 text-center text-gray-400">
            <p>暂无组数据</p>
            <p className="text-xs mt-1">点击 &ldquo;从 Keycloak 同步&rdquo; 后刷新</p>
          </div>
        ) : (
          <table className="w-full">
            <thead className="bg-gray-50">
              <tr>
                <th className="text-left px-4 py-3 text-sm font-medium text-gray-600">组名</th>
                <th className="text-left px-4 py-3 text-sm font-medium text-gray-600">路径</th>
                <th className="text-left px-4 py-3 text-sm font-medium text-gray-600">成员数</th>
              </tr>
            </thead>
            <tbody>
              {groups.map((g) => (
                <tr key={g.id} className="border-t hover:bg-gray-50">
                  <td className="px-4 py-3 text-sm font-medium">{g.name}</td>
                  <td className="px-4 py-3 text-sm text-gray-500 font-mono text-xs">{g.path}</td>
                  <td className="px-4 py-3 text-sm">{g.member_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="text-xs text-gray-400">
        <p>用户与组数据只读同步自 Keycloak IdP ({process.env.NEXT_PUBLIC_KEYCLOAK_URL || "未配置"})。</p>
        <p>管理台不维护用户/组数据。建议每 15 分钟定时同步一次。</p>
      </div>
    </div>
  );
}
