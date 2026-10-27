"""v1.9 幻觉治理两项补丁的回归用例。

1. Prompt 冲突检测死命令：两处生成入口都必须带上「矛盾时并列列出并注明出处」；
2. 后置引用校验 validate_citations：长数字 / 日期在上下文里比对，未命中打
   WARNING 并标记 suspect，但绝不拦截、不抛错。
"""
import logging

from langchain_core.messages import HumanMessage

import app.agent.graph as graph
from app.agent.graph import _ANSWER_SYSTEM_PROMPT
from app.rag import citation_guard, llm as rag_llm

# 【v1.13】块号由 0-based 改为 1-based，与 rag_tool.format_context 真实下发一致
# （旧样本写「第0块」是 v1.9 遗留，会让下面的块号校验用例失真）
CONTEXT = (
    "[来源1] 文档《公司差旅报销制度》 第1块：\n"
    "| 职级 | 住宿标准 | 报销时限 |\n| --- | --- | --- |\n| 部门经理 | 800 元/晚 | 30 天内 |\n"
    "2024年1月5日修订，合计报销金额 4,820 元。"
)


# ---------------------------------------------------------------- 任务2：Prompt


def test_answer_system_prompt_has_conflict_rule():
    assert "【冲突处理死命令】" in _ANSWER_SYSTEM_PROMPT
    assert "严禁自行判断" in _ANSWER_SYSTEM_PROMPT
    assert "并列列出" in _ANSWER_SYSTEM_PROMPT
    assert "注明其来源" in _ANSWER_SYSTEM_PROMPT


def test_rag_prompt_has_conflict_rule():
    prompt = rag_llm.build_prompt("住宿标准是多少？", ["| 职级 | 标准 |", "| 经理 | 800 |"], "公司制度库")
    assert "【冲突处理死命令】" in prompt
    assert "严禁自行判断" in prompt
    # 冲突规则必须在资料之前，避免被长上下文淹没
    assert prompt.index("【冲突处理死命令】") < prompt.index("【资料】")


def test_both_prompts_share_same_wording():
    """两处入口措辞必须一致，否则模型在不同链路下行为不一致。"""
    prompt = rag_llm.build_prompt("问题", ["资料"], "库")
    for phrase in ("【冲突处理死命令】", "严禁自行判断", "并列列出", "注明其来源"):
        assert phrase in prompt and phrase in _ANSWER_SYSTEM_PROMPT


# ------------------------------------------------------- 任务3：后置引用校验


def test_cited_numbers_pass():
    answer = "部门经理住宿标准为 800 元/晚，报销时限 30 天内，本次合计 4,820 元。"
    result = citation_guard.validate_citations(answer, CONTEXT)
    assert result["suspect"] is False
    assert result["misses"] == []
    assert result["checked"] >= 2


def test_thousand_separator_normalized():
    """答案写 4820、上下文写 4,820 时必须视为一致（归一化去千分位）。"""
    answer = "本次合计报销 4820 元。"
    result = citation_guard.validate_citations(answer, CONTEXT)
    assert result["suspect"] is False


def test_hallucinated_number_flagged_and_logged(caplog):
    answer = "部门经理住宿标准为 1200 元/晚，报销时限 30 天内。"
    with caplog.at_level(logging.WARNING, logger="app.rag.citation_guard"):
        result = citation_guard.validate_citations(answer, CONTEXT)

    assert result["suspect"] is True
    assert "1200" in result["misses"]
    assert any("hallucination_suspect" in r.getMessage() for r in caplog.records)


def test_short_numbers_ignored():
    """1~2 位数字（序号、来源标号、比例）不参与校验，避免噪声。"""
    answer = "见[来源1]，共 2 条，占比 8.4%，金额 800 元。"
    result = citation_guard.validate_citations(answer, CONTEXT)
    assert result["suspect"] is False


def test_date_formats_normalized():
    """2024-01-05 与 2024年1月5日 视为同一日期。"""
    result = citation_guard.validate_citations("制度于 2024-01-05 修订。", CONTEXT)
    assert result["suspect"] is False


