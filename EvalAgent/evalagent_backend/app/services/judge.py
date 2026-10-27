"""RAGAS 裁判层：在项目二自己的进程里打分。

三个必须记住的坑（都在这里被挡住）：
1. **思考型模型 content 为空** → 必须传 enable_thinking=false，否则 RAGAS 解析不到 JSON，
   指标静默变 NaN，看起来像"跑成功了但没分"。
2. **RAGAS 把异常吞成 NaN** → 全空必须显式抛 RagasScoringUnavailable，不能当成功落库。
3. **非 OpenAI 官方端点必须 check_embedding_ctx_length=False** → 否则 langchain 会把文本
   转成 tiktoken id 数组发给上游，dashscope 直接 400。
"""

import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from langchain_core.callbacks import BaseCallbackHandler
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from ragas import EvaluationDataset, SingleTurnSample, evaluate
from ragas.embeddings import LangchainEmbeddingsWrapper
from ragas.llms import LangchainLLMWrapper
from ragas.metrics import answer_relevancy, context_precision, context_recall, faithfulness
from ragas.run_config import RunConfig

from app.core.config import settings
from app.models.eval_run import CORE_METRICS
from app.services.runtime_config import JudgeConfig, build_judge_config

logger = logging.getLogger("evalagent.judge")


# ==================== ragas 解析回退缺陷补丁 ====================
# ragas 0.2.6 的 RagasOutputParser.parse_output_string（prompt/pydantic_prompt.py:388）：
#     try:      result = super().parse(extract_json(output_string))
#     except OutputParserException:
#         if retries_left != 0:
#             fixed = await fix_output_format_prompt.generate(...)   # 返回 StringIO 模型
#             result = fixed          # ← 缺陷：fix 完没有重新 parse，直接把 StringIO 当结果返回
#         else: raise RagasOutputParserException()
#     return result
# 后果：裁判模型只要**第一次**输出不是合法 JSON（弱模型/长输出极常见），指标拿到的就不是
# NLIStatementOutput 而是 StringIO，上层 `.statements` 直接 AttributeError →
# ragas 吞成 NaN → 指标静默为空；同时还白白多烧 3 次「修复格式」的 LLM 调用
# （实测一条样本 78 次 judge_calls、67 秒，最后仍然是空）。
# 补丁：fix 之后把修好的字符串再 parse 一次，真的还是坏才抛异常。
_RAGAS_PARSER_PATCHED = False


def _install_ragas_parser_patch() -> None:
    """让 ragas 的「修复格式」真正生效（幂等）。"""
    global _RAGAS_PARSER_PATCHED
    if _RAGAS_PARSER_PATCHED:
        return

    from langchain_core.exceptions import OutputParserException
    from ragas.exceptions import RagasOutputParserException
    from ragas.prompt.pydantic_prompt import (
        OutputStringAndPrompt,
        RagasOutputParser,
        fix_output_format_prompt,
    )
    from ragas.prompt.utils import extract_json

    async def parse_output_string(  # noqa: ANN001
        self, output_string, prompt_value, llm, callbacks, retries_left: int = 1,
    ):
        callbacks = callbacks or []
        try:
            return self.parse(extract_json(output_string))
        except OutputParserException:
            if retries_left == 0:
                raise RagasOutputParserException() from None

        fixed = await fix_output_format_prompt.generate(
            llm=llm,
            data=OutputStringAndPrompt(
                output_string=output_string,
                prompt_value=prompt_value.to_string(),
            ),
            callbacks=callbacks,
            retries_left=retries_left - 1,
        )
        try:
            # 关键改动：把「修好的字符串」重新解析成目标模型，而不是原样返回 StringIO
            return self.parse(extract_json(fixed.output_string))
        except OutputParserException as exc:
            raise RagasOutputParserException() from exc

    RagasOutputParser.parse_output_string = parse_output_string
    _RAGAS_PARSER_PATCHED = True
    logger.info("已修补 ragas 解析回退缺陷（fix_output_format 后重新解析）")


_install_ragas_parser_patch()


