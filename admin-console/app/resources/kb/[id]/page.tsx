/** KB 详情路由页面。
 *
 * RAG 系统通过 {ADMIN_CONSOLE_URL}/resources/kb/{kb_id} 跳转到此页面。
 */

"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import api from "@/lib/api";
import PermissionGrantDialog from "@/components/acl/PermissionGrantDialog";
import RoleBindingManager from "@/components/acl/RoleBindingManager";
import RestrictionManager from "@/components/acl/RestrictionManager";
import PermissionTrace from "@/components/acl/PermissionTrace";

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

export default function KBDetailPage() {
  const params = useParams();
  const kbId = params.id as string;

  const [acl, setAcl] = useState<ACLItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<"acl" | "roles" | "restrictions" | "trace">("acl");
  const [grantOpen, setGrantOpen] = useState(false);

  const loadACL = async () => {
    try {
      const res = await api.get("/api/v1/acl", { params: { resource_type: "kb", resource_id: kbId } });
      setAcl(res.data);
    } catch { setAcl([]); }
    setLoading(false);
  };

  useEffect(() => { loadACL(); }, [kbId]);

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold">知识库授权管理</h1>
          <p className="text-gray-500 text-sm mt-1">KB ID: {kbId}</p>
        </div>
        <button onClick={() => setGrantOpen(true)} className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700">
          + 授予权限
        </button>
      </div>

      <div className="flex gap-4 mb-6 border-b">
        {(["acl", "roles", "restrictions", "trace"] as const).map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              activeTab === tab ? "border-blue-600 text-blue-600" : "border-transparent text-gray-500 hover:text-gray-700"
            }`}
          >
            {{ acl: "ACL 权限", roles: "角色绑定", restrictions: "限制规则", trace: "权限溯源" }[tab]}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="text-center py-12 text-gray-400">加载中...</div>
      ) : (
        <div>
          {activeTab === "acl" && (
            <div className="space-y-2">
              {acl.length === 0 ? (
                <p className="text-gray-400 text-center py-8">暂无 ACL 权限记录</p>
              ) : (
                acl.filter((a) => !a.revoked).map((entry) => (
                  <div key={entry.id} className="flex items-center justify-between p-3 bg-white border rounded-lg">
                    <div>
                      <span className="font-mono text-sm">{entry.principal}</span>
                      <span className="mx-2 text-gray-300">|</span>
                      <span className="text-blue-600 font-medium">{entry.action}</span>
                    </div>
                    <span className="text-xs text-gray-400">授予者: {entry.granted_by}</span>
                  </div>
                ))
              )}
            </div>
          )}
          {activeTab === "roles" && <RoleBindingManager />}
          {activeTab === "restrictions" && <RestrictionManager />}
          {activeTab === "trace" && <PermissionTrace />}
        </div>
      )}

      <PermissionGrantDialog
        open={grantOpen}
        onClose={() => setGrantOpen(false)}
        onGranted={loadACL}
        defaultResourceType="kb"
        defaultResourceId={kbId}
      />
    </div>
  );
}