def test_hallucinated_date_flagged():
    result = citation_guard.validate_citations("制度于 2025年3月9日 修订。", CONTEXT)
    assert result["suspect"] is True
    assert any("2025" in m for m in result["misses"])


def test_no_context_skips_check():
    """direct 路由没有检索上下文，不能因为答案里有数字就误报。"""
    result = citation_guard.validate_citations("一个季度大约 90 天。", "")
    assert result["suspect"] is False
    assert result["checked"] == 0


# ------------------------------------------------ 无上下文却声称有出处（v1.11）

def test_no_context_with_source_claim_is_suspect(caplog):
    """direct 路由（不检索）却写出「来源：《文档》第N块」= 凭空贴出处，必须标记。

    这是 v1.11 补的盲区：此前 context 为空直接放行，导致 direct 路由下
    模型复述上文内容并自行标注来源，系统完全照不到。
    """
    answer = "研发部去成都：400 元/天。\n\n来源：\n- 《公司差旅报销制度.md》 第1块"
    with caplog.at_level(logging.WARNING, logger=citation_guard.__name__):
        result = citation_guard.validate_citations(answer, "")

    assert result["suspect"] is True
    assert result["checked"] == 0          # 没做数字比对，只判了出处声明
    assert any("《公司差旅报销制度.md》" in m for m in result["misses"])
    assert any("source_claim_without_context" in r.getMessage() for r in caplog.records)


def test_no_context_with_numbered_citation_is_suspect():
    """[来源1] 形态同样算「声称有出处」。"""
    result = citation_guard.validate_citations("标准是 800 元一晚 [来源1]。", None)
    assert result["suspect"] is True
    assert result["misses"] == ["[来源1]"]


def test_no_context_plain_answer_stays_clean():
    """没有出处声明的直答（闲聊/计算）不能被误杀。"""
    assert citation_guard.validate_citations("成都到北京约 1800 公里。", "")["suspect"] is False


def test_context_present_uses_fact_matching_not_claim_rule():
    """有上下文时走原有数字比对：来源标注本身不算幻觉。"""
    result = citation_guard.validate_citations(
        "住宿 800 元/晚（来源：《公司差旅报销制度》第1块）", CONTEXT
    )
    assert result["suspect"] is False
    assert result["checked"] >= 1


def test_answer_node_forbids_source_claim_without_context(monkeypatch):
    """生成层也加了硬约束：无工具结果时明确告知模型不得标注出处。"""
    captured: dict = {}

    class _Resp:
        content = "好的"

    class _LLM:
        def invoke(self, messages):
            captured["messages"] = messages
            return _Resp()

    monkeypatch.setattr(graph, "get_chat_model", lambda *a, **kw: _LLM())
    graph.answer_node({"messages": [HumanMessage(content="总结一下刚才的结论")],
                       "question": "总结一下刚才的结论", "tool_output": ""})

    tail = captured["messages"][-1].content
    assert "本轮未检索知识库" in tail
    assert "严禁输出" in tail and "[来源N]" in tail


def test_refusal_answer_skips_check():
    """拒答文案没有事实主张，直接放行。"""
    result = citation_guard.validate_citations("知识库中未找到相关信息（2024 年数据）。", CONTEXT)
    assert result["suspect"] is False
    assert result["checked"] == 0


def test_empty_answer_is_safe():
    """空答案不产生任何未命中；返回结构固定（供调用方直接取键）。"""
    result = citation_guard.validate_citations("", CONTEXT)
    assert result == {
        "suspect": False, "checked": 0, "misses": [], "chunk_misses": [],
    }


def test_extract_facts_dedup_and_order():
    facts = citation_guard.extract_facts("2024年1月5日，800 元，800 元，4,820 元")
    assert facts[0] == "2024年1月5日"
    assert facts.count("800") == 1
    assert "4,820" in facts


def test_validation_never_raises_on_weird_input():
    """校验是旁路逻辑，任何脏数据都不允许把主流程打挂。"""
    assert citation_guard.validate_citations(None, None)["suspect"] is False
    assert citation_guard.validate_citations("!!!", "###")["suspect"] is False


