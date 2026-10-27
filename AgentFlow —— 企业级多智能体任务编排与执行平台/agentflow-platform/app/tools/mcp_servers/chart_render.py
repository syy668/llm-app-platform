"""MCP Server：chart-render（PRD FR-006）。

要点：
- 数据 ≤500 行，超限截断并置 ``truncated``；
- 字段名与文本值做字符白名单校验，防 ECharts option 注入；
- 渲染在**独立进程池**执行（单图 15s 超时），避免 matplotlib 占用事件循环；
- 同时返回 base64 图片与落盘的 ``/static`` URL（PRD 输出 Schema + W2 要求的 base64）。
"""

from __future__ import annotations

import asyncio
import base64
import io
import re
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from typing import Any

from app.core.logging import get_logger
from app.core.w2_config import w2
from app.schemas.mcp import ChartRenderInput, ChartRenderOutput, McpErrorCode
from app.tools.mcp_stdio import FunctionTool, MCPServerApp, ToolError, run_server

logger = get_logger(__name__)

SERVER_NAME = "chart-render"
TOOL_NAME = "chart-render"

# 字段名 / 文案字符白名单（中英数下划线空格与常见标点），阻断 JS 注入片段
_FIELD_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fa5]{1,32}$")
_TEXT_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fa5\s\-—:：,，.。%()/（）]{1,100}$")
_INVALID_X_CHARS = re.compile(r"[<>{}$`\"']")

_SIZES = {"s": (4.8, 3.0, 90), "m": (7.2, 4.2, 110), "l": (9.6, 5.4, 130)}

_pool: ProcessPoolExecutor | None = None


def _get_pool() -> ProcessPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(max_workers=w2.CHART_POOL_SIZE)
    return _pool


def validate_payload(args: ChartRenderInput) -> tuple[list[dict[str, Any]], bool]:
    """字段 / 类型校验 + 行数截断。失败抛 CHART_DATA_INVALID。"""
    if not args.data:
        raise ToolError(McpErrorCode.CHART_DATA_INVALID, "data 为空，拒绝生成空图")
    if not _FIELD_RE.match(args.x_field):
        raise ToolError(McpErrorCode.CHART_DATA_INVALID, f"x_field 非法：{args.x_field}")
    for field in args.y_fields:
        if not _FIELD_RE.match(field):
            raise ToolError(McpErrorCode.CHART_DATA_INVALID, f"y_field 非法：{field}")
    if args.title and not _TEXT_RE.match(args.title):
        raise ToolError(McpErrorCode.CHART_DATA_INVALID, "title 含非法字符")

    rows = args.data
    truncated = False
    if len(rows) > w2.CHART_MAX_ROWS:
        rows = rows[: w2.CHART_MAX_ROWS]
        truncated = True

    cleaned: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ToolError(McpErrorCode.CHART_DATA_INVALID, f"第 {index + 1} 行不是对象")
        if args.x_field not in row:
            raise ToolError(McpErrorCode.CHART_DATA_INVALID, f"第 {index + 1} 行缺少 x_field={args.x_field}")
        item: dict[str, Any] = {"x": _stringify(row[args.x_field])}
        for field in args.y_fields:
            if field not in row:
                raise ToolError(McpErrorCode.CHART_DATA_INVALID, f"第 {index + 1} 行缺少字段 {field}")
            try:
                item[field] = float(row[field])
            except (TypeError, ValueError) as exc:
                raise ToolError(
                    McpErrorCode.CHART_DATA_INVALID,
                    f"第 {index + 1} 行字段 {field} 非数值：{row[field]!r}",
                ) from exc
        cleaned.append(item)
    return cleaned, truncated


def _stringify(value: Any) -> str:
    text = str(value)
    if _TEXT_RE.match(text):
        return text
    return _INVALID_X_CHARS.sub("", text)[:100]


_AXIS_RATIO = 100.0


def _split_axes(rows: list[dict[str, Any]], y_fields: list[str]) -> tuple[list[str], list[str]]:
    """按量纲把指标拆到主/次坐标轴，避免「GMV 1.4e8 + 订单量 200」同轴时小指标被压成 0。

    判据：某指标的量级低于最大指标量级的 1/100，则视为不可同轴（放到次轴）。
    返回 (主轴字段, 次轴字段)；数据量级相近时次轴为空，行为与升级前完全一致。
    """
    peaks: dict[str, float] = {}
    for field in y_fields:
        values = []
        for row in rows:
            value = row.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            values.append(abs(float(value)))
        peaks[field] = max(values, default=0.0)
    overall = max(peaks.values(), default=0.0)
    if overall <= 0:
        return list(y_fields), []
    primary = [field for field in y_fields if peaks[field] >= overall / _AXIS_RATIO]
    secondary = [field for field in y_fields if field not in primary]
    return primary or list(y_fields[:1]), secondary


