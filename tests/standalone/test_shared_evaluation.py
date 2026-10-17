"""Metric correctness for the reproducible shared retrieval evaluation."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

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


def test_service_early_exit_reports_diagnostics(tmp_path):
    """Early subprocess termination surfaces diagnostics instead of decode errors."""
    spec = importlib.util.spec_from_file_location("shared_evaluation", PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    orig_popen = subprocess.Popen

    def bad_popen(*args, **kwargs):
        diag = kwargs.get("stderr")
        if diag:
            diag.write("simulated startup crash")
            diag.flush()
        return orig_popen(
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    with patch("subprocess.Popen", side_effect=bad_popen):
        with pytest.raises(RuntimeError, match="simulated startup crash"):
            with module.service(tmp_path, tmp_path / "policy.json"):
                pass
