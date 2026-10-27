# legacy —— 历史构建产物（只读参考）

本目录存放已被替换的构建配置，**仅用于回滚参考**，日常构建不要使用。

| 文件 | 来源 | 说明 |
|---|---|---|
| `Dockerfile.w1-single-stage` | W1 | 单阶段镜像：直接用 `python:3.11-slim` 装依赖后拷源码，镜像体积大、含编译工具链、以 root 运行、无 `HEALTHCHECK`。W5-Part2 已重写为多阶段构建（`/Dockerfile`）。 |

## 回滚方式（仅在多阶段镜像出问题时）

```bash
# 用旧单阶段 Dockerfile 构建，镜像名加 -legacy 后缀以便区分
docker build -f deploy/legacy/Dockerfile.w1-single-stage -t agentflow-backend:legacy .
```

> 回滚镜像**不包含** OTel 依赖与 `app/bootstrap.py` 装配层，因此：
> `/health` `/ready` `/metrics` `/observability/status` 这些端点不存在，
> 对应 compose 的探针需临时改回 `/healthz`，且 `ENABLE_OTEL` 相关环境变量无效。
> 该镜像也不支持多副本 SSE 会话共享（会话表在进程内），不要用于 `--scale backend>1`。

## 已移除的文件

`docker-compose.w1.yml` —— 与根目录 `docker-compose.yml` **逐字节相同**，属冗余备份，v1.0.0 归档时删除。
需要 W1 单机栈时直接使用根目录的 `docker-compose.yml`（mysql + redis + backend）。
