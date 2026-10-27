"""报表生成工具：把查询到的数据格式化为标准 Markdown 报表。"""
from langchain_core.tools import tool
from pydantic import BaseModel, Field


class ReportInput(BaseModel):
    """generate_report 工具入参。"""

    data: str = Field(..., description="查询到的数据文本，支持“列名 | 列名”管道分隔的多行数据")
    title: str = Field("数据报表", description="报表标题")


@tool(args_schema=ReportInput)
def generate_report(data: str, title: str = "数据报表") -> str:
    """将查询到的数据格式化为标准 Markdown 报表。

    Args:
        data: 查询到的数据文本（多行；管道分隔的行会转成 Markdown 表格）。
        title: 报表标题。
    """
    lines = [ln.rstrip() for ln in (data or "").strip().splitlines() if ln.strip()]
    if not lines:
        return f"# {title}\n\n> 无数据"

    md = [f"# {title}", ""]

    # 逐段扫描：连续的管道分隔行转为 Markdown 表格，其余行保留为普通文本
    # （兼容 db_query_tool 输出：首行说明文字 + 后续管道数据行）
    i = 0
    while i < len(lines):
        if "|" in lines[i]:
            block = []
            while i < len(lines) and "|" in lines[i]:
                block.append(lines[i])
                i += 1
            header = [c.strip() for c in block[0].split("|")]
            md.append("| " + " | ".join(header) + " |")
            md.append("|" + "|".join([" --- "] * len(header)) + "|")
            for row_ln in block[1:]:
                cells = [c.strip() for c in row_ln.split("|")]
                cells += [""] * (len(header) - len(cells))  # 补齐列，避免错位
                md.append("| " + " | ".join(cells[:len(header)]) + " |")
            md.append("")
        else:
            md.append(lines[i])
            md.append("")
            i += 1

    md.append("> 报表由系统自动生成")
    return "\n".join(md)
