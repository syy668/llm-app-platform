@echo off
rem ============================================================================
rem EvalAgent 一键启动脚本（Windows 双击或命令行执行）
rem 前置：仅要求已安装 Docker Desktop 与项目根目录 .env 文件。
rem ============================================================================
chcp 65001 >nul
cd /d "%~dp0.."

echo ==^> [1/3] 检查 Docker 环境...
docker --version >nul 2>&1 || (echo 未检测到 docker，请先安装并启动 Docker Desktop & exit /b 1)
docker compose version >nul 2>&1 || (echo 未检测到 docker compose 插件，请升级 Docker Desktop & exit /b 1)

if not exist .env (
    echo ==^> 未发现 .env，正在从 .env.example 生成（请按需修改数据库密码 / LLM 密钥）
    copy .env.example .env >nul
)

echo ==^> [2/3] 构建并启动 4 个服务（MySQL / Redis / 后端 app / 前端 frontend）...
docker compose up -d --build
if errorlevel 1 (echo 启动失败，请查看上方报错 & exit /b 1)

echo ==^> [3/3] 查看服务状态...
docker compose ps

echo.
echo 启动完成！
echo   前端入口 : http://localhost
echo   后端文档 : http://localhost:8000/docs
echo   健康检查 : http://localhost:8000/api/v1/health
echo 首次部署请初始化管理员：
echo   docker compose exec app python scripts/seed.py --username admin --password ^<密码^> --role admin
