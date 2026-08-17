from __future__ import annotations

import unittest

from zen_agent.turn_format import (
    analyze_conversation,
    analyze_turn,
    declared_language_tags,
    detect_script_language,
    requires_language_tag,
    summarize,
)


PROMPT = "Greet with `<|ENGLISH|>` Hello. If the caller speaks Hindi use `<|HINDI|>`."


class DeclaredTagTests(unittest.TestCase):
    def test_extracts_only_concrete_language_tags(self) -> None:
        prompt = PROMPT + " Emit <|IDENTIFIED_INPUT_LANGUAGE|> as the slot name."
        self.assertEqual(declared_language_tags(prompt), frozenset({"ENGLISH", "HINDI"}))

    def test_prompt_without_tags_requires_none(self) -> None:
        self.assertFalse(requires_language_tag("Be polite and concise."))
        self.assertEqual(declared_language_tags("Be polite."), frozenset())


class ScriptDetectionTests(unittest.TestCase):
    def test_detects_devanagari_as_hindi(self) -> None:
        self.assertEqual(detect_script_language("नमस्ते आप कैसे हैं"), "HINDI")

    def test_detects_latin_as_english(self) -> None:
        self.assertEqual(detect_script_language("Hello, is this a good time?"), "ENGLISH")

    def test_returns_none_without_letters(self) -> None:
        self.assertIsNone(detect_script_language("12345 !!! ???"))


class RomanisedHindiTests(unittest.TestCase):
    """Latin-script Hindi read as English, mis-tagged the turn, and failed verification."""

    def test_romanised_hindi_is_hindi(self) -> None:
        for text in ("Kya aap abhi baat kar sakte hain?",
                     "Ji haan, main aapko batata hoon",
                     "Aapki SIP ko dobara shuru karne ke liye"):
            self.assertEqual(detect_script_language(text), "HINDI", text)

    def test_english_with_hindi_homographs_stays_english(self) -> None:
        for text in ("Thank you for your patience, the main order is confirmed",
                     "I will send you the details today",
                     "Sure, I can help with that"):
            self.assertEqual(detect_script_language(text), "ENGLISH", text)

    def test_a_single_marker_is_not_enough(self) -> None:
        self.assertEqual(detect_script_language("Your order is on the way bhi"), "ENGLISH")


class AnalyzeTurnTests(unittest.TestCase):
    def setUp(self) -> None:
        self.declared = declared_language_tags(PROMPT)

    def test_compliant_turn_is_untouched(self) -> None:
        finding = analyze_turn(
            "turn_0001", "<|ENGLISH|> Hello there", self.declared, required=True
        )
        self.assertTrue(finding.compliant)
        self.assertEqual(finding.observed_tag, "ENGLISH")
        self.assertIsNone(finding.proposed_text)

    def test_missing_tag_is_repaired_from_script(self) -> None:
        finding = analyze_turn(
            "turn_0002", "नमस्ते बताइए", self.declared, required=True
        )
        self.assertFalse(finding.compliant)
        self.assertEqual(finding.proposed_tag, "HINDI")
        self.assertEqual(finding.proposed_text, "<|HINDI|> नमस्ते बताइए")

    def test_undeclared_tag_is_flagged_not_repaired(self) -> None:
        finding = analyze_turn(
            "turn_0003", "<|FRENCH|> Bonjour", self.declared, required=True
        )
        self.assertFalse(finding.compliant)
        self.assertEqual(finding.observed_tag, "FRENCH")
        self.assertIsNone(finding.proposed_text)

    def test_unresolved_placeholder_is_flagged(self) -> None:
        finding = analyze_turn(
            "turn_0004", "<|LANGUAGE_TAG|> Hello", self.declared, required=True
        )
        self.assertFalse(finding.compliant)
        self.assertIn("placeholder", finding.issues[0])

    def test_prompt_without_tag_rule_never_flags(self) -> None:
        finding = analyze_turn("turn_0005", "Hello", frozenset(), required=False)
        self.assertTrue(finding.compliant)


class ConversationTests(unittest.TestCase):
    def test_only_assistant_turns_are_analyzed_and_summarized(self) -> None:
        turns = [
            {"turn_id": "turn_0001", "role": "user", "text": "Hi"},
            {"turn_id": "turn_0002", "role": "assistant", "text": "<|ENGLISH|> Hello"},
            {"turn_id": "turn_0003", "role": "assistant", "text": "How can I help?"},
        ]
        findings = analyze_conversation(PROMPT, turns)
        self.assertEqual([f.turn_id for f in findings], ["turn_0002", "turn_0003"])
        self.assertEqual(
            summarize(findings),
            {
                "assistant_turns": 2,
                "compliant": 1,
                "non_compliant": 1,
                "auto_repairable": 1,
            },
        )


if __name__ == "__main__":
    unittest.main()
