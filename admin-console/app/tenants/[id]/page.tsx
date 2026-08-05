/** 租户详情页 — 基本信息编辑 + 成员管理。
 *
 * 设计依据：docs/tenant_design.md §3.4.1 租户详情页。
 */

"use client";

import { useEffect, useState, useCallback } from "react";
import { useParams, useRouter } from "next/navigation";
import {
  Loader2, ArrowLeft, Users, UserPlus, Trash2, Pencil,
  Building2, Clock, Shield,
} from "lucide-react";
import api from "@/lib/api";

interface TenantDetail {
  id: string;
  name: string;
  description: string;
  status: "active" | "suspended" | "deleted";
  member_count: number;
  created_by: string;
  created_at: string;
  updated_at: string;
}

interface TenantMember {
  id: string;
  user_id: string;
  role: string;
  granted_by: string;
  granted_at: string;
  revoked: boolean;
}

export default function TenantDetailPage() {
  const params = useParams();
  const router = useRouter();
  const tenantId = params.id as string;

  const [tenant, setTenant] = useState<TenantDetail | null>(null);
  const [members, setMembers] = useState<TenantMember[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // 编辑状态
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState("");
  const [editDesc, setEditDesc] = useState("");
  const [saving, setSaving] = useState(false);

  // 添加成员 Dialog
  const [addOpen, setAddOpen] = useState(false);
  const [addUserId, setAddUserId] = useState("");
  const [addRole, setAddRole] = useState("member");
  const [addError, setAddError] = useState("");
  const [adding, setAdding] = useState(false);

  // 移除成员
  const [removeTarget, setRemoveTarget] = useState<TenantMember | null>(null);
  const [removing, setRemoving] = useState(false);

  const fetchTenant = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const { data } = await api.get(`/api/v1/tenants/${tenantId}`);
      setTenant(data);
      setEditName(data.name);
      setEditDesc(data.description);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "加载租户信息失败";
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [tenantId]);

  const fetchMembers = useCallback(async () => {
    try {
      const { data } = await api.get(`/api/v1/tenants/${tenantId}/members`);
      setMembers(data.members);
    } catch {
      // silent
    }
  }, [tenantId]);

  useEffect(() => {
    fetchTenant();
    fetchMembers();
  }, [fetchTenant, fetchMembers]);

  const handleSave = async () => {
    setSaving(true);
    try {
      const { data } = await api.patch(`/api/v1/tenants/${tenantId}`, {
        name: editName.trim(),
        description: editDesc.trim(),
      });
      setTenant(data);
      setEditing(false);
    } catch (e: unknown) {
      if (e && typeof e === "object" && "response" in e) {
        const resp = (e as { response: { data?: { detail?: string } } }).response;
        alert(resp.data?.detail || "保存失败");
      }
    } finally {
      setSaving(false);
    }
  };

  const handleAddMember = async () => {
    setAddError("");
    if (!addUserId.trim()) {
      setAddError("用户 ID 不能为空");
      return;
    }
    setAdding(true);
    try {
      await api.post(`/api/v1/tenants/${tenantId}/members`, {
        user_id: addUserId.trim(),
        role: addRole,
      });
      setAddOpen(false);
      setAddUserId("");
      setAddRole("member");
      fetchMembers();
      fetchTenant();
    } catch (e: unknown) {
      if (e && typeof e === "object" && "response" in e) {
        const resp = (e as { response: { data?: { detail?: string } } }).response;
        setAddError(resp.data?.detail || "添加失败");
      } else {
        setAddError("添加失败");
      }
    } finally {
      setAdding(false);
    }
  };

  const handleRemoveMember = async () => {
    if (!removeTarget) return;
    setRemoving(true);
    try {
      await api.delete(`/api/v1/tenants/${tenantId}/members/${removeTarget.user_id}`);
      setRemoveTarget(null);
      fetchMembers();
      fetchTenant();
    } catch {
      alert("移除失败");
    } finally {
      setRemoving(false);
    }
  };

  const formatDate = (iso: string) => {
    if (!iso) return "-";
    try { return new Date(iso).toLocaleString("zh-CN"); } catch { return iso; }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16 text-gray-400">
        <Loader2 size={24} className="animate-spin mr-2" /> 加载中...
      </div>
    );
  }

  if (error || !tenant) {
    return (
      <div className="p-6">
        <button onClick={() => router.back()} className="flex items-center gap-2 text-sm text-blue-600 hover:underline mb-4">
          <ArrowLeft size={16} /> 返回
        </button>
        <div className="p-4 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">
          {error || "租户不存在"}
        </div>
      </div>
    );
  }

  return (
    <div className="p-6">
      {/* 返回按钮 */}
      <button onClick={() => router.push("/tenants")} className="flex items-center gap-2 text-sm text-blue-600 hover:underline mb-4">
        <ArrowLeft size={16} /> 返回租户列表
      </button>

      {/* 租户基本信息 */}
      <div className="bg-white rounded-lg border border-gray-200 p-6 mb-6">
        <div className="flex items-start justify-between">
          <div className="flex items-center gap-3">
            <Building2 size={28} className="text-blue-500" />
            <div>
              {editing ? (
                <div className="flex items-center gap-2">
                  <input
                    type="text"
                    value={editName}
                    onChange={(e) => setEditName(e.target.value)}
                    className="text-xl font-bold px-2 py-1 border border-gray-300 rounded focus:outline-none focus:ring-2 focus:ring-blue-500"
                  />
                  <button
                    onClick={handleSave}
                    disabled={saving}
                    className="px-3 py-1 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
                  >
                    {saving ? "保存中..." : "保存"}
                  </button>
                  <button onClick={() => setEditing(false)} className="px-3 py-1 text-sm text-gray-600 hover:bg-gray-100 rounded">
                    取消
                  </button>
                </div>
              ) : (
                <>
                  <h1 className="text-2xl font-bold text-gray-900">{tenant.name}</h1>
                  <p className="text-sm text-gray-500 font-mono">{tenant.id}</p>
                </>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <span className={`px-2.5 py-1 text-xs rounded-full font-medium ${
              tenant.status === "active" ? "bg-green-100 text-green-700" :
              tenant.status === "suspended" ? "bg-yellow-100 text-yellow-700" :
              "bg-red-100 text-red-700"
            }`}>
              {tenant.status === "active" ? "活跃" : tenant.status === "suspended" ? "已停用" : "已删除"}
            </span>
            {!editing && (
              <button
                onClick={() => { setEditName(tenant.name); setEditDesc(tenant.description); setEditing(true); }}
                className="p-1.5 text-gray-400 hover:text-blue-500 hover:bg-blue-50 rounded transition-colors"
                title="编辑"
              >
                <Pencil size={16} />
              </button>
            )}
          </div>
        </div>

        {editing ? (
          <div className="mt-4">
            <label className="block text-xs font-medium text-gray-600 mb-1">描述</label>
            <textarea
              value={editDesc}
              onChange={(e) => setEditDesc(e.target.value)}
              rows={2}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
        ) : (
          tenant.description && (
            <p className="mt-3 text-sm text-gray-600">{tenant.description}</p>
          )
        )}

        <div className="flex items-center gap-6 mt-4 text-sm text-gray-500">
          <span className="flex items-center gap-1">
            <Users size={14} /> {tenant.member_count} 名成员
          </span>
          <span className="flex items-center gap-1">
            <Shield size={14} /> 创建者: <span className="font-mono">{tenant.created_by}</span>
          </span>
          <span className="flex items-center gap-1">
            <Clock size={14} /> 创建于 {formatDate(tenant.created_at)}
          </span>
        </div>
      </div>

      {/* 成员管理 */}
      <div className="bg-white rounded-lg border border-gray-200">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <h2 className="text-lg font-semibold text-gray-900">
            👥 租户成员 ({members.filter(m => !m.revoked).length})
          </h2>
          <button
            onClick={() => setAddOpen(true)}
            className="flex items-center gap-2 px-3 py-1.5 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
          >
            <UserPlus size={14} />
            添加成员
          </button>
        </div>

        {/* 成员表格 */}
        {members.length === 0 ? (
          <div className="py-12 text-center text-gray-400">
            <Users size={32} className="mx-auto mb-2" />
            <p>暂无成员</p>
          </div>
        ) : (
          <table className="w-full">
            <thead className="bg-gray-50">
              <tr>
                <th className="text-left px-6 py-3 text-xs font-semibold text-gray-500 uppercase">用户</th>
                <th className="text-left px-6 py-3 text-xs font-semibold text-gray-500 uppercase">角色</th>
                <th className="text-left px-6 py-3 text-xs font-semibold text-gray-500 uppercase">添加者</th>
                <th className="text-left px-6 py-3 text-xs font-semibold text-gray-500 uppercase">添加时间</th>
                <th className="text-right px-6 py-3 text-xs font-semibold text-gray-500 uppercase">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {members.map((m) => (
                <tr key={m.id} className={`hover:bg-gray-50 ${m.revoked ? "opacity-50" : ""}`}>
                  <td className="px-6 py-3">
                    <span className="text-sm font-medium text-gray-900 font-mono">{m.user_id}</span>
                    {m.revoked && <span className="ml-2 text-xs text-red-500">(已移除)</span>}
                  </td>
                  <td className="px-6 py-3">
                    <span className={`px-2 py-0.5 text-xs rounded-full font-medium ${
                      m.role === "tenant_admin" ? "bg-purple-100 text-purple-700" : "bg-blue-100 text-blue-700"
                    }`}>
                      {m.role === "tenant_admin" ? "管理员" : "成员"}
                    </span>
                  </td>
                  <td className="px-6 py-3 text-sm text-gray-500 font-mono">{m.granted_by}</td>
                  <td className="px-6 py-3 text-sm text-gray-500">{formatDate(m.granted_at)}</td>
                  <td className="px-6 py-3 text-right">
                    {!m.revoked && (
                      <button
                        onClick={() => setRemoveTarget(m)}
                        className="p-1.5 text-gray-400 hover:text-red-500 hover:bg-red-50 rounded transition-colors"
                        title="移除成员"
                      >
                        <Trash2 size={14} />
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* 添加成员 Dialog */}
      {addOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="bg-white rounded-xl shadow-xl w-full max-w-sm p-6">
            <h2 className="text-lg font-semibold text-gray-900 mb-4">添加成员</h2>
            {addError && (
              <div className="mb-3 p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">{addError}</div>
            )}
            <div className="space-y-3">
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">用户 ID *</label>
                <input
                  type="text"
                  value={addUserId}
                  onChange={(e) => setAddUserId(e.target.value)}
                  placeholder="user:alice"
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">租户角色</label>
                <select
                  value={addRole}
                  onChange={(e) => setAddRole(e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                >
                  <option value="member">member — 普通成员</option>
                  <option value="tenant_admin">tenant_admin — 租户管理员</option>
                </select>
              </div>
            </div>
            <div className="flex justify-end gap-3 mt-6">
              <button onClick={() => { setAddOpen(false); setAddError(""); }} className="px-4 py-2 text-sm text-gray-600 hover:bg-gray-100 rounded-lg">
                取消
              </button>
              <button onClick={handleAddMember} disabled={adding} className="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 flex items-center gap-2">
                {adding && <Loader2 size={14} className="animate-spin" />}
                添加
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 移除成员确认 */}
      {removeTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="bg-white rounded-xl shadow-xl w-full max-w-sm p-6">
            <h2 className="text-lg font-semibold text-gray-900 mb-2">确认移除此成员?</h2>
            <p className="text-sm text-gray-600 mb-4">
              将 <span className="font-mono font-medium">{removeTarget.user_id}</span> 从租户中移除。
            </p>
            <div className="flex justify-end gap-3">
              <button onClick={() => setRemoveTarget(null)} className="px-4 py-2 text-sm text-gray-600 hover:bg-gray-100 rounded-lg">
                取消
              </button>
              <button onClick={handleRemoveMember} disabled={removing} className="px-4 py-2 text-sm bg-red-600 text-white rounded-lg hover:bg-red-700 disabled:opacity-50 flex items-center gap-2">
                {removing && <Loader2 size={14} className="animate-spin" />}
                确认移除
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
