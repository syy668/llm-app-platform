"""BM25 检索（内存实现）。

对英文按词、中文按单个字符切分（正则），无需依赖中文分词器。
"""
import math
import re

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[一-鿿]")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


class BM25Index:
    """标准 BM25（k1=1.5, b=0.75），索引在内存中构建。"""

    def __init__(self, corpus: list[str]):
        self.k1 = 1.5
        self.b = 0.75
        self._build(corpus)

    def _build(self, corpus: list[str]) -> None:
        self.n = len(corpus)
        self.docs = [tokenize(t) for t in corpus]
        self.doc_len = [len(d) for d in self.docs]
        self.avgdl = (sum(self.doc_len) / self.n) if self.n else 0.0

        df: dict[str, int] = {}
        for doc in self.docs:
            for term in set(doc):
                df[term] = df.get(term, 0) + 1
        self.df = df
        self.idf = {
            t: math.log(1 + (self.n - n + 0.5) / (n + 0.5))
            for t, n in df.items()
        }

    def search(self, query: str, top_k: int) -> list[tuple[int, float]]:
        """返回 [(corpus_index, score)]，按得分降序，最多 top_k 条。"""
        query_terms = tokenize(query)
        scores = [0.0] * self.n
        for term in query_terms:
            idf = self.idf.get(term, 0.0)
            if idf == 0.0:
                continue
            for i, doc in enumerate(self.docs):
                tf = doc.count(term)
                if tf == 0:
                    continue
                dl = self.doc_len[i] or 1
                denom = tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                scores[i] += idf * (tf * (self.k1 + 1)) / denom

        ranked = sorted(
            ((i, s) for i, s in enumerate(scores) if s > 0),
            key=lambda x: x[1],
            reverse=True,
        )
        return ranked[:top_k]
