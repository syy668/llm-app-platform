"""v1.9 PDF 跨页表格表头回填（尽力局）回归用例。

不依赖真实 PDF：用假 pdfplumber 模块精确控制「上一页表格是否被页尾截断」
「本页表格是否从页首开始」「本页首行是数据还是表头」，覆盖 5 种判定分支。
"""
import sys
import types
from pathlib import Path

import pytest

import app.rag.loader as loader

SAMPLE_PDF = (
    Path(__file__).resolve().parent.parent
    / "samples" / "demo_docs" / "generated" / "跨页销售明细表.pdf"
)

HEADER = ["渠道", "Q1销售额"]

PAGE1_ROWS = [HEADER, ["天猫旗舰店", "4820"]]
PAGE2_DATA_ROWS = [["京东自营", "1200"], ["拼多多", "900"]]


class _FakeTable:
    def __init__(self, bbox, rows):
        self.bbox = bbox
        self._rows = rows

    def extract(self):
        return self._rows


class _FakePage:
    """最小可用的 pdfplumber Page 替身：只实现 loader 用到的 4 个成员。"""

    def __init__(self, height, tables, bands=None):
        self.height = height
        self.width = 595.0
        self._tables = tables
        self._bands = bands or []  # [(top, bottom, text)]

    def find_tables(self):
        return self._tables

    def extract_text(self):
        return "\n".join(t for _, _, t in self._bands)

    def crop(self, box):
        top, bottom = box[1], box[3]
        text = "\n".join(t for s, e, t in self._bands if s >= top - 1 and e <= bottom + 1)
        return types.SimpleNamespace(extract_text=lambda: text)


class _FakePdf:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _run(monkeypatch, pages) -> str:
    fake = types.SimpleNamespace(open=lambda path: _FakePdf(pages))
    monkeypatch.setitem(sys.modules, "pdfplumber", fake)
    return loader._extract_pdf_pdfplumber("dummy.pdf")


def _page1(monkeypatch, bottom=830.0):
    """第 1 页：表头 + 1 行数据，表格底边默认贴到页尾（842 - 12）。"""
    return _FakePage(
        842.0,
        tables=[_FakeTable((60, 500, 540, bottom), PAGE1_ROWS)],
        bands=[(60, 480, "一、分渠道销售明细")],
    )


def test_continuation_page_gets_header_backfilled(monkeypatch, caplog):
    import logging

    page2 = _FakePage(842.0, tables=[_FakeTable((60, 70, 540, 400), PAGE2_DATA_ROWS)])
    with caplog.at_level(logging.INFO, logger="app.rag.loader"):
        text = _run(monkeypatch, [_page1(monkeypatch), page2])

    # 表头出现两次：第 1 页自带一次，第 2 页续页回填一次
    assert text.count("| 渠道 | Q1销售额 |") == 2
    assert text.count("| --- | --- |") == 2
    # 回填后的续页表：表头紧跟数据，不再出现“无列名的裸数字”
    assert "| 渠道 | Q1销售额 |\n| --- | --- |\n| 京东自营 | 1200 |" in text
    assert any("跨页续表" in r.getMessage() for r in caplog.records)


def test_page_with_own_header_not_backfilled(monkeypatch):
    page2 = _FakePage(842.0, tables=[_FakeTable((60, 70, 540, 400), [HEADER, ["京东自营", "1200"]])])
    text = _run(monkeypatch, [_page1(monkeypatch), page2])

    # 本页已有表头（与上一页一致）→ 不重复插入
    assert text.count("| 渠道 | Q1销售额 |") == 2
    assert text.count("| --- | --- |") == 2


def test_no_backfill_when_prev_table_not_cut(monkeypatch):
    """上一页表格底边离页尾还很远 → 上一页表没被截断，本页是新表，不回填。"""
    page1 = _page1(monkeypatch, bottom=400.0)
    page2 = _FakePage(842.0, tables=[_FakeTable((60, 70, 540, 400), PAGE2_DATA_ROWS)])
    text = _run(monkeypatch, [page1, page2])

    assert text.count("| 渠道 | Q1销售额 |") == 1


