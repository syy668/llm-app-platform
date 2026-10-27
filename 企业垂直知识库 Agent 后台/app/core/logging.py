"""日志配置：统一格式、级别可配置，输出到标准输出（跨平台）。

【工程化收尾 新增】Trace 链路：所有日志自动携带当前请求的 trace_id。
中间件（core/middleware.py）在请求入口把 TraceID 写入 ContextVar，
本模块的 TraceIdFilter 读取该值注入每条日志记录；无请求上下文（如
启动期、后台评测线程）时显示 "-"。
"""
import logging
import sys

from ..config import settings
from .middleware import get_trace_id


class TraceIdFilter(logging.Filter):
    """给每条日志记录注入 trace_id 字段（取自请求上下文，缺省 "-"）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id()
        return True


def setup_logging() -> None:
    """初始化根日志器；重复调用幂等，避免叠加 handler。"""
    root = logging.getLogger()
    if getattr(root, "_kb_configured", False):
        return
    root.setLevel(settings.log_level.upper())

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(TraceIdFilter())
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | trace_id=%(trace_id)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)
    root._kb_configured = True
