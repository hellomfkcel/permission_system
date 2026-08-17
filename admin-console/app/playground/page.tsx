/** 策略模拟器 — 使用 PolicySimulator 组件。
 */

"use client";

import PolicySimulator from "@/components/acl/PolicySimulator";

export default function PlaygroundPage() {
  return (
    <div>
      <div className="mb-6">
        <h1 className="text-2xl font-bold">🧪 策略模拟器 (Playground)</h1>
        <p className="text-sm text-gray-500 mt-1">
          沙箱环境 — 模拟 Cerbos PDP 权限判定结果，不产生实际授权副作用。
          支持 JSON Principal 和 JWT Credential 两种输入模式。
        </p>
      </div>
      <PolicySimulator />
    </div>
  );
}
