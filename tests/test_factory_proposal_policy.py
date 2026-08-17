from __future__ import annotations

import unittest

from zen_agent.factory_worker import FactoryWorker


def proposal(**overrides):
    base = {
        "prompt_usable": True,
        "conversation_assessable": True,
        "quarantine_reasons": [],
        "assistant_turns": 6,
        "divergent_turns": [],
        "unassessable_turns": [],
    }
    base.update(overrides)
    return base


class ProposalPolicyTests(unittest.TestCase):
    """The refiner maximises turn quality; divergence excludes turns, not packets."""

    def classify(self, decision, prop, **kwargs):
        return FactoryWorker._classify_proposal(decision, prop, **kwargs)

    def test_clean_pass_is_a_verified_candidate(self) -> None:
        status, reason = self.classify("PASS", proposal())
        self.assertEqual(status, "VERIFIED_CANDIDATE")
        self.assertIn("initial", reason)

    def test_partial_divergence_is_salvaged_not_quarantined(self) -> None:
        status, reason = self.classify(
            "PASS", proposal(divergent_turns=["turn_0007", "turn_0009"])
        )
        self.assertEqual(status, "PARTIAL_CANDIDATE")
        self.assertIn("2 of 6 turns excluded", reason)

    def test_fully_divergent_conversation_is_quarantined(self) -> None:
        status, _ = self.classify(
            "PASS", proposal(assistant_turns=2, divergent_turns=["a", "b"])
        )
        self.assertEqual(status, "QUARANTINED")

    def test_unusable_prompt_quarantines_regardless_of_verdict(self) -> None:
        status, _ = self.classify("PASS", proposal(prompt_usable=False))
        self.assertEqual(status, "QUARANTINED")

    def test_advisory_quarantine_prose_no_longer_discards_the_packet(self) -> None:
        """Turn-scoped prose used to veto whole conversations. It is advisory now."""
        status, _ = self.classify(
            "PASS", proposal(quarantine_reasons=["no backend result at turn_0012"])
        )
        self.assertEqual(status, "VERIFIED_CANDIDATE")

    def test_turn_scoped_missing_evidence_excludes_only_that_turn(self) -> None:
        status, reason = self.classify(
            "PASS", proposal(unassessable_turns=["turn_0012"])
        )
        self.assertEqual(status, "PARTIAL_CANDIDATE")
        self.assertIn("1 of 6 turns excluded", reason)

    def test_divergent_and_unassessable_turns_both_count(self) -> None:
        status, reason = self.classify(
            "PASS",
            proposal(divergent_turns=["turn_0002"], unassessable_turns=["turn_0004"]),
        )
        self.assertEqual(status, "PARTIAL_CANDIDATE")
        self.assertIn("2 of 6 turns excluded", reason)

    def test_overlapping_exclusions_are_not_double_counted(self) -> None:
        status, reason = self.classify(
            "PASS",
            proposal(divergent_turns=["turn_0002"], unassessable_turns=["turn_0002"]),
        )
        self.assertIn("1 of 6 turns excluded", reason)

    def test_whole_conversation_blocker_quarantines(self) -> None:
        status, _ = self.classify("PASS", proposal(conversation_assessable=False))
        self.assertEqual(status, "QUARANTINED")

    def test_fail_routes_to_repair(self) -> None:
        status, _ = self.classify("FAIL", proposal())
        self.assertEqual(status, "REPAIR")

    def test_fail_routes_to_repair_even_when_divergent(self) -> None:
        # The old gate dropped these; a divergent proposal is still repairable.
        status, _ = self.classify("FAIL", proposal(divergent_turns=["turn_0003"]))
        self.assertEqual(status, "REPAIR")

    def test_abstain_quarantines(self) -> None:
        status, reason = self.classify("ABSTAIN", proposal())
        self.assertEqual(status, "QUARANTINED")
        self.assertIn("ABSTAIN", reason)

    def test_repair_stage_is_named_in_the_reason(self) -> None:
        _, reason = self.classify("PASS", proposal(), repaired=True)
        self.assertIn("repair", reason)


if __name__ == "__main__":
    unittest.main()
