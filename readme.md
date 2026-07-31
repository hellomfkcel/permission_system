
启动命令

# 开发模式：权限服务后端
cd ~/permission-system/permission-service
conda activate perm_service
uvicorn app.main:app --host 0.0.0.0 --port 18080 --reload

# 开发模式：管理台前端
cd ~/permission-system/admin-console
npm run dev  # http://localhost:3002

# Docker 生产部署
docker compose up -d perm-postgres perm-redis  # 基础设施
docker compose up -d permission-service admin-console  # 应用

切换到 Remote 模式

# ~/proj_rag_dev/.env
AUTHZ_SERVICE_MODE=remote
AUTHZ_SERVICE_URL=http://localhost:18080
