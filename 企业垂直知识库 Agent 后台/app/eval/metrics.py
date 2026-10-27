"""RAGAS 四项指标计算封装。

- 兼容 ragas 0.4.x 与 langchain_community 0.4.x（后者移除了 vertexai 子模块，
  而 ragas 0.4.x 无条件导入它），在 import ragas 前注入占位 shim。
- 【批次2 统一】判分用的 judge LLM 不再自己 new ChatOpenAI，改为走 `app/llm` 工厂
  （purpose="eval" 套用 EVAL_LLM_TIMEOUT / EVAL_LLM_MAX_RETRIES），
  与问答链路共用同一套 provider 配置，UI 切换供应商后判分同步生效。
- 【v1.7 解耦】判分模型改走 `get_eval_chat_model()`：配置页填了 eval_llm_model
  就用独立判分模型（模型名/地址/密钥可分离），留空则回退复用对话模型。
  目的是消除「生成与判分同一个模型」的自评偏差，同时保持未配置时的旧行为。
  注意：provider=mock 时判分同样使用 MockChatModel，指标无实际语义，
  真实评测请在系统配置页把 provider 切到 openai/anthropic。
- Embedding 复用 app/rag 的向量化逻辑。
"""
import logging
import sys
import types
import warnings

import pandas as pd
from langchain_core.embeddings import Embeddings

from ..config import settings
from ..llm import get_eval_chat_model
from ..rag import embeddings as app_embeddings

logger = logging.getLogger(__name__)


def _install_vertexai_shim() -> None:
    """补齐 langchain_community.chat_models.vertexai 占位模块（本项目不使用 VertexAI）。"""
    if "langchain_community.chat_models.vertexai" in sys.modules:
        return
    mod = types.ModuleType("langchain_community.chat_models.vertexai")

    class ChatVertexAI:  # 占位类，仅满足 ragas 的无条件导入
        pass

    mod.ChatVertexAI = ChatVertexAI
    sys.modules["langchain_community.chat_models.vertexai"] = mod


_install_vertexai_shim()
warnings.filterwarnings("ignore", category=DeprecationWarning)

from ragas import evaluate, RunConfig  # noqa: E402
from ragas.dataset_schema import EvaluationDataset, SingleTurnSample  # noqa: E402
from ragas.embeddings import LangchainEmbeddingsWrapper  # noqa: E402
from ragas.llms import LangchainLLMWrapper  # noqa: E402
from ragas.metrics import (  # noqa: E402
    AnswerRelevancy,
    ContextPrecision,
    ContextRecall,
    Faithfulness,
)

_METRIC_NAMES = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")


class RagasScoringUnavailable(RuntimeError):
    """判分链路不可用，导致指标无法产出。

    【v1.17 新增】为什么必须有这个异常，而不是继续返回一堆 None：
    线上真实故障 run 63/64/65 —— 判分模型 `qwen3.7-flash-2026-07-15` 的免费额度耗尽，
    上游对每次调用返回 403。ragas 内部把指标异常吞成 NaN，于是每个样本都"评测成功"
    但四项指标全是 NULL：进度条 30/30 正常走完、状态 done、指标列全是「—」。
    30 条样本的检索与生成算力全部白花，而界面没有任何一处能看出是故障。

    `retryable=False`：这类故障是配置性的（额度/密钥/模型名），重试不会变好，
    只会把同一次 403 再打 N 遍。调用方据此跳过重试，直接记失败并上报原因。
    """

    retryable = False


def _eval_model_label() -> str:
    """判分模型名（仅用于错误文案；不参与任何判定）。"""
    try:
        from ..core import runtime_config

        return str(
            runtime_config.get_value("eval_llm_model")
            or runtime_config.get_value("llm_model")
            or "-"
        )
    except Exception:  # pragma: no cover - 文案取不到不能影响主流程
        return "-"



class _AppEmbeddings(Embeddings):
    """把项目已有向量化逻辑包装成 langchain Embeddings，供 ragas 使用。"""

    def embed_documents(self, texts):
        return app_embeddings.embed_texts(list(texts))

    def embed_query(self, text):
        return app_embeddings.embed_texts([text])[0]


