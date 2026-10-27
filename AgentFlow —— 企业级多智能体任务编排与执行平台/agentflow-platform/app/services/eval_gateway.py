"""项目二 EvalAgent 适配层（PRD FR-013 / A-02）。

职责：把「引用忠实度」这类需要 RAGAS 的评分外包给项目二，接口失败时**降级**为本地规则
（绝不阻塞主流程）。开关 ``MOCK_EXTERNAL_EVAL``：

- ``true``（默认）：直接返回 ``faithfulness=None``，表示"用本地规则"，不发任何网络请求；
- ``false``：``POST {eval}/api/v1/eval/runs`` 创建评测 → 轮询 ``GET /eval/runs/{id}`` 取 metrics；
  失败重试 1 次（间隔 2s），仍失败返回 ``degraded=True, faithfulness=None``。

W4 的评测看板（FR-016）会复用 ``create_run`` / ``get_run``，这里一并封装但不在 W2 路由暴露。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.schemas.agent_io import EvidenceRef

logger = get_logger(__name__)

REVIEWER_DATASET_ID = "agentflow-reviewer-gate"


def is_mock() -> bool:
    return settings.MOCK_EXTERNAL_EVAL


@dataclass(slots=True)
class EvalScore:
    """faithfulness 为 None 表示"外部不可用，请走本地规则"。"""

    faithfulness: float | None = None
    degraded: bool = False
    source: str = "mock"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.faithfulness is not None


class EvalGateway:
    def __init__(
        self,
        *,
        mock: bool | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.mock = is_mock() if mock is None else mock
        self.base_url = (base_url or settings.EXTERNAL_EVAL_BASE_URL).rstrip("/")
        self.timeout = timeout or settings.EXTERNAL_EVAL_TIMEOUT_SECONDS
        self._transport = transport

    # ---------------- 内部 ----------------
    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout, transport=self._transport)

    async def _evaluate(self, payload: dict[str, Any]) -> EvalScore:
        last_error: str | None = None
        for attempt in range(2):  # 首次 + 重试 1 次（PRD 4.2）
            try:
                async with self._client() as client:
                    created = await client.post(f"{self.base_url}/api/v1/eval/runs", json=payload)
                    created.raise_for_status()
                    run_id = (created.json().get("data") or {}).get("run_id") or created.json().get("id")
                    if not run_id:
                        raise ValueError("eval 接口未返回 run_id")

                    metrics = await self._poll(client, str(run_id))
                score = metrics.get("faithfulness")
                if score is None:
                    return EvalScore(None, degraded=True, source="external", raw=metrics)
                # 外部返回 0~1，映射为 1~5 分制（对齐 ReviewScores）
                return EvalScore(
                    faithfulness=round(1 + 4 * float(score), 2), degraded=False, source="external", raw=metrics
                )
            except (httpx.HTTPError, asyncio.TimeoutError, ValueError, KeyError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("eval_http_failed", extra={"attempt": attempt + 1, "error": last_error[:200]})
                if attempt == 0:
                    await asyncio.sleep(2.0)

        logger.warning("eval_degraded", extra={"error": last_error})
        return EvalScore(None, degraded=True, source="external", raw={"error": last_error})

    async def _poll(self, client: httpx.AsyncClient, run_id: str, attempts: int = 3) -> dict[str, Any]:
        metrics: dict[str, Any] = {}
        for index in range(attempts):
            response = await client.get(f"{self.base_url}/api/v1/eval/runs/{run_id}")
            response.raise_for_status()
            body = response.json()
            data = body.get("data") or body
            metrics = data.get("metrics") or {}
            if metrics:
                return metrics
            if index < attempts - 1:
                await asyncio.sleep(0.5)
        return metrics

    # ---------------- 对外 ----------------
    async def faithfulness(self, report_digest: str, evidence: list[EvidenceRef]) -> EvalScore:
        if self.mock:
            logger.info("eval_mock_mode", extra={"evidence": len(evidence)})
            return EvalScore(None, degraded=False, source="mock")

        payload = {
            "dataset_id": REVIEWER_DATASET_ID,
            "target": "agentflow",
            "input": {
                "report": report_digest[:8000],
                "evidence": [item.model_dump() for item in evidence[:20]],
            },
            "metrics": ["faithfulness"],
        }
        return await self._evaluate(payload)

    async def create_run(self, dataset_id: str, variant: str | None = None) -> dict[str, Any]:
        """W4 评测看板使用（回归 / A/B）。Mock 下返回占位 run_id。"""
        if self.mock:
            return {"run_id": f"mock-{dataset_id}", "status": "DONE", "mock": True}
        payload = {"dataset_id": dataset_id, "target": "agentflow", "variant": variant}
        async with self._client() as client:
            response = await client.post(f"{self.base_url}/api/v1/eval/runs", json=payload)
            response.raise_for_status()
            return response.json()

    async def get_run(self, run_id: str) -> dict[str, Any]:
        if self.mock:
            return {"run_id": run_id, "status": "DONE", "metrics": {}, "mock": True}
        async with self._client() as client:
            response = await client.get(f"{self.base_url}/api/v1/eval/runs/{run_id}")
            response.raise_for_status()
            return response.json()


_default_gateway: EvalGateway | None = None


def get_eval_gateway() -> EvalGateway:
    global _default_gateway
    if _default_gateway is None:
        _default_gateway = EvalGateway()
    return _default_gateway


__all__ = ["is_mock", "EvalScore", "EvalGateway", "get_eval_gateway", "REVIEWER_DATASET_ID"]
