/** 共享 Hook：获取资源名称映射表。
 *
 * 从权限服务 /api/v1/resources 获取所有已注册资源，
 * 构建 resource_key → name 的映射表。
 *
 * resource_key 格式："resource_type:resource_id"（如 "kb:a6a9f8c0-..."）。
 *
 * 设计依据：docs/外部系统设计.md §2.4.4
 *   管理台专用 API GET /api/v1/resources 现已返回 name 字段。
 *
 * 使用方式:
 *   const { getName, loading } = useResourceNames();
 *   const displayName = getName("kb", "a6a9f8c0-...") ?? "a6a9f8c0-...";
 */

"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import api from "@/lib/api";

interface ResourceItem {
  resource_type: string;
  resource_id: string;
  name: string | null;
}

/** 单例缓存：多个组件共享同一份数据，避免重复请求 */
let _cachedMap: Map<string, string> | null = null;
let _fetchPromise: Promise<Map<string, string>> | null = null;

async function fetchResourceMap(): Promise<Map<string, string>> {
  if (_cachedMap) return _cachedMap;
  if (_fetchPromise) return _fetchPromise;

  _fetchPromise = (async () => {
    const map = new Map<string, string>();
    try {
      // 分两次获取（无过滤条件的 /api/v1/resources 返回所有资源）
      const [kbRes, docRes] = await Promise.all([
        api.get<ResourceItem[]>("/api/v1/resources", { params: { type: "kb" } }),
        api.get<ResourceItem[]>("/api/v1/resources", { params: { type: "document" } }),
      ]);
      for (const r of [...(kbRes.data || []), ...(docRes.data || [])]) {
        if (r.name) {
          map.set(`${r.resource_type}:${r.resource_id}`, r.name);
        }
      }
    } catch {
      // 静默失败：映射表为空时各组件回退显示 resource_id
    }
    _cachedMap = map;
    return map;
  })();

  return _fetchPromise;
}

export function useResourceNames() {
  const [nameMap, setNameMap] = useState<Map<string, string>>(_cachedMap ?? new Map());
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    if (_cachedMap) {
      setNameMap(_cachedMap);
      return;
    }
    fetchResourceMap().then((map) => {
      if (mountedRef.current) setNameMap(new Map(map));
    });
    return () => { mountedRef.current = false; };
  }, []);

  const getName = useCallback(
    (resourceType: string, resourceId: string): string | null => {
      return nameMap.get(`${resourceType}:${resourceId}`) ?? null;
    },
    [nameMap],
  );

  /** 格式化显示：有名称显示名称，否则回退 resource_id */
  const formatResource = useCallback(
    (resourceType: string | null, resourceId: string | null): string => {
      if (!resourceType || !resourceId) return "—";
      return getName(resourceType, resourceId) ?? resourceId;
    },
    [getName],
  );

  const loading = !_cachedMap;

  return { getName, formatResource, loading };
}
