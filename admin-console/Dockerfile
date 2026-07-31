# Multi-stage build for admin console (standalone mode)
FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
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
