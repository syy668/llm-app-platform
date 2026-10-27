"""LLM 客户端：OpenAI 兼容接口 + 主备降级（DeepSeek-V3 → SiliconFlow Qwen2.5-72B）。

设计要点：
- **统一 OpenAI 兼容协议**：``POST {base_url}/chat/completions``，不绑定任何厂商 SDK；
- **降级链**（PRD 4.2）：主模型任意失败（超时 / 5xx / 429）→ 备用模型重试 1 次；
- **Mock 兜底**：``LLM_MOCK=true`` 或不配置 API Key 时使用确定性 Mock 客户端，
  本地无网也能跑通全链路（Mock 仅覆盖"能跑通"，不承诺规划质量）；
- Token 记账：优先取响应 ``usage``，缺失时用本地估算，保证 ``token_ledger`` 不为空。
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.context import get_task_id
from app.core.exceptions import AppException
from app.core.logging import get_logger
from app.core.w2_config import w2
from app.utils.tokens import estimate_messages_tokens, estimate_tokens

logger = get_logger(__name__)

# Mock 客户端依赖的提示词标记（由 agents/prompts.py 注入 system 消息）
MOCK_MARKER_PLANNER = "AGENTFLOW:TASK=PLANNER"
MOCK_MARKER_EXECUTOR = "AGENTFLOW:TASK=EXECUTOR"
MOCK_MARKER_REVIEWER = "AGENTFLOW:TASK=REVIEWER"
MOCK_MARKER_SUMMARIZER = "AGENTFLOW:TASK=SUMMARIZER"


class LLMError(RuntimeError):
    def __init__(self, message: str, *, code: str = "LLM_ERROR", retryable: bool = True) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


class TokenBudgetExceededError(AppException):
    """任务 9：单任务 Token 累计消耗达到上限（防重试风暴）。

    继承 ``AppException`` 而非 ``LLMError`` 是刻意的：
    - ``LLMClient.complete`` 的主备降级只捕获 ``LLMError``，预算熔断**不会**触发备用模型重试；
    - 执行图中节点的宽泛 ``except Exception`` 需为它放行（executor 已加 re-raise 分支），
      最终由 ``_drive``/``run`` 的图级异常处理统一落 ``FAILED`` + 错误码落库。
    """

    def __init__(self, *, task_id: int, used: int, limit: int) -> None:
        self.task_id = task_id
        self.used_tokens = used
        self.limit = limit
        super().__init__(
            "TOKEN_BUDGET_EXCEEDED",
            f"任务 {task_id} 的 LLM Token 消耗（{used}）已达单任务上限（{limit}），执行已熔断",
            status_code=429,
            detail={"task_id": task_id, "used_tokens": used, "limit": limit},
        )


@dataclass(slots=True)
class LLMResponse:
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    provider: str = "primary"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class BaseLLMClient(ABC):
    model: str = "unknown"

    @abstractmethod
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = True,
    ) -> LLMResponse: ...


class OpenAICompatibleClient(BaseLLMClient):
    """标准 OpenAI Chat Completions 客户端（DeepSeek / SiliconFlow 均适用）。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int | None = None,
        provider: str = "primary",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout or w2.LLM_TIMEOUT_SECONDS
        self.provider = provider

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = True,
    ) -> LLMResponse:
        if not self.api_key:
            raise LLMError("未配置 API Key", code="LLM_NOT_CONFIGURED", retryable=False)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": w2.LLM_TEMPERATURE if temperature is None else temperature,
            "max_tokens": max_tokens or w2.LLM_MAX_TOKENS,
            "stream": False,
        }
        if json_mode:
            # DeepSeek / Qwen 均支持 json_object；不支持时服务端会忽略该字段
            payload["response_format"] = {"type": "json_object"}

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions", json=payload, headers=headers
                )
        except httpx.TimeoutException as exc:
            raise LLMError(f"LLM 超时（{self.model}）", code="LLM_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 网络错误: {exc}", code="LLM_UNAVAILABLE") from exc

        if response.status_code >= 400:
            body = response.text[:300]
            retryable = response.status_code in (408, 429) or response.status_code >= 500
            raise LLMError(
                f"LLM HTTP {response.status_code}: {body}",
                code="LLM_HTTP_ERROR",
                retryable=retryable,
            )

        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM 响应结构异常: {str(data)[:300]}", code="LLM_BAD_RESPONSE") from exc

        usage = data.get("usage") or {}
        return LLMResponse(
            content=content,
            model=data.get("model") or self.model,
            prompt_tokens=int(usage.get("prompt_tokens") or estimate_messages_tokens(messages)),
            completion_tokens=int(usage.get("completion_tokens") or estimate_tokens(content)),
            provider=self.provider,
            raw={"id": data.get("id"), "finish_reason": data.get("choices", [{}])[0].get("finish_reason")},
        )


