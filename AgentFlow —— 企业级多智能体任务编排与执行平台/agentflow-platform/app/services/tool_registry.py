"""MCP 工具注册表（v1.1 任务 1）：工具元信息 + 启停开关 + 运行时过滤。

三件事，**都不改 MCP 协议层与工具实现**：

1. **元信息**：把 4 个内置工具（kb-search / sql-query / chart-render / notify）的入参、
   出参、能力说明、风险级别、依赖配置整理成一份可读目录，供前端"工具注册表"页展示；
   入参 Schema 与 ``app/schemas/mcp.py`` 的 Pydantic 模型逐字段对齐（此处是给人和前端看的
   展示视图，真正的校验仍在 MCP Server 内部，避免出现"两套校验"分叉）。
2. **启停开关**：状态存 Redis Hash（``agentflow:tool:registry``），无 Redis 时退回进程内字典
   （降级不影响页面可用，只是重启后回到默认值）；每次变更写一条审计（``agentflow:tool:audit``）。
3. **运行时生效**：``create_runtime`` 装配 graph 前调用 ``filter_enabled_tools``，被禁用的工具
   不再出现在 Planner 的 ``tools`` 列表里（模型看不到即不会规划），Executor 也不会拿到它的
   签名。**工具进程本身仍在连接池里**——不做热摘除，避免影响已挂起任务的断点续跑。

设计边界（勿越界）：
- 不修改 ``app/tools/kb_adapter.py``（知识库适配器已验收）；
- 不修改 MCP Server / Client（协议层零改动）；
- 过滤失败（Redis 抖动、依赖未就绪）**一律放行全部工具**，绝不因为注册表故障阻塞任务。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from app.core.config import settings
from app.core.logging import get_logger
from app.schemas.agent_io import ToolDef

logger = get_logger(__name__)

#: Redis Hash：工具名 -> "1"(启用) / "0"(禁用)
REGISTRY_KEY = "tool:registry"
#: Redis List：变更审计（LPUSH，最多保留 200 条）
AUDIT_KEY = "tool:audit"
#: 审计保留条数
AUDIT_MAX = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _mask(value: str) -> str:
    """配置值脱敏：只暴露"是否已配置"，不泄露密钥内容。"""
    if not value:
        return ""
    if len(value) <= 6:
        return "***"
    return f"{value[:3]}***{value[-2:]}"


@dataclass(frozen=True, slots=True)
class ToolMeta:
    """一个内置工具的静态元信息（代码内定义，不从 LLM / 远端读取）。"""

    name: str
    display_name: str
    server: str
    category: str
    description: str
    capability: str
    risk_level: str
    external: bool
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    config_keys: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    default_enabled: bool = True
    # 任务 11：工具契约版本。底层接口升级（v1 -> v2）时在此登记，
    # 实际路由由 ToolVersionRouter 按 w2.TOOL_VERSION_ROUTES 决定（Agent 无感知）。
    version: str = "v1"


#: 内置工具目录。键名与 MCP ``tools/list`` 返回的 ``name`` 完全一致（大小写敏感）。
TOOL_CATALOG: dict[str, ToolMeta] = {
    "kb-search": ToolMeta(
        name="kb-search",
        display_name="知识库检索",
        server="kb-search",
        category="retrieval",
        description=(
            "检索企业知识库（历史电商经营周报），返回最相关的 1~5 条片段与得分。"
            "用于为归因结论补充历史证据（如库存风险、投放调整）。"
        ),
        capability=(
            "把用户目标里的症状词（断货 / 转化率 / 预算）转成检索语句，取回带 doc_id 的原文片段。"
            "MOCK_EXTERNAL_RAG=true 时返回内置 20 份周报样本；false 时走项目一"
            "「企业垂直知识库 Agent 后台」的 POST /api/v1/kbs/{kb_id}/query（服务账号鉴权 + 令牌缓存），"
            "失败按「熔断 → mall 只读库」顺序兜底，绝不阻塞编排。"
        ),
        risk_level="low",
        external=True,
        input_schema={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 200, "description": "检索语句"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3, "description": "返回片段数"},
                "tenant": {"type": "string", "maxLength": 64, "default": "agentflow", "description": "租户标识"},
            },
        },
        output_schema={
            "chunks": "片段数组：doc_id / title / content(≤300字) / score / source",
            "latency_ms": "工具内耗时（毫秒）",
            "degraded": "是否降级（true 表示未拿到真实证据）",
            "error_code": "降级原因：KB_TIMEOUT / KB_UNAVAILABLE / KB_AUTH_FAILED",
        },
        config_keys=(
            "MOCK_EXTERNAL_RAG",
            "EXTERNAL_RAG_CONTRACT",
            "EXTERNAL_RAG_BASE_URL",
            "EXTERNAL_RAG_KB_ID",
            "EXTERNAL_RAG_USERNAME",
        ),
        notes=(
            "禁用后 Planner 不再规划 kb 检索，评审的「引用忠实度」维度会按无引用扣分",
            "空结果（库中确实没有相关内容）不算失败，不触发熔断",
        ),
    ),
    "sql-query": ToolMeta(
        name="sql-query",
        display_name="只读 SQL 查询",
        server="sql-query",
        category="database",
        description=(
            "在只读电商数据仓库执行单条 SELECT 查询，返回列与行。仅允许查询 "
            "orders / traffic_records / ad_campaigns / skus / categories，自动追加 LIMIT，禁止任何写操作。"
        ),
        capability=(
            "5 道安全闸门：SQL 语法解析（sqlglot AST）、表白名单、强制 LIMIT、扫描行数上限、只读账号。"
            "是全部量化结论的主数据源（GMV / 转化率 / 库存周转）。"
        ),
        risk_level="medium",
        external=False,
        input_schema={
            "type": "object",
            "required": ["sql"],
            "properties": {
                "sql": {"type": "string", "minLength": 1, "maxLength": 4000, "description": "单条 SELECT 语句"},
                "max_rows": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
            },
        },
        output_schema={
            "columns": "列名数组",
            "rows": "二维数组（按 columns 顺序）",
            "row_count": "返回行数",
            "truncated": "是否被 LIMIT 截断",
            "exec_ms": "执行耗时（毫秒）",
            "scanned_rows": "优化器估算扫描行数",
            "applied_limit": "实际生效的 LIMIT",
        },
        config_keys=("SQL_ALLOWED_TABLES", "SQL_MAX_ROWS", "SQL_RO_DB", "SQL_RO_USER"),
        notes=("禁用后任务只能做定性分析，评审「结论有据性」会明显下降",),
    ),
    "chart-render": ToolMeta(
        name="chart-render",
        display_name="图表渲染",
        server="chart-render",
        category="visualization",
        description=(
            "根据结构化数据渲染折线图 / 柱状图 / 饼图，返回 base64 图片、静态 URL 与 echarts_option。"
            "data 最多 500 行，字段需为数值。"
        ),
        capability="服务端 Agg 后端出图（无 GUI 依赖），产物落 storage/charts 并通过 /static/charts 暴露。",
        risk_level="low",
        external=False,
        input_schema={
            "type": "object",
            "required": ["x_field", "y_fields"],
            "properties": {
                "chart_type": {"type": "string", "enum": ["line", "bar", "pie"], "default": "bar"},
                "title": {"type": "string", "maxLength": 100},
                "x_field": {"type": "string", "minLength": 1, "maxLength": 64},
                "y_fields": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 5},
                "data": {"type": "array", "items": {"type": "object"}, "maxItems": 2000},
                "size": {"type": "string", "enum": ["S", "M", "L"], "default": "M"},
            },
        },
        output_schema={
            "image_base64": "PNG base64（前端可直接渲染）",
            "url": "静态地址 /static/charts/xxx.png",
            "echarts_option": "ECharts option（前端交互式重绘）",
            "latency_ms": "渲染耗时",
            "degraded": "渲染失败时为 true",
        },
        config_keys=("CHART_MAX_ROWS", "CHART_OUTPUT_DIR", "CHART_USE_PROCESS_POOL"),
        notes=("禁用后报告只剩表格，产物区不再有图片",),
    ),
    "notify": ToolMeta(
        name="notify",
        display_name="经营通知（Mock 渠道）",
        server="notify",
        category="notification",
        description=(
            "向运营群发送经营分析通知（Mock 渠道，不出网）。调用前系统必须已存在 APPROVED 审批记录，"
            "否则返回 APPROVAL_MISSING。"
        ),
        capability=(
            "把最终结论推送到群（飞书 / 钉钉 / 企微通道枚举），**恒 dry-run**：只落库不真正出网。"
            "与 human_gate 强绑定——没有 APPROVED 审批单直接拒绝。"
        ),
        risk_level="high",
        external=False,
        input_schema={
            "type": "object",
            "required": ["title", "content"],
            "properties": {
                "channel": {"type": "string", "enum": ["feishu", "dingtalk", "wecom"], "default": "feishu"},
                "title": {"type": "string", "minLength": 1, "maxLength": 50},
                "content": {"type": "string", "minLength": 1, "maxLength": 2000},
                "receivers": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
            },
        },
        output_schema={
            "status": "SENT / QUEUED",
            "message_id": "Mock 消息 ID",
            "channel": "实际通道",
            "dry_run": "恒 true（不出网）",
        },
        config_keys=("NOTIFY_DRY_RUN", "NOTIFY_MAX_RECEIVERS"),
        notes=("高危工具：命中该工具的计划会先经 human_gate 人工审批",),
    ),
}

#: 配置项的展示口径（值在运行时从 settings 读取并脱敏）
_CONFIG_PURPOSE: dict[str, str] = {
    "MOCK_EXTERNAL_RAG": "知识库 Mock 开关：true=内置周报语料，false=项目一真实检索",
    "EXTERNAL_RAG_CONTRACT": "检索契约：project1_kb_query / legacy_kb_search / mall_only",
    "EXTERNAL_RAG_BASE_URL": "项目一后端根地址（演示环境为 8010）",
    "EXTERNAL_RAG_KB_ID": "检索目标知识库 ID",
    "EXTERNAL_RAG_USERNAME": "服务账号（自动登录换令牌，进程内缓存 25 分钟）",
    "SQL_ALLOWED_TABLES": "SQL 表白名单",
    "SQL_MAX_ROWS": "单次查询最大返回行数",
    "SQL_RO_DB": "只读库名",
    "SQL_RO_USER": "只读账号",
    "CHART_MAX_ROWS": "单图最大数据行数",
    "CHART_OUTPUT_DIR": "图表产物落盘目录",
    "CHART_USE_PROCESS_POOL": "是否用进程池渲染（避免 matplotlib 阻塞事件循环）",
    "NOTIFY_DRY_RUN": "通知 dry-run（恒 true）",
    "NOTIFY_MAX_RECEIVERS": "单条通知最大接收人数",
}


def _config_snapshot(keys: Sequence[str]) -> list[dict[str, str]]:
    hints: list[dict[str, str]] = []
    for key in keys:
        raw = getattr(settings, key, None)
        if raw is None:
            raw = getattr(_w2(), key, None)
        if isinstance(raw, bool):
            text = "true" if raw else "false"
        else:
            text = "" if raw is None else str(raw)
        sensitive = any(token in key.upper() for token in ("PASSWORD", "SECRET", "TOKEN", "KEY"))
        hints.append(
            {
                "key": key,
                "value": _mask(text) if sensitive else text,
                "purpose": _CONFIG_PURPOSE.get(key, ""),
                "sensitive": "true" if sensitive else "false",
            }
        )
    return hints


def _w2() -> Any:
    """惰性取 W2 配置（避免模块导入期就有配置副作用）。"""
    from app.core.w2_config import w2

    return w2


@dataclass
class ToolRegistry:
    """工具开关存储。``redis`` 为 None 时走进程内字典（降级）。"""

    redis: Any = None
    _memory: dict[str, bool] = field(default_factory=dict)

    # ---------------- 存储层 ----------------
    def _key(self, name: str) -> str:
        return settings.key(REGISTRY_KEY)

    async def _read_states(self) -> dict[str, bool]:
        if self.redis is not None:
            try:
                raw = await self.redis.hgetall(self._key("all"))
                return {str(k): str(v) in {"1", "true", "True"} for k, v in (raw or {}).items()}
            except Exception as exc:  # noqa: BLE001 - 存储故障不能挡住页面
                logger.warning("tool_registry_read_failed", extra={"error": str(exc)[:200]})
        return dict(self._memory)

    async def _write_state(self, name: str, enabled: bool, *, operator: str, reason: str) -> None:
        payload = "1" if enabled else "0"
        self._memory[name] = enabled
        if self.redis is None:
            return
        try:
            await self.redis.hset(self._key("all"), name, payload)
            record = json.dumps(
                {"at": _now_iso(), "tool": name, "enabled": enabled, "operator": operator, "reason": reason},
                ensure_ascii=False,
            )
            await self.redis.lpush(settings.key(AUDIT_KEY), record)
            await self.redis.ltrim(settings.key(AUDIT_KEY), 0, AUDIT_MAX - 1)
        except Exception as exc:  # noqa: BLE001
            logger.warning("tool_registry_write_failed", extra={"tool": name, "error": str(exc)[:200]})

    # ---------------- 查询 ----------------
    async def states(self) -> dict[str, bool]:
        stored = await self._read_states()
        return {
            name: bool(stored.get(name, meta.default_enabled)) for name, meta in TOOL_CATALOG.items()
        }

    async def is_enabled(self, name: str) -> bool:
        meta = TOOL_CATALOG.get(name)
        if meta is None:
            return True  # 未知工具不拦截（未来新增工具默认可用）
        stored = await self._read_states()
        return bool(stored.get(name, meta.default_enabled))

    async def list_tools(self) -> list[dict[str, Any]]:
        stored = await self._read_states()
        audit = await self.recent_audit()
        last: dict[str, dict[str, Any]] = {}
        for item in audit:
            last.setdefault(str(item.get("tool")), item)
        circuit_states = await self._read_circuit_states()
        items: list[dict[str, Any]] = []
        for name, meta in TOOL_CATALOG.items():
            record = last.get(name) or {}
            items.append(
                {
                    "name": name,
                    "display_name": meta.display_name,
                    "server": meta.server,
                    "category": meta.category,
                    "description": meta.description,
                    "capability": meta.capability,
                    "risk_level": meta.risk_level,
                    "external": meta.external,
                    "version": meta.version,
                    "input_schema": meta.input_schema,
                    "output_schema": meta.output_schema,
                    "config_hints": _config_snapshot(meta.config_keys),
                    "notes": list(meta.notes),
                    "enabled": bool(stored.get(name, meta.default_enabled)),
                    # 加固任务 2：熔断器只读状态（连续失败 5 次 → 60s 冷却），与人工开关独立
                    "circuit_open": bool(circuit_states.get(name)),
                    "updated_at": record.get("at"),
                    "updated_by": record.get("operator"),
                }
            )
        return items

    async def _read_circuit_states(self) -> dict[str, bool]:
        """读熔断器标记（加固任务 2）。读失败一律视为未熔断（fail-open）。"""
        if self.redis is None:
            return {}
        try:
            names = list(TOOL_CATALOG.keys())
            keys = [settings.key("tool:circuit", name) for name in names]
            values = await self.redis.mget(keys)
            return {
                name: bool(value)
                for name, value in zip(names, values or [])
            }
        except Exception as exc:  # noqa: BLE001 - 保护机制故障不挡页面
            logger.warning("tool_circuit_read_failed", extra={"error": str(exc)[:200]})
            return {}

    async def recent_audit(self, limit: int = 20) -> list[dict[str, Any]]:
        if self.redis is None:
            return []
        try:
            rows = await self.redis.lrange(settings.key(AUDIT_KEY), 0, max(0, limit - 1))
        except Exception:  # noqa: BLE001
            return []
        items: list[dict[str, Any]] = []
        for row in rows or []:
            try:
                items.append(json.loads(row))
            except (TypeError, ValueError):
                continue
        return items

    # ---------------- 变更 ----------------
    async def set_enabled(
        self, name: str, enabled: bool, *, operator: str = "system", reason: str = ""
    ) -> dict[str, Any]:
        if name not in TOOL_CATALOG:
            raise KeyError(name)
        await self._write_state(name, enabled, operator=operator, reason=reason[:200])
        logger.info("tool_toggled", extra={"tool": name, "enabled": enabled, "operator": operator})
        for item in await self.list_tools():
            if item["name"] == name:
                return item
        raise KeyError(name)


async def filter_enabled_tools(tools: Iterable[ToolDef], *, registry: ToolRegistry | None = None) -> list[ToolDef]:
    """运行时过滤：去掉被禁用的工具。**任何异常都退回全量**（可用性优先）。"""
    items = list(tools)
    try:
        reg = registry or ToolRegistry(redis=_shared_redis())
        states = await reg.states()
    except Exception as exc:  # noqa: BLE001
        logger.warning("tool_filter_skipped", extra={"error": str(exc)[:200]})
        return items
    kept = [tool for tool in items if states.get(tool.name, True)]
    if not kept:
        # 全部禁用会让 Planner 无工具可用、必然产出空计划：宁可退回全量也不让任务必失败
        logger.warning("tool_filter_all_disabled_fallback", extra={"total": len(items)})
        return items
    disabled = [tool.name for tool in items if tool.name not in {t.name for t in kept}]
    if disabled:
        logger.info("tools_filtered_out", extra={"disabled": disabled, "kept": len(kept)})
    return kept


def _shared_redis() -> Any:
    """惰性取共享 Redis；不可用时返回 None（注册表自动降级为进程内）。"""
    try:
        from app.core.redis_client import get_shared_redis

        return get_shared_redis()
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "AUDIT_KEY",
    "REGISTRY_KEY",
    "TOOL_CATALOG",
    "ToolMeta",
    "ToolRegistry",
    "filter_enabled_tools",
]