# ==================== 中文 / Markdown 切句器 ====================
# ragas 的 faithfulness 分两步：先按**句**把答案拆成陈述（_create_statements），
# 再逐条判有没有上下文支撑。但它的默认切句器是英文 pysbd，而且这一步只保留
# **以 . 。 ! ！ 结尾**的片段：
#     sentences_with_index = {i: s for i, s in enumerate(sentences)
#                             if s.strip().endswith((".", "。", "!", "！"))}
# 中文答案一旦是 markdown（表格行 `| 满1年 | 5天 |`、加粗标题 `**密码要求**`、列表
# `- 第一步...`），英文切句器切出来的片段是以换行/竖线结尾的 → **命中 0 句** →
# 陈述列表为空 → NLI 无话可判 → `_compute_score` 返回 NaN。
# 这就是「答案明明回答了、知识库也有资料，faithfulness 却是空」的真凶：
# 不是裁判判了 0 分，是它根本没拿到任何陈述（实测 7 条里 3 条这么空掉，
# 且这 3 条的答案全是表格/分点结构）。
class ChineseMarkdownSegmenter:
    """按中英文句末标点 + 换行切句，并给没终止符的片段补「。」，保证能进 NLI。"""

    _END = "。！？!?；;"
    _TABLE_RULE = re.compile(r"^\|?[\s:|-]+\|?$")

    def segment(self, text: str) -> list[str]:
        raw = text or ""
        lines = []
        for line in raw.splitlines():
            s = line.strip()
            if not s or self._TABLE_RULE.match(s):
                continue
            # 去掉 markdown 装饰：标题井号、引用、列表符、加粗、行内代码、表格竖线
            s = re.sub(r"^[#>\-\*\+\s]+", "", s)
            s = s.replace("**", "").replace("`", "").replace("|", " ")
            s = re.sub(r"\s{2,}", " ", s).strip()
            if s:
                lines.append(s)

        out: list[str] = []
        for line in lines:
            for part in re.split(r"(?<=[。！？!?；;])", line):
                part = part.strip(" ，,、;；")
                if len(part) < 4:  # 纯符号、表头残留不计
                    continue
                if part[-1] not in self._END:
                    part += "。"
                out.append(part)
        return out


_cn_segmenter = ChineseMarkdownSegmenter()


class JudgeUnavailable(RuntimeError):
    """裁判不可用（配置错误 / 额度耗尽 / 输出无法解析）。

    与"某条样本分数低"是两件事：前者不允许写 metric，后者照常落 0.1 这种真实低分。
    """


# 历史名字保留：runner / 测试大量 import 了 RagasScoringUnavailable
RagasScoringUnavailable = JudgeUnavailable

# 模型不满足结构输出时的统一文案（前端据此给出「换模型」的明确指引）
STRUCTURED_OUTPUT_UNSUPPORTED = "该模型不支持 RAGAS 结构化输出，请更换模型"

# 结构探活提示词：一条极短的「带上下文 + 判忠实度」任务，强制 JSON。
#
# 为什么不用原来的「回复两个字：就绪」——那只证明"能聊天"，不证明"能按 RAGAS 要的
# 结构吐 JSON"。kimi-k3 就是这么假成功的：探活 ok、真实评测每条样本都解析不出内容，
# 四项指标全空，一整轮跑完才判失败。
_STRUCTURED_PROBE = (
    "你是 RAG 评测裁判。请判断【回答】是否完全由【上下文】支持。\n"
    "严格只输出一个 JSON 对象，不要 markdown 代码块、不要任何解释文字：\n"
    '{"faithfulness": 0 到 1 之间的小数, "reason": "一句话理由"}\n\n'
    "【上下文】2026 年第三季度华东区销售额为 1200 万元。\n"
    "【回答】2026 年第三季度华东区销售额为 1200 万元。"
)


@dataclass
class JudgeOutcome:
    scores: dict[str, float | None]
    raw: dict[str, Any] = field(default_factory=dict)
    tokens: int = 0


