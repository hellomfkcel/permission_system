/**
 * Toast 通知组件 — 统一的用户提示系统。
 *
 * 设计依据：frontend-design.md §3.6 降级与错误状态处理。
 * 替换项目中所有 alert() 调用。
 */

"use client";

import { createContext, useContext, useState, useCallback, type ReactNode } from "react";

type ToastType = "success" | "error" | "warning" | "info";

interface Toast {
  id: number;
  type: ToastType;
  message: string;
}

interface ToastContextType {
  toasts: Toast[];
  showToast: (type: ToastType, message: string) => void;
  removeToast: (id: number) => void;
}

const ToastContext = createContext<ToastContextType>({
  toasts: [],
  showToast: () => {},
  removeToast: () => {},
});

let toastId = 0;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const removeToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const showToast = useCallback(
    (type: ToastType, message: string) => {
      const id = ++toastId;
      setToasts((prev) => [...prev.slice(-4), { id, type, message }]);
      setTimeout(() => removeToast(id), 4000);
    },
    [removeToast]
  );

  // 注册全局 Toast —— 供 React 组件树外的代码调用（lib/api.ts 的 axios 拦截器）
  if (typeof window !== "undefined") {
    (
      window as Window & { __globalToast?: typeof showToast }
    ).__globalToast = showToast;
  }

  const bgColor: Record<ToastType, string> = {
    success: "bg-green-600",
    error: "bg-red-600",
    warning: "bg-yellow-500",
    info: "bg-blue-600",
  };

  return (
    <ToastContext.Provider value={{ toasts, showToast, removeToast }}>
      {children}
      {/* Toast 容器 — 固定在右下角 */}
      <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={`${bgColor[toast.type]} text-white px-5 py-3 rounded-lg shadow-lg text-sm max-w-sm animate-slide-in`}
            onClick={() => removeToast(toast.id)}
          >
            {toast.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  return useContext(ToastContext);
}

/**
 * 确认操作工具函数 — 统一确认对话框入口。
 *
 * 当前实现使用 window.confirm()，后续可升级为自定义 Dialog 组件。
 * 所有需要用户确认的操作应通过此函数而非直接调用 confirm()。
 */
export function showConfirm(message: string): boolean {
  return window.confirm(message);
}
