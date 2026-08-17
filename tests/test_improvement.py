from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from zen_agent.improvement import (
    GovernanceError,
    ImprovementStore,
    PromotionBlockedError,
    PromotionPolicy,
    aggregate_gap_clusters,
)


class ImprovementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = ImprovementStore(Path(self.temporary.name) / "improvement.db")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def proposal(self, *, training_ids=("train-1",)) -> str:
        return self.store.create_proposal(
            scope="prompt", component="golden-refiner",
            baseline_version="v1", candidate_version="v2",
            change={"artifact_sha256": "a" * 64}, gap_ids=("gap-1",),
            training_ids=training_ids, created_by="improvement-planner",
            idempotency_key="proposal-key",
        )

    @staticmethod
    def results(ids, *, baseline_passes=2, critical_regression=False):
        values = []
        for index, item_id in enumerate(ids):
            baseline = index < baseline_passes
            candidate = True
            if critical_regression and index == 0:
                candidate = False
            values.append({
                "id": item_id, "baseline_pass": baseline,
                "candidate_pass": candidate,
                "critical": critical_regression and index == 0,
                "baseline_covered": True, "candidate_covered": True,
                "user_turn_integrity": True,
            })
        return values

    def evaluate(self, proposal_id, ids, **changes):
        return self.store.record_evaluation(
            proposal_id, held_out_ids=ids,
            results=self.results(ids, **changes), evaluator_id="independent-evaluator",
            independent_evaluator_approved=True, idempotency_key="evaluation-key",
        )

    def test_gap_clustering_is_deterministic_and_filters_passes(self):
        failures = [{
            "conversation_id": "c1", "turn_id": "a1", "axis_id": "language",
            "subaxis_id": "fluency", "variant_id": "code_mix",
            "defect_code": "wrong_register", "severity": "critical",
        }]
        citations = [
            {"conversation_id": "c2", "turn_id": "a2", "axis_id": "language",
             "subaxis_id": "fluency", "variant_id": "code_mix",
             "defect_code": "wrong_register", "golden_verdict": "FAIL"},
            {"conversation_id": "c3", "golden_verdict": "PASS"},
        ]
        feedback = [{
            "conversation_id": "c2", "turn_id": "a2", "axis_id": "language",
            "subaxis_id": "fluency", "variant_id": "code_mix",
            "defect_code": "wrong_register", "decision": "REQUEST_REPAIR",
        }]
        first = aggregate_gap_clusters(failures, citations, feedback)
        second = aggregate_gap_clusters(reversed(failures), reversed(citations), reversed(feedback))
        self.assertEqual(first, second)
        self.assertEqual(first[0]["evidence_count"], 3)
        self.assertEqual(first[0]["affected_conversation_ids"], ["c1", "c2"])
        self.assertEqual(first[0]["critical_count"], 1)

    def test_candidate_never_silently_promotes(self):
        proposal_id = self.proposal()
        self.assertEqual(self.store.proposal_status(proposal_id), "CANDIDATE")
        self.assertEqual(self.store.status(), {"CANDIDATE": 1})
        self.assertFalse(any("promoted" in event["event_type"] for event in self.store.events()))

    def test_held_out_overlap_with_training_is_rejected(self):
        proposal_id = self.proposal(training_ids=("shared", "train-2"))
        with self.assertRaisesRegex(GovernanceError, "overlap"):
            self.store.record_evaluation(
                proposal_id, held_out_ids=("shared", "test-1"),
                results=self.results(("shared", "test-1")),
                evaluator_id="independent-evaluator",
                independent_evaluator_approved=True,
                idempotency_key="overlap-eval",
            )

    def test_held_out_results_must_exactly_match_benchmark_ids(self):
        proposal_id = self.proposal()
        with self.assertRaisesRegex(GovernanceError, "exactly match"):
            self.store.record_evaluation(
                proposal_id, held_out_ids=("held-1", "held-2"),
                results=self.results(("held-1",)), evaluator_id="independent-evaluator",
                independent_evaluator_approved=True, idempotency_key="bad-eval",
            )

    def test_critical_regression_blocks_promotion_even_with_human_approval(self):
        proposal_id = self.proposal()
        ids = ("held-1", "held-2", "held-3")
        self.evaluate(proposal_id, ids, baseline_passes=2, critical_regression=True)
        self.store.approve(
            proposal_id, approver_id="human-owner", decision="APPROVE",
            reason="candidate reviewed", idempotency_key="approval-key",
        )
        with self.assertRaisesRegex(PromotionBlockedError, "critical regressions"):
            self.store.promote(
                proposal_id,
                policy=PromotionPolicy(minimum_sample_size=3, minimum_absolute_improvement=0),
                idempotency_key="promotion-key",
            )

    def test_successful_approved_promotion_remains_not_activated(self):
        proposal_id = self.proposal()
        ids = ("held-1", "held-2", "held-3", "held-4")
        self.evaluate(proposal_id, ids, baseline_passes=2)
        self.store.approve(
            proposal_id, approver_id="human-owner", decision="APPROVE",
            reason="held-out evidence accepted", idempotency_key="approval-key",
        )
        promotion_id = self.store.promote(
            proposal_id,
            policy=PromotionPolicy(minimum_sample_size=4, minimum_absolute_improvement=0.25),
            idempotency_key="promotion-key",
        )
        self.assertTrue(promotion_id.startswith("promotion-"))
        self.assertEqual(self.store.proposal_status(proposal_id), "PROMOTED_NOT_ACTIVATED")
        row = self.store.db.execute(
            "SELECT activated FROM improvement_promotions WHERE id=?", (promotion_id,)
        ).fetchone()
        self.assertEqual(row["activated"], 0)

    def test_promotion_requires_explicit_human_approval(self):
        proposal_id = self.proposal()
        ids = ("held-1", "held-2", "held-3")
        self.evaluate(proposal_id, ids, baseline_passes=1)
        with self.assertRaisesRegex(PromotionBlockedError, "human approval"):
            self.store.promote(
                proposal_id,
                policy=PromotionPolicy(minimum_sample_size=3, minimum_absolute_improvement=0.1),
                idempotency_key="promotion-key",
            )

    def test_mutations_are_idempotent_but_key_reuse_is_guarded(self):
        proposal_id = self.proposal()
        duplicate = self.store.create_proposal(
            scope="prompt", component="golden-refiner",
            baseline_version="v1", candidate_version="v2",
            change={"artifact_sha256": "a" * 64}, gap_ids=("gap-1",),
            training_ids=("train-1",), created_by="improvement-planner",
            idempotency_key="proposal-key",
        )
        self.assertEqual(proposal_id, duplicate)
        with self.assertRaisesRegex(GovernanceError, "different request"):
            self.store.create_proposal(
                scope="prompt", component="golden-refiner",
                baseline_version="v1", candidate_version="v3",
                change={"artifact_sha256": "b" * 64}, gap_ids=("gap-2",),
                created_by="improvement-planner", idempotency_key="proposal-key",
            )


if __name__ == "__main__":
    unittest.main()