def build_echarts_option(args: ChartRenderInput, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """生成 ECharts option（不含任何可执行脚本，纯 JSON 结构）。"""
    title = {"text": args.title or ""}
    if args.chart_type == "pie":
        y = args.y_fields[0]
        return {
            "title": title,
            "tooltip": {"trigger": "item"},
            "legend": {"orient": "vertical", "left": "left"},
            "series": [
                {
                    "type": "pie",
                    "radius": "60%",
                    "data": [{"name": r["x"], "value": r[y]} for r in rows],
                }
            ],
        }
    primary, secondary = _split_axes(rows, list(args.y_fields))
    y_axis: list[dict[str, Any]] = [{"type": "value"}]
    if secondary:
        # 次轴：量纲差两个数量级以上的指标单独成轴，否则小指标在图上恒为 0 高度
        y_axis.append({"type": "value", "splitLine": {"show": False}})
    series = [
        {"type": args.chart_type, "name": field, "data": [r[field] for r in rows]}
        for field in primary
    ]
    series.extend(
        {"type": args.chart_type, "name": field, "data": [r[field] for r in rows], "yAxisIndex": 1}
        for field in secondary
    )
    return {
        "title": title,
        "tooltip": {"trigger": "axis"},
        "legend": {"data": list(args.y_fields)},
        "xAxis": {"type": "category", "data": [r["x"] for r in rows]},
        "yAxis": y_axis if len(y_axis) > 1 else {"type": "value"},
        "series": series,
    }


_SERIES_COLORS = ("#2f6fed", "#f2884b", "#37b26c", "#8b5cf6")


def _amount_formatter(value: float, _pos: int = 0) -> str:
    """金额轴刻度中文化：1.4e8 → 1.40亿，避免图上出现 "1e8" 这类不可读标注。"""
    magnitude = abs(value)
    if magnitude >= 1e8:
        return f"{value / 1e8:.2f}亿"
    if magnitude >= 1e4:
        return f"{value / 1e4:.0f}万"
    return f"{value:.0f}"


def render_png(payload: dict[str, Any]) -> bytes:
    """matplotlib 同步渲染（在子进程中执行，禁止用 pyplot 全局状态）。"""
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import rcParams
    from matplotlib.figure import Figure
    from matplotlib.ticker import FuncFormatter

    rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "PingFang SC", "DejaVu Sans"]
    rcParams["axes.unicode_minus"] = False

    chart_type = payload["chart_type"]
    rows = payload["rows"]
    y_fields = payload["y_fields"]
    size = payload.get("size", "m")
    width, height, dpi = _SIZES.get(size, _SIZES["m"])

    figure = Figure(figsize=(width, height), dpi=dpi)
    axes = figure.subplots()
    x_values = [r["x"] for r in rows]
    primary, secondary = _split_axes(rows, list(y_fields))
    axes2 = axes.twinx() if secondary and chart_type != "pie" else None

    if chart_type == "pie":
        y = y_fields[0]
        axes.pie([r[y] for r in rows], labels=x_values, autopct="%1.1f%%")
    elif chart_type == "line":
        for index, field in enumerate(primary):
            axes.plot(x_values, [r[field] for r in rows], marker="o", label=field,
                      color=_SERIES_COLORS[index % len(_SERIES_COLORS)])
        if axes2 is not None:
            for index, field in enumerate(secondary):
                axes2.plot(x_values, [r[field] for r in rows], marker="s", linestyle="--", label=field,
                           color=_SERIES_COLORS[(len(primary) + index) % len(_SERIES_COLORS)])
        axes.grid(True, linestyle="--", alpha=0.4)
    else:
        width_bar = 0.8 / max(len(y_fields), 1)
        # 组内并排、组间留白：直接 i+offset 会让相邻类别柱体贴死，演示图上难以区分
        span = width_bar * len(y_fields)
        centers = [i * 1.25 for i in range(len(rows))]
        for index, field in enumerate(y_fields):
            offsets = [center - span / 2 + width_bar * (index + 0.5) for center in centers]
            target = axes if field in primary or axes2 is None else axes2
            target.bar(offsets, [r[field] for r in rows], width=width_bar * 0.92, label=field,
                       color=_SERIES_COLORS[index % len(_SERIES_COLORS)], edgecolor="white", linewidth=0.6)
        axes.set_xticks(centers)
        axes.set_xticklabels(x_values, rotation=30, ha="right")

    if chart_type != "pie":
        # 图例统一放到轴下方：双轴场景下默认"best"位置会压住柱体
        handles, labels = axes.get_legend_handles_labels()
        if axes2 is not None:
            handles2, labels2 = axes2.get_legend_handles_labels()
            handles, labels = handles + handles2, labels + labels2
        if handles:
            axes.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, -0.16),
                        ncol=min(len(handles), 3), frameon=False)
        # 主/次轴刻度按量纲中文化，避免 1e8 科学计数法污染演示截图
        if not all(isinstance(r.get(primary[0]), (int, float)) and abs(float(r.get(primary[0]) or 0)) < 1e5 for r in rows):
            axes.yaxis.set_major_formatter(FuncFormatter(_amount_formatter))
        if axes2 is not None and secondary:
            axes2.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v:.0f}"))

    if payload.get("title"):
        axes.set_title(payload["title"])

    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", bbox_inches="tight")
    return buffer.getvalue()


