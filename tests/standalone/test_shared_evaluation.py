"""Metric correctness for the reproducible shared retrieval evaluation."""

import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / "scripts/evaluate_shared.py"


def test_binary_retrieval_metrics_rank_and_misses():
    """Rank-two relevance penalizes MRR/nDCG; missing labels penalize recall."""
    spec = importlib.util.spec_from_file_location("shared_evaluation", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    scores = module.quality([{"id": "irrelevant"}, {"id": "a"}], {"a", "missing"})
    assert scores["recall_at_5"] == 0.5
    assert scores["precision_at_5"] == 0.2
    assert scores["mrr"] == 0.5
    assert 0 < scores["ndcg_at_5"] < 0.5
    perfect = module.quality([{"id": "a"}, {"id": "b"}], {"a", "b"})
    assert perfect["ndcg_at_5"] == pytest.approx(1)
    assert perfect["recall_at_5"] == perfect["mrr"] == 1
