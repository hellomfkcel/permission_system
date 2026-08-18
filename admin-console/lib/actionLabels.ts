/**
 * 动作标签 — 数据驱动的通用标签，不绑定任何项目（无 kb/doc 等硬编码）。
 *
 * 资源类型的中文标签来自后端 `/api/v1/auth/config` 的 `resource_type_labels`
 * （Cerbos 策略 / 平台配置维护），动作动词由本项目通用字典映射；
 * 未知类型 / 未知动词一律回退为原始动作字符串，保证任何项目的动作都能显示。
 *
 * 用法：
 *   const { getLabel } = useActionLabels();
 *   getLabel("kb:read")          // → "知识库 读取"（标签来自后端）
 *   getLabel("oa_leave:approve") // → "oa_leave 审批"（未知类型回退原名）
 */

import { useEffect, useState } from "react";
import api from "@/lib/api";

/** 通用动作动词 → 中文标签。任何项目的动作都可复用；未知动词回退原样。 */
const VERB_LABELS: Record<string, string> = {
  read: "读取",
  write: "写入",
  manage: "管理",
  grant: "授权",
  view: "查看",
  download: "下载",
  retrieve: "检索",
  unmount: "移除",
  purge: "删除",
  share: "分享",
  create: "创建",
  update: "更新",
  delete: "删除",
  list: "列表",
  execute: "执行",
  approve: "审批",
  submit: "提交",
  export: "导出",
  import: "导入",
  publish: "发布",
  archive: "归档",
  restore: "恢复",
  upload: "上传",
  edit: "编辑",
  review: "复核",
};

/** 标签数据缓存（模块级，跨组件共享，拉取一次）。
 *
 * 同时保留：
 *  - typeLabels：资源类型名 → 中文标签（resource_type_labels）
 *  - actionToType：动作 → 资源类型（由 resource_actions 反查，
 *    处理动作前缀与资源类型名不一致的情况，如 doc:read → document）
 */
let _typeLabels: Record<string, string> | null = null;
let _actionToType: Record<string, string> | null = null;
let _loading: Promise<{ labels: Record<string, string>; actionToType: Record<string, string> }> | null = null;

export function fetchActionLabelData(): Promise<{
  labels: Record<string, string>;
  actionToType: Record<string, string>;
}> {
  if (_typeLabels && _actionToType) {
    return Promise.resolve({ labels: _typeLabels, actionToType: _actionToType });
  }
  if (!_loading) {
    _loading = api
      .get("/api/v1/auth/config")
      .then((r) => {
        const labels: Record<string, string> = r.data.resource_type_labels || {};
        const resourceActions: Record<string, string[]> = r.data.resource_actions || {};
        const actionToType: Record<string, string> = {};
        for (const [rt, actions] of Object.entries(resourceActions)) {
          for (const a of actions) {
            if (!(a in actionToType)) actionToType[a] = rt;
          }
        }
        _typeLabels = labels;
        _actionToType = actionToType;
        return { labels, actionToType };
      })
      .catch(() => {
        const empty: Record<string, string> = {};
        _typeLabels = empty;
        _actionToType = empty;
        return { labels: empty, actionToType: empty };
      });
  }
  return _loading;
}

/** 纯函数：动作 → 可读标签。可直接用于渲染（不含网络依赖）。 */
export function actionLabel(
  action: string,
  typeLabels?: Record<string, string>,
  actionToType?: Record<string, string>,
): string {
  const idx = action.indexOf(":");
  if (idx < 0) return action;
  const prefix = action.slice(0, idx);
  const verb = action.slice(idx + 1);
  // 优先用 resource_actions 反查真实资源类型（doc:read → document），
  // 无映射时回退到动作前缀本身。
  const type = actionToType?.[action] || prefix;
  const typeLabel = typeLabels?.[type] || type;
  const verbLabel = VERB_LABELS[verb] || verb;
  return `${typeLabel} ${verbLabel}`;
}

/** Hook：获取后端资源类型标签与动作映射，并返回绑定好标签的动作标签函数。 */
export function useActionLabels() {
  const [labels, setLabels] = useState<Record<string, string>>({});
  const [actionToType, setActionToType] = useState<Record<string, string>>({});

  useEffect(() => {
    let alive = true;
    fetchActionLabelData().then((data) => {
      if (alive) {
        setLabels(data.labels);
        setActionToType(data.actionToType);
      }
    });
    return () => {
      alive = false;
    };
  }, []);

  return {
    typeLabels: labels,
    getLabel: (action: string) => actionLabel(action, labels, actionToType),
  };
}
