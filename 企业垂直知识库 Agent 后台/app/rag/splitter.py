"""文本分块与 token 估算。

分块策略（两档，按文本内容自动选择）：
- 无表格文本：字符滑窗，行为与此前完全一致；
- 含表格文本：以整行为最小切分单位打包（行不被拦腰截断），并为每个"从表格中段开始"的块
  自动前置该表格的**表头行 + 分隔线**，保证任何一条记录脱离原块后仍自带列名。
"""
import re

_CJK_RE = re.compile(r"[一-鿿]")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")

# 判定"表格行"的竖线阈值：Markdown / xlsx / docx 抽出的行至少有 2 个分隔符
_TABLE_LINE_MIN_PIPES = 2
# 连续出现多少条表格行才认为文本中存在表格区域
_TABLE_MIN_ROWS = 2


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：中文按字符计、英文/数字按词计。"""
    return max(1, len(_CJK_RE.findall(text)) + len(_WORD_RE.findall(text)))


def _is_table_line(line: str) -> bool:
    return line.count("|") >= _TABLE_LINE_MIN_PIPES


def _is_divider(line: str) -> bool:
    """Markdown 表格分隔线：每个单元格只由 ``-`` ``:`` 空格组成，且至少一个 ``-``。"""
    if not _is_table_line(line) or "-" not in line:
        return False
    cells = line.strip().strip("|").split("|")
    return all(set(cell) <= set("-: ") for cell in cells)


def has_table(lines: list[str]) -> bool:
    """粗略判断文本是否包含表格区域（连续若干行都带多个竖线分隔符）。"""
    hits = 0
    for line in lines:
        if _is_table_line(line):
            hits += 1
            if hits >= _TABLE_MIN_ROWS:
                return True
    return False


def _table_prefixes(lines: list[str]) -> list[str | None]:
    """为表格区域内除首行外的每一行，准备"该补到块首的表头前缀"。

    返回与 ``lines`` 等长的列表，元素含义：
    - ``None``：不在表格区域内，或本身就是表头行（无需补）；
    - 表头行字符串：该行是分隔线，只缺表头；
    - ``"表头\\n分隔线"``：该行是数据行，表头与分隔线都需要补。

    分隔线只存在于 Markdown 形态的表格（pdf 抽表、.md 源文件）；docx/xlsx 抽出的行没有分隔线，
    此时前缀只有表头一行，同样能表达列名。
    """
    prefixes: list[str | None] = [None] * len(lines)
    i, total = 0, len(lines)
    while i < total:
        if not _is_table_line(lines[i]):
            i += 1
            continue
        start = i
        while i < total and _is_table_line(lines[i]):
            i += 1
        end = i - 1
        if end - start + 1 < _TABLE_MIN_ROWS:  # 单行孤岛不当作表格，避免把正文误补
            continue

        header = lines[start].strip()
        divider = lines[start + 1].strip() if _is_divider(lines[start + 1]) else None
        full = f"{header}\n{divider}" if divider else header
        for k in range(start + 1, end + 1):
            prefixes[k] = header if (divider and k == start + 1) else full
    return prefixes


def split_text(text: str, chunk_size: int = 500, chunk_overlap: int = 50) -> list[str]:
    """分块入口：含表格走行对齐切分，否则走字符滑窗。返回非空块列表。"""
    text = (text or "").strip()
    if not text:
        return []

    # 重叠不小于块大小时无意义，退化为无重叠
    if chunk_overlap >= chunk_size:
        chunk_overlap = 0

    lines = text.split("\n")
    if has_table(lines):
        return _split_by_lines(lines, chunk_size, chunk_overlap)
    return _split_by_chars(text, chunk_size, chunk_overlap)


def _split_by_chars(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """原字符滑窗实现（保持行为不变，供无表格文本使用）。"""
    chunks: list[str] = []
    step = max(1, chunk_size - chunk_overlap)
    start = 0
    while start < len(text):
        chunk = text[start:start + chunk_size].strip()
        if chunk:
            chunks.append(chunk)
        if start + chunk_size >= len(text):
            break
        start += step
    return chunks


def _split_by_lines(lines: list[str], chunk_size: int, chunk_overlap: int) -> list[str]:
    """按整行打包成不超过 chunk_size 的块，行本身绝不被截断。

    - 单行超过 chunk_size（PDF 抽文常见的一整段单行）时只能硬切，切完清空缓冲；
    - 重叠取上一块尾部的**整行**，累计长度不超过 chunk_overlap；
    - 只含重叠行、没有新行的缓冲不会重复输出；
    - 块若从表格中段（或分隔线）开始，自动前置该表格的表头（含分隔线），并把这部分
      长度计入 chunk_size 预算，避免"补表头"把块撑出上限。
    """
    prefixes = _table_prefixes(lines)
    chunks: list[str] = []
    buf: list[int] = []          # 存行号，便于查前缀
    buf_len = 0                  # 仅统计正文长度（不含前缀）
    has_new = False

    def head_of(idxs: list[int]) -> int | None:
        """块内第一条非空行的行号。"""
        for idx in idxs:
            if lines[idx].strip():
                return idx
        return None

    def reserve(idxs: list[int]) -> int:
        """该块需要额外前置的表头长度（含换行）。"""
        if not idxs:
            return 0
        head = head_of(idxs)
        prefix = prefixes[head] if head is not None else None
        return len(prefix) + 1 if prefix else 0

    def render(idxs: list[int]) -> str:
        body = "\n".join(lines[i] for i in idxs).strip()
        if not body:
            return ""
        head = head_of(idxs)
        prefix = prefixes[head] if head is not None else None
        return f"{prefix}\n{body}" if prefix else body

    def emit() -> None:
        nonlocal buf, buf_len, has_new
        content = render(buf)
        if content:
            chunks.append(content)
        # 回溯尾部整行作为下一块的重叠上下文
        tail: list[int] = []
        total = 0
        for idx in reversed(buf):
            add = len(lines[idx].rstrip()) + (1 if tail else 0)
            if total + add > chunk_overlap:
                break
            tail.insert(0, idx)
            total += add
        buf = tail
        buf_len = total
        has_new = False

    for idx, raw in enumerate(lines):
        line = raw.rstrip()
        if len(line) > chunk_size:
            if has_new:
                emit()
            # 超长单行无法行对齐，按字符硬切（不与前后行共用重叠）
            for i in range(0, len(line), chunk_size):
                piece = line[i:i + chunk_size].strip()
                if piece:
                    chunks.append(piece)
            buf, buf_len, has_new = [], 0, False
            continue

        add = len(line) + (1 if buf else 0)
        if buf and buf_len + add + reserve(buf) > chunk_size:
            emit()
            add = len(line) + (1 if buf else 0)
            if buf_len + add + reserve(buf) > chunk_size:
                # 重叠尾行已占满预算时放弃重叠，保证向前推进
                buf, buf_len = [], 0
                add = len(line)
        buf.append(idx)
        buf_len += add
        has_new = True

    if has_new:
        content = render(buf)
        if content:
            chunks.append(content)
    return chunks