class ChartRenderTool:
    name = TOOL_NAME
    description = (
        "根据结构化数据渲染折线图 / 柱状图 / 饼图，返回 base64 图片、静态 URL 与 echarts_option。"
        "data 最多 500 行，字段需为数值。"
    )

    def __init__(self, *, timeout: float | None = None, use_process_pool: bool | None = None) -> None:
        self.timeout = timeout or float(w2.CHART_RENDER_TIMEOUT_SECONDS)
        self.use_process_pool = w2.CHART_USE_PROCESS_POOL if use_process_pool is None else use_process_pool

    async def __call__(self, args: ChartRenderInput) -> dict[str, Any]:
        started = time.perf_counter()
        rows, truncated = validate_payload(args)
        option = build_echarts_option(args, rows)
        payload = {
            "chart_type": args.chart_type,
            "rows": rows,
            "y_fields": list(args.y_fields),
            "title": args.title,
            "size": args.size.value if hasattr(args.size, "value") else str(args.size),
        }

        try:
            image_bytes = await self._render(payload)
        except asyncio.TimeoutError as exc:
            raise ToolError(McpErrorCode.RENDER_TIMEOUT, f"图表渲染超过 {self.timeout}s") from exc
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("chart_render_failed")
            raise ToolError(McpErrorCode.RENDER_FAILED, f"渲染失败: {exc}") from exc

        chart_id = f"chart_{uuid.uuid4().hex[:12]}"
        image_url = await asyncio.to_thread(self._persist, chart_id, image_bytes)

        output = ChartRenderOutput(
            chart_id=chart_id,
            image_base64=base64.b64encode(image_bytes).decode("ascii"),
            image_url=image_url,
            echarts_option=option,
            render_ms=int((time.perf_counter() - started) * 1000),
            truncated=truncated,
            rows_used=len(rows),
        )
        return output.model_dump()

    async def _render(self, payload: dict[str, Any]) -> bytes:
        if not self.use_process_pool:
            # 测试 / 低配环境：进程内渲染，保留超时语义
            return await asyncio.wait_for(asyncio.to_thread(render_png, payload), timeout=self.timeout)
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(_get_pool(), render_png, payload), timeout=self.timeout
        )

    def _persist(self, chart_id: str, image_bytes: bytes) -> str:
        directory = w2.chart_output_path
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{chart_id}.png"
        path.write_bytes(image_bytes)
        return f"{w2.CHART_STATIC_URL_PREFIX}/{chart_id}.png"


def build_app(tool: ChartRenderTool | None = None) -> MCPServerApp:
    return MCPServerApp(
        SERVER_NAME,
        [
            FunctionTool(
                name=TOOL_NAME,
                description=ChartRenderTool.description,
                input_model=ChartRenderInput,
                handler=tool or ChartRenderTool(),
            )
        ],
    )


def main() -> None:  # pragma: no cover - 子进程入口
    run_server(build_app())


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "SERVER_NAME",
    "TOOL_NAME",
    "validate_payload",
    "build_echarts_option",
    "render_png",
    "ChartRenderTool",
    "build_app",
    "main",
]
