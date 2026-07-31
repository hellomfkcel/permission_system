/**
 * SidebarWrapper — 仅在已认证页面显示侧边导航栏。
 *
 * 登录页 /login 不显示侧边栏。
 */

"use client";

import { usePathname } from "next/navigation";
import Sidebar from "@/components/layout/Sidebar";

const PUBLIC_PATHS = ["/login"];

export default function SidebarWrapper({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  if (PUBLIC_PATHS.includes(pathname)) {
    return <>{children}</>;
  }

  return (
    <>
      <Sidebar />
      {children}
    </>
  );
}
