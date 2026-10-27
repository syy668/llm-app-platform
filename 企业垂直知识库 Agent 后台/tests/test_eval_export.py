"""评测 CSV 导出单元测试。"""
from app.api.v1.eval_export import scores_to_csv


class _FakeScore:
    def __init__(self, item_id, question, answer, f, ar, cp, cr, contexts, latency):
        self.item_id = item_id
        self.question = question
        self.answer = answer
        self.faithfulness = f
        self.answer_relevancy = ar
        self.context_precision = cp
        self.context_recall = cr
        self.retrieved_contexts = contexts
        self.latency_ms = latency


def test_scores_to_csv():
    rows = [
        _FakeScore(1, "问题1", "答案1", 0.5, 0.6, 0.7, 0.8, ["片段A", "片段B"], 100),
        _FakeScore(2, "问题2", "答案2", None, None, None, None, None, 200),
    ]
    csv_text = scores_to_csv(rows)
    lines = csv_text.strip().splitlines()
    assert len(lines) == 3  # 表头 + 2 行数据
    assert "item_id" in lines[0] and "faithfulness" in lines[0]
    assert "问题1" in lines[1]
    assert "0.5" in lines[1]
    assert "片段A || 片段B" in lines[1]
    assert lines[2].startswith("2,问题2,答案2,")


def test_scores_to_csv_empty():
    assert scores_to_csv([]).strip().splitlines()[0].startswith("item_id")