# ------------------------------------------- 块号校验（v1.13，纯文本溯源幻觉）
# 真实样本：知识库里《人事制度手册.md》(doc_id=10) 共 3 块。某次检索只召回了
# 第 1、3 块，模型却在答案里写「第2块」——所有数字都真实存在，数字比对照不到，
# 前端只能弹「没有第 2 块」让用户自己找。以下用例锁死这条链路。

REAL_CONTEXT = (
    "[来源1] 文档《人事制度手册.md》 第1块：\n"
    "## 7. 工资条\n每月10日通过HR自助平台发布电子工资条，疑问可在5个工作日内发起核对。\n"
    "[来源2] 文档《人事制度手册.md》 第3块：\n"
    "## 9. 劳动关系证明\n离职当天完成门禁卡归还，离职证明由HR在1个工作日内出具。\n"
)


def test_chunk_ref_correct_passes():
    """引用块号与上下文一致 → 不算幻觉。"""
    result = citation_guard.validate_citations(
        "每月10日发布工资条。（来源：《人事制度手册.md》第1块）", REAL_CONTEXT
    )
    assert result["suspect"] is False
    assert result["chunk_misses"] == []


def test_chunk_ref_wrong_flagged_and_logged(caplog):
    """复现真实 bug：上下文只有第 1、3 块，答案却写第 2 块 → 必须标记。"""
    answer = "每月10日发布工资条。（来源：《人事制度手册.md》第2块）"
    with caplog.at_level(logging.WARNING, logger=citation_guard.__name__):
        result = citation_guard.validate_citations(answer, REAL_CONTEXT)

    assert result["suspect"] is True
    assert result["chunk_misses"] == ["《人事制度手册.md》第2块"]
    assert result["misses"] == []          # 数字全部对得上，问题只在块号
    assert any("chunk_ref_mismatch" in r.getMessage() for r in caplog.records)


def test_chunk_ref_checked_even_without_long_numbers():
    """答案只有出处、没有长数字时也不能漏判（放在 `if not facts` 之后就会漏）。"""
    result = citation_guard.validate_citations("见（来源：《人事制度手册.md》第9块）", REAL_CONTEXT)
    assert result["suspect"] is True
    assert result["chunk_misses"] == ["《人事制度手册.md》第9块"]


def test_chunk_ref_tolerates_missing_extension():
    """模型常漏写扩展名，归一化后应视为同一文档。"""
    result = citation_guard.validate_citations(
        "每月10日发布工资条（来源：《人事制度手册》第1块）", REAL_CONTEXT
    )
    assert result["suspect"] is False


def test_chunk_ref_page_unit_compared_as_chunk():
    """模型把块号写成页码时按块号尽力比对，与前端 PLAIN_CITE_RE 口径一致。"""
    ok = citation_guard.validate_citations("见（来源：《人事制度手册.md》第3页）", REAL_CONTEXT)
    bad = citation_guard.validate_citations("见（来源：《人事制度手册.md》第7页）", REAL_CONTEXT)
    assert ok["suspect"] is False
    assert bad["suspect"] is True


def test_chunk_ref_skipped_for_non_rag_context():
    """db_query / report 的上下文没有块级标号 → 不做块号比对，避免全量误杀。"""
    db_context = "total_amount=128000, order_count=37, avg_amount=3459.5"
    result = citation_guard.validate_citations(
        "本月订单 37 笔，合计 128000 元（来源：《订单明细》第2块）", db_context
    )
    assert result["chunk_misses"] == []
    assert result["suspect"] is False


def test_chunk_ref_ignores_article_number_in_context():
    """上下文正文里的「第3条」不是块号，不能污染可用集合。"""
    ctx = REAL_CONTEXT + "\n依据《员工手册》第3条规定执行。\n"
    result = citation_guard.validate_citations(
        "见（来源：《员工手册》第1块）", ctx
    )
    # 《员工手册》在上下文里只以「第3条」出现，没有块级标号 → 判为未召回
    assert result["chunk_misses"] == ["《员工手册》第1块"]
