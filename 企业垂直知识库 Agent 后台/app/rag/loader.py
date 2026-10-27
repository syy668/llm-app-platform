"""文档文本解析：PDF / Word / Excel / txt / md。

第三方解析库均按需惰性导入，避免未安装时影响应用启动。

表格处理口径：
- docx：按 document body 子节点真实顺序流式遍历 ``w:p`` / ``w:tbl``，表格留在原位；
- pdf ：优先 pdfplumber 抽表并转成 Markdown（列关系显式化），抽不到表或依赖缺失时退回 pypdf；
- xlsx：openpyxl 逐行 join，天然保留列序。
"""
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# 扩展名 -> 文件类型标识
_EXT_MAP = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".txt": "txt",
    ".md": "md",
}

# ---- 【v1.9】PDF 跨页续表判定阈值（单位 pt，A4 高约 842pt）----
# 本页第一张表的顶边距页顶小于 _PAGE_TOP_MARGIN，视为「表格从页首继续」；
# 上一页最后一张表的底边落到页面下部（>= 页面高度的 _PAGE_BOTTOM_RATIO），
# 视为「表格被页尾截断」。两个条件同时成立、且列数匹配、首行像数据行，才回填表头。
#
# 为什么用「页面高度的比例」而不是固定留白：实测同一份 PDF 中，表格因「下一行放不下」
# 而换页，最后一行的底边距页底可能是 60~120pt（取决于行高与页脚留白），固定 80pt
# 会漏判；而真跨页时表格底边几乎必然落在页面下部，比例阈值更稳。
# 代价是「上一页表格止于页面下部 + 下一页顶部恰好另起一张无表头同列数表格」会被误判，
# 这种版式极罕见，且宁可多一个表头也不要出现无列名的裸数据。
_PAGE_TOP_MARGIN = 100.0
_PAGE_BOTTOM_RATIO = 0.78


def detect_file_type(filename: str) -> str | None:
    """根据文件名后缀识别文件类型，不支持的类型返回 None。"""
    ext = Path(filename).suffix.lower()
    return _EXT_MAP.get(ext)


def extract_text(file_path: str, file_type: str) -> str:
    """按文件类型解析文本。"""
    if file_type == "pdf":
        return _extract_pdf(file_path)
    if file_type == "docx":
        return _extract_docx(file_path)
    if file_type == "xlsx":
        return _extract_xlsx(file_path)
    if file_type in ("txt", "md"):
        return _extract_text_file(file_path)
    raise ValueError(f"不支持的文件类型: {file_type}")


def _clean_cell(value) -> str:
    """单元格归一化：None -> ''，内部换行压成空格，竖线转义避免破坏列结构。"""
    if value is None:
        return ""
    return " ".join(str(value).split()).replace("|", "\\|")


