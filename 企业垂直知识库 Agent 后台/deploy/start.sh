#!/usr/bin/env bash
# ============================================================================
# EvalAgent 一键启动脚本（Linux / macOS / Git Bash 通用）
# 前置：仅要求已安装 Docker（含 docker compose 插件）与项目根目录 .env 文件。
# 用法：bash deploy/start.sh
# ============================================================================
set -e
cd "$(dirname "$0")/.."

echo "==> [1/3] 检查 Docker 环境..."
docker --version >/dev/null || { echo "未检测到 docker，请先安装 Docker Desktop / Docker Engine"; exit 1; }
docker compose version >/dev/null || { echo "未检测到 docker compose 插件，请升级 Docker"; exit 1; }

if [ ! -f .env ]; then
    echo "==> 未发现 .env，正在从 .env.example 生成（请按需修改数据库密码 / LLM 密钥）"
    cp .env.example .env
fi

echo "==> [2/3] 构建并启动 4 个服务（MySQL / Redis / 后端 app / 前端 frontend）..."
docker compose up -d --build

echo "==> [3/3] 等待服务就绪..."
docker compose ps

cat <<'EOF'

启动完成！
  前端入口 : http://localhost
  后端文档 : http://localhost:8000/docs
  健康检查 : http://localhost:8000/api/v1/health
首次部署请初始化管理员：
  docker compose exec app python scripts/seed.py --username admin --password <密码> --role admin
EOF