class RagasEvaluator:
    """RAGAS 评测器：LLM / Embedding / 指标只构建一次，可复用。

    LLM 取值优先级与问答链路一致：系统配置页覆盖值 > .env（runtime_config 内部处理），
    保证 UI 切换模型/供应商后评测判分与问答同步生效。
    """

    def __init__(self):
        # 【v1.7】判分 LLM 走 get_eval_chat_model：配置了 eval_llm_model 则用独立
        # 判分模型，否则回退复用对话模型（默认行为不变）。
        # purpose="eval" 自动套用 EVAL_LLM_TIMEOUT / EVAL_LLM_MAX_RETRIES 的超时与重试策略。
        # 判分需要稳定复现，故固定 temperature=0（不读配置页的采样温度）。
        self._judge_model = get_eval_chat_model(temperature=0.0)
        llm = LangchainLLMWrapper(self._judge_model)
        embeddings = LangchainEmbeddingsWrapper(_AppEmbeddings())
        self.metrics = [
            Faithfulness(llm=llm),
            AnswerRelevancy(llm=llm, embeddings=embeddings),
            ContextPrecision(llm=llm),
            ContextRecall(llm=llm),
        ]
        self._preflight_passed = False

    # ------------------------------------------------------------------
    # 【v1.17】开跑前探活：把「配置性故障」挡在第一个样本之前
    # ------------------------------------------------------------------
    def preflight(self, *, force: bool = False) -> None:
        """用一次最小请求验证判分模型可用；不可用则抛 `RagasScoringUnavailable`。

        为什么值得花这一次调用：不探活的话，一个跑 30 个样本的任务会在
        每个样本上各失败 4 次判分调用（且每次都要先跑完检索与生成），
        最后得到一份「状态 done、指标全空」的假成功。一次探活的成本是几十 token，
        换来的是「任务一开始就带着可读原因失败」。
        """
        if self._preflight_passed and not force:
            return
        try:
            self._judge_model.invoke("ping")
        except Exception as exc:  # noqa: BLE001 - 统一翻译成可读原因
            from ..core.llm_errors import llm_error_hint

            raise RagasScoringUnavailable(
                f"判分模型不可用（{_eval_model_label()}）：{llm_error_hint(exc)}"
            ) from None
        self._preflight_passed = True

    def evaluate_one(self, question: str, answer: str, contexts: list[str], reference: str) -> dict:
        """计算单条样本的四项指标，返回 {metric: float|None}。

        【v1.17】四项**全部**为空时抛 `RagasScoringUnavailable`，不再返回全 None：
        全空只可能来自判分链路故障（单条样本的内容问题通常只影响其中一两项，
        例如 contexts 为空只打穿 faithfulness/context_precision）。返回全 None 等于
        让故障伪装成「已评测、分数为空」，调用方无从分辨。
        """
        sample = SingleTurnSample(
            user_input=question,
            response=answer,
            retrieved_contexts=list(contexts or []),
            reference=reference,
        )
        # 【B1-1 修复】ragas RunConfig 默认 timeout=180s，长样本判分超时后指标静默变
        # None 丢失；显式传入 run_config 上调整体超时（默认 600s，EVAL_RAGAS_TIMEOUT 可配）。
        result = evaluate(
            dataset=EvaluationDataset(samples=[sample]),
            metrics=self.metrics,
            run_config=RunConfig(timeout=settings.eval_ragas_timeout),
        )
        row = result.to_pandas().iloc[0]

        out = {}
        for name in _METRIC_NAMES:
            try:
                v = float(row[name])
                out[name] = None if pd.isna(v) else round(v, 6)
            except Exception:
                out[name] = None

        if all(v is None for v in out.values()):
            # force=True：探活在 run 启动时通过过，此刻仍全空说明判分链路在运行中挂了
            # （额度耗尽最典型），重探一次拿到真实原因，比抛「四项指标均未产出」有用。
            self.preflight(force=True)
            raise RagasScoringUnavailable(
                f"四项指标均未产出（判分模型 {_eval_model_label()} 未返回可解析结果），"
                "该样本不计入成功，请检查判分模型配置"
            )
        missing = [k for k, v in out.items() if v is None]
        if missing:
            logger.warning(
                "部分指标未产出 metrics=%s contexts=%s answer_len=%s",
                ",".join(missing), len(contexts or []), len(answer or ""),
            )
        return out
