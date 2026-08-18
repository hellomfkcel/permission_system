/** 审计日志 — 使用 AuditLogViewer 组件。
 */

"use client";

import AuditLogViewer from "@/components/acl/AuditLogViewer";

export default function AuditPage() {
  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold">🔍 审计日志</h1>
          <p className="text-sm text-gray-500 mt-1">
            权限变更事件由 Outbox 模式持久化（permission_changes 表）。
            版本号全局单调递增，用于 RAG 侧事件对账与跨系统取证。
          </p>
        </div>
      </div>
      <AuditLogViewer />
    </div>
  );
}
