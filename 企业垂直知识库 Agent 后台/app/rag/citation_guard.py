"""轻量级后置引用校验（v1.9）。

## 为什么需要

Prompt 里已经写了「仅依据资料回答、注明来源」，但那是**软约束**：模型完全可能
挂着 `[来源1]` 说一个资料里根本没有的数字（例如把 800 元记成 900 元）。
RAGAS 的 faithfulness 只能离线发现，线上没法逐条兜底。

本模块做一件很窄的事：把答案里的**硬事实**（≥3 位的长数字、日期）抽出来，
到检索上下文里做子串比对；比对不上就打 WARNING 并在响应里标 `suspect`。

## 设计取舍（防误杀优先）

1. **只标记，不拦截**：误判代价是不必要的提示，拦截代价是答对也不给出答案，
   显式偏向前者；上游据此决定是加提示还是走人工复核；
2. **没有上下文就不比对数字**：direct 路由（不检索）合法地会提到数字，拿空上下文
   去比对必然全量误报；但**「本轮没检索却声称有出处」要单独判**——`[来源N]`、
   `来源：《X》第N块` 这类标注在无上下文时只能是模型编的（v1.11 补的口子，
   此前 entire direct 路由是校验盲区）；
3. **拒答文案直接放行**：「资料中未找到相关信息」这类回答本身没有事实主张，
   不必校验（否则里面的年份、编号会被当成幻觉）；
4. **比对前做归一化**：去掉千分位、全半角逗号与空白，并把 `年/月/日`、`/`、`.`
   统一成 `-`，让 `4,820` 与 `4820`、`2024年1月5日` 与 `2024-01-05` 能对上；
5. **数字位数门槛**：忽略 1~2 位数字（序号、块号、`[来源1]`、比例 `8.4`），
   既压低噪声，也避免把引用标号当成事实。

## 已知局限

子串比对只能证明「这个数在上下文里出现过」，不能证明「用在正确的位置上」。
例如上下文里同时有 500 与 800，答案把差旅标准说成 500，校验仍会通过 ——
这一步是「降低幻觉留存率」，不是「证明答案正确」。

## v1.13 补口子：块号校验

数字比对之外，模型还会**抄错块号**：上下文里明明写着「文档《人事制度手册.md》 第1块」，
答案却写成「来源：《人事制度手册.md》第2块」。这类错误里所有数字都真实存在，
子串比对完全照不到，前端只能弹「没有第 2 块」让用户自己找（v1.12 之前的体验）。

`check_chunk_refs()` 把答案里声称的「《文档》第N块」抽出来，与上下文里**实际下发**
的块级标号做集合比对。同样只标记不拦截。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# 数字（含千分位与小数）：4,820 / 27.35 / 800
_NUMBER_RE = re.compile(r"\d[\d,，]*(?:\.\d+)?")
# 日期：2024-01-05 / 2024/1/5 / 2024年1月5日 / 2024年1月
_DATE_RE = re.compile(
    r"\d{4}\s*(?:[-/年])\s*\d{1,2}\s*(?:(?:[-/月])\s*\d{1,2}\s*日?)?"
)
# 拒答/无信息文案：这类答案不构成事实主张
_REFUSAL_MARKERS = ("未找到", "没有找到", "未检索到", "无法回答", "资料中没有", "NO_DATA")

# 声称有出处的表述：[来源1] / 来源：《X》第2块 / 《X》第3页
# 【v1.11】无上下文时用它判定「凭空标注来源」——direct 路由没有任何检索结果，
# 出现这种标注必然是模型凭上文记忆或臆想补的，属于高置信度幻觉信号。
_SOURCE_CLAIM_RE = re.compile(
    r"\[来源\s*\d+\]"
    r"|(?:来源\s*[:：]?\s*\d*\s*[:：]?\s*)?《[^》\n]{1,80}》\s*第\s*\d{1,4}\s*(?:块|页)"
)

# 长数字门槛：去掉分隔符后至少 3 位
_MIN_DIGITS = 3

# 【v1.13】上下文里**实际下发**的块级标号：`[来源1] 文档《X》 第3块：`
# 只认「块」，因为 rag_tool.format_context 只会这么写；正文里正常出现的
# 「《X》第3条」不会被误当成块号。
_CONTEXT_CHUNK_RE = re.compile(r"《([^》\n]{1,80})》\s*第\s*(\d{1,4})\s*块")
# 答案里**声称**的块级出处：来源：《X》第2块 / 《X》第3页（页按块号尽力比对）
_ANSWER_CHUNK_RE = re.compile(
    r"(?:来源\s*[:：]?\s*\d*\s*[:：]?\s*)?《([^》\n]{1,80})》\s*第\s*(\d{1,4})\s*(?:块|页)"
)


def _normalize(text: str) -> str:
    """归一化：去千分位/逗号/空白，年月日与常见分隔符统一为 '-'，并去掉前导零。

    两处入口（答案 token 与上下文正文）必须走同一个函数，否则
    `2024-01-05` 与 `2024年1月5日` 归一化后一个是 `2024-01-05`、另一个是
    `2024-1-5`，会被误判成幻觉。
    """
    s = (text or "").replace(",", "").replace("，", "")
    s = re.sub(r"\s+", "", s)
    s = s.replace("年", "-").replace("月", "-").replace("日", "")
    s = s.replace("/", "-").replace(".", "-")
    # 去掉各数字段的前导零（01 -> 1），仅处理非数字开头的段，避免破坏 1001 这类值
    return re.sub(r"(?<!\d)0+(?=\d)", "", s)


def extract_facts(answer: str) -> list[str]:
    """从答案中抽取需要核验的硬事实（长数字 + 日期），保持出现顺序且去重。"""
    text = answer or ""
    facts: list[str] = []

    for m in _DATE_RE.finditer(text):
        token = m.group(0).strip()
        if token and token not in facts:
            facts.append(token)

    date_spans = [m.span() for m in _DATE_RE.finditer(text)]
    for m in _NUMBER_RE.finditer(text):
        # 日期内部的数字不重复计一次
        if any(start <= m.start() < end for start, end in date_spans):
            continue
        token = m.group(0).strip()
        digits = re.sub(r"\D", "", token)
        if len(digits) < _MIN_DIGITS:
            continue
        if token not in facts:
            facts.append(token)
    return facts


def _norm_doc_name(name: str) -> str:
    """文档名归一化：去空白、忽略大小写、忽略常见扩展名。

    必须与前端 `citation.ts::normalizeDocName` 保持同一口径：模型经常漏写
    `.md`/`.pdf`，上下文写全名、答案写简称，不归一化就会全量误报。
    """
    s = re.sub(r"\s+", "", name or "").lower()
    return re.sub(r"\.(pdf|docx?|md|txt|xlsx?)$", "", s)


def check_chunk_refs(answer: str, context: str) -> list[str]:
    """校验答案声称的「《文档》第N块」是否真在本次检索上下文里。

    返回未命中的引用原文（如 `《人事制度手册.md》第2块`），按出现顺序去重。

    ## 边界（防误杀优先）
    - **上下文解析不出任何块级标号就跳过**：db_query / report 等非 rag 上下文
      没有块号可比，拿空集合去比对必然把正常引用全判成幻觉；
    - 文档名归一化（去空白/大小写/扩展名），容忍模型漏写 `.md`；
    - 「页」也按块号尽力比对 —— 模型常把块号写成页码，与前端 `PLAIN_CITE_RE` 一致。
    """
    answer_text = answer or ""
    context_text = context or ""
    if not answer_text or not context_text:
        return []

    available = {
        (_norm_doc_name(doc), int(no)) for doc, no in _CONTEXT_CHUNK_RE.findall(context_text)
    }
    if not available:
        return []

    misses: list[str] = []
    for doc, no in _ANSWER_CHUNK_RE.findall(answer_text):
        label = f"《{doc.strip()}》第{int(no)}块"
        if (_norm_doc_name(doc), int(no)) not in available and label not in misses:
            misses.append(label)
    return misses


def validate_citations(answer: str, context: str) -> dict:
    """校验答案中的硬事实（长数字/日期）与块级出处是否都能在检索上下文里找到。

    返回 `{"suspect": bool, "checked": int, "misses": list[str],
    "chunk_misses": list[str]}`；
    未命中时记录 `WARNING: hallucination_suspect`，**不抛错、不拦截**。

    `misses` 只装「数字/日期」未命中（保持既有语义，调用方按此下发
    `citation_misses`）；块号未命中单独放 `chunk_misses`，两者都会置 `suspect`。
    """
    answer_text = answer or ""
    context_text = context or ""

    # 纯拒答：没有事实主张，直接放行
    if any(k in answer_text for k in _REFUSAL_MARKERS):
        return {"suspect": False, "checked": 0, "misses": [], "chunk_misses": []}

    # 无上下文（direct 路由 / 工具失败）：不拿空上下文去比对数字（必然全量误报），
    # 但**「本轮没有检索却声称有出处」是硬信号**——这种来源标注只能是模型凭上文
    # 记忆补的，或者干脆编的。单独判一次，避免 direct 路由成为校验盲区。
    if not context_text.strip():
        claims = [m.group(0).strip() for m in _SOURCE_CLAIM_RE.finditer(answer_text)]
        if claims:
            logger.warning(
                "hallucination_suspect reason=source_claim_without_context claims=%d detail=%s",
                len(claims), claims[:5],
            )
            # chunk_misses 只装块号形态（《X》第N块/页），保持该字段语义单一；
            # `[来源1]` 这类序号主张仍然体现在 misses 里。
            chunk_claims = [m.group(0).strip() for m in _ANSWER_CHUNK_RE.finditer(answer_text)]
            return {
                "suspect": True, "checked": 0, "misses": claims[:5],
                "chunk_misses": chunk_claims[:5],
            }
        return {"suspect": False, "checked": 0, "misses": [], "chunk_misses": []}

    facts = extract_facts(answer_text)
    # 【v1.13】块号校验必须放在 `if not facts` 之前：回答可能只有出处、没有长数字
    chunk_misses = check_chunk_refs(answer_text, context_text)
    if not facts and not chunk_misses:
        return {"suspect": False, "checked": 0, "misses": [], "chunk_misses": []}

    norm_ctx = _normalize(context_text)
    misses = [t for t in facts if _normalize(t) not in norm_ctx]

    if chunk_misses:
        logger.warning(
            "hallucination_suspect reason=chunk_ref_mismatch chunk_misses=%d detail=%s",
            len(chunk_misses), chunk_misses[:10],
        )
    if misses:
        logger.warning(
            "hallucination_suspect checked=%d misses=%d detail=%s",
            len(facts), len(misses), misses[:10],
        )

    return {
        "suspect": bool(misses or chunk_misses),
        "checked": len(facts),
        "misses": misses,
        "chunk_misses": chunk_misses,
    }
