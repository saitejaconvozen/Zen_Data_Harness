from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import sys
import unittest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "plugins" / "golden-conversations" / "scripts")
)

import run_refiner  # noqa: E402


SYSTEM_PROMPT = "Always begin with `<|ENGLISH|>` or `<|HINDI|>`."


def _packet():
    texts = [
        ("turn_0001", "user", "Hi"),
        ("turn_0002", "assistant", "Hello, how can I help?"),
        ("turn_0003", "user", "About my order"),
        ("turn_0004", "assistant", "Let me check that."),
    ]
    return {
        "packet_id": "rp_" + "a" * 64,
        "system_prompt": SYSTEM_PROMPT,
        "turns": [
            {"turn_id": t, "role": r, "text": x,
             "text_sha256": sha256(x.encode()).hexdigest()}
            for t, r, x in texts
        ],
    }


def _registry():
    return {
        "axes": [
            {
                "id": "AX001", "enabled": True,
                "subaxes": [
                    {
                        "id": "AX001-SA001", "enabled": True,
                        "variants": [{"id": "AX001-SA001-V001", "enabled": True}],
                    }
                ],
            }
        ]
    }


def _annotation():
    return {
        "axis_id": "AX001",
        "subaxis_id": "AX001-SA001",
        "variant_id": "AX001-SA001-V001",
    }


def _decision(rows):
    packet = _packet()
    return {
        "schema_version": "zen.review-decision/1",
        "decision_id": "rd_" + "b" * 64,
        "packet_id": packet["packet_id"],
        "assignment_id": run_refiner.assignment_id(packet["packet_id"]),
        "worker": {"role": "REFINER", "model_id": run_refiner.MODEL},
        "decision": {"assistant_turns": rows},
    }


def _row(turn_id, **overrides):
    row = {
        "turn_id": turn_id,
        "action": "KEEP",
        "golden_text": {"turn_0002": "Hello, how can I help?",
                        "turn_0004": "Let me check that."}[turn_id],
        "semantic_delta": "NONE",
        "source_quality": "PERFECT",
        # turn_0004 is last, so no user turn follows it.
        "downstream_coherence": "PRESERVED" if turn_id == "turn_0002" else "TERMINAL_TURN",
        "divergence_reason": None,
        "correction_reason": "",
        "annotations": [],
        "evidence_status": "SUFFICIENT",
        "response_language": "ENGLISH",
    }
    row.update(overrides)
    return row