def _table_to_markdown(rows) -> str:
    """二维单元格数组 -> Markdown 表格（首行为表头，补分隔线，按最宽行补齐列）。"""
    cleaned: list[list[str]] = []
    for row in rows or []:
        cells = [_clean_cell(c) for c in (row or [])]
        if any(cells):
            cleaned.append(cells)
    if not cleaned:
        return ""

    width = max(len(r) for r in cleaned)
    cleaned = [r + [""] * (width - len(r)) for r in cleaned]

    lines = [
        "| " + " | ".join(cleaned[0]) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend("| " + " | ".join(r) + " |" for r in cleaned[1:])
    return "\n".join(lines)


# --------------------------------------------------------------------------- PDF


def _extract_pdf(path: str) -> str:
    """PDF 解析：pdfplumber 抽表转 Markdown；任何异常都回退到 pypdf 纯文本。"""
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        logger.info("[解析提示] 未安装 pdfplumber，PDF 退回 pypdf 纯文本解析（表格仅保留顺序）")
        return _extract_pdf_pypdf(path)

    try:
        return _extract_pdf_pdfplumber(path)
    except Exception:
        logger.warning("pdfplumber 解析失败，回退 pypdf: %s", path, exc_info=True)
        return _extract_pdf_pypdf(path)


def _crop_text(page, top: float, bottom: float) -> str:
    """抽取页面纵向区间 [top, bottom] 内的文本。"""
    try:
        return (page.crop((0, top, page.width, bottom)).extract_text() or "").strip()
    except Exception:
        logger.warning("页面横条裁剪失败 top=%s bottom=%s", top, bottom, exc_info=True)
        return ""


def _page_blocks_with_tables(page, prepared: list[tuple[float, float, str]]) -> list[str]:
    """按表格纵向位置把页面切成横条，正文与 Markdown 表格按原顺序交错输出。

    pdfplumber 的 ``extract_text`` 会把表格单元格按空格拼进正文，若再把表格统一追加到页尾，
    会同时踩两个坑：① 同一份数据出现两遍（实测天猫旗舰店出现 2 次，token 翻倍、检索重复命中）；
    ② 章节标题与所属表格脱节（表格全被搬到页尾）。因此这里按 bbox 逐段切：表格上方的正文、
    表格的 Markdown、下一段正文……依次拼接，既不重复也不丢顺序。
    """
    blocks: list[str] = []
    cursor = 0.0
    height = float(page.height)
    for top, bottom, markdown in sorted(prepared, key=lambda item: item[0]):
        if top - cursor > 1:  # 高度过小的条带通常是表格边框线，跳过避免空块
            band = _crop_text(page, cursor, top)
            if band:
                blocks.append(band)
        if markdown:
            blocks.append(markdown)
        cursor = max(cursor, bottom)
    if height - cursor > 1:
        tail = _crop_text(page, cursor, height)
        if tail:
            blocks.append(tail)
    return blocks


def _table_rows(table) -> list[list[str]]:
    """pdfplumber 表格 -> 归一化二维单元格（去掉全空行；单表异常不影响整页）。"""
    try:
        raw = table.extract() or []
    except Exception:
        logger.warning("表格单元格抽取失败，跳过该表", exc_info=True)
        return []
    rows: list[list[str]] = []
    for row in raw:
        cells = [_clean_cell(c) for c in (row or [])]
        if any(cells):
            rows.append(cells)
    return rows


def _same_row(a: list[str], b: list[str]) -> bool:
    """两行是否等价（用于识别「续页自己重复了表头」）。"""
    return [c.strip() for c in a] == [c.strip() for c in b]


def _looks_like_data_row(cells: list[str]) -> bool:
    """数据行特征：至少一个单元格含数字（表头通常是纯文字）。

    这是本方案的核心启发式——跨页续表的首行是数据行（`天猫旗舰店 | 4,820 | ...`），
    而独立新表的首行是表头（`渠道 | Q1销售额 | ...`）。含数字即判定为数据，
    于是给它补上上一页的表头。
    """
    return any(re.search(r"\d", c) for c in cells)


def _extract_pdf_pdfplumber(path: str) -> str:
    import pdfplumber

    parts: list[str] = []
    table_count = 0
    # 【v1.9】跨页续表状态：上一页最后一张表的表头 + 它是否被页尾截断
    prev_header: list[str] | None = None
    prev_ended_at_bottom = False
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            try:
                found = page.find_tables()
            except Exception:
                # 抽表失败只影响该页表格，正文照常保留
                logger.warning("第 %d 页表格抽取失败，仅保留正文", i, exc_info=True)
                found = []

            # (top, bottom, rows)：先保留原始行，跨页判定需要用首行内容
            tables: list[tuple[float, float, list[list[str]]]] = []
            for t in found:
                rows = _table_rows(t)
                if rows:
                    tables.append((float(t.bbox[1]), float(t.bbox[3]), rows))

            # 【v1.9】跨页续表回填：上一页表被页尾截断 + 本页表从页首开始
            # + 本页首行不像表头（与旧表头不同、列数一致、含数字）→ 补表头
            if tables and prev_header and prev_ended_at_bottom:
                top, bottom, rows = tables[0]
                if (
                    top <= _PAGE_TOP_MARGIN
                    and not _same_row(rows[0], prev_header)
                    and len(rows[0]) == len(prev_header)
                    and _looks_like_data_row(rows[0])
                ):
                    tables[0] = (top, bottom, [prev_header] + rows)
                    logger.info(
                        "[解析提示] 第 %d 页表格疑似跨页续表，已回填上一页表头（%d 列）",
                        i, len(prev_header),
                    )

            prepared = [
                (top, bottom, _table_to_markdown(rows))
                for top, bottom, rows in tables
            ]

            if prepared:
                try:
                    blocks = _page_blocks_with_tables(page, prepared)
                except Exception:
                    logger.warning("第 %d 页表格排版失败，退回页尾追加", i, exc_info=True)
                    blocks = [(page.extract_text() or "").strip()]
                    blocks.extend(markdown for _, _, markdown in prepared)
            else:
                blocks = [(page.extract_text() or "").strip()]

            blocks = [b for b in blocks if b]
            if not blocks:
                if not tables:
                    prev_header, prev_ended_at_bottom = None, False
                continue
            table_count += sum(1 for _, _, markdown in prepared if markdown)

            # 更新跨页状态：本页最后一张表的表头与其底边位置
            if tables:
                last_top, last_bottom, last_rows = tables[-1]
                prev_header = last_rows[0] if len(last_rows[0]) >= 2 else None
                prev_ended_at_bottom = last_bottom >= float(page.height) * _PAGE_BOTTOM_RATIO
            else:
                prev_header, prev_ended_at_bottom = None, False

            parts.append("\n".join([f"[第{i}页]", *blocks]))

    if table_count:
        logger.info(
            "[解析提示] %s 检测到 %d 个表格，已转换为 Markdown 结构",
            Path(path).name,
            table_count,
        )
    return "\n\n".join(parts)


def _extract_pdf_pypdf(path: str) -> str:
    from pypdf import PdfReader

    reader = PdfReader(path)
    parts = []
    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            parts.append(f"[第{i}页]\n{text}")
    return "\n\n".join(parts)


# -------------------------------------------------------------------------- DOCX


def _docx_table_rows(table) -> list[str]:
    """表格 -> 每行一条 ``a | b | c`` 记录；横向合并单元格只输出一次。"""
    rows: list[str] = []
    for row in table.rows:
        cells: list[str] = []
        prev_tc = None
        for cell in row.cells:
            # 合并单元格的多个 _Cell 指向同一 w:tc 节点，重复输出会凭空多出列
            if cell._tc is prev_tc:
                continue
            prev_tc = cell._tc
            cells.append(_clean_cell(cell.text))
        line = " | ".join(cells).strip()
        if line.strip(" |"):
            rows.append(line)
    return rows


def _extract_docx(path: str) -> str:
    """按 body 子节点真实顺序遍历，段落与表格不再被拆成两趟。

    旧实现先收集 ``doc.paragraphs`` 再把 ``doc.tables`` 追加到文末，
    导致表格与其所属章节标题脱节（实测同一份销售表：表格从第 151 字符漂到第 804 字符）。
    """
    from docx import Document as DocxDocument
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = DocxDocument(path)
    parts: list[str] = []
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            text = Paragraph(child, doc).text.strip()
            if text:
                parts.append(text)
        elif child.tag == qn("w:tbl"):
            parts.extend(_docx_table_rows(Table(child, doc)))
    return "\n".join(parts)


# -------------------------------------------------------------------------- XLSX


def _extract_xlsx(path: str) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets:
        parts.append(f"[Sheet: {ws.title}]")
        for row in ws.iter_rows(values_only=True):
            values = [_clean_cell(c) for c in row]
            line = " | ".join(values).strip()
            if line.strip(" |"):
                parts.append(line)
    return "\n".join(parts)


def _extract_text_file(path: str) -> str:
    """文本文件：依次尝试 utf-8-sig / gbk / latin-1，兼容 Windows 常见编码。

    utf-8-sig 会自动去掉 UTF-8 BOM（PowerShell `Set-Content -Encoding UTF8` 会写入 BOM），
    避免 BOM 字符污染分块内容。
    """
    data = Path(path).read_bytes()
    for enc in ("utf-8-sig", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")
