"""项目一真实 RAGAS 评测适配器（AgentFlow 评测链路的真实后端）。

为什么需要这个模块 —— 一段必须写清楚的更正
--------------------------------------------
AgentFlow 原设计（``eval_run_service.create_run`` 的非 mock 分支）把评测转发给
``EVALAGENT_BASE_URL``（默认 ``http://localhost:8100``），注释称其为「项目二
EvalAgent 接口」。**本机不存在这个服务**：项目二 ``evalagent_vue`` 是纯 Vue3
前端，其 README 开宗明义写着「不新增任何后端服务，全部通过 HTTP 调用上游项目一」。
也就是说 8100 端口从来没有监听者，非 mock 模式必然 502。

真正持有 RAGAS 判分底座的是**项目一**（``企业垂直知识库 Agent 后台``）：
``app/eval/metrics.py`` 里 ``Faithfulness / AnswerRelevancy / ContextPrecision /
ContextRecall`` 四个 ragas 指标，判分 LLM 由 ``get_eval_chat_model`` 提供且
**temperature 硬编码 0.0**（判分要可复现）。项目一还提供了完整的运行态接口：

    POST /api/v1/eval/runs              {"dataset_id": int}  → {"id": int, "status": "pending"}
    GET  /api/v1/eval/runs/{run_id}     → {status, metrics{4项}, total/success/failed_items,
                                           started_at, finished_at, error_msg, model}
    GET  /api/v1/system/config          → {eval_llm_model, ...}（判分模型名，如实记录）

接口口径（刻意如此，勿"顺手优化"）
----------------------------------
1. **指标名沿用 ragas 原名**，不映射成 AgentFlow 原有的
   ``task_completion / plan_f1 / tool_acc``。后者是智能体编排指标，前者是 RAG
   质量指标，两者语义不同。把 0.85 的 faithfulness 填进 ``task_completion``
   会制造一个新的「字段名与真实含义不一致」的伪装 —— 这正是 v1.3 刚根治的病。
   字段缺数据就写 ``None``，前端显示「—」，绝不补 0 或补估算值。
2. **评的是「知识库检索 + 生成链路」**，不是 AgentFlow 端到端多智能体编排质量。
   这条边界必须对使用者明示，否则「AgentFlow 评测 0.87」会被误读成「编排质量 0.87」。
3. **token_cost 不做估算**。项目一的 ``trace_logs`` 只记问答生成调用，RAGAS 判分
   是 ragas 内部直调 langchain，不经过项目一 trace callback，逐 run 拿不到判分用量。
   与其编一个"看起来精确"的数字，不如留 ``None`` 并记录 ``token_cost_basis``
   说明缺口，另用 ``scripts/probe_judge_token_usage.py`` 抽样精测（见文档）。

鉴权复用 ``EXTERNAL_RAG_*`` 服务账号（与 kb-search 同一个后端、同一套凭据）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 项目一评测运行的终态集合（与 eval_service 的状态机一致）
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})

#: ragas 四项指标（与项目一 app/eval/metrics.py::_METRIC_NAMES 严格一致）
RAGAS_METRIC_NAMES = (
    "faithfulness",
    "answer_relevancy",
    "context_precision",
    "context_recall",
)


class ProjectOneEvalError(RuntimeError):
    """项目一评测接口不可用 / 鉴权失败 / 返回体不符合契约。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProjectOneEvalAdapter:
    """项目一 ``/api/v1/eval/*`` 适配器（触发 + 轮询 + 判分模型元信息）。

    复用 ``ProjectOneKbProvider`` 的令牌缓存策略：静态令牌优先，否则账号密码登录，
    进程内缓存 ``EXTERNAL_RAG_TOKEN_TTL_SECONDS`` 秒；收到 401 立即失效重登一次。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        dataset_id: int | None = None,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: float | None = None,
        token_ttl: int | None = None,
    ) -> None:
        self.base_url = (
            base_url
            or settings.PROJECT1_EVAL_BASE_URL
            or settings.EXTERNAL_RAG_BASE_URL
        ).rstrip("/")
        self.dataset_id = int(
            dataset_id if dataset_id is not None else settings.PROJECT1_EVAL_DATASET_ID
        )
        self._static_token = token if token is not None else settings.EXTERNAL_RAG_TOKEN
        self.username = username if username is not None else settings.EXTERNAL_RAG_USERNAME
        self.password = password if password is not None else settings.EXTERNAL_RAG_PASSWORD
        self.timeout = float(timeout or settings.PROJECT1_EVAL_TIMEOUT_SECONDS)
        self.token_ttl = int(
            token_ttl if token_ttl is not None else settings.EXTERNAL_RAG_TOKEN_TTL_SECONDS
        )
        self._token: str | None = None
        self._token_deadline: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        """是否具备调用条件：需要数据集 ID + 某种凭据。"""
        return bool(
            self.dataset_id > 0
            and (self._static_token or (self.username and self.password))
        )

    # ------------------------------------------------------------------
    # 鉴权
    # ------------------------------------------------------------------
    async def _login(self) -> str:
        if not (self.username and self.password):
            raise ProjectOneEvalError(
                "未配置 EXTERNAL_RAG_TOKEN，也未配置 EXTERNAL_RAG_USERNAME/PASSWORD"
            )
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/api/v1/auth/login",
                json={"username": self.username, "password": self.password},
            )
        if response.status_code in (401, 403):
            raise ProjectOneEvalError(
                f"项目一登录失败：{response.text[:200]}", status_code=response.status_code
            )
        response.raise_for_status()
        data = (response.json() or {}).get("data") or {}
        token = str(data.get("access_token") or "")
        if not token:
            raise ProjectOneEvalError("项目一登录响应缺少 access_token")
        logger.info("project1_eval_login_ok", extra={"base_url": self.base_url})
        return token

    async def _ensure_token(self, *, force: bool = False) -> str:
        if self._static_token:
            return self._static_token
        if not force and self._token and time.monotonic() < self._token_deadline:
            return self._token
        async with self._lock:
            if not force and self._token and time.monotonic() < self._token_deadline:
                return self._token
            token = await self._login()
            self._token = token
            self._token_deadline = time.monotonic() + max(60, self.token_ttl)
            return token

    async def _request(
        self, method: str, path: str, *, json: dict | None = None, retry_on_401: bool = True
    ) -> dict[str, Any]:
        token = await self._ensure_token()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(
                method,
                f"{self.base_url}{path}",
                json=json,
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code == 401 and retry_on_401 and not self._static_token:
            # 令牌过期：失效缓存重登一次（与 kb-search 适配器同一策略）
            token = await self._ensure_token(force=True)
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    json=json,
                    headers={"Authorization": f"Bearer {token}"},
                )
        if response.status_code >= 400:
            raise ProjectOneEvalError(
                f"项目一 {method} {path} → {response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
            )
        body = response.json()
        if isinstance(body, dict) and body.get("code") not in (None, 0):
            raise ProjectOneEvalError(f"项目一业务错误：{body.get('message')} (code={body.get('code')})")
        return body

    # ------------------------------------------------------------------
    # 评测接口
    # ------------------------------------------------------------------
    async def create_run(self) -> dict[str, Any]:
        """触发项目一评测；返回 ``{"run_id": int, "status": str}``。"""
        if self.dataset_id <= 0:
            raise ProjectOneEvalError(
                "未配置 PROJECT1_EVAL_DATASET_ID（项目一评测数据集 ID），无法触发真实评测"
            )
        body = await self._request(
            "POST", "/api/v1/eval/runs", json={"dataset_id": int(self.dataset_id)}
        )
        data = (body or {}).get("data") or {}
        run_id = data.get("id")
        if run_id is None:
            raise ProjectOneEvalError("项目一评测接口未返回 run id")
        logger.info(
            "project1_eval_triggered",
            extra={"ext_run_id": int(run_id), "dataset_id": self.dataset_id},
        )
        return {"run_id": int(run_id), "status": str(data.get("status") or "pending")}

    async def get_run(self, ext_run_id: int | str) -> dict[str, Any]:
        """读取项目一评测运行详情（原样返回 data 段）。"""
        body = await self._request("GET", f"/api/v1/eval/runs/{int(ext_run_id)}")
        return (body or {}).get("data") or {}

    async def get_judge_model(self) -> str | None:
        """读取项目一当前判分模型名，用于如实记录「这批分是谁打的」。

        失败返回 None（不阻断评测）：元信息缺失不该让一次已付费的跑批失败。
        """
        try:
            body = await self._request("GET", "/api/v1/system/config")
            cfg = (body or {}).get("data") or {}
            model = cfg.get("eval_llm_model") or cfg.get("llm_model")
            return str(model) if model else None
        except Exception as exc:  # noqa: BLE001 - 元信息失败不阻断主流程
            logger.warning("project1_eval_judge_model_unavailable", extra={"error": str(exc)[:200]})
            return None


def build_metrics_payload(
    upstream: dict[str, Any],
    *,
    judge_model: str | None = None,
    fallback_dataset_label: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把项目一的 RunOut 转成 (AgentFlow metrics, detail 补丁)。

    返回两个 dict：
    - ``metrics``：只放 ragas 四项 + 可得的计数，键名与语义严格对应；
    - ``detail_extra``：来源元信息（``eval_backend`` / ``judge_model`` /
      ``samples_count`` / ``token_cost_basis`` / 耗时），供前端与审计追溯。

    关键取舍：拿不到的字段一律 ``None``，不估算、不补 0。
    """
    raw_metrics = upstream.get("metrics") if isinstance(upstream.get("metrics"), dict) else {}
    metrics: dict[str, Any] = {}
    for name in RAGAS_METRIC_NAMES:
        value = raw_metrics.get(name)
        metrics[name] = float(value) if isinstance(value, (int, float)) else None

    total = int(upstream.get("total_items") or 0)
    success = int(upstream.get("success_items") or 0)
    failed = int(upstream.get("failed_items") or 0)

    # 耗时：用项目一的 started_at/finished_at（真实评测墙钟），拿不到就留 None
    duration_s: float | None = None
    started, finished = upstream.get("started_at"), upstream.get("finished_at")
    if started and finished:
        try:
            from datetime import datetime

            fmt_set = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f")
            for fmt in fmt_set:
                try:
                    t0 = datetime.strptime(str(started), fmt)
                    t1 = datetime.strptime(str(finished), fmt)
                    duration_s = round((t1 - t0).total_seconds(), 1)
                    break
                except ValueError:
                    continue
        except Exception:  # noqa: BLE001 - 时间解析失败不影响指标
            duration_s = None

    metrics["sample_count"] = total or None
    metrics["success_items"] = success
    metrics["failed_items"] = failed
    metrics["duration_s"] = duration_s
    # 真实评测的成本口径：项目一不逐 run 记判分 token（ragas 内部直调，不经 trace），
    # 因此这里不填数字。缺口与抽样精测方法见 docs/真实RAGAS评测-v1.4.md。
    metrics["token_cost"] = None

    detail_extra: dict[str, Any] = {
        "eval_backend": "project1_ragas",
        "judge_model": judge_model,
        "judge_temperature": 0.0,
        "answer_model": upstream.get("model"),
        "samples_count": total or None,
        "success_items": success,
        "failed_items": failed,
        "duration_s": duration_s,
        "token_cost_basis": "not_measured_per_run",
        "metrics_scope": "kb_retrieval_chain",
        "upstream_run_id": upstream.get("id"),
    }
    if fallback_dataset_label:
        detail_extra["dataset_label"] = fallback_dataset_label
    return metrics, detail_extra


__all__ = [
    "ProjectOneEvalAdapter",
    "ProjectOneEvalError",
    "RAGAS_METRIC_NAMES",
    "TERMINAL_STATUSES",
    "build_metrics_payload",
]
