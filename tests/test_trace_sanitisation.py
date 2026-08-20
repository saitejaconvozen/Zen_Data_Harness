"""A conversation is what the two people said, and nothing else.

Recorded transcripts carry platform scaffolding: session metadata, silence
placeholders, speech-to-text diagnostics. Before this ran at the boundary, 53%
of a released corpus contained XML metadata blocks — with real phone numbers —
in the assistant position, and 51% contained the sentence "User didn't respond
to the above message" sitting where caller speech should be. A model fine-tuned
on that learns to emit both.

The original guard only matched a metadata block that was the *entire* turn, so
"[voice] <session_metadata>...</session_metadata> hello?" passed as dialogue.
"""

from __future__ import annotations

import unittest

from pathlib import Path

from zen_agent.adapters.mongodb import bind_conversation, sanitise_turn


def conversation(*turns: tuple[str, str]) -> dict:
    history = [{"role": "system", "content": "You are a booking agent."}]
    history.extend({"role": role, "content": text} for role, text in turns)
    return {"_id": "x", "call_id": "c1", "agent_id": "a1", "chat_history": history}


def exchanges(count: int) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for i in range(count):
        out.append(("user", f"caller line {i}"))
        out.append(("assistant", f"agent line {i}"))
    return out


class SanitiseTurnTests(unittest.TestCase):
    def test_metadata_embedded_beside_speech_is_stripped(self) -> None:
        text = "[voice] <session_metadata>\n<user_number>919082660489</user_number>\n</session_metadata> hello sir"
        speech, removed = sanitise_turn(text)
        self.assertEqual(speech, "hello sir")
        self.assertIn("session_metadata", removed)
        self.assertNotIn("919082660489", speech)

    def test_a_silence_placeholder_is_not_speech(self) -> None:
        for text in (
            "User didn't respond to the above message",
            "user did not respond to the above message",
            "[voice] User didn't respond",
        ):
            speech, removed = sanitise_turn(text)
            self.assertEqual(speech, "", text)
            self.assertEqual(removed, ("silence_placeholder",), text)

    def test_key_value_metadata_preamble_is_stripped(self) -> None:
        speech, removed = sanitise_turn(
            "dealer_name: Raju\nuser_phone_number: 9947096227\nagent_name: Adhira\nnamaste"
        )
        self.assertEqual(speech, "namaste")
        self.assertNotIn("9947096227", speech)

    def test_stt_diagnostics_are_stripped(self) -> None:
        speech, _ = sanitise_turn(
            "[WARNING: Low confidence]\nSystem: transcript may be incorrect (score=-7.1)\nno we can connect"
        )
        self.assertIn("no we can connect", speech)
        self.assertNotIn("WARNING", speech)

    def test_real_speech_is_untouched(self) -> None:
        """Control tags are the agent's protocol, not scaffolding."""
        for text in ("<|ENGLISH|> Hi, am I speaking with Rajesh?",
                     "नमस्ते, मैं एक मिनट में चेक करता हूँ WAITING 5"):
            speech, removed = sanitise_turn(text)
            self.assertEqual(speech, text)
            self.assertEqual(removed, ())


class BindSanitisationTests(unittest.TestCase):
    def test_scaffolding_turns_leave_the_dialogue(self) -> None:
        packet = bind_conversation(conversation(
            ("assistant", "[voice] <session_metadata><user_number>91908</user_number></session_metadata>"),
            ("user", "User didn't respond to the above message"),
            *exchanges(3),
        ))
        roles = [t["role"] for t in packet["turns"]]
        self.assertEqual(roles.count("runtime_metadata"), 2)
        self.assertEqual(packet["user_turn_count"], 3)
        self.assertEqual(packet["assistant_turn_count"], 3)
        for turn in packet["turns"]:
            self.assertNotIn("session_metadata", turn["text"])
            self.assertNotIn("didn't respond", turn["text"])

    def test_the_source_stays_provably_recoverable(self) -> None:
        """Sanitising must not cost the provenance guarantee."""
        packet = bind_conversation(conversation(
            ("assistant", "[voice] <session_metadata><a>1</a></session_metadata> hello"),
            *exchanges(3),
        ))
        altered = [t for t in packet["turns"] if t.get("sanitised")]
        self.assertTrue(altered)
        for turn in altered:
            # Both hashes present: what shipped, and what was recorded.
            self.assertNotEqual(turn["text_sha256"], turn["raw_text_sha256"])
        # Conversation identity is still the raw source, so re-fetching is idempotent.
        self.assertEqual(len(packet["source_content_sha256"]), 64)

    def test_a_conversation_that_is_mostly_silence_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            bind_conversation(conversation(
                ("user", "User didn't respond to the above message"),
                ("assistant", "hello?"),
                ("user", "User didn't respond to the above message"),
                ("assistant", "hello?"),
                ("user", "User didn't respond to the above message"),
                ("assistant", "hello?"),
            ))


if __name__ == "__main__":
    unittest.main()


class DialogueRoleTests(unittest.TestCase):
    """A demoted turn must leave the dialogue everywhere, not just at binding.

    `build_review` dispatches on role and treated anything that was not `tool`
    or `user` as an assistant turn. Sanitisation demotes scaffolding to
    `runtime_metadata`, so those turns reappeared downstream as assistant turns
    carrying an empty string — 1,368 of them. For SFT every assistant turn is a
    training target, so that is 1,368 examples teaching the model to say nothing.
    """

    def test_only_conversation_roles_are_dialogue(self) -> None:
        from zen_agent.factory_review import is_dialogue_turn

        for role in ("user", "assistant", "tool"):
            self.assertTrue(is_dialogue_turn({"role": role}), role)
        for role in ("runtime_metadata", "system", "", None, "future_role"):
            self.assertFalse(is_dialogue_turn({"role": role}), repr(role))

    def test_no_dialogue_turn_survives_binding_empty(self) -> None:
        """After binding, every turn still counted as dialogue carries speech.

        This is the property that matters for SFT: an assistant turn with no
        text is a training example that teaches the model to answer with
        nothing.
        """
        from zen_agent.factory_review import is_dialogue_turn

        packet = bind_conversation(conversation(
            ("assistant", "[voice] <session_metadata><a>1</a></session_metadata>"),
            ("user", "User didn't respond to the above message"),
            ("assistant", "<|ENGLISH|> hello there"),
            *exchanges(3),
        ))
        dialogue = [t for t in packet["turns"] if is_dialogue_turn(t)]
        self.assertTrue(dialogue)
        for turn in dialogue:
            self.assertTrue(
                turn["text"].strip() or turn.get("tool_calls"),
                f"empty {turn['role']} turn at index {turn['source_index']}",
            )


