"""Text similarity between books, with a small interface so a better model can be plugged in later.

    embedder = TfidfEmbedder()
    embedder.fit({isbn: "summary and subjects text", ...})
    embedder.similarity(isbn_a, isbn_b)              # 0..1
    embedder.shared_terms(isbn_a, isbn_b, 3)         # ["survival", "mars"]: why they are alike

Embedder is the interface BookGraph uses: fit() once with the text of every book, then similarity() for
any two of them. TfidfEmbedder is the default and needs only the standard library: each text becomes a
vector of TF-IDF weights (a word counts for more the rarer it is across all the books) and two books are
as alike as the cosine of their vectors. To use a sentence-embedding model instead, subclass Embedder,
return its cosine similarity (clamped to 0..1) from similarity(), and hand it to BookGraph(embedder=...).
"""

from __future__ import annotations

import math
import re
from typing import Mapping

WORD = re.compile(r"[a-z][a-z']+")
STOPWORDS = frozenset("""
a about after again all also an and any are as at be because been before being between both but by can could did do
does doing down during each few for from further had has have having he her here hers him his how i if in into is it
its just me more most my no nor not now of off on once only or other our out over own same she should so some such
than that the their them then there these they this those through to too under until up very was we were what when
where which while who whom why will with would you your
""".split())


class Embedder:
    """What BookGraph needs from a text model."""

    def fit(self, documents: Mapping[str, str]) -> None:
        """Learn from the text of every book: {isbn: text}. Called again whenever the catalogue is reloaded."""
        raise NotImplementedError

    def similarity(self, a: str, b: str) -> float:
        """How alike two books' texts are, 0 (nothing) to 1 (the same); 0 for an unknown ISBN."""
        raise NotImplementedError

    def shared_terms(self, a: str, b: str, n: int = 3) -> list:
        """A few words that make the two alike, for explanations. Optional: models without words return []."""
        return []


def stem(word: str) -> str:
    """The word with a plural 's' trimmed so "robots" matches "robot" ("cosmos", "class", "focus" stay)."""
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is", "os")) else word


def words_of(text: str) -> list:
    """The lower-case words of a text without stop words, as written ("noble's" is "noble", "it's" is gone)."""
    words = (w.strip("'").removesuffix("'s") for w in WORD.findall(str(text or "").lower()))
    return [w for w in words if len(w) >= 3 and w not in STOPWORDS]


def tokens(text: str) -> list:
    """words_of() with each word stemmed."""
    return [stem(w) for w in words_of(text)]


class TfidfEmbedder(Embedder):
    def __init__(self):
        self.vectors: dict[str, dict] = {}
        self._shown: dict[str, str] = {}            # stem -> a word as written, for explanations

    def fit(self, documents: Mapping[str, str]) -> None:
        counts = {isbn: _counts(tokens(text)) for isbn, text in documents.items()}
        self._shown = {}
        for text in documents.values():
            for word in words_of(text):
                self._shown.setdefault(stem(word), word)
        n = len(counts)
        df: dict[str, int] = {}
        for words in counts.values():
            for word in words:
                df[word] = df.get(word, 0) + 1
        self.vectors = {}
        for isbn, words in counts.items():
            weights = {w: (1 + math.log(c)) * (math.log((1 + n) / (1 + df[w])) + 1) for w, c in words.items()}
            norm = math.sqrt(sum(v * v for v in weights.values()))
            self.vectors[isbn] = {w: v / norm for w, v in weights.items()} if norm else {}

    def similarity(self, a: str, b: str) -> float:
        va, vb = self.vectors.get(a), self.vectors.get(b)
        if not va or not vb:
            return 0.0
        if len(va) > len(vb):
            va, vb = vb, va
        return min(1.0, sum(v * vb.get(w, 0.0) for w, v in va.items()))

    def shared_terms(self, a: str, b: str, n: int = 3) -> list:
        va, vb = self.vectors.get(a, {}), self.vectors.get(b, {})
        shared = sorted(((va[w] * vb[w], w) for w in va.keys() & vb.keys()), reverse=True)
        return [self._shown.get(w, w) for _, w in shared[:n]]


def _counts(words: list) -> dict:
    counts: dict[str, int] = {}
    for word in words:
        counts[word] = counts.get(word, 0) + 1
    return counts
