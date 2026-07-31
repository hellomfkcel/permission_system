/** 文档详情路由页面。
 *
 * 设计依据：docs/外部系统设计.md §3.3 资源管理页面结构。
 */

"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import api from "@/lib/api";
import PermissionGrantDialog from "@/components/acl/PermissionGrantDialog";

interface ACLItem {
  id: string;
  principal: string;
  resource_type: string;
  resource_id: string;
  action: string;
  granted_by: string;
  granted_at: string;
  revoked: boolean;
}

export default function DocumentDetailPage() {
  const params = useParams();
  const docId = params.id as string;

  const [acl, setAcl] = useState<ACLItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [grantOpen, setGrantOpen] = useState(false);

  const loadACL = async () => {
    try {
      const res = await api.get("/api/v1/acl", { params: { resource_type: "document", resource_id: docId } });
      setAcl(res.data);
    } catch { setAcl([]); }
    setLoading(false);
  };

  useEffect(() => { loadACL(); }, [docId]);

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold">文档授权管理</h1>
          <p className="text-gray-500 text-sm mt-1">Document ID: {docId}</p>
        </div>
        <button onClick={() => setGrantOpen(true)} className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700">
          + 授予权限
        </button>
      </div>

      {loading ? (
        <div className="text-center py-12 text-gray-400">加载中...</div>
      ) : acl.length === 0 ? (
        <p className="text-gray-400 text-center py-8">暂无 ACL 权限记录</p>
      ) : (
        <div className="space-y-2">
          {acl.filter((a) => !a.revoked).map((entry) => (
            <div key={entry.id} className="flex items-center justify-between p-3 bg-white border rounded-lg">
              <div>
                <span className="font-mono text-sm">{entry.principal}</span>
                <span className="mx-2 text-gray-300">|</span>
                <span className="text-blue-600 font-medium">{entry.action}</span>
              </div>
              <span className="text-xs text-gray-400">授予者: {entry.granted_by}</span>
            </div>
          ))}
        </div>
      )}

      <PermissionGrantDialog
        open={grantOpen}
        onClose={() => setGrantOpen(false)}
        onGranted={loadACL}
        defaultResourceType="document"
        defaultResourceId={docId}
      />
    </div>
  );
}
