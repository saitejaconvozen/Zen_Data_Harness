from __future__ import annotations

import unittest

from zen_agent.dialogue_act import (
    audit_decision,
    coherence_violation,
    is_clarification_request,
    is_question,
    opens_with_reply_token,
)


class PrimitiveTests(unittest.TestCase):
    def test_markers_and_tags_do_not_hide_the_question(self) -> None:
        self.assertTrue(
            is_question("<|ENGLISH|> PATIENCE 2 Do you have a minute? WAITING 10")
        )
        self.assertFalse(is_question("<|ENGLISH|> PATIENCE 2 Thanks for waiting."))

    def test_clarification_detected_in_both_scripts(self) -> None:
        for text in ("Hmm, I didn't quite catch that. Could you please repeat?",
                     "Sorry, could you say that again?",
                     "मुझे समझ नहीं आया, दोबारा बताइए?"):
            self.assertTrue(is_clarification_request(text), text)
        self.assertFalse(is_clarification_request("What is your order number?"))

    def test_reply_tokens_in_both_scripts(self) -> None:
        for text in ("[voice] yeah is there a pick up", "[voice] ji", "haan bilkul", "no thanks"):
            self.assertTrue(opens_with_reply_token(text), text)
        for text in ("[voice] where is the venue", "payment नहीं करना है"):
            self.assertFalse(opens_with_reply_token(text), text)


class CoherenceViolationTests(unittest.TestCase):
    """Reconstructs the real failure found in conversation #426."""

    def test_answer_replaced_by_clarification_is_flagged(self) -> None:
        reason = coherence_violation(
            "Hmm, the speakers are Bhuvan Dheer and others. Shall I share more details?",
            "Hmm, I didn't quite catch that. Could you please repeat?",
            "[voice] yeah is there a pick up and drop",
        )
        self.assertIsNotNone(reason)

    def test_same_question_reworded_is_not_flagged(self) -> None:
        self.assertIsNone(coherence_violation(
            "Aapki EMI pending hai. Kab tak pay kar sakte ho?",
            "Your EMI has been pending. When can you pay?",
            "[voice] payment नहीं करना है",
        ))

    def test_substantive_next_turn_is_not_our_business(self) -> None:
        # Only a bare acknowledgement proves the reply answered the old act.
        self.assertIsNone(coherence_violation(
            "Shall I share more details?", "Could you repeat?",
            "[voice] where is the venue located",
        ))

    def test_no_following_user_turn_is_safe(self) -> None:
        self.assertIsNone(coherence_violation("Shall I?", "Could you repeat?", None))

    def test_question_dropped_entirely_is_flagged(self) -> None:
        self.assertIsNotNone(coherence_violation(
            "We start at nine thirty. Shall I book you in?",
            "We start at nine thirty.",
            "[voice] yes please",
        ))


class AuditDecisionTests(unittest.TestCase):
    def _packet(self):
        return {"turns": [
            {"turn_id": "turn_0001", "role": "user", "text": "who is coming"},
            {"turn_id": "turn_0002", "role": "assistant",
             "text": "The speakers are Bhuvan and Sandeep. Shall I share more details?"},
            {"turn_0002": None, "turn_id": "turn_0003", "role": "user",
             "text": "[voice] yeah is there a pick up and drop"},
        ]}

    def _row(self, **kw):
        row = {"turn_id": "turn_0002", "action": "REPLACE",
               "golden_text": "Hmm, I didn't quite catch that. Could you please repeat?",
               "downstream_coherence": "PRESERVED"}
        row.update(kw)
        return row

    def test_contradicted_preserved_is_reported(self) -> None:
        found = audit_decision([self._row()], self._packet())
        self.assertEqual([f["turn_id"] for f in found], ["turn_0002"])

    def test_already_divergent_is_left_alone(self) -> None:
        self.assertEqual(
            audit_decision([self._row(downstream_coherence="DIVERGENT")], self._packet()), []
        )

    def test_kept_turns_are_never_flagged(self) -> None:
        self.assertEqual(
            audit_decision([self._row(action="KEEP")], self._packet()), []
        )


if __name__ == "__main__":
    unittest.main()
