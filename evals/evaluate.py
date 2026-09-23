"""Run deterministic retrieval metrics against a versioned golden dataset."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from query import RagQueryPipeline


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    queries: int
    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    mrr: float
    ndcg_at_5: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "queries": self.queries,
            "recall_at_1": round(self.recall_at_1, 4),
            "recall_at_3": round(self.recall_at_3, 4),
            "recall_at_5": round(self.recall_at_5, 4),
            "mrr": round(self.mrr, 4),
            "ndcg_at_5": round(self.ndcg_at_5, 4),
        }


def _ndcg(relevance: list[int], cutoff: int) -> float:
    values = relevance[:cutoff]
    dcg = sum(score / math.log2(rank + 2) for rank, score in enumerate(values))
    ideal = sorted(values, reverse=True)
    idcg = sum(score / math.log2(rank + 2) for rank, score in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def evaluate(dataset: dict[str, Any], *, top_k: int = 5) -> EvaluationMetrics:
    chunks = tuple(
        SimpleNamespace(
            text=row["text"], document_id=row["document_id"], index=row["chunk_index"],
            tenant_id=row["tenant_id"], source=row["source"],
        )
        for row in dataset["chunks"]
    )
    pipeline = RagQueryPipeline(chunks, top_k=top_k, min_score=0.0)
    recall_hits = {1: 0, 3: 0, 5: 0}
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    for item in dataset["queries"]:
        ranked = pipeline._retrieve(item["question"], item["tenant_id"])
        expected = set(item["relevant_sources"])
        relevance = [int(chunk.source in expected) for chunk, _score in ranked]
        for cutoff in recall_hits:
            recall_hits[cutoff] += int(any(relevance[:cutoff]))
        first = next((rank for rank, value in enumerate(relevance, start=1) if value), None)
        reciprocal_ranks.append(1.0 / first if first else 0.0)
        ndcgs.append(_ndcg(relevance, 5))
    count = len(dataset["queries"])
    if count == 0:
        raise ValueError("dataset must contain at least one query")
    return EvaluationMetrics(
        queries=count,
        recall_at_1=recall_hits[1] / count,
        recall_at_3=recall_hits[3] / count,
        recall_at_5=recall_hits[5] / count,
        mrr=sum(reciprocal_ranks) / count,
        ndcg_at_5=sum(ndcgs) / count,
    )


def load_dataset(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        dataset = json.load(handle)
    if not isinstance(dataset, dict) or not dataset.get("chunks") or not dataset.get("queries"):
        raise ValueError("dataset must contain non-empty chunks and queries")
    return dataset


if __name__ == "__main__":
    default = Path(__file__).with_name("datasets") / "golden-v1.json"
    print(json.dumps(evaluate(load_dataset(default)).as_dict(), indent=2))
