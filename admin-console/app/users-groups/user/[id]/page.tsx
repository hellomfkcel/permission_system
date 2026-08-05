/** 用户详情页 — 有效权限聚合 + 角色绑定 + ACL 溯源。

 * 设计依据：docs/外部系统设计.md §3.3 /users-groups 用户/组详情。
 * P2-3：补全管理台用户详情页。
 */

"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
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

interface AclEntry {
  id: string;
  principal: string;
  resource_type: string;
  resource_id: string;
  action: string;
  granted_by: string;
  granted_at: string;
  revoked: boolean;
  expires_at?: string;
}

interface RoleBinding {
  id: string;
  principal: string;
  role: string;
  resource_type?: string;
  resource_id?: string;
  granted_by: string;
  granted_at: string;
  revoked: boolean;
}

export default function UserDetailPage() {
  const params = useParams();
  const userId = decodeURIComponent(params.id as string);

  const [user, setUser] = useState<UserInfo | null>(null);
  const [acls, setAcls] = useState<AclEntry[]>([]);
  const [roleBindings, setRoleBindings] = useState<RoleBinding[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<"acls" | "roles">("acls");

  useEffect(() => {
    const load = async () => {
      setLoading(true);
      try {
        // 并行加载用户列表（找到目标用户）、ACL 和角色绑定
        const [usersRes, aclsRes, rolesRes] = await Promise.all([
          api.get("/api/v1/auth/users"),
          api.get(`/api/v1/acl`, { params: { principal: `user:${userId}` } }),
          api.get("/api/v1/roles/bindings", { params: { principal: `user:${userId}` } }),
        ]);

        // 从用户列表中匹配
        const found = (usersRes.data as UserInfo[]).find(
          (u) => u.user_id === userId || u.principals?.includes(`user:${userId}`)
        );
        setUser(found || null);
        setAcls(Array.isArray(aclsRes.data) ? aclsRes.data : aclsRes.data?.items || []);
        setRoleBindings(
          Array.isArray(rolesRes.data) ? rolesRes.data : rolesRes.data?.items || []
        );
      } catch {
        // 加载失败，保持空状态
      } finally {
        setLoading(false);
      }
    };
    load();
  }, [userId]);

  const activeAcls = acls.filter((a) => !a.revoked);
  const activeBindings = roleBindings.filter((b) => !b.revoked);

  // 按资源类型分组
  const groupedAcls = activeAcls.reduce(
    (acc, a) => {
      const key = a.resource_type || "unknown";
      if (!acc[key]) acc[key] = [];
      acc[key].push(a);
      return acc;
    },
    {} as Record<string, AclEntry[]>
  );

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[300px]">
        <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600" />
      </div>
    );
  }

  return (
    <div>
      {/* 面包屑 */}
      <div className="flex items-center gap-2 text-sm text-gray-500 mb-6">
        <Link href="/users-groups" className="hover:text-blue-600">
          👥 用户与组
        </Link>
        <span>/</span>
        <span className="text-gray-800 font-medium">👤 {user?.display_name || user?.username || userId.slice(0, 8) + "..."}</span>
      </div>

      {/* 用户概览卡片 */}
      <div className="bg-white rounded-lg shadow p-6 mb-6">
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-2xl font-bold flex items-center gap-2">
              <span className="w-10 h-10 bg-blue-100 text-blue-700 rounded-full flex items-center justify-center text-lg">
                {(user?.display_name || userId).charAt(0).toUpperCase()}
              </span>
              {user?.display_name || user?.username || userId.slice(0, 8) + "..."}
            </h1>
            {user && (
              <div className="mt-3 space-y-1 text-sm text-gray-600">
                <div>
                  <span className="font-medium">用户名：</span>
                  {user.username || userId}
                </div>
                <div>
                  <span className="font-medium">邮箱：</span>
                  {user.email || "-"}
                </div>
                <div>
                  <span className="font-medium">租户：</span>
                  {user.tenants.length > 0
                    ? user.tenants.map((t) => (
                        <span key={t} className="bg-purple-100 text-purple-800 px-2 py-0.5 rounded text-xs mr-1">{t}</span>
                      ))
                    : (user.tenant_id || "-")}
                </div>
                <div>
                  <span className="font-medium">角色：</span>
                  {user.roles.length > 0 ? (
                    <span className="flex flex-wrap gap-1 mt-1">
                      {user.roles.map((r) => (
                        <span
                          key={r}
                          className="bg-purple-100 text-purple-700 px-2 py-0.5 rounded text-xs"
                        >
                          {r}
                        </span>
                      ))}
                    </span>
                  ) : (
                    <span className="text-gray-400">无</span>
                  )}
                </div>
                <div>
                  <span className="font-medium">组：</span>
                  {user.groups.length > 0 ? (
                    <span className="flex flex-wrap gap-1 mt-1">
                      {user.groups.map((g) => (
                        <span
                          key={g}
                          className="bg-green-100 text-green-700 px-2 py-0.5 rounded text-xs"
                        >
                          {g}
                        </span>
                      ))}
                    </span>
                  ) : (
                    <span className="text-gray-400">无</span>
                  )}
                </div>
              </div>
            )}
          </div>

          {/* 统计卡片 */}
          <div className="flex gap-4">
            <div className="text-center p-3 bg-gray-50 rounded-lg">
              <div className="text-2xl font-bold text-blue-600">
                {activeAcls.length}
              </div>
              <div className="text-xs text-gray-500">ACL 权限</div>
            </div>
            <div className="text-center p-3 bg-gray-50 rounded-lg">
              <div className="text-2xl font-bold text-green-600">
                {activeBindings.length}
              </div>
              <div className="text-xs text-gray-500">角色绑定</div>
            </div>
          </div>
        </div>
      </div>

      {/* Tab 切换 */}
      <div className="flex gap-1 mb-4 bg-gray-100 rounded-lg p-1 w-fit">
        <button
          onClick={() => setActiveTab("acls")}
          className={`px-4 py-1.5 rounded text-sm font-medium transition-colors ${
            activeTab === "acls"
              ? "bg-white text-gray-800 shadow-sm"
              : "text-gray-500 hover:text-gray-700"
          }`}
        >
          ACL 权限 ({activeAcls.length})
        </button>
        <button
          onClick={() => setActiveTab("roles")}
          className={`px-4 py-1.5 rounded text-sm font-medium transition-colors ${
            activeTab === "roles"
              ? "bg-white text-gray-800 shadow-sm"
              : "text-gray-500 hover:text-gray-700"
          }`}
        >
          角色绑定 ({activeBindings.length})
        </button>
      </div>

      {/* ACL 权限列表 */}
      {activeTab === "acls" && (
        <div className="space-y-4">
          {Object.keys(groupedAcls).length === 0 ? (
            <div className="bg-white rounded-lg shadow p-8 text-center text-gray-400">
              该用户暂无 ACL 权限记录
            </div>
          ) : (
            Object.entries(groupedAcls).map(([resourceType, entries]) => (
              <div key={resourceType} className="bg-white rounded-lg shadow">
                <div className="px-6 py-3 border-b bg-gray-50 rounded-t-lg">
                  <h3 className="font-medium text-sm text-gray-700">
                    📦 {resourceType}
                    <span className="text-gray-400 ml-2">({entries.length})</span>
                  </h3>
                </div>
                <div className="overflow-auto">
                  <table className="w-full text-sm">
                    <thead className="text-left text-gray-500 border-b">
                      <tr>
                        <th className="px-6 py-2 font-medium">资源 ID</th>
                        <th className="px-6 py-2 font-medium">操作</th>
                        <th className="px-6 py-2 font-medium">授予者</th>
                        <th className="px-6 py-2 font-medium">授予时间</th>
                        <th className="px-6 py-2 font-medium">过期</th>
                      </tr>
                    </thead>
                    <tbody>
                      {entries.map((entry) => (
                        <tr key={entry.id} className="border-b hover:bg-gray-50">
                          <td className="px-6 py-2 font-mono text-xs">
                            {entry.resource_id}
                          </td>
                          <td className="px-6 py-2">
                            <span className="bg-blue-100 text-blue-700 px-2 py-0.5 rounded text-xs">
                              {entry.action}
                            </span>
                          </td>
                          <td className="px-6 py-2 text-gray-600">
                            {entry.granted_by}
                          </td>
                          <td className="px-6 py-2 text-gray-500 text-xs">
                            {entry.granted_at
                              ? new Date(entry.granted_at).toLocaleDateString()
                              : "-"}
                          </td>
                          <td className="px-6 py-2 text-gray-500 text-xs">
                            {entry.expires_at
                              ? new Date(entry.expires_at).toLocaleDateString()
                              : "永不过期"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ))
          )}
        </div>
      )}

      {/* 角色绑定列表 */}
      {activeTab === "roles" && (
        <div className="bg-white rounded-lg shadow">
          {activeBindings.length === 0 ? (
            <div className="p-8 text-center text-gray-400">
              该用户暂无角色绑定记录
            </div>
          ) : (
            <div className="overflow-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-gray-500 border-b">
                  <tr>
                    <th className="px-6 py-3 font-medium">角色</th>
                    <th className="px-6 py-3 font-medium">资源范围</th>
                    <th className="px-6 py-3 font-medium">授予者</th>
                    <th className="px-6 py-3 font-medium">授予时间</th>
                  </tr>
                </thead>
                <tbody>
                  {activeBindings.map((binding) => (
                    <tr key={binding.id} className="border-b hover:bg-gray-50">
                      <td className="px-6 py-2">
                        <span className="bg-purple-100 text-purple-700 px-2 py-0.5 rounded text-xs font-medium">
                          {binding.role}
                        </span>
                      </td>
                      <td className="px-6 py-2 text-gray-600 text-xs">
                        {binding.resource_type && binding.resource_id
                          ? `${binding.resource_type}:${binding.resource_id}`
                          : binding.resource_type || "全局"}
                      </td>
                      <td className="px-6 py-2 text-gray-600">
                        {binding.granted_by}
                      </td>
                      <td className="px-6 py-2 text-gray-500 text-xs">
                        {binding.granted_at
                          ? new Date(binding.granted_at).toLocaleDateString()
                          : "-"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* 底部：回到列表 + 权限溯源入口 */}
      <div className="mt-6 flex gap-3">
        <Link
          href="/users-groups"
          className="text-sm text-gray-500 hover:text-blue-600"
        >
          ← 返回用户列表
        </Link>
        <Link
          href={`/permissions?principal=user:${userId}`}
          className="text-sm text-blue-600 hover:underline"
        >
          🔍 在权限管理中查看
        </Link>
      </div>
    </div>
  );
}
