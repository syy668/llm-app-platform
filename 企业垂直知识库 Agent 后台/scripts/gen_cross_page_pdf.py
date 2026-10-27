"""生成「跨页表格」演示 PDF（v1.9 跨页表头回填的回归样本）。

用途：`tests/test_pdf_cross_page_table.py::test_real_pdf_continuation_gets_header`
需要一个真实的、表格跨页且续页没有重复表头的 PDF。fpdf2 手绘表格（带边框线，
pdfplumber 才能用 ruling lines 识别出表格），并在接近页底时手动换页，
从而复现「第 2 页第一行只有数据、没有列名」的场景。

用法：
    ./.venv/Scripts/python.exe scripts/gen_cross_page_pdf.py
输出：
    samples/demo_docs/generated/跨页销售明细表.pdf
"""
from pathlib import Path

from fpdf import FPDF

OUT = (
    Path(__file__).resolve().parent.parent
    / "samples" / "demo_docs" / "generated" / "跨页销售明细表.pdf"
)

HEADER = ["Channel", "Q1", "Q2"]
ROWS = [[f"CH-{i:03d}", str(1000 + i), str(2000 + i)] for i in range(36)]
# 换页阈值（mm）：A4 高 297mm，底部留白 10mm；260mm 处换页可保证第 1 页表格被截断
BREAK_Y = 250


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    pdf.cell(0, 8, "Cross-page table: channel sales detail", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=9)

    def row(cells: list[str]) -> None:
        for cell in cells:
            pdf.cell(40, 7, cell, border=1)
        pdf.ln()

    row(HEADER)
    for data in ROWS:
        if pdf.get_y() > BREAK_Y:
            pdf.add_page()
        row(data)

    pdf.output(str(OUT))
    print(f"已生成：{OUT}")


if __name__ == "__main__":
    main()
