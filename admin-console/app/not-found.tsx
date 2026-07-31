/** 全局 404 页面 — 路由不存在时显示。 */
export default function NotFound() {
  return (
    <div className="flex items-center justify-center min-h-[60vh]">
      <div className="flex flex-col items-center gap-4 text-center">
        <div className="text-6xl font-bold text-gray-200">404</div>
        <h2 className="text-lg font-semibold text-gray-700">页面未找到</h2>
        <p className="text-sm text-gray-400">您访问的页面不存在或已被移除。</p>
        <a href="/dashboard" className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700 transition-colors">
          返回首页
        </a>
      </div>
    </div>
  );
}
