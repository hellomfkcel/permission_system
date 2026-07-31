/** 权限动词目录 — 单一权威源。
 *
 * 设计依据：docs/RAG系统设计v14.md §4（上游契约 10 个动词）
 *          + docs/权限管理系统架构设计.md §2.1
 *
 * 管理台所有组件从此文件导入，避免多处硬编码导致不一致。
 */

/** 资源类型 */
export const RESOURCE_TYPES = ["kb", "document"] as const;
export type ResourceType = (typeof RESOURCE_TYPES)[number];

/** 所有有效动词（10 个，权威定义来自上游契约 §4） */
export const ALL_ACTIONS = [
  "kb:read",
  "kb:write",
  "kb:manage",
  "kb:grant",
  "doc:view",
  "doc:download",
  "doc:retrieve",
  "doc:unmount",
  "doc:purge",
  "doc:share",
] as const;
export type Action = (typeof ALL_ACTIONS)[number];

/** 按资源类型分组的动词 */
export const ACTIONS_BY_RESOURCE: Record<ResourceType, Action[]> = {
  kb: ["kb:read", "kb:write", "kb:manage", "kb:grant"],
  document: [
    "doc:view",
    "doc:download",
    "doc:retrieve",
    "doc:unmount",
    "doc:purge",
    "doc:share",
  ],
};

/** 动词中文标签 */
export const ACTION_LABELS: Record<Action, string> = {
  "kb:read": "读取知识库",
  "kb:write": "写入知识库",
  "kb:manage": "管理知识库",
  "kb:grant": "授权管理",
  "doc:view": "查看文档",
  "doc:download": "下载文档",
  "doc:retrieve": "检索文档",
  "doc:unmount": "移除文档",
  "doc:purge": "彻底删除",
  "doc:share": "分享文档",
};

/** 派生角色列表 */
export const DERIVED_ROLES = [
  "kb_reader",
  "kb_writer",
  "kb_admin",
  "admin",
] as const;
export type DerivedRole = (typeof DERIVED_ROLES)[number];

/** 派生角色中文标签 */
export const ROLE_LABELS: Record<DerivedRole, string> = {
  kb_reader: "知识库读者",
  kb_writer: "知识库作者",
  kb_admin: "知识库管理员",
  admin: "超级管理员",
};

/** 审计事件类型 → 标签映射 */
export const AUDIT_EVENT_LABELS: Record<string, string> = {
  ACL_GRANTED: "权限授予",
  ACL_REVOKED: "权限回收",
  ACL_BATCH_GRANTED: "批量授予",
  ROLE_BOUND: "角色绑定",
  ROLE_UNBOUND: "角色解绑",
  RESTRICTION_ADDED: "添加限制",
  RESTRICTION_REMOVED: "移除限制",
  RESOURCE_REGISTERED: "资源注册",
  RESOURCE_RETIRED: "资源退役",
  MOUNT_LINKED: "挂载建立",
  MOUNT_UNLINKED: "解除挂载",
  RESOURCE_ATTR_UPDATED: "属性更新",
  OWNERSHIP_TRANSFERRED: "所有权转移",
};
