/** 全局 Layout — 侧边导航栏 + 内容区 + AuthGuard */
import type { Metadata } from "next";
import "./globals.css";
import AuthGuard from "@/components/layout/AuthGuard";
import SidebarWrapper from "@/components/layout/SidebarWrapper";
import { ToastProvider } from "@/components/shared/Toast";

export const metadata: Metadata = {
  title: "RAG 权限管理台",
  description: "RAG v14 权限管理系统管理台",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body className="flex min-h-screen bg-gray-100">
        <ToastProvider>
          <AuthGuard>
            <SidebarWrapper>
              <main className="flex-1 p-8 overflow-auto">{children}</main>
            </SidebarWrapper>
          </AuthGuard>
        </ToastProvider>
      </body>
    </html>
  );
}
