"""把 samples/demo_docs 下的演示文档 Markdown 生成为真实 PDF / DOCX。

用途：为知识库上传解析链路（pdf/docx）准备"有挑战性"的素材。
依赖：fpdf2（PDF，需系统中有中文字体）、python-docx（DOCX）。

用法：
  python scripts/gen_demo_docs.py                 # 只生成素材1、素材2（PDF + DOCX）
  python scripts/gen_demo_docs.py --all           # 四份全生成
  python scripts/gen_demo_docs.py --only 1,3      # 指定素材编号
  python scripts/gen_demo_docs.py --formats pdf   # 只出 PDF

设计说明：
- 不引入 markdown 第三方库，内置一个够用的轻量解析器（标题/段落/表格/列表/引用/加粗），
  避免为了演示素材再往依赖里塞东西。
- PDF 侧用 fpdf2 的 table() 渲染，跨页自动断行；表格列宽按"显示宽度"（中文按 2 计）加权分配，
  避免 9 列表格被均分后挤成竖排。
- 中文字体优先 simhei.ttf（TTF），不用 .ttc：fpdf2 对 TrueType Collection 支持不稳。
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MD_DIR = ROOT / "samples" / "demo_docs"
OUT_DIR = MD_DIR / "generated"

# 素材编号 -> (markdown 文件名, 输出基名)
DOCS: dict[str, tuple[str, str]] = {
    "1": ("2025年各渠道销售与库存盘点表.md", "2025年各渠道销售与库存盘点表"),
    "2": ("核心数据库集群故障应急响应手册.md", "核心数据库集群故障应急响应手册"),
    "3": ("公司差旅报销制度.md", "公司差旅报销制度"),
    "4": ("电商大促复盘数据分析.md", "电商大促复盘数据分析"),
}

FONT_CANDIDATES = [
    Path("C:/Windows/Fonts/simhei.ttf"),
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/simsun.ttc"),
    Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
]


# ---------------------------------------------------------------- Markdown 解析


@dataclass
class Block:
    kind: str  # heading / paragraph / table / bullet / quote
    text: str = ""
    level: int = 0
    rows: list[list[str]] | None = None


def parse_markdown(md: str) -> list[Block]:
    """把 Markdown 拆成块级结构；支持 # 标题、| 表格、- 列表、> 引用、普通段落。"""
    lines = md.replace("\r\n", "\n").split("\n")
    blocks: list[Block] = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            blocks.append(Block("heading", m.group(2).strip(), level=len(m.group(1))))
            i += 1
            continue

        # 表格：当前行以 | 开头，且下一行是分隔行
        if stripped.startswith("|") and i + 1 < len(lines) and re.match(
            r"^\|[\s:\-|]+\|$", lines[i + 1].strip()
        ):
            rows: list[list[str]] = []
            header = _split_row(stripped)
            rows.append(header)
            i += 2  # 跳过分隔行
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_split_row(lines[i].strip()))
                i += 1
            blocks.append(Block("table", rows=rows))
            continue

        # 引用（连续多行合并为一段）
        if stripped.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append(Block("quote", " ".join(x for x in buf if x)))
            continue

        # 列表
        if re.match(r"^([-*+]|\d+\.)\s+", stripped):
            blocks.append(Block("bullet", re.sub(r"^([-*+]|\d+\.)\s+", "", stripped)))
            i += 1
            continue

        # 普通段落：合并连续非空行（中文换行不产生空格）
        buf = []
        while i < len(lines) and lines[i].strip() and not lines[i].strip().startswith(
            ("#", "|", ">")
        ):
            if re.match(r"^([-*+]|\d+\.)\s+", lines[i].strip()):
                break
            buf.append(lines[i].strip())
            i += 1
        if buf:
            blocks.append(Block("paragraph", "".join(buf)))
    return blocks


