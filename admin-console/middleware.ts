/**
 * Next.js Middleware — 服务器端路由保护。
 *
 * 检查 session cookie（登录时设置），未认证用户重定向到 /login。
 * 客户端 AuthGuard 提供双层保护。
 *
 * 设计依据：frontend-design.md §0 认证与租户。
 */

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

const PUBLIC_PATHS = ["/login", "/auth/callback", "/favicon.ico"];

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // 公开路径放行
  if (PUBLIC_PATHS.some((p) => pathname.startsWith(p))) {
    return NextResponse.next();
  }

  // 静态资源放行
  if (
    pathname.startsWith("/_next") ||
    pathname.startsWith("/fonts") ||
    pathname.match(/\.(ico|png|svg|jpg|jpeg|css)$/)
  ) {
    return NextResponse.next();
  }

  // 检查 session cookie
  const session = request.cookies.get("admin_session")?.value;

  if (!session) {
    const loginUrl = new URL("/login", request.url);
    return NextResponse.redirect(loginUrl);
  }

  return NextResponse.next();
}

export const config = {
  matcher: [
    "/((?!_next|fonts|favicon.ico).*)",
  ],
};
