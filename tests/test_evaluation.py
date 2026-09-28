from __future__ import annotations

import json
import os

import pytest

from evals.evaluate import (
    DEFAULT_DATASET,
    THRESHOLDS,
    best_threshold,
    check,
    evaluate,
    load_dataset,
    ndcg,
)


def test_memory_backend_meets_its_regression_thresholds() -> None:
    report = evaluate(load_dataset(DEFAULT_DATASET))
    assert report.queries == 50 and report.unanswerable == 10
    assert check(report, json.loads(THRESHOLDS.read_text())) == []


@pytest.mark.skipif(not os.getenv("RAG_EVAL_DSN"), reason="set RAG_EVAL_DSN")
def test_postgres_backend_meets_its_regression_thresholds() -> None:
    report = evaluate(
        load_dataset(DEFAULT_DATASET), backend="postgres", dsn=os.environ["RAG_EVAL_DSN"]
    )
    assert check(report, json.loads(THRESHOLDS.read_text())) == []


def test_metric_helpers() -> None:
    assert ndcg([1, 0, 0], 5, 1) == 1.0
    assert ndcg([0, 1], 5, 1) == pytest.approx(0.6309, abs=1e-4)
    assert ndcg([0, 0], 5, 1) == 0.0
    assert best_threshold([0.6, 0.7], [0.2, 0.3]) == 0.6


def test_check_reports_regressions() -> None:
    report = evaluate(load_dataset(DEFAULT_DATASET))
    strict = {"memory": {"min": {"recall_at_5": 1.01}, "max": {"latency_p95_ms": -1}}}
    assert len(check(report, strict)) == 2
