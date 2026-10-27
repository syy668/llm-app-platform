#!/usr/bin/env bash
# ============================================================================
# EvalAgent 一键停止 / 清理脚本（Linux / macOS / Git Bash 通用）
# 用法：
#   bash deploy/stop.sh          # 停止服务（保留数据卷与镜像）
#   bash deploy/stop.sh clean    # 停止并清理数据卷（MySQL/Redis 数据将被删除！）
#   bash deploy/stop.sh purge    # 停止并清理数据卷 + 本地构建镜像（彻底清理）
# ============================================================================
set -e
cd "$(dirname "$0")/.."

MODE="${1:-stop}"

echo "==> 停止全部服务..."
docker compose down

if [ "$MODE" = "clean" ] || [ "$MODE" = "purge" ]; then
    echo "==> 删除数据卷（mysql_data / redis_data，数据不可恢复！）"
    docker compose down -v
fi

if [ "$MODE" = "purge" ]; then
    echo "==> 删除本地构建镜像（evalagent-backend / evalagent-frontend）"
    docker rmi evalagent-backend:latest evalagent-frontend:latest 2>/dev/null || true
fi

echo "==> 完成。重新启动：bash deploy/start.sh"
