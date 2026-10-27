"""报表生成工具测试：混合文本转 Markdown 表格、纯管道行兼容、空数据兜底。"""
from app.agent.tools.report_tool import generate_report


def test_mixed_text_becomes_markdown_table():
    """db_query_tool 输出（首行说明文字 + 管道数据行）应正确转为 Markdown 表格，而非整体进代码块。"""
    data = (
        "业务库【设备表】查询结果：\n"
        "id | device_name | status\n"
        "1 | 核心交换机-01 | online\n"
        "2 | 数据库服务器-01 | online"
    )
    out = generate_report.invoke({"data": data, "title": "设备汇总报表"})

    assert out.startswith("# 设备汇总报表")
    assert "业务库【设备表】查询结果：" in out
    assert "| id | device_name | status |" in out          # 表头
    assert "| --- | --- | --- |" in out                    # 分隔行（Markdown 表格标志）
    assert "| 1 | 核心交换机-01 | online |" in out         # 数据行
    assert "```" not in out                                # 不再落入代码块兜底
    assert out.endswith("> 报表由系统自动生成")


def test_pure_pipe_lines_still_supported():
    """旧格式（全部为管道行）行为不变：首行即表头。"""
    out = generate_report.invoke({"data": "a | b\n1 | 2", "title": "T"})
    assert "| a | b |" in out and "| 1 | 2 |" in out


def test_empty_data_fallback():
    out = generate_report.invoke({"data": "", "title": "空报表"})
    assert out == "# 空报表\n\n> 无数据"
