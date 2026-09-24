"""Optional learned reranking with a deterministic fallback."""

from __future__ import annotations

import re
from typing import Any


class CrossEncoderReranker:
    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2") -> None:
        self.model_name = model_name
        self._model: Any | None = None

    def _load(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as exc:
                raise RuntimeError(
                    "install sentence-transformers to enable learned reranking"
                ) from exc
            self._model = CrossEncoder(self.model_name)
        return self._model

    def rerank(self, question: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows:
            return []
        try:
            scores = self._load().predict([(question, row["text"]) for row in rows])
            ranked = [dict(row, score=float(score)) for row, score in zip(rows, scores, strict=True)]
        except RuntimeError:
            query_terms = set(re.findall(r"[a-zA-Z0-9]+", question.lower()))
            ranked = [
                dict(row, score=float(row.get("score", 0.0)) + 0.1 * len(
                    query_terms & set(re.findall(r"[a-zA-Z0-9]+", row["text"].lower()))
                ))
                for row in rows
            ]
        return sorted(ranked, key=lambda row: -row["score"])
