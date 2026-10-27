"""OpenTelemetry 全链路埋点 —— W5-Part2 任务 2。

## 设计原则：埋点即「旁路」，业务代码零改动

所有埋点通过两条机制注入，**不修改任何一个 Graph 节点 / MCP 工具 / LLM 客户端**：

1. **回调（Callback）**：给 LangGraph 调用注入一个 ``BaseCallbackHandler``，
   从 ``on_chain_start/end`` 的 ``metadata["langgraph_node"]`` 识别节点，节点级 span 由此产生；
2. **装饰器 / 方法包装（Monkey Patch）**：在 ``install_instrumentation()`` 里对
   **唯一边界方法**打一层包装 —— 全部是「进入前开 span、返回后关 span」，
   不改参数、不改返回值、不改异常语义：

| 边界 | 被包装的唯一点 | 为什么是它 |
|---|---|---|
| Graph | ``CompiledStateGraph.ainvoke / astream / astream_events`` | 所有图执行都从这里进（W2 TaskService、W3 StreamingTaskService） |
| MCP | ``MCPClient.call_tool`` | stdio 与 HTTP 两种传输都汇合在这一个方法 |
| LLM | ``LLMClient.complete`` | 主备切换门面，唯一入口 |
| SQL | SQLAlchemy ``Engine`` 的 ``before/after_cursor_execute`` 事件 | ORM 与裸 SQL 全覆盖，零侵入 |

## 开关语义（``ENABLE_OTEL=false`` 时）

``install_instrumentation()`` 直接 return，**一个 patch 都不打**、provider 不初始化、
ContextVar 不读取 —— 运行时开销为 0，W1~W5-Part1 的 207 条用例行为逐字节不变。

## 语义约定（OTel semconv）

- HTTP：``http.request.method`` / ``url.path`` / ``http.response.status_code`` / ``server.address``
- RPC：``rpc.system="mcp"`` / ``rpc.method="tools/call"`` / ``mcp.tool.name`` / ``mcp.error.code``
- GenAI：``gen_ai.operation.name`` / ``gen_ai.request.model`` / ``gen_ai.usage.input_tokens`` / ``gen_ai.usage.output_tokens``
- DB：``db.system`` / ``db.operation`` / ``db.statement``（参数默认不采集）
- 图：``langgraph.node`` / ``langgraph.step`` / ``task.id`` / ``graph.goal``

## 指标（Prometheus 文本暴露，``GET /metrics``）

- ``agentflow_mcp_calls_total{server,tool,status}`` —— MCP 调用成功率的分母/分子
- ``agentflow_mcp_call_duration_seconds{server,tool}`` —— 直方图样本（曝露 sum/count + p50/p95/p99）
- ``agentflow_llm_tokens_total{model,kind}`` —— token 消耗计数器
- ``agentflow_graph_node_duration_seconds{node}`` —— 节点耗时
- ``agentflow_http_requests_total{method,route,status}``
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections import defaultdict, deque
from typing import Any, Iterator

from app.core.context import get_trace_id
from app.core.logging import get_logger
from app.core.w6_config import w6

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 0) 可选依赖：未安装 SDK 时全部退化为 no-op（不抛错、不影响启动）
# ---------------------------------------------------------------------------
try:  # pragma: no cover - 取决于运行环境
    from opentelemetry import context as otel_context, trace as otel_trace
    from opentelemetry.trace import SpanKind, Status, StatusCode

    HAVE_OTEL_API = True
except Exception:  # noqa: BLE001  # pragma: no cover
    otel_context = None  # type: ignore[assignment]
    otel_trace = None  # type: ignore[assignment]
    HAVE_OTEL_API = False

    class SpanKind:  # type: ignore[no-redef]
        INTERNAL = "internal"
        SERVER = "server"
        CLIENT = "client"
        PRODUCER = "producer"
        CONSUMER = "consumer"

    class StatusCode:  # type: ignore[no-redef]
        OK = "OK"
        ERROR = "ERROR"
        UNSET = "UNSET"

    class Status:  # type: ignore[no-redef]
        def __init__(self, status_code: Any = None, description: str = "") -> None:
            self.status_code = status_code
            self.description = description


try:  # pragma: no cover
    from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor

    # OTel SDK 1.45 起把 ``samplers.py`` 改名为 ``sampling.py``，两个路径都要兼容
    try:
        from opentelemetry.sdk.trace.samplers import ParentBased, TraceIdRatioBased
    except ModuleNotFoundError:  # pragma: no cover - 新旧版本差异
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    HAVE_OTEL_SDK = True
except Exception:  # noqa: BLE001  # pragma: no cover
    HAVE_OTEL_SDK = False


INSTRUMENTATION_NAME = "agentflow.otel"
_TASK_ID_VAR: Any = None
if HAVE_OTEL_API:  # pragma: no branch
    from contextvars import ContextVar

    _TASK_ID_VAR = ContextVar("agentflow_otel_task_id", default=None)
    _GOAL_VAR = ContextVar("agentflow_otel_goal", default=None)
    # ainvoke 内部会调用自身上下文里的 astream，两层 patch 会各开一个 graph.run，
    # 用深度计数把「内层」识别出来，只让最外层建 trace 根 span。
    _GRAPH_DEPTH_VAR = ContextVar("agentflow_otel_graph_depth", default=0)
else:  # pragma: no cover
    _GOAL_VAR = None
    _GRAPH_DEPTH_VAR = None


# ---------------------------------------------------------------------------
# 1) 指标注册表（极简 Prometheus 文本暴露，不强依赖 prometheus_client）
# ---------------------------------------------------------------------------
class MetricsRegistry:
    """进程内计数器 + 直方图样本；``render()`` 输出 Prometheus 文本格式。

    刻意不引第三方依赖：本阶段只需要 5 个指标，自实现 60 行足够，
    且避免为「指标」再拉一个二进制扩展依赖进生产镜像。
    """

    _MAX_SAMPLES = 2048

    def __init__(self, prefix: str = "agentflow") -> None:
        self.prefix = prefix
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], deque[float]] = {}
        self._help: dict[str, str] = {
            "mcp_calls_total": "MCP tool call count by server/tool/status",
            "mcp_call_duration_seconds": "MCP tool call latency in seconds",
            "llm_tokens_total": "LLM token consumption by model and kind",
            "llm_calls_total": "LLM invocation count by model/status",
            "graph_node_duration_seconds": "LangGraph node execution latency in seconds",
            "graph_runs_total": "Graph run count by status",
            "http_requests_total": "HTTP request count by method/route/status",
            "db_calls_total": "Database statement count by operation",
        }

    # ---------------- 写入 ----------------
    def inc(self, name: str, value: float = 1.0, labels: dict[str, Any] | None = None) -> None:
        key = (name, tuple(sorted((k, str(v)) for k, v in (labels or {}).items())))
        with self._lock:
            self._counters[key] += value

    def observe(self, name: str, value: float, labels: dict[str, Any] | None = None) -> None:
        key = (name, tuple(sorted((k, str(v)) for k, v in (labels or {}).items())))
        with self._lock:
            series = self._histograms.get(key)
            if series is None:
                series = deque(maxlen=self._MAX_SAMPLES)
                self._histograms[key] = series
            series.append(float(value))

    # ---------------- 读取 ----------------
    def counter_value(self, name: str, labels: dict[str, Any] | None = None) -> float:
        key = (name, tuple(sorted((k, str(v)) for k, v in (labels or {}).items())))
        with self._lock:
            return float(self._counters.get(key, 0.0))

    def total(self, name: str) -> float:
        """该指标所有 label 组合的总和（跨 series 求和）。"""
        with self._lock:
            return float(sum(v for (metric, _labels), v in self._counters.items() if metric == name))

    def histogram_values(self, name: str, labels: dict[str, Any] | None = None) -> list[float]:
        """样本读取：``labels=None`` 时聚合该指标的所有 label 组合。"""
        with self._lock:
            if labels is None:
                merged: list[float] = []
                for (metric, _labels), values in self._histograms.items():
                    if metric == name:
                        merged.extend(values)
                return merged
            key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
            return list(self._histograms.get(key, ()))

    def percentile(self, name: str, q: float, labels: dict[str, Any] | None = None) -> float:
        values = sorted(self.histogram_values(name, labels))
        if not values:
            return 0.0
        idx = max(0, min(len(values) - 1, int(round(q * (len(values) - 1)))))
        return values[idx]

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()

    # ---------------- 导出 ----------------
    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            counters = dict(self._counters)
            histograms = {k: list(v) for k, v in self._histograms.items()}

        grouped: dict[str, list[tuple[tuple[tuple[str, str], ...], float]]] = defaultdict(list)
        for (name, labels), value in counters.items():
            grouped[name].append((labels, value))
        for name, items in sorted(grouped.items()):
            metric = f"{self.prefix}_{name}"
            lines.append(f"# HELP {metric} {self._help.get(name, name)}")
            lines.append(f"# TYPE {metric} counter")
            for labels, value in sorted(items):
                lines.append(f"{metric}{_fmt_labels(labels)} {value:g}")

        hist_grouped: dict[str, list[tuple[tuple[tuple[str, str], ...], list[float]]]] = defaultdict(list)
        for (name, labels), values in histograms.items():
            hist_grouped[name].append((labels, values))
        for name, items in sorted(hist_grouped.items()):
            metric = f"{self.prefix}_{name}"
            lines.append(f"# HELP {metric} {self._help.get(name, name)}")
            lines.append(f"# TYPE {metric} summary")
            for labels, values in sorted(items):
                ordered = sorted(values)
                total = sum(ordered)
                suffix = _fmt_labels(labels)
                lines.append(f"{metric}_count{suffix} {len(ordered)}")
                lines.append(f"{metric}_sum{suffix} {total:.6f}")
                for q, label in ((0.5, "0.5"), (0.95, "0.95"), (0.99, "0.99")):
                    idx = max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))
                    lines.append(f'{metric}{{quantile="{label}"{suffix[1:] if suffix else ""}}} {ordered[idx]:.6f}')
        if not lines:
            lines.append(f"# no metrics yet (prefix={self.prefix})")
        return "\n".join(lines) + "\n"


def _fmt_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
    return "{" + inner + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


metrics = MetricsRegistry(prefix=w6.OTEL_METRICS_PREFIX)


# ---------------------------------------------------------------------------
# 2) 全局状态
# ---------------------------------------------------------------------------
class _TracingState:
    def __init__(self) -> None:
        self.initialized = False
        self.instrumented = False
        self.provider: Any = None
        self.tracer: Any = None
        self.exporter_name = "none"
        self.fallback_used = False
        self.patches: list[tuple[Any, str, Any]] = []
        self.lock = threading.Lock()


_state = _TracingState()


class _NullSpan:
    """OTel 关闭 / SDK 缺失时的空 span，所有方法都是 no-op。"""

    __slots__ = ("_name",)

    def __init__(self, name: str = "") -> None:
        self._name = name

    def set_attribute(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def set_attributes(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def add_event(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def record_exception(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def set_status(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def end(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def is_recording(self) -> bool:
        return False

    def get_span_context(self) -> Any:
        return None


class _NullTracer:
    def start_span(self, name: str, **_kwargs: Any) -> _NullSpan:
        return _NullSpan(name)

    def start_as_current_span(self, name: str, **_kwargs: Any) -> Any:
        return contextlib.nullcontext(_NullSpan(name))


# ---------------------------------------------------------------------------
# 3) 初始化 / 关停
# ---------------------------------------------------------------------------
def _as_attr(value: Any) -> Any:
    """属性值规范化：只保留 OTel 允许的原始类型，超长截断。"""
    limit = w6.OTEL_MAX_ATTR_LENGTH
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return text[:limit]


def _attrs(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: _as_attr(v) for k, v in payload.items() if v is not None}


def _probe_otlp(endpoint: str, *, timeout: float = 0.4) -> bool:
    """轻量探测 OTLP 端点是否可达（只做 TCP 连接，不发 payload）。"""
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(endpoint)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _build_exporter() -> tuple[Any, str]:
    """按配置构造 exporter；OTLP 不可达时自动降级为控制台。"""
    choice = w6.OTEL_EXPORTER
    if choice == "none":
        return None, "none"

    if choice == "otlp":
        reachable = _probe_otlp(w6.OTEL_EXPORTER_OTLP_ENDPOINT)
        if reachable:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                exporter = OTLPSpanExporter(
                    endpoint=w6.otel_traces_url,
                    headers=w6.otel_headers or None,
                    timeout=w6.OTEL_EXPORTER_TIMEOUT_SECONDS,
                )
                return _FallbackExporter(exporter), "otlp"
            except Exception as exc:  # noqa: BLE001 - 依赖缺失 / 参数错误
                logger.warning("otel_otlp_exporter_unavailable", extra={"error": str(exc)[:200]})
        if not w6.OTEL_AUTO_FALLBACK_CONSOLE:
            logger.warning("otel_otlp_unreachable_stay_silent", extra={"endpoint": w6.OTEL_EXPORTER_OTLP_ENDPOINT})
            return None, "none"
        logger.warning(
            "otel_otlp_unreachable_fallback_console",
            extra={"endpoint": w6.OTEL_EXPORTER_OTLP_ENDPOINT},
        )
        return ConsoleSpanExporter(), "console(fallback)"

    return ConsoleSpanExporter(), "console"


class _FallbackExporter:
    """包一层 exporter：连续导出失败达到阈值后切到控制台，保证「链路不空」。"""

    def __init__(self, inner: Any, *, threshold: int = 3) -> None:
        self._inner = inner
        self._threshold = threshold
        self._failures = 0
        self._console: Any = None

    def export(self, spans: Any) -> Any:
        if self._console is not None:
            return self._console.export(spans)
        try:
            result = self._inner.export(spans)
            self._failures = 0
            return result
        except Exception as exc:  # noqa: BLE001
            self._failures += 1
            if self._failures >= self._threshold:
                logger.warning(
                    "otel_export_fallback_console",
                    extra={"failures": self._failures, "error": str(exc)[:200]},
                )
                self._console = ConsoleSpanExporter()
                return self._console.export(spans)
            return None

    def shutdown(self) -> None:
        for target in (self._console, self._inner):
            shutdown = getattr(target, "shutdown", None)
            if callable(shutdown):
                with contextlib.suppress(Exception):
                    shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        for target in (self._console, self._inner):
            flush = getattr(target, "force_flush", None)
            if callable(flush):
                with contextlib.suppress(Exception):
                    return bool(flush(timeout_millis))
        return True


def init_tracing(*, span_exporter: Any = None, force: bool | None = None) -> bool:
    """初始化 TracerProvider。返回是否真的启用了追踪。

    ``span_exporter`` 供测试注入 ``InMemorySpanExporter``；生产不用传。
    """
    enabled = w6.ENABLE_OTEL if force is None else bool(force)
    if not enabled:
        return False
    if _state.initialized:
        return True
    if not (HAVE_OTEL_API and HAVE_OTEL_SDK):
        logger.warning("otel_sdk_missing_tracing_disabled", extra={"hint": "pip install opentelemetry-sdk"})
        return False

    with _state.lock:
        if _state.initialized:  # double-check
            return True
        resource = Resource.create(
            {
                SERVICE_NAME: w6.OTEL_SERVICE_NAME,
                SERVICE_VERSION: w6.OTEL_SERVICE_VERSION,
                "deployment.environment": w6.OTEL_DEPLOYMENT_ENV,
                "telemetry.sdk.language": "python",
            }
        )
        sampler = ParentBased(TraceIdRatioBased(w6.OTEL_SAMPLE_RATIO))
        provider = TracerProvider(resource=resource, sampler=sampler)

        exporter, name = (span_exporter, "memory") if span_exporter is not None else _build_exporter()
        if exporter is not None:
            if span_exporter is not None:
                provider.add_span_processor(SimpleSpanProcessor(exporter))
            else:
                provider.add_span_processor(
                    BatchSpanProcessor(
                        exporter,
                        max_queue_size=w6.OTEL_BATCH_MAX_QUEUE,
                        max_export_batch_size=w6.OTEL_BATCH_MAX_EXPORT,
                        schedule_delay_millis=w6.OTEL_BATCH_SCHEDULE_DELAY_MS,
                    )
                )
        else:
            name = "none"

        otel_trace.set_tracer_provider(provider)
        _state.provider = provider
        _state.tracer = provider.get_tracer(INSTRUMENTATION_NAME, w6.OTEL_SERVICE_VERSION)
        _state.exporter_name = name
        _state.initialized = True
        logger.info("otel_tracing_initialized", extra={"service": w6.OTEL_SERVICE_NAME, "exporter": name})
        return True


def shutdown_tracing() -> None:
    """flush + 还原所有 patch（测试与优雅关停都用它）。"""
    uninstall_instrumentation()
    with _state.lock:
        provider = _state.provider
        _state.provider = None
        _state.tracer = None
        _state.initialized = False
    if provider is not None:
        with contextlib.suppress(Exception):
            provider.shutdown()


def get_tracer() -> Any:
    if not w6.ENABLE_OTEL or _state.tracer is None:
        return _NullTracer()
    return _state.tracer


def tracing_enabled() -> bool:
    return bool(w6.ENABLE_OTEL and _state.initialized)


def exporter_name() -> str:
    return _state.exporter_name


def current_trace_id() -> str:
    """当前 span 的 trace_id（hex），用于把业务日志与链路对齐。"""
    if not HAVE_OTEL_API or not w6.ENABLE_OTEL:
        return ""
    span = otel_trace.get_current_span()
    ctx = span.get_span_context() if span is not None else None
    if ctx is None or not getattr(ctx, "is_valid", False):
        return ""
    return format(ctx.trace_id, "032x")


def current_span_id() -> str:
    if not HAVE_OTEL_API or not w6.ENABLE_OTEL:
        return ""
    span = otel_trace.get_current_span()
    ctx = span.get_span_context() if span is not None else None
    if ctx is None or not getattr(ctx, "is_valid", False):
        return ""
    return format(ctx.span_id, "016x")


@contextlib.contextmanager
def span(
    name: str,
    *,
    kind: Any = None,
    attributes: dict[str, Any] | None = None,
    record_exception: bool = True,
) -> Iterator[Any]:
    """通用 span 上下文：关闭时返回空 span，调用方无需判断开关。

    任务 12（双轨打通）：开启时自动把自研 Trace 的 ``trace_id``（ContextVar，与
    MySQL trace_logs 同源）注入 ``agentflow.trace_id`` 属性——在 OTel 后端看到
    可疑 span 即可回 MySQL 查完整轨迹，调用方显式传值时以调用方为准。
    """
    if not tracing_enabled():
        yield _NullSpan(name)
        return

    attrs = dict(attributes or {})
    if "agentflow.trace_id" not in attrs:
        own_trace_id = get_trace_id()
        if own_trace_id:
            attrs["agentflow.trace_id"] = own_trace_id

    tracer = _state.tracer
    try:
        cm = tracer.start_as_current_span(
            name,
            kind=kind or SpanKind.INTERNAL,
            attributes=_attrs(attrs),
            record_exception=record_exception,
            set_status_on_exception=record_exception,
        )
    except Exception as exc:  # noqa: BLE001 - 埋点永不打断业务
        logger.warning("otel_span_start_failed", extra={"span": name, "error": str(exc)[:160]})
        yield _NullSpan(name)
        return

    with cm as active:
        try:
            yield active
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(Exception):
                active.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
            raise


def record_span_exception(active: Any, exc: BaseException) -> None:
    with contextlib.suppress(Exception):
        active.record_exception(exc)
        active.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {str(exc)[:180]}"))


def set_task_context(task_id: Any, goal: Any) -> tuple[Any, Any]:
    """在 Graph 入口把 task_id / goal 塞进 ContextVar，供节点 span 读取。"""
    if _TASK_ID_VAR is None:  # pragma: no cover - 无 OTel API 时才走这里
        return (None, None)
    return (_TASK_ID_VAR.set(task_id), _GOAL_VAR.set(goal))


def reset_task_context(tokens: tuple[Any, Any]) -> None:
    task_token, goal_token = tokens
    if _TASK_ID_VAR is None:  # pragma: no cover
        return
    with contextlib.suppress(Exception):
        if goal_token is not None:
            _GOAL_VAR.reset(goal_token)
        if task_token is not None:
            _TASK_ID_VAR.reset(task_token)


# ---------------------------------------------------------------------------
# 4) 埋点一：LangGraph 节点（回调，不改节点）
# ---------------------------------------------------------------------------
def _callback_base() -> Any:
    try:
        from langchain_core.callbacks import BaseCallbackHandler

        return BaseCallbackHandler
    except Exception:  # noqa: BLE001  # pragma: no cover

        class _Base:  # type: ignore[no-redef]
            pass

        return _Base


class GraphNodeSpanHandler(_callback_base()):  # type: ignore[misc]
    """从 LangGraph 回调里识别「节点本体」并生成 span。

    LangGraph 会为每个节点及其后的路由函数（``route_after_*``）都发一次
    ``on_chain_start``，二者 ``metadata['langgraph_node']`` 相同，靠 runnable 名称区分：
    只有名称等于节点名时才开节点 span，路由函数跳过（否则每步会出现两个 span）。
    """

    raise_error = False
    run_inline = True

    def __init__(self, *, task_id: Any = None, goal: Any = None, parent_context: Any = None) -> None:
        super().__init__()
        self._spans: dict[Any, tuple[Any, float, str]] = {}
        self._active_nodes: dict[str, int] = {}
        self._task_id = task_id
        self._goal = goal
        self._parent_context = parent_context

    def bind_parent(self, parent_span: Any) -> None:
        """把后续节点 span 挂到 ``graph.run`` 根 span 之下（回调里 contextvars 不可靠，显式传 context）。"""
        if parent_span is None or not HAVE_OTEL_API:
            return
        try:
            self._parent_context = otel_trace.set_span_in_context(parent_span)
        except Exception:  # noqa: BLE001
            self._parent_context = None

    # ---- 内部 ----
    def _node_name(self, serialized: Any, metadata: dict[str, Any], kwargs: dict[str, Any]) -> str | None:
        node = metadata.get("langgraph_node")
        if not node:
            return None
        name = None
        if isinstance(serialized, dict):
            name = serialized.get("name")
        elif serialized is not None:
            name = getattr(serialized, "name", None)
        if not name:
            name = (kwargs.get("name") if isinstance(kwargs, dict) else None) or None
        # 名称与节点名不一致 → 是 route_* 条件边，跳过
        if name and name != node:
            return None
        return str(node)

    # ---- LangChain 回调 ----
    def on_chain_start(
        self,
        serialized: Any,
        inputs: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        if not tracing_enabled() or not w6.OTEL_TRACE_GRAPH:
            return
        node = self._node_name(serialized, metadata or {}, kwargs)
        if node is None:
            return
        # 同一节点名在回调里会出现两层（外层 RunnableSequence + 内层节点函数），
        # 只给最外层开 span，内层只做一个计数占位，结束时递减。
        if self._active_nodes.get(node):
            self._active_nodes[node] += 1
            self._spans[run_id] = (None, 0.0, node)  # type: ignore[assignment]
            return

        step = (metadata or {}).get("langgraph_step")
        attrs = {
            "langgraph.node": node,
            # 任务 12：agent.name 是节点名的语义别名（Planner/Executor/Reviewer/Summarizer
            # 即多智能体系统的 agent 名），便于 OTel 后端按 agent 维度检索
            "agent.name": node,
            "langgraph.step": step,
            "task.id": self._task_id,
            "graph.goal": self._goal,
            # 双轨关联：自研 MySQL trace 的 trace_id（与 trace_logs 表同源，可互查）
            "agentflow.trace_id": get_trace_id() or None,
            "graph.node_kind": "langgraph_node",
        }
        try:
            self._active_nodes[node] = 1
            active = _state.tracer.start_span(
                f"graph.node {node}",
                context=self._parent_context,
                kind=SpanKind.INTERNAL,
                attributes=_attrs(attrs),
            )
            self._spans[run_id] = (active, time.perf_counter(), node)
        except Exception as exc:  # noqa: BLE001
            logger.warning("otel_graph_span_failed", extra={"node": node, "error": str(exc)[:160]})

    def on_chain_end(self, outputs: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        self._finish(run_id, error=None)

    def on_chain_error(self, error: BaseException, *, run_id: Any = None, **kwargs: Any) -> None:
        self._finish(run_id, error=error)

    def _finish(self, run_id: Any, *, error: BaseException | None) -> None:
        entry = self._spans.pop(run_id, None)
        if entry is None:
            return
        active, started, node = entry
        depth = self._active_nodes.get(node, 0)
        if depth > 0:
            if depth == 1:
                self._active_nodes.pop(node, None)
            else:
                self._active_nodes[node] = depth - 1
        if active is None:  # 内层占位：不产生 span
            return
        elapsed = time.perf_counter() - started
        with contextlib.suppress(Exception):
            active.set_attribute("langgraph.node.duration_ms", round(elapsed * 1000, 2))
            metrics.observe("graph_node_duration_seconds", elapsed, {"node": node})
        if error is not None:
            record_span_exception(active, error)
        with contextlib.suppress(Exception):
            active.end()

    def close_pending(self) -> None:
        """异常退出时兜底结束残留 span（避免 span 泄漏）。"""
        for run_id in list(self._spans):
            self._finish(run_id, error=None)


def _inject_callbacks(config: Any, handler: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = dict(config or {})
    existing = cfg.get("callbacks")
    if existing is None:
        cfg["callbacks"] = [handler]
    elif isinstance(existing, (list, tuple)):
        cfg["callbacks"] = [*existing, handler]
    else:
        cfg["callbacks"] = [existing, handler]
    return cfg


def _graph_input_task(inputs: Any, config: dict[str, Any]) -> tuple[Any, Any]:
    task_id: Any = None
    goal: Any = None
    if isinstance(inputs, dict):
        task_id = inputs.get("task_id")
        goal = inputs.get("goal")
        state = inputs.get("state")
        if isinstance(state, dict):
            task_id = task_id if task_id is not None else state.get("task_id")
            goal = goal if goal is not None else state.get("goal")
    if task_id is None:
        configurable = (config or {}).get("configurable") or {}
        task_id = configurable.get("thread_id")
    return task_id, goal


def _install_graph_patch() -> None:
    try:
        from langgraph.graph.state import CompiledStateGraph
    except Exception:  # noqa: BLE001  # pragma: no cover
        logger.warning("otel_graph_patch_skipped", extra={"reason": "langgraph 未安装"})
        return

    if getattr(CompiledStateGraph, "_otel_patched", False):
        return

    orig_ainvoke = CompiledStateGraph.ainvoke
    orig_astream = CompiledStateGraph.astream
    orig_astream_events = CompiledStateGraph.astream_events

    @contextlib.contextmanager
    def _graph_scope(inputs: Any, config: Any, *, mode: str) -> Iterator[tuple[Any, dict[str, Any]]]:
        """开 ``graph.run`` 根 span → 把节点回调挂到该 span 之下 → 注入 config。

        根 span 先启动，节点 span 才能成为它的子 span（trace 树才是一棵树而不是一堆并列节点）。
        """
        cfg = config if isinstance(config, dict) else {}
        task_id, goal = _graph_input_task(inputs, cfg)
        tokens = set_task_context(task_id, goal)
        depth_token = _GRAPH_DEPTH_VAR.set(_GRAPH_DEPTH_VAR.get() + 1) if _GRAPH_DEPTH_VAR else None
        handler = GraphNodeSpanHandler(task_id=task_id, goal=goal)
        try:
            with span(
                "graph.run",
                attributes={"task.id": task_id, "graph.goal": goal, "graph.mode": mode},
            ) as root:
                handler.bind_parent(root)
                yield handler, _inject_callbacks(config, handler)
        finally:
            handler.close_pending()
            if depth_token is not None:
                _GRAPH_DEPTH_VAR.reset(depth_token)
            reset_task_context(tokens)

    async def patched_ainvoke(self: Any, input: Any, config: Any = None, **kwargs: Any) -> Any:
        if not tracing_enabled() or not w6.OTEL_TRACE_GRAPH:
            return await orig_ainvoke(self, input, config=config, **kwargs)
        if _GRAPH_DEPTH_VAR is not None and _GRAPH_DEPTH_VAR.get() > 0:
            return await orig_ainvoke(self, input, config=config, **kwargs)
        status = "invoke"
        try:
            with _graph_scope(input, config, mode="ainvoke") as (_handler, cfg):
                return await orig_ainvoke(self, input, config=cfg, **kwargs)
        except Exception:
            status = "invoke_error"
            raise
        finally:
            metrics.inc("graph_runs_total", labels={"status": status})

    async def patched_astream(self: Any, input: Any, config: Any = None, **kwargs: Any) -> Any:
        if not tracing_enabled() or not w6.OTEL_TRACE_GRAPH:
            async for chunk in orig_astream(self, input, config=config, **kwargs):
                yield chunk
            return
        if _GRAPH_DEPTH_VAR is not None and _GRAPH_DEPTH_VAR.get() > 0:
            async for chunk in orig_astream(self, input, config=config, **kwargs):
                yield chunk
            return
        with _graph_scope(input, config, mode="astream") as (_handler, cfg):
            async for chunk in orig_astream(self, input, config=cfg, **kwargs):
                yield chunk

    async def patched_astream_events(self: Any, input: Any, config: Any = None, **kwargs: Any) -> Any:
        if not tracing_enabled() or not w6.OTEL_TRACE_GRAPH:
            async for event in orig_astream_events(self, input, config=config, **kwargs):
                yield event
            return
        if _GRAPH_DEPTH_VAR is not None and _GRAPH_DEPTH_VAR.get() > 0:
            async for event in orig_astream_events(self, input, config=config, **kwargs):
                yield event
            return
        status = "stream"
        try:
            with _graph_scope(input, config, mode="astream_events") as (_handler, cfg):
                async for event in orig_astream_events(self, input, config=cfg, **kwargs):
                    yield event
        except Exception:
            status = "stream_error"
            raise
        finally:
            metrics.inc("graph_runs_total", labels={"status": status})

    CompiledStateGraph.ainvoke = patched_ainvoke  # type: ignore[assignment]
    CompiledStateGraph.astream = patched_astream  # type: ignore[assignment]
    CompiledStateGraph.astream_events = patched_astream_events  # type: ignore[assignment]
    CompiledStateGraph._otel_patched = True  # type: ignore[attr-defined]
    _state.patches.append((CompiledStateGraph, "ainvoke", orig_ainvoke))
    _state.patches.append((CompiledStateGraph, "astream", orig_astream))
    _state.patches.append((CompiledStateGraph, "astream_events", orig_astream_events))


# ---------------------------------------------------------------------------
# 5) 埋点二：MCP 调用（stdio / HTTP 共用 MCPClient.call_tool 这一个汇合点）
# ---------------------------------------------------------------------------
def _install_mcp_patch() -> None:
    from app.tools.mcp_client import MCPClient, MCPTransportError, ToolInvocationError

    if getattr(MCPClient, "_otel_patched", False):
        return
    orig = MCPClient.call_tool

    async def patched_call_tool(self: Any, name: str, arguments: dict[str, Any], *, timeout: float | None = None) -> Any:
        if not tracing_enabled() or not w6.OTEL_TRACE_MCP:
            return await orig(self, name, arguments, timeout=timeout)

        from app.utils.masking import mask_mapping

        server = getattr(self, "server_name", "") or "unknown"
        transport_name = type(getattr(self, "transport", object())).__name__
        retry_limit = int(getattr(getattr(self, "transport", None), "retry_times", 0) or 0)
        started = time.perf_counter()
        labels = {"server": server, "tool": name}
        attrs = {
            "rpc.system": "mcp",
            "rpc.method": "tools/call",
            "mcp.server": server,
            "mcp.tool.name": name,
            "mcp.transport": transport_name,
            "mcp.retry.limit": retry_limit,
            "mcp.arguments": _safe_json(mask_mapping(arguments or {})),
            "task.id": _current_task_id(),
        }
        with span(f"mcp.tools/call {name}", kind=SpanKind.CLIENT, attributes=attrs) as active:
            try:
                result = await orig(self, name, arguments, timeout=timeout)
            except ToolInvocationError as exc:
                _finish_mcp_error(active, exc.code, exc)
                metrics.inc("mcp_calls_total", labels={**labels, "status": "error", "code": exc.code})
                metrics.observe("mcp_call_duration_seconds", time.perf_counter() - started, labels)
                raise
            except MCPTransportError as exc:
                _finish_mcp_error(active, exc.code, exc)
                metrics.inc("mcp_calls_total", labels={**labels, "status": "transport_error", "code": exc.code})
                metrics.observe("mcp_call_duration_seconds", time.perf_counter() - started, labels)
                raise
            except Exception as exc:  # noqa: BLE001 - 任何异常都要能定位到工具
                _finish_mcp_error(active, "UNEXPECTED", exc)
                metrics.inc("mcp_calls_total", labels={**labels, "status": "error", "code": "UNEXPECTED"})
                metrics.observe("mcp_call_duration_seconds", time.perf_counter() - started, labels)
                raise

            elapsed = time.perf_counter() - started
            if isinstance(result, dict):
                active.set_attribute("mcp.result.row_count", int(result.get("row_count") or 0))
                active.set_attribute("mcp.result.is_error", bool(result.get("isError")))
            active.set_attribute("mcp.duration_ms", round(elapsed * 1000, 2))
            metrics.inc("mcp_calls_total", labels={**labels, "status": "ok"})
            metrics.observe("mcp_call_duration_seconds", elapsed, labels)
            return result

    MCPClient.call_tool = patched_call_tool  # type: ignore[assignment]
    MCPClient._otel_patched = True  # type: ignore[attr-defined]
    _state.patches.append((MCPClient, "call_tool", orig))


def _finish_mcp_error(active: Any, code: str, exc: BaseException) -> None:
    with contextlib.suppress(Exception):
        active.set_attribute("mcp.error.code", code)
        active.set_attribute("mcp.error.type", type(exc).__name__)
    record_span_exception(active, exc)


# ---------------------------------------------------------------------------
# 6) 埋点三：LLM 调用（token / 模型 / 时延）
# ---------------------------------------------------------------------------
def _install_llm_patch() -> None:
    from app.agents.llm import LLMClient

    if getattr(LLMClient, "_otel_patched", False):
        return
    orig = LLMClient.complete

    async def patched_complete(self: Any, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        if not tracing_enabled() or not w6.OTEL_TRACE_LLM:
            return await orig(self, messages, **kwargs)

        model = str(getattr(getattr(self, "primary", None), "model", "unknown") or "unknown")
        mock_mode = bool(getattr(self, "mock_mode", False))
        provider = "mock" if mock_mode else "primary"
        prompt_chars = sum(len(str(m.get("content") or "")) for m in messages)
        started = time.perf_counter()
        attrs = {
            "gen_ai.operation.name": "chat",
            "gen_ai.system": "mock" if mock_mode else "openai-compatible",
            "gen_ai.request.model": model,
            "gen_ai.request.max_tokens": kwargs.get("max_tokens"),
            "gen_ai.request.temperature": kwargs.get("temperature"),
            "gen_ai.prompt.chars": prompt_chars,
            "task.id": _current_task_id(),
        }
        with span("gen_ai.chat", kind=SpanKind.CLIENT, attributes=attrs) as active:
            try:
                response = await orig(self, messages, **kwargs)
            except Exception as exc:  # noqa: BLE001
                record_span_exception(active, exc)
                metrics.inc("llm_calls_total", labels={"model": model, "status": "error", "provider": provider})
                raise

            elapsed = time.perf_counter() - started
            in_tokens = int(getattr(response, "prompt_tokens", 0) or 0)
            out_tokens = int(getattr(response, "completion_tokens", 0) or 0)
            actual_model = str(getattr(response, "model", model) or model)
            with contextlib.suppress(Exception):
                active.set_attribute("gen_ai.usage.input_tokens", in_tokens)
                active.set_attribute("gen_ai.usage.output_tokens", out_tokens)
                active.set_attribute("gen_ai.usage.total_tokens", in_tokens + out_tokens)
                active.set_attribute("gen_ai.response.model", actual_model)
                active.set_attribute("gen_ai.duration_ms", round(elapsed * 1000, 2))
            metrics.inc("llm_calls_total", labels={"model": actual_model, "status": "ok", "provider": provider})
            metrics.inc("llm_tokens_total", in_tokens, labels={"model": actual_model, "kind": "input"})
            metrics.inc("llm_tokens_total", out_tokens, labels={"model": actual_model, "kind": "output"})
            return response

    LLMClient.complete = patched_complete  # type: ignore[assignment]
    LLMClient._otel_patched = True  # type: ignore[attr-defined]
    _state.patches.append((LLMClient, "complete", orig))


# ---------------------------------------------------------------------------
# 7) 埋点四：SQL（SQLAlchemy 引擎事件，ORM / 裸 SQL 全覆盖）
# ---------------------------------------------------------------------------
def _install_sql_patch() -> None:
    try:
        from sqlalchemy import event
        from sqlalchemy.engine import Engine
    except Exception:  # noqa: BLE001  # pragma: no cover
        return

    if getattr(Engine, "_otel_patched", False):
        return

    stack_key = "_otel_sql_spans"

    def before_cursor_execute(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        if not tracing_enabled() or not w6.OTEL_TRACE_SQL:
            return
        operation = (statement or "").strip().split(" ", 1)[0].upper() or "UNKNOWN"
        attrs = {
            "db.system": "sqlalchemy",
            "db.operation": operation,
            "db.statement": (statement or "")[: w6.OTEL_MAX_ATTR_LENGTH],
            "db.executemany": bool(executemany),
        }
        if w6.OTEL_CAPTURE_SQL_PARAMS:
            attrs["db.parameters"] = _safe_json(parameters)
        try:
            active = _state.tracer.start_span(f"db.{operation.lower()}", kind=SpanKind.CLIENT, attributes=_attrs(attrs))
            conn.info.setdefault(stack_key, []).append((active, time.perf_counter(), operation))
        except Exception:  # noqa: BLE001 - 埋点失败不影响 SQL
            return

    def after_cursor_execute(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        stack = conn.info.get(stack_key) or []
        if not stack:
            return
        active, started, operation = stack.pop()
        elapsed = time.perf_counter() - started
        with contextlib.suppress(Exception):
            active.set_attribute("db.duration_ms", round(elapsed * 1000, 2))
            metrics.inc("db_calls_total", labels={"operation": operation})
            active.end()

    def handle_error(exception_context: Any) -> None:
        conn = getattr(exception_context, "connection", None)
        if conn is None:
            return
        stack = conn.info.get(stack_key) or []
        if not stack:
            return
        active, _, _ = stack.pop()
        record_span_exception(active, getattr(exception_context, "original_exception", Exception("db error")))
        with contextlib.suppress(Exception):
            active.end()

    event.listen(Engine, "before_cursor_execute", before_cursor_execute)
    event.listen(Engine, "after_cursor_execute", after_cursor_execute)
    event.listen(Engine, "handle_error", handle_error)
    Engine._otel_patched = True  # type: ignore[attr-defined]
    _state.patches.append((Engine, "listen:sql", (before_cursor_execute, after_cursor_execute, handle_error)))


# ---------------------------------------------------------------------------
# 8) 安装 / 卸载
# ---------------------------------------------------------------------------
def install_instrumentation(*, force: bool | None = None) -> bool:
    """按开关安装全部埋点。**关闭时不打任何 patch**，运行时零开销。"""
    enabled = w6.ENABLE_OTEL if force is None else bool(force)
    if not enabled:
        logger.info("otel_instrumentation_skipped", extra={"enable_otel": w6.ENABLE_OTEL})
        return False
    if _state.instrumented:
        return True

    if not init_tracing(force=True):
        return False

    with _state.lock:
        if _state.instrumented:
            return True
        if w6.OTEL_TRACE_GRAPH:
            _install_graph_patch()
        if w6.OTEL_TRACE_MCP:
            _install_mcp_patch()
        if w6.OTEL_TRACE_LLM:
            _install_llm_patch()
        if w6.OTEL_TRACE_SQL:
            _install_sql_patch()
        _state.instrumented = True

    logger.info(
        "otel_instrumentation_installed",
        extra={
            "graph": w6.OTEL_TRACE_GRAPH,
            "mcp": w6.OTEL_TRACE_MCP,
            "llm": w6.OTEL_TRACE_LLM,
            "sql": w6.OTEL_TRACE_SQL,
            "exporter": _state.exporter_name,
        },
    )
    return True


def uninstall_instrumentation() -> None:
    """还原全部 patch（测试 teardown / 优雅关停）。"""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    with _state.lock:
        patches, _state.patches = list(_state.patches), []
        _state.instrumented = False

    for target, name, original in patches:
        try:
            if name.startswith("listen:"):
                before, after, err = original  # type: ignore[misc]
                event.remove(Engine, "before_cursor_execute", before)
                event.remove(Engine, "after_cursor_execute", after)
                event.remove(Engine, "handle_error", err)
                Engine._otel_patched = False  # type: ignore[attr-defined]
                continue
            setattr(target, name, original)
            if name == "call_tool":
                target._otel_patched = False
            elif name == "complete":
                target._otel_patched = False
            elif name == "astream_events":
                target._otel_patched = False
        except Exception as exc:  # noqa: BLE001 - 还原失败不应阻塞关停
            logger.warning("otel_uninstall_patch_failed", extra={"target": str(target)[:80], "error": str(exc)[:160]})


def status() -> dict[str, Any]:
    """``/api/v1/observability/status`` 与自检清单使用。"""
    return {
        "enabled": w6.ENABLE_OTEL,
        "initialized": _state.initialized,
        "instrumented": _state.instrumented,
        "exporter": _state.exporter_name,
        "service": w6.OTEL_SERVICE_NAME,
        "sample_ratio": w6.OTEL_SAMPLE_RATIO,
        "sdk_available": HAVE_OTEL_SDK and HAVE_OTEL_API,
        "trace_targets": {
            "http": w6.OTEL_TRACE_HTTP,
            "graph": w6.OTEL_TRACE_GRAPH,
            "mcp": w6.OTEL_TRACE_MCP,
            "llm": w6.OTEL_TRACE_LLM,
            "sql": w6.OTEL_TRACE_SQL,
        },
        "metrics": {
            "mcp_calls_total": metrics.total("mcp_calls_total"),
            "llm_tokens_total": metrics.total("llm_tokens_total"),
            "p95_mcp_duration_seconds": metrics.percentile("mcp_call_duration_seconds", 0.95),
        },
    }


def render_metrics() -> str:
    return metrics.render()


def _current_task_id() -> Any:
    if _TASK_ID_VAR is None:  # pragma: no cover
        return None
    return _TASK_ID_VAR.get()


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)[: w6.OTEL_MAX_ATTR_LENGTH]
    except Exception:  # noqa: BLE001
        return str(value)[: w6.OTEL_MAX_ATTR_LENGTH]


def _reset_for_tests() -> None:
    """仅测试用：清空状态、还原 patch、清空指标。"""
    shutdown_tracing()
    metrics.reset()
    _state.exporter_name = "none"


__all__ = [
    "GraphNodeSpanHandler",
    "MetricsRegistry",
    "current_span_id",
    "current_trace_id",
    "exporter_name",
    "get_tracer",
    "init_tracing",
    "install_instrumentation",
    "metrics",
    "record_span_exception",
    "render_metrics",
    "set_task_context",
    "shutdown_tracing",
    "span",
    "status",
    "tracing_enabled",
    "uninstall_instrumentation",
]
