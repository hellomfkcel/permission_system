# Multi-stage build for admin console (standalone mode)
FROM node:20-alpine AS builder
WORKDIR /app
# NEXT_PUBLIC_* 在 next build 时内联进 JS 束，必须在构建阶段注入
# （docker-compose.yml 的 build.args 传入；仅 environment: 运行时设置不会进构建）
ARG NEXT_PUBLIC_PERMISSION_SERVICE_URL
ARG NEXT_PUBLIC_KEYCLOAK_URL
ARG NEXT_PUBLIC_KEYCLOAK_REALM
ARG NEXT_PUBLIC_KEYCLOAK_CLIENT_ID
ENV NEXT_PUBLIC_PERMISSION_SERVICE_URL=$NEXT_PUBLIC_PERMISSION_SERVICE_URL \
    NEXT_PUBLIC_KEYCLOAK_URL=$NEXT_PUBLIC_KEYCLOAK_URL \
    NEXT_PUBLIC_KEYCLOAK_REALM=$NEXT_PUBLIC_KEYCLOAK_REALM \
    NEXT_PUBLIC_KEYCLOAK_CLIENT_ID=$NEXT_PUBLIC_KEYCLOAK_CLIENT_ID
COPY package*.json ./
# npm 换国内镜像源（默认 registry.npmjs.org 在国内极慢）
RUN npm config set registry https://registry.npmmirror.com && npm ci
COPY . .
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=3000

# standalone 模式：只需复制 standalone 目录 + static 资源
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
# 注: 项目无 public/ 目录，跳过 COPY public/

EXPOSE 3000
CMD ["node", "server.js"]