class MockLLMClient(BaseLLMClient):
    """确定性 Mock 客户端：无 API Key / 离线 / CI 时保证图能跑通。

    不做"智能规划"，只按提示词里的任务标记返回结构合法的 JSON；
    真实规划质量由 DeepSeek 提供（``LLM_MOCK=false``）。
    """

    model = "mock-llm"

    def __init__(self, responses: list[str] | None = None) -> None:
        self._queue: list[str] = list(responses or [])

    def push(self, *contents: str) -> None:
        self._queue.extend(contents)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = True,
    ) -> LLMResponse:
        prompt = "\n".join(str(m.get("content") or "") for m in messages)
        last_user = next(
            (str(m.get("content") or "") for m in reversed(messages) if m.get("role") == "user"), prompt
        )
        if self._queue:
            content = self._queue.pop(0)
        elif MOCK_MARKER_PLANNER in prompt:
            content = json.dumps(self._mock_plan(last_user), ensure_ascii=False)
        elif MOCK_MARKER_EXECUTOR in prompt:
            content = json.dumps({"tool": "none", "reason": "mock 未选择工具"}, ensure_ascii=False)
        elif MOCK_MARKER_REVIEWER in prompt:
            content = json.dumps(
                {
                    "scores": {"evidence": 4, "faithfulness": 4, "consistency": 4, "completeness": 4, "format": 4},
                    "total": 4.0,
                    "pass": True,
                    "suggestions": [],
                },
                ensure_ascii=False,
            )
        elif MOCK_MARKER_SUMMARIZER in prompt:
            content = json.dumps(
                {
                    "markdown": self._mock_summary(prompt),
                    "cited_charts": [],
                    "cited_docs": [],
                    "degraded": False,
                },
                ensure_ascii=False,
            )
        else:
            content = "{}"
        return LLMResponse(
            content=content,
            model=self.model,
            prompt_tokens=estimate_messages_tokens(messages),
            completion_tokens=estimate_tokens(content),
            provider="mock",
        )

    @staticmethod
    def _mock_plan(prompt: str) -> dict[str, Any]:
        """按目标关键词拼装最小可用 DAG（3~6 个子任务），仅供本地自测。"""
        # 只看"目标："那一行，避免把 system prompt 里的示例关键词也算进来
        goal_line = next((line for line in prompt.splitlines() if line.strip().startswith("目标：")), prompt)
        lowered = goal_line.lower()
        subtasks: list[dict[str, Any]] = [
            {
                "seq": 1,
                "name": "取数：上周核心指标",
                "description": "查询目标时间窗内的 GMV、订单量、转化率等核心指标",
                "tool_hint": "sql_query",
                "depends_on": [],
                "risk_level": "normal",
                "acceptance": "返回至少 1 行指标数据",
            },
            {
                "seq": 2,
                "name": "归因：环比与品类下钻",
                "description": "对比上一周期，定位跌幅最大的品类与转化率变化",
                "tool_hint": "none",
                "depends_on": [1],
                "risk_level": "normal",
                "acceptance": "给出至少 2 条带数据的归因结论",
            },
        ]
        seq = 3
        if any(key in lowered for key in ("周报", "库存", "检索", "历史")):
            subtasks.append(
                {
                    "seq": seq,
                    "name": "检索历史周报佐证",
                    "description": "检索历史周报中与该异常相关的记录",
                    "tool_hint": "kb_search",
                    "depends_on": [2],
                    "risk_level": "normal",
                    "acceptance": "返回至少 1 条相关片段",
                }
            )
            seq += 1
        if any(key in lowered for key in ("图", "chart", "可视化", "饼图", "趋势")):
            subtasks.append(
                {
                    "seq": seq,
                    "name": "生成对比图表",
                    "description": "按品类绘制 GMV 环比对比图",
                    "tool_hint": "chart_render",
                    "depends_on": [2],
                    "risk_level": "normal",
                    "acceptance": "返回可访问的图表 URL",
                }
            )
            seq += 1
        subtasks.append(
            {
                "seq": seq,
                "name": "汇总生成分析报告",
                "description": "整合取数与归因结论，输出 markdown 报告",
                "tool_hint": "none",
                "depends_on": [s["seq"] for s in subtasks],
                "risk_level": "normal",
                "acceptance": "报告含结论、证据与图表引用",
            }
        )
        if any(key in lowered for key in ("通知", "推送", "发到", "运营群")):
            subtasks.append(
                {
                    "seq": seq + 1,
                    "name": "推送运营群通知",
                    "description": "将报告摘要推送到运营群（高风险，需审批）",
                    "tool_hint": "notify",
                    "depends_on": [seq],
                    "risk_level": "high",
                    "acceptance": "消息发送成功并返回 message_id",
                }
            )
        return {
            "subtasks": subtasks,
            "assumptions": ["时间范围以任务入参为准", "Mock 规划仅用于本地自测"],
            "replan_count": 0,
        }

    @staticmethod
    def _mock_summary(prompt: str) -> str:
        return (
            "## 经营分析报告（Mock）\n\n"
            "### 一、结论\n"
            "- 目标周期核心指标整体承压，主因集中在单一品类的转化率下滑。\n"
            "- 历史周报已记录相关库存风险，形成「缺货 → 流量结构恶化 → 转化率下跌」链条。\n\n"
            "### 二、证据\n"
            "- 取数结果见子任务 1 明细（含 GMV 与转化率）。\n"
            "- 归因结论见子任务 2（品类下钻）。\n\n"
            "### 三、建议\n"
            "1. 优先恢复爆款 SKU 供给；2. 复核该品类投放预算；3. 监控长尾流量承接效率。\n"
        )


