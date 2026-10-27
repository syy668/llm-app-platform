"""路由纠偏 / 报表兜底守卫（v1.9 修补）。

背景（线上复现的缺陷）：
    用户问「去年同期退款率是多少？」「全年 GMV 是多少？Q4 占多少？」，
    分类器判成 report → report_node 查业务库 → 业务库只有设备/工单/告警三张表，
    于是拿到"业务库概览…请在问题中明确设备/工单/告警"这句兜底话术，
    再被 generate_report 包装成《业务数据汇总报表》输出。
    表现是：**答非所问，却看起来像一份正式报表**（比直接说"没找到"危害更大）。

修复口径：
    1) 业务库关键词白名单：判成 db_query / report 但问题里没有任何表名关键词 → 纠偏到 rag；
    2) report_node 见到 NO_BUSINESS_DATA_MARK 就不再生成报表，只回"未找到"说明。
"""
import pytest

from app.agent import graph as graph_mod
from app.agent.tools.db_query_tool import NO_BUSINESS_DATA_MARK


@pytest.mark.parametrize(
    "question",
    ["去年同期退款率是多少？", "全年 GMV 是多少？Q4 占多少？", "客单价环比变化多少"],
)
def test_report_route_downgrades_to_rag_without_business_keyword(monkeypatch, question):
    """分类器判 report，但问题不含任何业务表关键词 → 必须纠偏到 rag。"""
    monkeypatch.setattr(graph_mod, "classify_route", lambda llm, q: "report")
    monkeypatch.setattr(graph_mod, "get_chat_model", lambda *a, **kw: object())

    state = graph_mod.classify_node({"question": question})
    assert state["route"] == "rag"


def test_db_query_route_downgrades_to_rag_without_business_keyword(monkeypatch):
    """db_query 同理：没有表名关键词就查不到东西，不该走业务库。"""
    monkeypatch.setattr(graph_mod, "classify_route", lambda llm, q: "db_query")
    monkeypatch.setattr(graph_mod, "get_chat_model", lambda *a, **kw: object())

    state = graph_mod.classify_node({"question": "退款率是多少"})
    assert state["route"] == "rag"


@pytest.mark.parametrize(
    "question,expected",
    [
        ("生成设备汇总报表", "report"),
        ("统计一下本周告警数量", "report"),
        ("把工单整理成报表", "report"),
        ("查一下设备清单", "db_query"),
        ("看一下 alert 列表", "db_query"),
    ],
)
def test_business_keyword_keeps_route(monkeypatch, question, expected):
    """含业务表关键词的问题，路由保持原判，不被误纠偏。"""
    monkeypatch.setattr(graph_mod, "classify_route", lambda llm, q: expected)
    monkeypatch.setattr(graph_mod, "get_chat_model", lambda *a, **kw: object())

    assert graph_mod.classify_node({"question": question})["route"] == expected


def test_report_node_does_not_wrap_no_data_into_report(monkeypatch):
    """业务库无匹配数据时，绝不能产出《业务数据汇总报表》。"""
    fallback = f"{NO_BUSINESS_DATA_MARK}：当前仅提供「设备 / 工单 / 告警」三类数据，无法回答该问题。"
    monkeypatch.setattr(graph_mod, "query_business_db", lambda q: fallback)

    out = graph_mod.report_node({"question": "全年 GMV 是多少？"})

    assert out["final_answer"].startswith(NO_BUSINESS_DATA_MARK)
    assert "业务数据汇总报表" not in out["final_answer"]
    assert "报表由系统自动生成" not in out["final_answer"]


def test_report_node_still_builds_report_with_data(monkeypatch):
    """有数据时报表行为不变（回归保护）。"""
    data = "业务库【工单表】查询结果：\ncustomer | amount\nA | 100\nB | 200"
    monkeypatch.setattr(graph_mod, "query_business_db", lambda q: data)

    out = graph_mod.report_node({"question": "生成工单汇总报表"})

    assert out["final_answer"].startswith("# 工单汇总报表")
    assert "| customer | amount |" in out["final_answer"]
    assert "报表由系统自动生成" in out["final_answer"]


def test_no_business_data_text_is_explicit():
    """兜底文案必须自带「未找到」标记，且不再暗示"这就是答案"。

    注：本用例只测纯函数——测试环境 DATABASE_URL 是 sqlite，
    而业务表 DDL 是 MySQL 方言（AUTO_INCREMENT / ENGINE），真实查询无法在单测里跑。
    """
    from app.agent.tools.db_query_tool import no_business_data_text

    text = no_business_data_text(3, 3, 3)

    assert text.startswith(NO_BUSINESS_DATA_MARK)
    assert "无法回答该问题" in text
    assert "设备 3 台" in text  # 概览仍保留，但只作为"可选范围"提示
