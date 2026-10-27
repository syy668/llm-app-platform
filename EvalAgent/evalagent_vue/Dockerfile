# syntax=docker/dockerfile:1
# ============================================================================
# EvalAgent Vue3 前端镜像（多阶段）：
#   阶段1 builder：node 编译产出 dist 静态资源；
#   阶段2 runtime：nginx 托管静态资源，并反向代理后端 /api。
# 单机部署只需 Docker，无需本机 Node 环境。
# API 基址在构建期注入（默认 /api/v1 相对路径，经 nginx 反代到 app:8000）。
# ============================================================================

# ---------- 阶段1：构建 ----------
FROM node:20-alpine AS builder

WORKDIR /app

# 先复制依赖清单，利用层缓存
COPY package.json package-lock.json ./
RUN npm ci

# 再复制源码并构建（VITE_ 前缀变量在构建期注入产物）
COPY . .
ARG VITE_UPSTREAM_API_BASE_URL=/api/v1
ARG VITE_LOGIN_URL=/login
ENV VITE_UPSTREAM_API_BASE_URL=$VITE_UPSTREAM_API_BASE_URL \
    VITE_LOGIN_URL=$VITE_LOGIN_URL
RUN npm run build

# ---------- 阶段2：运行时 ----------
FROM nginx:1.27-alpine

# 自定义站点配置：history 路由回退 + /api 反向代理 + gzip（见 nginx.conf）
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
