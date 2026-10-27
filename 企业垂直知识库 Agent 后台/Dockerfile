# syntax=docker/dockerfile:1
# ============================================================================
# 应用镜像（多阶段构建，精简体积）：
#   阶段1 builder：安装依赖到独立 venv（/opt/venv），利用层缓存；
#   阶段2 runtime：仅复制 venv + 源码，不带 pip 缓存与构建工具。
# 向量库 Chroma 与上传文件走挂载卷，不打进镜像。
# 环境变量统一由 docker-compose.yml / .env 注入（详见 .env.example）。
# 启动命令使用 uvicorn；建表由应用 lifespan 自动完成（AUTO_CREATE_TABLES）。
# ============================================================================

# ---------- 阶段1：依赖构建 ----------
FROM python:3.11-slim AS builder

WORKDIR /app

# 先只复制依赖清单，利用 Docker 层缓存（依赖不变时不重装）
COPY requirements.txt ./

# 装进独立 venv，便于整目录搬运到运行阶段
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# ---------- 阶段2：运行时 ----------
FROM python:3.11-slim

WORKDIR /app

# 复制 venv 并加入 PATH；日志直出 stdout 便于 docker logs 采集
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 仅复制运行所需源码与资源（tests/docs 等由 .dockerignore 排除）
COPY app ./app
COPY scripts ./scripts
COPY samples ./samples

EXPOSE 8000

# 启动时 AUTO_CREATE_TABLES 自动建表；管理员账号用 seed 脚本初始化：
#   docker compose exec app python scripts/seed.py --username admin --password <密码> --role admin
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