class TokenCounter(BaseCallbackHandler):
    """从 langchain 回调里累计 token 用量（供应商不回 usage 时保持 0）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.total = 0
        self.calls = 0

    def on_llm_end(self, response: Any, **_: Any) -> None:  # noqa: ANN401
        usage = None
        try:
            usage = (response.llm_output or {}).get("token_usage")
            if usage is None and response.generations:
                msg = response.generations[0][0].message
                usage = getattr(msg, "usage_metadata", None)
        except Exception:  # noqa: BLE001 - 计费字段不能影响打分
            usage = None
        with self._lock:
            self.calls += 1
            if isinstance(usage, dict):
                self.total += int(usage.get("total_tokens") or 0)

    def snapshot(self) -> tuple[int, int]:
        with self._lock:
            return self.total, self.calls


class RagasJudge:
    """单例裁判。首次使用时构建 LLM / Embedding 客户端。

    热更新：每次 _build 都会比对配置签名（模型/base/key/embedding/温度），
    签名变了就重建客户端 —— 界面上改完配置无需重启 8200 即生效。
    """

    def __init__(self) -> None:
        self._llm = None
        self._embeddings = None
        self._counter = TokenCounter()
        self._ready = False
        self._lock = threading.Lock()
        self._sig: tuple | None = None
        self._cfg: JudgeConfig | None = None
        # ragas 指标是模块级单例：回填 llm/embeddings 时必须串行
        self._metric_lock = threading.Lock()

    # ---------------- 构建 ----------------
    @staticmethod
    def _make_clients(cfg: JudgeConfig, counter: TokenCounter):
        """按给定配置构建 (llm, embeddings)。不修改单例状态，供「候选配置探活」复用。"""
        if cfg.missing():
            raise JudgeUnavailable(f"裁判模型未配置：{' / '.join(cfg.missing())} 为空")

        extra_body: dict[str, Any] = {}
        if settings.JUDGE_DISABLE_THINKING:
            # 不同厂商「关思考」的参数名不一样，搞错 = 参数被静默忽略 = 思考模式照常跑：
            #   - 阿里百炼 dashscope：enable_thinking=false
            #   - DeepSeek 官方 api.deepseek.com：thinking={"type":"disabled"}
            # 不关思考时，推理型模型会把答案写进 reasoning_content 而 content 为空，
            # RAGAS 解析不到 JSON → 指标 NaN；同时 reasoning token 按输出价计费，
            # 一次评测能多烧 3~4 倍钱（2026-10-27 实测 7 条多花了几块钱）。
            base = (cfg.api_base or "").lower()
            if "deepseek.com" in base:
                extra_body["thinking"] = {"type": "disabled"}
            else:
                extra_body["enable_thinking"] = False

        # 显式传入 http_client：openai SDK 内部默认 trust_env=True，
        # 会把裁判请求交给系统代理（本机实测是被沙箱代理劫持，抖动时表现为打分超时）。
        # 见 config.HTTP_TRUST_ENV 的注释。
        http_client = httpx.Client(
            timeout=httpx.Timeout(settings.JUDGE_TIMEOUT),
            trust_env=settings.HTTP_TRUST_ENV,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )

        chat = ChatOpenAI(
            model=cfg.model,
            api_key=cfg.api_key,
            base_url=cfg.api_base or None,
            # 为保证评测结果的可复现性，裁判温度强制锁定为 0（见 JudgeConfig.temperature）
            temperature=0,
            max_tokens=settings.JUDGE_MAX_TOKENS,
            timeout=settings.JUDGE_TIMEOUT,
            # 客户端重试压到 1 次：openai SDK 的 (1+max_retries) 会和外层
            # JUDGE_ITEM_TIMEOUT 相乘，2 次重试 × 120s = 360s 已经超过单样本预算（见 config 注释）
            max_retries=1,
            extra_body=extra_body or None,
            callbacks=[counter],
            http_client=http_client,
        )
        embed = OpenAIEmbeddings(
            model=cfg.embedding_model,
            # 向量模型可以与裁判 LLM 不同源（emb_api_* 留空时自动回落到裁判配置）。
            # 典型场景：裁判 LLM 走百炼 qwen，embedding 走硅基流动 bge-m3（与项目一检索同模型）。
            api_key=cfg.emb_api_key,
            base_url=cfg.emb_api_base or None,
            # 关键：非 OpenAI 端点不认 tiktoken id 数组
            check_embedding_ctx_length=False,
            timeout=settings.JUDGE_TIMEOUT,
            max_retries=1,
            http_client=http_client,
        )
        return LangchainLLMWrapper(chat), LangchainEmbeddingsWrapper(embed)

    def _build(self) -> None:
        cfg = build_judge_config()
        sig = cfg.signature()
        if self._llm is not None and self._sig == sig:
            return
        with self._lock:
            # 双检：并发进来时只有第一个真正重建
            if self._llm is not None and self._sig == sig:
                return
            llm, embeddings = self._make_clients(cfg, self._counter)
            self._llm, self._embeddings = llm, embeddings
            self._sig = sig
            self._cfg = cfg
            self._ready = False
            logger.info(
                "裁判就绪：model=%s embedding=%s base=%s thinking=%s temperature=0(locked)",
                cfg.model,
                cfg.embedding_model,
                cfg.api_base or "<默认>",
                not settings.JUDGE_DISABLE_THINKING,
            )

    def reload(self) -> None:
        """丢弃现有客户端，下次使用按最新配置重建（配置写库后调用）。"""
        with self._lock:
            self._llm = None
            self._embeddings = None
            self._sig = None
            self._cfg = None
            self._ready = False

    def preflight(self, candidate: JudgeConfig | None = None) -> str:
        """**结构探活**：既要连通，也要能按 RAGAS 要的 JSON 结构输出。

        探活判定三档（区分开，排障才不会跑偏）：
          1. 调用异常 / content 为空  → 配置或思考模式问题（原样抛出原始原因）；
          2. 有内容但抠不出 JSON      → **模型不支持结构化输出**，必须拦在保存前；
          3. JSON 里没有 0~1 数值字段 → 同上（模型只是"形似"，骗不过阈值解析）。

        candidate 非空时用「候选配置」探活且不影响当前单例 ——
        保存前先验，验证不过就不落库。
        """
        if candidate is None:
            self._build()
            llm, embeddings = self._llm, self._embeddings
            target = self._cfg
        else:
            llm, embeddings = self._make_clients(candidate, self._counter)
            target = candidate

        started = time.perf_counter()
        try:
            out = llm.langchain_llm.invoke(_STRUCTURED_PROBE)
        except Exception as exc:  # noqa: BLE001
            raise JudgeUnavailable(f"裁判模型探活失败：{type(exc).__name__}: {exc}") from exc

        content = str(getattr(out, "content", "") or "")
        if not content.strip():
            raise JudgeUnavailable(
                "裁判模型返回空 content（大概率是思考模式未关闭，或 max_tokens 被 reasoning 吃光）"
            )

        parsed = _extract_json_object(content)
        if parsed is None:
            raise JudgeUnavailable(
                f"{STRUCTURED_OUTPUT_UNSUPPORTED}（模型有返回内容，但解析不出 JSON；"
                f"返回片段：{content.strip()[:120]}）"
            )
        if not _has_probability(parsed):
            raise JudgeUnavailable(
                f"{STRUCTURED_OUTPUT_UNSUPPORTED}（JSON 里没有 0~1 的数值字段；"
                f"返回：{str(parsed)[:120]}）"
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        # 向量模型也要探：answer_relevancy 全靠它，配错 base/key 同样会静默空分，
        # 但失败时只体现为"某一条指标是 NaN"，不探的话要等评测跑完才发现。
        dim = self._probe_embeddings(embeddings, target)
        if candidate is None:
            self._ready = True
        model = target.model if target else ""
        logger.info(
            "裁判结构探活通过：model=%s embedding=%s dim=%s latency=%sms",
            model, target.embedding_model, dim, latency_ms,
        )
        return f"结构化输出正常（{model}，{latency_ms}ms）；向量 {target.embedding_model} {dim} 维正常"

    @staticmethod
    def _probe_embeddings(embeddings: Any, cfg: JudgeConfig) -> int:
        """向量模型探活：能出向量、维度一致、不是全零。

        为什么要检"全零"：个别网关/代理对不认识的 embedding 模型会返回
        `[[0.0]*dim]` 这种"成功但无意义"的响应，answer_relevancy 会稳定算成 0 ——
        比报错更难查，因为它长得像"模型相关性很差"。
        """
        where = cfg.emb_api_base or cfg.api_base
        try:
            vecs = embeddings.embed_documents(["销售额统计", "上下文召回测试"])
        except Exception as exc:  # noqa: BLE001
            raise JudgeUnavailable(
                f"向量模型探活失败（{cfg.embedding_model} @ {where}）："
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if not vecs or any(not v for v in vecs):
            raise JudgeUnavailable("向量模型探活失败：返回的向量为空")
        dims = {len(v) for v in vecs}
        if len(dims) != 1:
            raise JudgeUnavailable(f"向量模型探活失败：同一批文本返回了不同维度 {sorted(dims)}")
        dim = dims.pop()
        if dim == 0:
            raise JudgeUnavailable("向量模型探活失败：向量维度为 0")
        if all(abs(float(x)) < 1e-12 for x in vecs[0]):
            raise JudgeUnavailable("向量模型探活失败：返回全零向量（疑似网关占位，会让 answer_relevancy 恒为 0）")
        return dim

    # ---------------- 打分 ----------------
    @staticmethod
    def _run_config() -> RunConfig:
        """收紧 RAGAS 的重试与超时。

        默认 (timeout=180, max_retries=10, max_wait=60) 意味着一条样本解析失败后
        会指数退避重试十次，期间完全静默（无日志无网络），表现为 run 永久卡住。
        """
        return RunConfig(
            timeout=settings.JUDGE_METRIC_TIMEOUT,
            max_retries=settings.JUDGE_MAX_RETRIES,
            max_wait=settings.JUDGE_MAX_WAIT,
            max_workers=max(2, settings.JUDGE_MAX_CONCURRENCY),
            log_tenacity=settings.JUDGE_LOG_TENACITY,
        )

    @staticmethod
    def _tight_run_config() -> RunConfig:
        """补跑用的收紧配置：只给一次机会、单指标 60s 上限。

        补跑的目的是「拣回被瞬时抖动吞掉的那一两个指标」，不是再做一次完整重试 ——
        真不兼容的模型（结构化输出能力缺失）再给十次也一样，反而把 run 拖长。
        """
        return RunConfig(
            timeout=min(settings.JUDGE_METRIC_TIMEOUT, 60),
            max_retries=1,
            max_wait=5,
            max_workers=max(2, settings.JUDGE_MAX_CONCURRENCY),
            log_tenacity=settings.JUDGE_LOG_TENACITY,
        )

    def _evaluate_metrics(
        self,
        metrics: list,
        dataset: EvaluationDataset,
        llm,
        embeddings,
        *,
        tight: bool = False,
    ) -> dict[str, float | None]:
        """跑一批指标，返回 {指标名: 分数或 None}。

        ragas 在 ``raise_exceptions=False`` 下会把**单个指标**的异常吞成 NaN
        （实测：一条样本并发时 CP 抛 APIConnectionError，其余三项正常，CP 静默变空），
        所以这里把 None 原样返回，由调用方决定「补跑一次」还是「记为缺失」。
        整个 evaluate 崩掉（不是单指标）仍然抛出，交给上层判断是否致命。
        """
        # ragas 的指标是**模块级单例**，多线程并发 evaluate 会互相覆盖 llm/embeddings。
        # 每次调用前在锁内回填一次（很便宜），避免并发下指标拿到 None 而静默空分。
        with self._metric_lock:
            for metric in metrics:
                if hasattr(metric, "llm"):
                    metric.llm = llm
                if hasattr(metric, "embeddings"):
                    metric.embeddings = embeddings
                # faithfulness 需要按句拆陈述；默认英文切句器会把中文 markdown 答案切成 0 句，
                # 直接导致 NaN（见 ChineseMarkdownSegmenter 注释）
                if settings.JUDGE_CN_SEGMENTER and hasattr(metric, "sentence_segmenter"):
                    metric.sentence_segmenter = _cn_segmenter

        try:
            result = evaluate(
                dataset=dataset,
                metrics=metrics,
                llm=llm,
                embeddings=embeddings,
                raise_exceptions=False,
                run_config=self._tight_run_config() if tight else self._run_config(),
            )
            frame = result.to_pandas()
            row = frame.iloc[0].to_dict() if len(frame) else {}
        except Exception as exc:  # noqa: BLE001
            raise RagasScoringUnavailable(
                f"RAGAS 打分异常：{type(exc).__name__}: {exc}"
            ) from exc

        return {m.name: _clean(row.get(m.name)) for m in metrics}

    def score(
        self,
        question: str,
        answer: str,
        contexts: list[str],
        reference: str | None,
        reference_contexts: list[str] | None = None,
    ) -> JudgeOutcome:
        """单条样本打分（同步，调用方用 to_thread 包）。

        metrics 按可用字段裁剪：没有 reference 就算不了 precision/recall，
        硬跑只会得到 NaN，不如不跑。
        """
        self._build()
        # 关键：先取本地引用。reload() 会把 self._llm 置 None，
        # 若直接读 self._llm 恰好跨在 reload 中间，就会拿 None 传给 evaluate，
        # 表现为 ragas 报 "set LLM before use" / NoneType has no attribute 'generate'。
        llm, embeddings = self._llm, self._embeddings

        sample = SingleTurnSample(
            user_input=question,
            response=answer or "",
            retrieved_contexts=list(contexts or []),
            reference=reference or None,
            reference_contexts=list(reference_contexts or []) or None,
        )
        dataset = EvaluationDataset(samples=[sample])

        metrics = [faithfulness, answer_relevancy]
        if reference:
            metrics += [context_precision, context_recall]

        before_total, before_calls = self._counter.snapshot()
        used = self._evaluate_metrics(metrics, dataset, llm, embeddings)
        scores: dict[str, float | None] = {name: used.get(name) for name in CORE_METRICS}

        # ---- 单指标缺失定向补跑 ----
        # 只补「本次用到却算空」的指标（没 reference 时 CP/CR 本就不该算，不算缺失）。
        # 这一层能把「四项里坏一项」从"整条样本废掉"降级成"补一次就好"
        # —— 实测并发下 CP 丢失概率不低（APIConnectionError），补跑通常能拿回 0.9+。
        missing = [m for m in metrics if scores.get(m.name) is None]
        retried = False
        if missing and settings.JUDGE_MISSING_METRIC_RETRY:
            retried = True
            logger.warning("样本指标缺失，定向补跑一次：%s", [m.name for m in missing])
            again = self._evaluate_metrics(missing, dataset, llm, embeddings, tight=True)
            for name, value in again.items():
                if scores.get(name) is None and value is not None:
                    scores[name] = value

        still_missing = [m.name for m in metrics if scores.get(m.name) is None]

        after_total, after_calls = self._counter.snapshot()
        return JudgeOutcome(
            scores=scores,
            raw={
                "metrics_used": [m.name for m in metrics],
                "judge_calls": after_calls - before_calls,
                "missing": still_missing,
                "missing_retried": retried,
            },
            tokens=after_total - before_total,
        )


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出第一个 JSON 对象。

    真机上模型爱干三件事，这里都得容错：包 ```json 围栏、加"好的，结果如下："前缀、
    在 JSON 后面再补一段解释。所以先原样试，失败再按首尾花括号截取重试。
    注意「大括号截取」不做嵌套平衡校验 —— 探活只是粗筛，宁可放过也别误杀。
    """
    raw = (text or "").strip()
    if not raw:
        return None

    # 1) 整串就是 JSON（最常见的理想情况）
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, TypeError):
        pass

    # 2) 去 ```json 围栏再试
    if raw.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        stripped = re.sub(r"\s*```$", "", stripped).strip()
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, TypeError):
            raw = stripped

    # 3) 从前缀废话里捞：从第一个 { 起做增量解析（raw_decode 会忽略尾部多余内容，
    #    所以 '刚才的话：{"a":1} 完毕' 和 '{"a":1}{"b":2}' 都能拿到第一个对象）
    start = raw.find("{")
    if start == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw[start:])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _has_probability(obj: dict[str, Any]) -> bool:
    """JSON 里至少有一个 0~1 的数值字段（对应 RAGAS 的分数位）。

    bool 要排除：`True` 能被 float() 转成 1.0，会把 {"ok": true} 误判成合格输出。
    """
    for value in obj.values():
        if isinstance(value, bool):
            continue
        try:
            num = float(value)
        except (TypeError, ValueError):
            continue
        if 0.0 <= num <= 1.0:
            return True
    return False


def _clean(value: Any) -> float | None:
    """NaN / inf / None 一律归 None —— NULL 表示"没算出来"，与 0.0 严格区分。"""
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return round(num, 6)


judge = RagasJudge()
