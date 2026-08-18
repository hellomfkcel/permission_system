/** 组详情路由页面。
 */

"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import api from "@/lib/api";
import PermissionTrace from "@/components/acl/PermissionTrace";
import { useActionLabels } from "@/lib/actionLabels";

interface ACLSummary {
  id: string;
  principal: string;
  resource_type: string;
  resource_id: string;
  action: string;
  granted_by: string;
  granted_at: string;
}

interface RoleSummary {
  id: string;
  principal: string;
  role: string;
  resource_type: string | null;
  resource_id: string | null;
  granted_by: string;
}

export default function GroupDetailPage() {
  const params = useParams();
  const groupId = params.id as string;
  const principal = `group:${groupId}`;
  const { getLabel } = useActionLabels();

  const [aclEntries, setAclEntries] = useState<ACLSummary[]>([]);
  const [roleBindings, setRoleBindings] = useState<RoleSummary[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const loadData = async () => {
      try {
        const [aclRes, roleRes] = await Promise.all([
          api.get("/api/v1/acl/effective", { params: { principal } }),
          api.get("/api/v1/roles/bindings", { params: { principal } }),
        ]);
        setAclEntries(aclRes.data?.effective_permissions || aclRes.data || []);
        setRoleBindings(roleRes.data || []);
      } catch {
        setAclEntries([]);
        setRoleBindings([]);
      }
      setLoading(false);
    };
    loadData();
  }, [principal]);

  if (loading) return <div className="p-6 text-center text-gray-400">加载中...</div>;

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <h1 className="text-2xl font-bold mb-2">组详情</h1>
      <p className="text-gray-500 text-sm mb-6">主体: <code className="bg-gray-100 px-2 py-0.5 rounded">{principal}</code></p>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-white border rounded-lg p-4">
          <h2 className="font-semibold mb-3">权限汇总 (ACL)</h2>
          {aclEntries.length === 0 ? (
            <p className="text-gray-400 text-sm">暂无直接授予的 ACL 权限</p>
          ) : (
            <div className="space-y-2 max-h-80 overflow-y-auto">
              {aclEntries.map((entry, i) => (
                <div key={i} className="flex items-center justify-between p-2 bg-gray-50 rounded text-sm">
                  <span>{entry.resource_type}/{entry.resource_id}</span>
                  <span className="text-blue-600 font-medium">{getLabel(entry.action)}</span>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="bg-white border rounded-lg p-4">
          <h2 className="font-semibold mb-3">角色绑定</h2>
          {roleBindings.length === 0 ? (
            <p className="text-gray-400 text-sm">暂无角色绑定</p>
          ) : (
            <div className="space-y-2 max-h-80 overflow-y-auto">
              {roleBindings.map((rb) => (
                <div key={rb.id} className="flex items-center justify-between p-2 bg-gray-50 rounded text-sm">
                  <span className="font-mono">{rb.role}</span>
                  {rb.resource_type && <span className="text-gray-400">{rb.resource_type}/{rb.resource_id}</span>}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="mt-6">
        <h2 className="font-semibold mb-3">权限溯源</h2>
        <PermissionTrace />
      </div>
    </div>
  );
}