def test_no_backfill_when_table_not_at_page_top(monkeypatch):
    """本页表格从页面中部开始（上方还有正文）→ 不是续页，不回填。"""
    page2 = _FakePage(
        842.0,
        tables=[_FakeTable((60, 300, 540, 700), PAGE2_DATA_ROWS)],
        bands=[(60, 280, "二、附录说明")],
    )
    text = _run(monkeypatch, [_page1(monkeypatch), page2])

    assert text.count("| 渠道 | Q1销售额 |") == 1


def test_no_backfill_when_column_count_mismatch(monkeypatch):
    """列数对不上（4 列 vs 2 列表头）→ 大概率是另一张表，不回填。"""
    page2 = _FakePage(
        842.0,
        tables=[_FakeTable((60, 70, 540, 400), [["A", "1", "2", "3"], ["B", "4", "5", "6"]])],
    )
    text = _run(monkeypatch, [_page1(monkeypatch), page2])

    assert text.count("| 渠道 | Q1销售额 |") == 1


def test_no_backfill_when_first_row_has_no_digits(monkeypatch):
    """首行全是文字（像表头而不是数据）→ 判定为本页新表，不回填。"""
    page2 = _FakePage(
        842.0,
        tables=[_FakeTable((60, 70, 540, 400), [["季度", "目标额"], ["Q1", "5000"]])],
    )
    text = _run(monkeypatch, [_page1(monkeypatch), page2])

    assert text.count("| 渠道 | Q1销售额 |") == 1


def test_single_page_pdf_unchanged(monkeypatch):
    """单页 / 无表格页面完全走原逻辑，不产生任何回填。"""
    page = _FakePage(842.0, tables=[], bands=[(60, 100, "纯文字页面，无表格")])
    text = _run(monkeypatch, [page])

    assert "纯文字页面，无表格" in text
    assert "|" not in text
    assert "跨页续表" not in text


def test_header_state_resets_on_text_page(monkeypatch):
    """中间夹了一整页正文后，后面的表不应再套用更早的表头。"""
    page3 = _FakePage(842.0, tables=[_FakeTable((60, 70, 540, 400), PAGE2_DATA_ROWS)])
    text = _run(monkeypatch, [_page1(monkeypatch), _FakePage(842.0, tables=[]), page3])

    assert text.count("| 渠道 | Q1销售额 |") == 1


# ------------------------------------------------- 真实 PDF（端到端）

needs_sample_pdf = pytest.mark.skipif(
    not SAMPLE_PDF.exists(), reason="缺少跨页表格样本 PDF（scripts/gen_cross_page_pdf.py 可重建）"
)


@needs_sample_pdf
def test_real_pdf_continuation_gets_header(caplog):
    """真实 2 页 PDF：第 2 页续表首行是数据，回填后紧跟表头与分隔线。"""
    import logging

    with caplog.at_level(logging.INFO, logger="app.rag.loader"):
        text = loader.extract_text(str(SAMPLE_PDF), "pdf")

    assert text.count("| Channel | Q1 | Q2 |") == 2
    page2 = text.split("[第2页]")[1].strip()
    assert page2.startswith("| Channel | Q1 | Q2 |")
    assert page2.split("\n")[1] == "| --- | --- | --- |"
    assert "CH-033" in page2
    assert any("跨页续表" in r.getMessage() for r in caplog.records)


# ------------------------------------------------- 真实 PDF（端到端）

needs_sample_pdf = pytest.mark.skipif(
    not SAMPLE_PDF.exists(), reason="缺少跨页表格样本 PDF（scripts/gen_cross_page_pdf.py 可重建）"
)


@needs_sample_pdf
def test_real_pdf_continuation_gets_header(caplog):
    """真实 2 页 PDF：第 2 页续表首行是数据，回填后紧跟表头与分隔线。"""
    import logging

    with caplog.at_level(logging.INFO, logger="app.rag.loader"):
        text = loader.extract_text(str(SAMPLE_PDF), "pdf")

    assert text.count("| Channel | Q1 | Q2 |") == 2
    page2 = text.split("[第2页]")[1].strip()
    assert page2.startswith("| Channel | Q1 | Q2 |")
    assert page2.split("\n")[1] == "| --- | --- | --- |"
    assert "CH-033" in page2
    assert any("跨页续表" in r.getMessage() for r in caplog.records)
