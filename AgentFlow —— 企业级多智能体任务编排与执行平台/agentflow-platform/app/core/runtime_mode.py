"""运行模式探测：识别当前进程是否为嵌入式演示环境（SQLite + fakeredis）。

为什么需要它：`scripts/dev_serve.py::_bind_embedded_infra` 会把
`app.db.session._engine` / `app.core.redis_client._client` 两个进程级单例
换成 SQLite / fakeredis。这份"换库"动作此前是静默的——看日志、看 /readyz
都分不清连的是演示库还是真实库。本模块给出唯一的权威判定，供：

  - `app/main.py` lifespan 启动时打醒目 WARNING；
  - `/readyz` 返回体携带 `mode` 字段，前端据此常驻"演示模式"角标。

判定原则：以**实际绑定的单例**为准（与 /readyz 探针同一口径），
不读配置——配置说用 MySQL 不代表本进程真的绑定了 MySQL。
"""

from __future__ import annotations

import app.core.redis_client as redis_module
import app.db.session as db_session_module

DEMO_MODE = "demo"
PRODUCTION_MODE = "production"


def detect_demo_mode() -> bool:
    """检测当前进程绑定的基础设施是否为嵌入式演示实例。

    判定依据（任一命中即演示模式）：
      - DB 单例已创建且 engine URL 以 ``sqlite`` 开头；
      - Redis 单例的类型来自 ``fakeredis`` 包。
    """
    engine = db_session_module._engine
    if engine is not None and str(engine.url).startswith("sqlite"):
        return True
    client = redis_module._client
    if client is not None and type(client).__module__.split(".")[0] == "fakeredis":
        return True
    return False


def runtime_mode() -> str:
    """当前运行模式标识：``demo`` / ``production``。"""
    return DEMO_MODE if detect_demo_mode() else PRODUCTION_MODE
