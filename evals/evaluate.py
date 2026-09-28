"""Evaluate the full RAG pipeline against a versioned golden dataset.

The evaluator drives the real ``RagApiApplication``: documents go through the
upload route, questions through ``/v1/query``, so ingestion, identity, ACLs,
retrieval, the evidence gate, generation, and citation alignment are all
measured together.

    python -m evals.evaluate                       # in-memory lexical backend
    RAG_EVAL_DSN=postgresql://rag_app:...@localhost/rag \\
        python -m evals.evaluate --backend postgres  # pgvector hybrid backend
    python -m evals.evaluate --check               # fail on regression (CI)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from api import RagApiApplication
from auth import ApiKeyStore
from gateway import configured_provider
from permissions import Principal
from providers import ExtractiveProvider, ModelProvider

DATASETS = Path(__file__).with_name("datasets")
DEFAULT_DATASET = DATASETS / "golden-v2.json"
THRESHOLDS = Path(__file__).with_name("thresholds.json")
EVAL_KEY = "eval-key"


@dataclass(frozen=True, slots=True)
class Report:
    backend: str
    dataset: str
    queries: int
    answerable: int
    unanswerable: int
    # Retrieval, over answerable questions.
    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    mrr: float
    ndcg_at_5: float
    paraphrase_recall_at_5: float
    # Answers.
    answer_rate: float
    abstention_accuracy: float
    citation_precision: float
    expected_term_recall: float
    # Evidence-gate calibration.
    min_score: float
    suggested_min_score: float
    # Latency of /v1/query in milliseconds.
    latency_p50_ms: float
    latency_p95_ms: float

    def as_dict(self) -> dict[str, Any]:
        return {
            key: round(value, 4) if isinstance(value, float) else value
            for key, value in asdict(self).items()
        }


def load_dataset(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        dataset = json.load(handle)
    if not isinstance(dataset, dict) or not dataset.get("documents") or not dataset.get("queries"):
        raise ValueError("dataset must contain non-empty documents and queries")
    return dataset


def ndcg(relevance: list[int], cutoff: int, relevant_count: int) -> float:
    dcg = sum(score / math.log2(rank + 2) for rank, score in enumerate(relevance[:cutoff]))
    ideal = sum(1 / math.log2(rank + 2) for rank in range(min(relevant_count, cutoff)))
    return dcg / ideal if ideal else 0.0


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))]


def best_threshold(answerable: list[float], unanswerable: list[float]) -> float:
    """Threshold maximizing balanced accuracy of answer-vs-abstain on top scores."""
    candidates = sorted({0.0, *answerable, *unanswerable})
    best, best_score = 0.0, -1.0
    for threshold in candidates:
        answered = sum(score >= threshold for score in answerable) / max(len(answerable), 1)
        abstained = sum(score < threshold for score in unanswerable) / max(len(unanswerable), 1)
        balanced = (answered + abstained) / 2
        if balanced > best_score:
            best, best_score = threshold, balanced
    return best


def _post(app: RagApiApplication, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    status, body = app.handle("POST", path, {"x-api-key": EVAL_KEY}, json.dumps(payload).encode())
    if status >= 400:
        raise RuntimeError(f"{path} returned {status}: {body}")
    return body


def build_app(
    backend: str, tenant_id: str, provider: ModelProvider, dsn: str | None
) -> RagApiApplication:
    keys = ApiKeyStore.single(EVAL_KEY, tenant_id)
    if backend == "memory":
        return RagApiApplication(keys=keys, provider=provider)
    if backend == "postgres":
        if not dsn:
            raise ValueError("the postgres backend needs RAG_EVAL_DSN")
        from persistence import PostgresPersistence

        return RagApiApplication(keys=keys, provider=provider, persistence=PostgresPersistence(dsn))
    raise ValueError(f"unknown backend {backend!r}")


def evaluate(
    dataset: dict[str, Any],
    *,
    backend: str = "memory",
    provider: ModelProvider | None = None,
    dsn: str | None = None,
    dataset_name: str = "",
) -> Report:
    tenant_id = f"eval-{uuid4().hex[:12]}"
    app = build_app(backend, tenant_id, provider or ExtractiveProvider(), dsn)
    principal = Principal(tenant_id, "api")
    try:
        for document in dataset["documents"]:
            _post(app, "/v1/documents", {"filename": document["source"], **document})
        return _score(app, principal, dataset, backend, dataset_name)
    finally:
        if app.persistence is not None:
            for row in app.persistence.list_documents(tenant_id):
                app.persistence.delete_document(tenant_id, row["document_id"])
        app.worker.stop()


def _score(
    app: RagApiApplication,
    principal: Principal,
    dataset: dict[str, Any],
    backend: str,
    dataset_name: str,
) -> Report:
    recall = {1: 0, 3: 0, 5: 0}
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    paraphrase_hits: list[int] = []
    answered = abstained = 0
    precisions: list[float] = []
    term_recalls: list[float] = []
    top_answerable: list[float] = []
    top_unanswerable: list[float] = []
    latencies: list[float] = []
    queries = dataset["queries"]
    answerable = [q for q in queries if q["relevant_sources"]]
    unanswerable = [q for q in queries if not q["relevant_sources"]]

    for item in queries:
        expected = set(item["relevant_sources"])
        # Ungated ranking for retrieval metrics and threshold calibration.
        ranked = app.retrieve(principal, item["question"], gated=False)
        top_score = ranked[0][1] if ranked else 0.0
        started = time.perf_counter()
        body = _post(app, "/v1/query", {"question": item["question"]})
        latencies.append((time.perf_counter() - started) * 1000)

        if not expected:
            top_unanswerable.append(top_score)
            abstained += not body["grounded"]
            continue
        top_answerable.append(top_score)
        sources: list[str] = []
        for chunk, _score in ranked:
            if chunk.source not in sources:
                sources.append(chunk.source)
        relevance = [int(source in expected) for source in sources]
        for cutoff in recall:
            recall[cutoff] += any(relevance[:cutoff])
        first = next((rank for rank, hit in enumerate(relevance, 1) if hit), None)
        reciprocal_ranks.append(1 / first if first else 0.0)
        ndcgs.append(ndcg(relevance, 5, len(expected)))
        if "paraphrase" in item.get("tags", []):
            paraphrase_hits.append(int(any(relevance[:5])))
        if body["grounded"]:
            answered += 1
            cited = [citation["source"] for citation in body["citations"]]
            precisions.append(sum(source in expected for source in cited) / len(cited))
        terms = item.get("expected_terms", [])
        if terms:
            answer = body["answer"].lower()
            term_recalls.append(sum(term.lower() in answer for term in terms) / len(terms))

    count = max(len(answerable), 1)
    return Report(
        backend=backend,
        dataset=dataset_name or dataset.get("version", ""),
        queries=len(queries),
        answerable=len(answerable),
        unanswerable=len(unanswerable),
        recall_at_1=recall[1] / count,
        recall_at_3=recall[3] / count,
        recall_at_5=recall[5] / count,
        mrr=sum(reciprocal_ranks) / count,
        ndcg_at_5=sum(ndcgs) / count,
        paraphrase_recall_at_5=statistics.fmean(paraphrase_hits) if paraphrase_hits else 0.0,
        answer_rate=answered / count,
        abstention_accuracy=abstained / max(len(unanswerable), 1),
        citation_precision=statistics.fmean(precisions) if precisions else 0.0,
        expected_term_recall=statistics.fmean(term_recalls) if term_recalls else 0.0,
        min_score=app.min_score,
        suggested_min_score=best_threshold(top_answerable, top_unanswerable),
        latency_p50_ms=percentile(latencies, 0.5),
        latency_p95_ms=percentile(latencies, 0.95),
    )


def check(report: Report, thresholds: dict[str, Any]) -> list[str]:
    """Return human-readable regressions against the backend's thresholds."""
    limits = thresholds.get(report.backend, {})
    values = report.as_dict()
    failures = []
    for metric, minimum in limits.get("min", {}).items():
        if values[metric] < minimum:
            failures.append(f"{metric} = {values[metric]} is below the minimum {minimum}")
    for metric, maximum in limits.get("max", {}).items():
        if values[metric] > maximum:
            failures.append(f"{metric} = {values[metric]} is above the maximum {maximum}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the RAG pipeline.")
    parser.add_argument("--backend", choices=["memory", "postgres"], default="memory")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument(
        "--provider",
        choices=["extractive", "configured"],
        default="extractive",
        help="'configured' uses RAG_MODEL_* settings (costs tokens)",
    )
    parser.add_argument("--check", action="store_true", help="exit 1 on regression")
    args = parser.parse_args(argv)
    provider = configured_provider() if args.provider == "configured" else ExtractiveProvider()
    report = evaluate(
        load_dataset(args.dataset),
        backend=args.backend,
        provider=provider,
        dsn=os.getenv("RAG_EVAL_DSN"),
        dataset_name=Path(args.dataset).name,
    )
    print(json.dumps(report.as_dict(), indent=2))
    if args.check:
        failures = check(report, json.loads(THRESHOLDS.read_text(encoding="utf-8")))
        for failure in failures:
            print(f"REGRESSION: {failure}", file=sys.stderr)
        return 1 if failures else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