def _split_row(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [c.strip() for c in row.split("|")]


def strip_markup(text: str) -> str:
    return text.replace("**", "").replace("`", "").replace("__", "")


def display_width(text: str) -> int:
    """中文/全角按 2 计，用于列宽加权。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


# ---------------------------------------------------------------- PDF


def _pick_font() -> Path:
    for p in FONT_CANDIDATES:
        if p.exists() and p.suffix.lower() == ".ttf":
            return p
    for p in FONT_CANDIDATES:  # 退一步：允许 ttc
        if p.exists():
            return p
    raise SystemExit("未找到中文字体，请安装 simhei.ttf 后重试")


def build_pdf(md_path: Path, out_path: Path, title: str) -> int:
    from fpdf import FPDF
    from fpdf.fonts import FontFace

    blocks = parse_markdown(md_path.read_text(encoding="utf-8"))
    font = _pick_font()

    # 宽表格（>=8 列）用横向 A4，否则纵向
    max_cols = max((len(b.rows[0]) for b in blocks if b.kind == "table" and b.rows), default=0)
    landscape = max_cols >= 8

    pdf = FPDF(orientation="L" if landscape else "P", unit="mm", format="A4")
    pdf.set_margins(15, 15, 15)
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.add_font("CN", "", str(font))
    pdf.add_font("CN", "B", str(font))  # 复用同一字体充当粗体，保证 markdown=True 不报缺字体
    pdf.add_page()
    epw = pdf.epw

    for b in blocks:
        if b.kind == "heading":
            sizes = {1: 17, 2: 13.5, 3: 11.5}
            pdf.ln(4 if b.level == 1 else 2.5)
            pdf.set_font("CN", "B", sizes.get(b.level, 10.5))
            pdf.multi_cell(epw, 8, strip_markup(b.text), new_x="LMARGIN", new_y="NEXT")
            if b.level <= 2:
                y = pdf.get_y() + 1
                pdf.set_draw_color(150, 150, 150)
                pdf.line(pdf.l_margin, y, pdf.l_margin + epw, y)
                pdf.ln(2)
        elif b.kind == "paragraph":
            pdf.set_font("CN", "", 10)
            pdf.multi_cell(epw, 6.2, strip_markup(b.text), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1.6)
        elif b.kind == "bullet":
            pdf.set_font("CN", "", 10)
            pdf.multi_cell(epw, 6.2, "· " + strip_markup(b.text), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(0.8)
        elif b.kind == "quote":
            pdf.set_font("CN", "", 10)
            pdf.set_text_color(70, 70, 70)
            pdf.set_left_margin(19)
            pdf.multi_cell(epw - 4, 6.2, strip_markup(b.text), new_x="LMARGIN", new_y="NEXT")
            pdf.set_left_margin(15)
            pdf.set_text_color(0, 0, 0)
            pdf.ln(2)
        elif b.kind == "table" and b.rows:
            _pdf_table(pdf, b.rows, FontFace)
            pdf.ln(3)

    pdf.set_title(title)
    pdf.output(str(out_path))
    return pdf.page_no()


def _pdf_table(pdf, rows: list[list[str]], FontFace) -> None:
    ncols = len(rows[0])
    weights = []
    for c in range(ncols):
        w = max(display_width(strip_markup(r[c])) if c < len(r) else 0 for r in rows)
        weights.append(max(w, 6))
    total = sum(weights)
    widths = [max(pdf.epw * w / total, 14) for w in weights]
    scale = pdf.epw / sum(widths)
    widths = [w * scale for w in widths]

    size = 8.5 if ncols <= 4 else (7.6 if ncols <= 6 else 6.8)
    pdf.set_font("CN", "", size)
    pdf.set_draw_color(180, 180, 180)

    align = []
    for c in range(ncols):
        body = [r[c] for r in rows[1:] if c < len(r)]
        numeric = body and all(re.fullmatch(r"[-\d,.%\s—/]+", strip_markup(v)) for v in body)
        align.append("RIGHT" if numeric else "LEFT")

    with pdf.table(
        col_widths=widths,
        text_align=tuple(align),
        line_height=size * 0.55,
        headings_style=FontFace(emphasis="BOLD", fill_color=(236, 238, 241)),
        first_row_as_headings=True,
        padding=1.2,
    ) as table:
        for r in rows:
            row = table.row()
            for c in range(ncols):
                row.cell(strip_markup(r[c]) if c < len(r) else "")


# ---------------------------------------------------------------- DOCX


def build_docx(md_path: Path, out_path: Path, title: str) -> int:
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    blocks = parse_markdown(md_path.read_text(encoding="utf-8"))
    max_cols = max((len(b.rows[0]) for b in blocks if b.kind == "table" and b.rows), default=0)

    doc = Document()
    sec = doc.sections[0]
    if max_cols >= 8:  # 宽表转横向
        sec.orientation = WD_ORIENT.LANDSCAPE
        sec.page_width, sec.page_height = sec.page_height, sec.page_width
    for side in ("left_margin", "right_margin"):
        setattr(sec, side, Pt(56))

    # 正文中文字体（Normal 样式必须同时设 ascii/eastAsia，否则中文回退宋体）
    normal = doc.styles["Normal"]
    normal.font.name = "微软雅黑"
    normal.font.size = Pt(10.5)
    normal.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "微软雅黑")

    # 标题样式的 eastAsia 也要显式指定，否则 Heading 走 Calibri Light + 等线，与正文不统一
    for name in ("Title", "Heading 1", "Heading 2", "Heading 3", "Heading 4"):
        try:
            st = doc.styles[name]
        except KeyError:
            continue
        st.font.name = "微软雅黑"
        st.font.color.rgb = RGBColor(0x1F, 0x1F, 0x1F)
        st.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "微软雅黑")

    def add_runs(par, text: str, size: float | None = None, color=None) -> None:
        for idx, seg in enumerate(text.split("**")):
            if not seg:
                continue
            run = par.add_run(strip_markup(seg))
            run.bold = idx % 2 == 1
            if size:
                run.font.size = Pt(size)
            if color:
                run.font.color.rgb = color

    doc.add_heading(title, level=0)

    for b in blocks:
        if b.kind == "heading":
            p = doc.add_heading("", level=min(b.level, 4))
            add_runs(p, b.text)
        elif b.kind == "paragraph":
            p = doc.add_paragraph()
            p.paragraph_format.first_line_indent = Pt(21)
            p.paragraph_format.space_after = Pt(4)
            add_runs(p, b.text)
        elif b.kind == "bullet":
            p = doc.add_paragraph(style="List Bullet")
            add_runs(p, b.text)
        elif b.kind == "quote":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(18)
            p.paragraph_format.space_after = Pt(4)
            add_runs(p, b.text, size=10, color=RGBColor(0x50, 0x50, 0x50))
        elif b.kind == "table" and b.rows:
            table = doc.add_table(rows=len(b.rows), cols=len(b.rows[0]))
            table.style = "Table Grid"
            table.autofit = True
            for ri, r in enumerate(b.rows):
                for ci in range(len(b.rows[0])):
                    cell = table.cell(ri, ci)
                    cell.text = ""
                    par = cell.paragraphs[0]
                    par.paragraph_format.space_after = Pt(0)
                    if ri == 0:
                        par.alignment = WD_ALIGN_PARAGRAPH.CENTER
                        run = par.add_run(strip_markup(r[ci]) if ci < len(r) else "")
                        run.bold = True
                        run.font.size = Pt(9)
                    else:
                        add_runs(par, r[ci] if ci < len(r) else "", size=9)
            doc.add_paragraph()

    doc.core_properties.title = title
    doc.core_properties.author = "经营分析部"
    doc.save(str(out_path))
    return len(doc.inline_shapes)  # 占位返回，页数由 Word 排版决定


# ---------------------------------------------------------------- CLI


def main() -> None:
    ap = argparse.ArgumentParser(description="生成演示文档的 PDF / DOCX")
    ap.add_argument("--only", default="1,2", help="素材编号，逗号分隔，如 1,2,3,4")
    ap.add_argument("--all", action="store_true", help="等价于 --only 1,2,3,4")
    ap.add_argument("--formats", default="pdf,docx", help="输出格式，逗号分隔")
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    args = ap.parse_args()

    keys = list(DOCS) if args.all else [k.strip() for k in args.only.split(",") if k.strip()]
    formats = {f.strip().lower() for f in args.formats.split(",") if f.strip()}
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for k in keys:
        if k not in DOCS:
            print(f"[跳过] 未知素材编号 {k}")
            continue
        md_name, base = DOCS[k]
        md_path = MD_DIR / md_name
        if not md_path.exists():
            print(f"[跳过] 缺少源文件 {md_path}")
            continue

        if "pdf" in formats:
            out = out_dir / f"{base}.pdf"
            pages = build_pdf(md_path, out, base)
            print(f"[PDF ] {out.name}  {out.stat().st_size / 1024:,.1f} KB  共 {pages} 页")
        if "docx" in formats:
            out = out_dir / f"{base}.docx"
            build_docx(md_path, out, base)
            print(f"[DOCX] {out.name}  {out.stat().st_size / 1024:,.1f} KB")

    print(f"\n输出目录：{out_dir}")


if __name__ == "__main__":
    main()
