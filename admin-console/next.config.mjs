/** @type {import('next').NextConfig} */
const nextConfig = {
  // standalone 输出模式：将依赖内联到 .next/standalone，减少 Docker 镜像体积。
  // Dockerfile 使用 runner 阶段只需 COPY standalone + static + public。
  output: "standalone",
};

export default nextConfig;
