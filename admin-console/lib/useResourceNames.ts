/** 共享 Hook：获取资源名称映射表。
 *
 * 从权限服务 /api/v1/resources 获取所有已注册资源，
 * 构建 resource_key → name 的映射表。
 *
 * resource_key 格式："resource_type:resource_id"（如 "kb:a6a9f8c0-..."）。
 *
 * Phase 3e 修复：缓存按 project_id 分片，支持项目切换后自动刷新。
 *
 * 使用方式:
 *   const { getName, loading } = useResourceNames(currentProjectId);
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

/** 多项目缓存：project_id → Map<resource_key, name> */
const _cacheByProject = new Map<string, Map<string, string>>();
const _promisesByProject = new Map<string, Promise<Map<string, string>>>();

async function fetchResourceMap(projectId: string | null): Promise<Map<string, string>> {
  const cacheKey = projectId || "__all__";
  const cached = _cacheByProject.get(cacheKey);
  if (cached) return cached;

  const pending = _promisesByProject.get(cacheKey);
  if (pending) return pending;

  const promise = (async () => {
    const map = new Map<string, string>();
    try {
      const params: Record<string, string> = { type: "kb" };
      // 平台模式不传 project_id，项目模式传 project_id
      if (projectId && projectId !== "__all__") {
        params.project_id = projectId;
      }
      const kbRes = await api.get<ResourceItem[]>("/api/v1/resources", { params });
      const docParams: Record<string, string> = { type: "document" };
      if (projectId && projectId !== "__all__") {
        docParams.project_id = projectId;
      }
      const docRes = await api.get<ResourceItem[]>("/api/v1/resources", { params: docParams });
      for (const r of [...(kbRes.data || []), ...(docRes.data || [])]) {
        if (r.name) {
          map.set(`${r.resource_type}:${r.resource_id}`, r.name);
        }
      }
    } catch {
      // 静默失败
    }
    _cacheByProject.set(cacheKey, map);
    return map;
  })();

  _promisesByProject.set(cacheKey, promise);
  return promise;
}

export function useResourceNames(projectId?: string | null) {
  const cacheKey = projectId || "__all__";
  const [nameMap, setNameMap] = useState<Map<string, string>>(
    _cacheByProject.get(cacheKey) ?? new Map()
  );
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    const cached = _cacheByProject.get(cacheKey);
    if (cached) {
      setNameMap(cached);
      return;
    }
    fetchResourceMap(projectId ?? null).then((map) => {
      if (mountedRef.current) setNameMap(new Map(map));
    });
    return () => {
      mountedRef.current = false;
    };
  }, [cacheKey]);

  const getName = useCallback(
    (resourceType: string, resourceId: string): string | null => {
      return nameMap.get(`${resourceType}:${resourceId}`) ?? null;
    },
    [nameMap],
  );

  const formatResource = useCallback(
    (resourceType: string | null, resourceId: string | null): string => {
      if (!resourceType || !resourceId) return "—";
      return getName(resourceType, resourceId) ?? resourceId;
    },
    [getName],
  );

  const loading = !_cacheByProject.has(cacheKey);

  return { getName, formatResource, loading };
}
