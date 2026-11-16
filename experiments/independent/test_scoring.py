"""Adversarial scoring cases and gates; no Qdrant/model required."""

import copy
import unittest

from dataset import inventory, load_dataset
from decision import decide
from run import verify_freeze
from scoring import paired_interval, score
from server import scope_filter


class ScoringTests(unittest.TestCase):
    """Exercise adversarial rankings and evidence gates."""

    def test_missing_current_with_stale_is_failure(self):
        """Verify missing current with stale is failure."""
        q = dict(correct_ids=["current"], conflict_ids=["old"], related_ids=["context"])
        self.assertEqual(score(q, ["old", "context"])["stale_before"], 1)
        self.assertEqual(score(q, ["current", "old"])["stale_before"], 0)
        self.assertEqual(score(q, ["old", "current"])["stale_before"], 1)
        self.assertEqual(score(q, ["other"])["stale_before"], 0)
        self.assertEqual(score(q, ["other"])["current_present"], 0)

    def test_historical_labels_reverse_current_and_old(self):
        """Verify historical labels reverse current and old."""
        q = dict(correct_ids=["old"], conflict_ids=["current"], related_ids=[])
        self.assertEqual(score(q, ["old", "current"])["historical_accuracy"], 1)
        self.assertEqual(score(q, ["current", "old"])["historical_accuracy"], 0)

    def test_multi_evidence_and_auxiliary_not_answer(self):
        """Verify multi evidence and auxiliary not answer."""
        q = dict(correct_ids=["a", "b"], conflict_ids=[], related_ids=["context"])
        s = score(q, ["context", "a"])
        self.assertEqual(s["recall"], 0.5)
        self.assertAlmostEqual(
            s["ndcg"], (1 / __import__("math").log2(3)) / (1 + 1 / __import__("math").log2(3))
        )
        self.assertEqual(s["related_count"], 1)

    def test_no_answer_is_retained_without_fake_recall(self):
        """Verify no answer is retained without fake recall."""
        s = score(dict(correct_ids=[], conflict_ids=["distractor"], related_ids=[]), ["distractor"])
        self.assertIsNone(s["recall"])
        self.assertIsNone(s["ndcg"])
        self.assertEqual(s["interference"], 1)
        self.assertEqual(s["returned_count"], 1)

    def test_cluster_interval_keeps_all_questions(self):
        """Verify cluster interval keeps all questions."""
        rows = [
            dict(fact_group_id="a", dense={"recall": 0}, hybrid={"recall": 1}),
            dict(fact_group_id="a", dense={"recall": 1}, hybrid={"recall": 0}),
            dict(fact_group_id="b", dense={"recall": 0}, hybrid={"recall": 0}),
        ]
        r = paired_interval(rows, "recall", 100)
        self.assertEqual(r["ci95"], [0, 0])
        self.assertEqual(r["fact_groups"], 2)

    def test_data_freeze_and_inventory(self):
        """Verify data freeze and inventory."""
        verify_freeze()
        _, queries = load_dataset()
        inv = inventory(queries)
        self.assertEqual(inv["all"]["total"]["queries"], 132)
        self.assertGreaterEqual(inv["holdout"]["stale_fact"]["queries"], 30)
        self.assertGreaterEqual(inv["holdout"]["mixed"]["queries"], 30)

    def test_null_scope_filter_is_exact(self):
        """Verify null scope filter is exact."""
        self.assertEqual(scope_filter(["u", None])["must"][1], {"is_null": {"key": "agent_id"}})

    def test_safety_dominates_quality(self):
        """Verify safety dominates quality."""
        result = dict(execution_status="SUCCESS", safety_failure=True)
        self.assertEqual(decide(result)["verdict"], "NO-GO")
        self.assertEqual(
            decide(dict(execution_status="ERROR", error="model unavailable"))["verdict"],
            "INCONCLUSIVE",
        )

    def test_point_estimate_cannot_pass_uncertainty(self):
        """Verify point estimate cannot pass uncertainty."""
        paired = dict(delta=0.1, ci95=[-0.1, 0.3], fact_groups=30)
        quality = {
            t: dict(
                queries=30,
                answered_queries=30,
                paired={k: copy.deepcopy(paired) for k in ["recall", "ndcg"]},
            )
            for t in ["mixed", "chinese", "english", "code", "identifiers"]
        }
        result = dict(
            execution_status="SUCCESS",
            split="holdout",
            quality=quality,
            lifecycle={"status": "PASS"},
        )
        self.assertEqual(decide(result)["verdict"], "INCONCLUSIVE")
        result["lifecycle"]["status"] = "FAIL"
        self.assertEqual(decide(result)["verdict"], "NO-GO")


if __name__ == "__main__":
    unittest.main()
