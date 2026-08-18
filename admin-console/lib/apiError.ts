/** 共享 API 错误处理 — 从后端错误响应中提取可读信息。 */

export interface ApiErrorBody {
  response?: { data?: { detail?: string; message?: string } };
  message?: string;
}

/** 提取错误信息：优先后端 detail → message → err.message，最后回退到 fallback。 */
export function apiErrorMessage(e: unknown, fallback: string): string {
  const err = e as ApiErrorBody;
  return (
    err?.response?.data?.detail ||
    err?.response?.data?.message ||
    err?.message ||
    fallback
  );
}
