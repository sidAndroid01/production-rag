"""Optional learned reranking with a deterministic fallback.

Both paths return scores in [0, 1] so the same evidence threshold applies:
cross-encoder logits pass through a sigmoid, and the fallback blends the
hybrid score with exact-term overlap without exceeding 1.
"""

from __future__ import annotations

import math
import re
from typing import Any

TERM_RE = re.compile(r"[a-zA-Z0-9]+")


def sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp = math.exp(value)
    return exp / (1.0 + exp)


class CrossEncoderReranker:
    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2") -> None:
        self.model_name = model_name
        self._model: Any | None = None
        self._unavailable = False

    def _load(self) -> Any | None:
        if self._model is None and not self._unavailable:
            try:
                from sentence_transformers import CrossEncoder
            except ImportError:
                # Remember the miss so every query does not retry the import.
                self._unavailable = True
                return None
            self._model = CrossEncoder(self.model_name)
        return self._model

    @staticmethod
    def _fallback_score(question: str, row: dict[str, Any]) -> float:
        query_terms = set(TERM_RE.findall(question.lower()))
        chunk_terms = set(TERM_RE.findall(row["text"].lower()))
        overlap = len(query_terms & chunk_terms) / max(len(query_terms), 1)
        base = min(max(float(row.get("score", 0.0)), 0.0), 1.0)
        return 0.9 * base + 0.1 * overlap

    def rerank(self, question: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows:
            return []
        model = self._load()
        if model is not None:
            logits = model.predict([(question, row["text"]) for row in rows])
            ranked = [
                dict(row, score=sigmoid(float(logit)))
                for row, logit in zip(rows, logits, strict=True)
            ]
        else:
            ranked = [dict(row, score=self._fallback_score(question, row)) for row in rows]
        return sorted(ranked, key=lambda row: -row["score"])
