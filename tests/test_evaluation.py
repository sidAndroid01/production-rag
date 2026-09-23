from evals.evaluate import evaluate, load_dataset


def test_golden_dataset_reports_retrieval_metrics() -> None:
    metrics = evaluate(load_dataset("evals/datasets/golden-v1.json"))
    assert metrics.queries == 4
    assert metrics.recall_at_5 == 1.0
    assert metrics.mrr == 1.0
    assert metrics.ndcg_at_5 == 1.0
