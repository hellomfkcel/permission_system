/** 封禁管理 — 主体封禁(型一) + 资源限制(型二)。
 *
 * 使用 RestrictionManager 组件。
 * 设计依据：docs/外部系统设计.md §3.3 + §2.4.4。
 */

"use client";

import RestrictionManager from "@/components/acl/RestrictionManager";

export default function RestrictionsPage() {
  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold">🚫 封禁与限制管理</h1>
          <p className="text-sm text-gray-500 mt-1">
            型一封禁（subject_ban）：主体被禁止所有操作，prefilter 返回 suspended=true<br />
            型二限制（resource_restriction）：特定资源的额外 deny 条件，filter 端点 pre-deny
          </p>
        </div>
      </div>

      <RestrictionManager />
    </div>
  );
}