class LLMClient:
    """主备门面：主模型失败自动切换备用模型（仅一次，避免放大故障）。"""

    def __init__(
        self,
        primary: BaseLLMClient | None = None,
        fallback: BaseLLMClient | None = None,
        *,
        mock: bool | None = None,
    ) -> None:
        self.mock_mode = w2.LLM_MOCK if mock is None else mock
        # 任务 9：防御参数读自配置、存为实例属性，测试可直接覆写（不污染全局配置）
        self.hard_timeout = w2.LLM_CALL_TOTAL_TIMEOUT_SECONDS
        self.max_tokens_per_task = w2.MAX_TOKENS_PER_TASK
        self.usage_ttl = w2.TASK_TOKEN_USAGE_TTL_SECONDS
        if self.mock_mode:
            self.primary: BaseLLMClient = primary or MockLLMClient()
            self.fallback: BaseLLMClient | None = None
            return

        self.primary = primary or OpenAICompatibleClient(
            base_url=w2.LLM_BASE_URL, api_key=w2.LLM_API_KEY, model=w2.LLM_MODEL, provider="deepseek"
        )
        if fallback is not None:
            self.fallback = fallback
        elif w2.LLM_FALLBACK_ENABLED and w2.LLM_FALLBACK_API_KEY:
            self.fallback = OpenAICompatibleClient(
                base_url=w2.LLM_FALLBACK_BASE_URL,
                api_key=w2.LLM_FALLBACK_API_KEY,
                model=w2.LLM_FALLBACK_MODEL,
                provider="siliconflow",
            )
        else:
            self.fallback = None

    # ---------------- 任务 9：任务级 Token 记账与硬熔断 ----------------
    @staticmethod
    def _usage_key(task_id: int) -> str:
        return f"task:token_usage:{task_id}"

    async def _check_task_budget(self) -> None:
        """请求前预检当前任务的累计 Token；达到上限即熔断，强制中断执行图。

        task_id 来自任务 7 注入的 ``task_id_var``（bind/unbind 生命周期内有效），
        未绑定（离线调用 / 评测等场景）不拦截；Redis 故障 fail-open（只留日志）。
        """
        task_id = get_task_id()
        if task_id is None or self.max_tokens_per_task <= 0:
            return
        try:
            from app.core.redis_client import get_shared_redis

            used = int(await get_shared_redis().get(self._usage_key(task_id)) or 0)
        except Exception as exc:  # noqa: BLE001 - 计数器故障不得阻断业务
            logger.warning(
                "task_token_budget_check_failed", extra={"task_id": task_id, "error": str(exc)[:200]}
            )
            return
        if used >= self.max_tokens_per_task:
            logger.warning(
                "task_token_budget_exceeded",
                extra={"task_id": task_id, "used": used, "limit": self.max_tokens_per_task},
            )
            raise TokenBudgetExceededError(task_id=task_id, used=used, limit=self.max_tokens_per_task)

    async def _record_task_usage(self, response: LLMResponse) -> None:
        """响应返回后立即 INCRBY 累计（TTL 24h）；旁路记账，失败只留 warning。"""
        task_id = get_task_id()
        if task_id is None or response.total_tokens <= 0 or self.usage_ttl <= 0:
            return
        try:
            from app.core.redis_client import get_shared_redis

            key = self._usage_key(task_id)
            await get_shared_redis().incrby(key, int(response.total_tokens))
            await get_shared_redis().expire(key, self.usage_ttl)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "task_token_usage_record_failed", extra={"task_id": task_id, "error": str(exc)[:200]}
            )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = True,
    ) -> LLMResponse:
        # 任务 9：发请求前先做任务级预算预检（AppException 不走主备降级）
        await self._check_task_budget()

        llm_exc: LLMError | None = None
        response: LLMResponse | None = None
        try:
            # 任务 9：外层 asyncio.wait_for 硬性总超时。不能在 except TimeoutError 里
            # 直接 raise 新异常——3.13 的 wait_for 内部是 asyncio.timeout 上下文，会与
            # 取消簿记冲突（加固任务 2 已踩过），所以先捕获再在 try 之外统一处理。
            response = await asyncio.wait_for(
                self.primary.complete(
                    messages, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode
                ),
                timeout=self.hard_timeout,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            llm_exc = LLMError(
                f"LLM 调用总超时（>{self.hard_timeout:g}s，model={self.primary.model}）",
                code="LLM_TIMEOUT",
            )
        except LLMError as exc:
            llm_exc = exc

        if llm_exc is not None:
            if self.fallback is None:
                raise llm_exc
            logger.warning(
                "llm_primary_failed_switch_fallback",
                extra={"error": str(llm_exc)[:200], "code": llm_exc.code, "fallback_model": self.fallback.model},
            )
            await asyncio.sleep(0.1)
            fb_exc: LLMError | None = None
            try:
                response = await asyncio.wait_for(
                    self.fallback.complete(
                        messages, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode
                    ),
                    timeout=self.hard_timeout,
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                fb_exc = LLMError(
                    f"LLM 备用模型调用总超时（>{self.hard_timeout:g}s，model={self.fallback.model}）",
                    code="LLM_TIMEOUT",
                )
            if fb_exc is not None:
                raise fb_exc

        assert response is not None  # 上面两条路径必有一条已赋值
        await self._record_task_usage(response)
        return response


__all__ = [
    "LLMError",
    "TokenBudgetExceededError",
    "LLMResponse",
    "BaseLLMClient",
    "OpenAICompatibleClient",
    "MockLLMClient",
    "LLMClient",
    "MOCK_MARKER_PLANNER",
    "MOCK_MARKER_EXECUTOR",
    "MOCK_MARKER_REVIEWER",
    "MOCK_MARKER_SUMMARIZER",
]
