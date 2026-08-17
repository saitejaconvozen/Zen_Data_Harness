from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from zen_agent.qa_audit import (
    AuditLedger,
    audit_conversation,
    select_sample,
)


ROOT = Path(__file__).resolve().parents[1]


def conversation(**turns_override):
    turns = [
        {"turn_id": "turn_0001", "role": "user", "text": "customerName: Randhir Marwa"},
        {"turn_id": "turn_0002", "role": "assistant", "action": "REPLACE",
         "source_text": "The speakers are Bhuvan and Sandeep. Shall I share more?",
         "golden_text": "Hmm, I didn't quite catch that. Could you repeat?",
         "source_quality": "MAJOR_GAP", "downstream_coherence": "PRESERVED"},
        {"turn_id": "turn_0003", "role": "user", "text": "[voice] yeah is there parking"},
    ]
    base = {"source_id": "abc123", "number": 1,
            "terminal": {"status": "VERIFIED_CANDIDATE"}, "turns": turns}
    base.update(turns_override)
    return base


class AuditChecksTests(unittest.TestCase):
    def kinds(self, conv, prompt=""):
        return {f.kind for f in audit_conversation(conv, prompt).findings}

    def test_false_preserved_is_caught(self) -> None:
        """The real #426 failure: an answer became a request to repeat."""
        kinds = self.kinds(conversation())
        self.assertIn("false-preserved", kinds)
        self.assertIn("answer-to-clarification", kinds)

    def test_replacement_without_a_defect_is_caught(self) -> None:
        c = conversation()
        c["turns"][1].update(source_quality="MINOR_GAP", golden_text="Reworded nicely.",
                             downstream_coherence="DIVERGENT")
        self.assertIn("replaced-without-defect", self.kinds(c))

    def test_kept_turn_must_be_byte_identical(self) -> None:
        c = conversation()
        c["turns"][1].update(action="KEEP", golden_text="something else",
                             source_quality="PERFECT")
        self.assertIn("keep-not-identical", self.kinds(c))

    def test_names_from_the_call_are_not_fabrication(self) -> None:
        # "Randhir" appears in the session metadata on turn_0001.
        c = conversation()
        c["turns"][1].update(golden_text="Namaste Randhir, when can you pay?",
                             downstream_coherence="DIVERGENT")
        self.assertNotIn("possible-fabrication", self.kinds(c))

    def test_genuinely_new_specifics_are_flagged(self) -> None:
        c = conversation()
        c["turns"][1].update(
            golden_text="Your EMI of 12500 rupees is due at the Ritz Carlton branch.",
            downstream_coherence="DIVERGENT")
        self.assertIn("possible-fabrication", self.kinds(c))

    def test_sentence_initial_capitals_are_not_entities(self) -> None:
        c = conversation()
        c["turns"][1].update(golden_text="Sure. Would you confirm? Thanks.",
                             downstream_coherence="DIVERGENT")
        self.assertNotIn("possible-fabrication", self.kinds(c))

    def test_a_clean_conversation_produces_nothing(self) -> None:
        c = conversation()
        c["turns"][1].update(action="KEEP", source_quality="PERFECT",
                             golden_text=c["turns"][1]["source_text"])
        self.assertEqual(self.kinds(c), set())

    def test_modified_user_turn_is_critical(self) -> None:
        c = conversation()
        c["turns"][2]["source_preserved"] = False
        findings = audit_conversation(c).findings
        self.assertTrue(any(f.severity == "CRITICAL" for f in findings))


class SamplingTests(unittest.TestCase):
    def candidates(self, n):
        return [{"source_id": f"id{i:04d}", "number": i} for i in range(n)]

    def test_no_sample_until_a_full_batch_exists(self) -> None:
        self.assertEqual(select_sample(self.candidates(49), set(), "r", 50, 0.2), [])

    def test_twenty_percent_of_fifty_is_ten(self) -> None:
        self.assertEqual(len(select_sample(self.candidates(50), set(), "r", 50, 0.2)), 10)

    def test_sampling_is_deterministic(self) -> None:
        a = select_sample(self.candidates(50), set(), "r", 50, 0.2)
        b = select_sample(self.candidates(50), set(), "r", 50, 0.2)
        self.assertEqual([x["source_id"] for x in a], [x["source_id"] for x in b])

    def test_already_audited_are_excluded(self) -> None:
        done = {f"id{i:04d}" for i in range(30)}
        # 70 remain, so one further batch of 50 is available.
        self.assertEqual(len(select_sample(self.candidates(100), done, "r", 50, 0.2)), 10)

    def test_sample_comes_from_the_batch(self) -> None:
        batch = {c["source_id"] for c in self.candidates(50)}
        picked = select_sample(self.candidates(200), set(), "r", 50, 0.2)
        self.assertTrue({c["source_id"] for c in picked} <= batch)


class LedgerTests(unittest.TestCase):
    def test_batches_advance_and_history_records(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            ledger = AuditLedger(Path(directory) / "qa.db")
            try:
                self.assertEqual(ledger.next_batch("r"), 1)
                audits = [audit_conversation(conversation())]
                ledger.record("r", 1, audits)
                self.assertEqual(ledger.audited("r"), {"abc123"})
                self.assertEqual(ledger.next_batch("r"), 2)
                history = ledger.history("r")
                self.assertEqual(history[0]["sampled"], 1)
                self.assertEqual(history[0]["flagged"], 1)
            finally:
                ledger.close()


if __name__ == "__main__":
    unittest.main()
