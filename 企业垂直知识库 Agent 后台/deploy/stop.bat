@echo off
rem ============================================================================
rem EvalAgent 一键停止 / 清理脚本（Windows）
rem 用法：
rem   deploy\stop.bat           停止服务（保留数据卷与镜像）
rem   deploy\stop.bat clean     停止并清理数据卷（MySQL/Redis 数据将被删除！）
rem   deploy\stop.bat purge     停止并清理数据卷 + 本地构建镜像（彻底清理）
rem ============================================================================
chcp 65001 >nul
cd /d "%~dp0.."

echo ==^> 停止全部服务...
docker compose down

if "%1"=="clean" (
    echo ==^> 删除数据卷（mysql_data / redis_data，数据不可恢复！^)
    docker compose down -v
)
if "%1"=="purge" (
    echo ==^> 删除数据卷与本地构建镜像
    docker compose down -v
    docker rmi evalagent-backend:latest evalagent-frontend:latest 2>nul
)

echo ==^> 完成。重新启动：deploy\start.bat
