# syntax=docker/dockerfile:1.6
# ---------------------------------------------------------------------------
# AgentFlow Platform —— 多阶段生产镜像（W5-Part2 任务 1）
#
# 目标：
#   1. 运行镜像**不含** gcc / 头文件 / 测试依赖，体积远小于单阶段构建；
#   2. 依赖层与代码层分离：改业务代码不会触发 pip 重装；
#   3. 非 root 运行 + 内置健康探针 + 统一入口 ``app.bootstrap:app``。
#
# 旧版（W1）单阶段 Dockerfile 已原样备份到 ``deploy/Dockerfile.w1-single-stage``；
# ``docker-compose.yml``（开发编排）仍指向本文件，暴露端口与启动方式保持向后兼容
# （额外提供 9100 给 MCP HTTP，不影响原有 8000）。
# ---------------------------------------------------------------------------

# ============================ Stage 1: builder ============================
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# 编译工具链只在构建阶段存在（少数包无 wheel 时兜底）；运行阶段不带这些
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libffi-dev \
 && rm -rf /var/lib/apt/lists/*

# 独立 venv：整目录 COPY 到运行阶段，避免与系统 site-packages 混杂
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY requirements.txt requirements-observability.txt ./
RUN pip install --upgrade pip \
 && pip install -r requirements.txt \
 && pip install -r requirements-observability.txt

# 预热字节码（运行阶段首次启动少一次编译）
COPY app ./app
RUN python -m compileall -q app

# ============================ Stage 2: runtime ============================
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    TZ=Asia/Shanghai \
    PATH="/opt/venv/bin:$PATH"

# 运行期只留时区与 CA 证书（httpx / OTLP 出网需要）
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# 应用代码（.dockerignore 已排除 tests / frontend / .venv / *.md）
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts

# 非 root：uid/gid 固定 10001，便于 K8s securityContext runAsUser 对齐
RUN groupadd --gid 10001 appuser \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin appuser \
 && mkdir -p /app/storage /app/logs \
 && chown -R appuser:appuser /app

USER appuser

# 8000 = 主应用；9100 = MCP HTTP（同一镜像，两种启动命令）
EXPOSE 8000 9100

# 探针走 /health（纯进程存活，不依赖 MySQL/Redis），与 compose healthcheck 一致
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

# 默认入口：带探针 / 指标 / 可选 OTel / 可选 MCP HTTP 的生产装配层
CMD ["uvicorn", "app.bootstrap:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