class ValidationTests(unittest.TestCase):
    def validate(self, rows):
        run_refiner.validate_decision(_decision(rows), _packet(), _registry())

    def test_perfect_kept_turns_need_no_annotation(self) -> None:
        """The old schema forced a finding on every turn, driving over-replacement."""
        self.validate([_row("turn_0002"), _row("turn_0004")])

    def test_an_unjustified_replacement_is_reverted_not_rejected(self) -> None:
        """No annotation means no defect was shown, so the source stands."""
        rows = [
            _row("turn_0002", action="REPLACE", golden_text="Hi there, how can I help?",
                 semantic_delta="STYLE_ONLY", source_quality="MAJOR_GAP"),
            _row("turn_0004"),
        ]
        self.validate(rows)
        self.assertEqual(rows[0]["action"], "KEEP")
        self.assertEqual(rows[0]["golden_text"], "Hello, how can I help?")
        self.assertTrue(rows[0]["unjustified_replacement_reverted_by_harness"])

        rows[0].update(action="REPLACE", golden_text="Hi there, how can I help?",
                       source_quality="MAJOR_GAP", annotations=[_annotation()])
        self.validate(rows)
        self.assertEqual(rows[0]["action"], "REPLACE")

    def test_stylistic_gaps_are_kept_not_rewritten(self) -> None:
        """MINOR_GAP is a preference, not a defect; rewriting it changes content."""
        self.validate([_row("turn_0002", source_quality="MINOR_GAP"), _row("turn_0004")])

    def test_a_kept_defect_excludes_the_turn_not_the_conversation(self) -> None:
        """The model found no grounded correction. Keeping the source is right;
        shipping it as exemplary data is not, so the turn is excluded."""
        for quality in ("MAJOR_GAP", "CRITICAL_GAP"):
            rows = [_row("turn_0002", source_quality=quality), _row("turn_0004")]
            self.validate(rows)
            self.assertEqual(rows[0]["evidence_status"], "INSUFFICIENT")
            self.assertTrue(rows[0]["kept_defect_excluded_by_harness"])
            self.assertEqual(rows[0]["golden_text"], "Hello, how can I help?")

    def test_a_stylistic_rewrite_is_reverted_to_the_source(self) -> None:
        rows = [
            _row("turn_0002", action="REPLACE", golden_text="Rephrased for style",
                 semantic_delta="STYLE_ONLY", source_quality="MINOR_GAP",
                 annotations=[_annotation()]),
            _row("turn_0004"),
        ]
        self.validate(rows)
        self.assertEqual(rows[0]["action"], "KEEP")
        self.assertEqual(rows[0]["golden_text"], "Hello, how can I help?")
        self.assertTrue(rows[0]["unjustified_replacement_reverted_by_harness"])

    def test_replacing_a_turn_graded_perfect_is_reverted(self) -> None:
        rows = [
            _row("turn_0002", action="REPLACE", golden_text="Different text",
                 semantic_delta="STYLE_ONLY", source_quality="PERFECT",
                 annotations=[_annotation()]),
            _row("turn_0004"),
        ]
        self.validate(rows)
        self.assertEqual(rows[0]["action"], "KEEP")
        self.assertEqual(rows[0]["golden_text"], "Hello, how can I help?")

    def test_terminal_turn_mislabels_are_corrected_not_rejected(self) -> None:
        """Whether a user turn follows is a fact the harness already knows.

        Rejecting the decision threw away every other turn in the conversation;
        the label is simply set to the truth instead.
        """
        # turn_0004 has no user turn after it, so PRESERVED is wrong.
        rows = [_row("turn_0002"), _row("turn_0004", downstream_coherence="PRESERVED")]
        self.validate(rows)
        self.assertEqual(rows[1]["downstream_coherence"], "TERMINAL_TURN")
        self.assertTrue(rows[1]["coherence_corrected_by_harness"])

        # turn_0002 is followed by a user turn, so TERMINAL_TURN is wrong.
        rows = [_row("turn_0002", downstream_coherence="TERMINAL_TURN"), _row("turn_0004")]
        self.validate(rows)
        self.assertEqual(rows[0]["downstream_coherence"], "PRESERVED")
        self.assertTrue(rows[0]["coherence_corrected_by_harness"])

    def test_a_missing_divergence_reason_is_supplied_not_rejected(self) -> None:
        rows = [
            _row("turn_0002", action="REPLACE", golden_text="What is your order number?",
                 semantic_delta="DIALOGUE_ACT", source_quality="MAJOR_GAP",
                 downstream_coherence="DIVERGENT", annotations=[_annotation()]),
            _row("turn_0004"),
        ]
        self.validate(rows)
        self.assertTrue(rows[0]["divergence_reason_supplied_by_harness"])
        self.assertIn("human review", rows[0]["divergence_reason"])

        rows[0]["divergence_reason"] = "asks for the order number the user never gave"
        self.validate(rows)


class LanguageTagTests(unittest.TestCase):
    def test_tag_is_applied_without_mutating_model_output(self) -> None:
        rows = [_row("turn_0002"), _row("turn_0004")]
        run_refiner.apply_language_tags(rows, _packet())
        # The KEEP byte-identity invariant survives.
        self.assertEqual(rows[0]["golden_text"], "Hello, how can I help?")
        self.assertEqual(rows[0]["golden_text_final"], "<|ENGLISH|> Hello, how can I help?")
        self.assertEqual(rows[0]["language_tag_applied"], "ENGLISH")

    def test_existing_tag_is_left_alone(self) -> None:
        rows = [_row("turn_0002", golden_text="<|HINDI|> नमस्ते")]
        run_refiner.apply_language_tags(rows, _packet())
        self.assertEqual(rows[0]["golden_text_final"], "<|HINDI|> नमस्ते")
        self.assertNotIn("language_tag_applied", rows[0])

    def test_declared_language_beats_the_script_heuristic(self) -> None:
        """Romanised Hindi is Latin script; only the model's declaration is reliable."""
        rows = [_row("turn_0002", action="REPLACE", source_quality="MAJOR_GAP",
                     golden_text="Kya aap abhi baat kar sakte hain?",
                     response_language="HINDI")]
        run_refiner.apply_language_tags(rows, _packet())
        self.assertEqual(rows[0]["language_tag_applied"], "HINDI")
        self.assertTrue(rows[0]["golden_text_final"].startswith("<|HINDI|>"))

    def test_undeclared_language_falls_back_to_detection(self) -> None:
        rows = [_row("turn_0002", action="REPLACE", source_quality="MAJOR_GAP",
                     golden_text="Aapka order kal tak aa jayega",
                     response_language="KLINGON")]
        run_refiner.apply_language_tags(rows, _packet())
        self.assertEqual(rows[0]["language_tag_applied"], "HINDI")

    def test_prompt_without_tag_rule_applies_nothing(self) -> None:
        packet = _packet()
        packet["system_prompt"] = "Be concise."
        rows = [_row("turn_0002")]
        run_refiner.apply_language_tags(rows, packet)
        self.assertEqual(rows[0]["golden_text_final"], "Hello, how can I help?")


if __name__ == "__main__":
    unittest.main()
