from __future__ import annotations

from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

from zen_agent.tools import ToolContext


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "golden_refinement_plugin", ROOT / "plugins" / "golden-refinement" / "plugin.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PLUGIN = _load_plugin()

SYSTEM_PROMPT = (
    "You are a voice agent. Always begin every response with `<|ENGLISH|>` "
    "or, when the caller speaks Hindi, with `<|HINDI|>`."
)


def _conversation(turns):
    normalized = [
        {
            "role": role,
            "text": text,
            "source_index": index,
            "text_sha256": sha256(text.encode("utf-8")).hexdigest(),
        }
        for index, (role, text) in enumerate(turns, start=1)
    ]
    body = json.dumps(normalized, sort_keys=True)
    return {
        "source_mongo_id": "abc123",
        "source_mongo_id_type": "ObjectId",
        "call_id": "call-1",
        "agent_id": "agent-1",
        "agent_version": "v1",
        "system_prompt": SYSTEM_PROMPT,
        "system_prompt_sha256": sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "source_content_sha256": sha256(body.encode("utf-8")).hexdigest(),
        "turns": normalized,
    }


class PacketPreparationTests(unittest.TestCase):
    """Packet preparation resolves tag compliance before any model call."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # The tool validates the governed taxonomy, so it runs against the repo.
        self.sample = ROOT / ".zen" / "test-sample.json"
        self.addCleanup(lambda: self.sample.unlink(missing_ok=True))

    def _prepare(self, turns):
        payload = {"result": {"conversations": [_conversation(turns)]}}
        self.sample.parent.mkdir(parents=True, exist_ok=True)
        self.sample.write_text(json.dumps(payload), encoding="utf-8")
        context = ToolContext("run-1", "task-1", ROOT)
        return PLUGIN._prepare_packets(
            context, {"sample_artifact": str(self.sample.relative_to(ROOT))}
        )

    def test_missing_language_tags_are_detected_and_repaired(self) -> None:
        result = self._prepare(
            [
                ("user", "Hello"),
                ("assistant", "Hi, how can I help?"),
                ("user", "My order"),
                ("assistant", "<|ENGLISH|> Let me check that."),
            ]
        )
        packet = result["packets"][0]
        self.assertEqual(
            packet["format_compliance"],
            {
                "assistant_turns": 2,
                "compliant": 1,
                "non_compliant": 1,
                "auto_repairable": 1,
            },
        )
        by_id = {turn["turn_id"]: turn for turn in packet["turns"]}
        missing = by_id["turn_0002"]["format"]
        self.assertFalse(missing["compliant"])
        self.assertEqual(missing["proposed_text"], "<|ENGLISH|> Hi, how can I help?")
        self.assertTrue(by_id["turn_0004"]["format"]["compliant"])

    def test_user_turns_carry_no_format_finding(self) -> None:
        result = self._prepare(
            [("user", "Hello"), ("assistant", "<|ENGLISH|> Hi")]
        )
        by_id = {turn["turn_id"]: turn for turn in result["packets"][0]["turns"]}
        self.assertNotIn("format", by_id["turn_0001"])
        self.assertIn("format", by_id["turn_0002"])

    def test_source_text_is_never_mutated_by_the_prepass(self) -> None:
        result = self._prepare(
            [("user", "Hello"), ("assistant", "Hi, how can I help?")]
        )
        turn = result["packets"][0]["turns"][1]
        # Provenance must survive: stored text still hashes to the source digest.
        self.assertEqual(turn["text"], "Hi, how can I help?")
        self.assertEqual(
            turn["text_sha256"], sha256(turn["text"].encode("utf-8")).hexdigest()
        )


class ToolCallPreservationTests(unittest.TestCase):
    """Tool calls are training signal, not metadata.

    Ingestion rejected assistant turns whose content was null — the shape a
    tool-calling turn actually has — and kept only `content` on the rest, so the
    invocation was silently dropped from every conversation that survived.
    """

    def setUp(self) -> None:
        self.sample = ROOT / ".zen" / "test-tool-sample.json"
        self.addCleanup(lambda: self.sample.unlink(missing_ok=True))

    def test_null_content_with_tool_calls_is_accepted(self) -> None:
        from zen_agent.adapters.mongodb import bind_conversation
        history = [{"role": "system", "content": "You are an agent."}]
        for i in range(3):
            history.append({"role": "user", "content": f"question {i}"})
            history.append({"role": "assistant", "content": f"answer {i}"})
        history.append({
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "lookup", "arguments": '{"q":"x"}'}}],
        })
        history.append({"role": "tool", "content": '{"ok":true}', "tool_call_id": "call_1"})
        bound = bind_conversation({
            "agent_id": "a", "call_id": "c", "chat_history": history,
        })
        calls = [t for t in bound["turns"] if t.get("tool_calls")]
        results = [t for t in bound["turns"] if t.get("tool_call_id")]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool_calls"][0]["function"]["name"], "lookup")
        self.assertIn("tool_calls_sha256", calls[0])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["role"], "tool")

    def test_a_turn_without_content_or_tool_calls_is_still_rejected(self) -> None:
        from zen_agent.adapters.mongodb import bind_conversation
        with self.assertRaises(ValueError):
            bind_conversation({"agent_id": "a", "call_id": "c", "chat_history": [
                {"role": "system", "content": "s"}, {"role": "assistant", "content": None},
            ]})

    def test_packet_preparation_carries_tool_calls(self) -> None:
        from hashlib import sha256 as _s
        turns = [
            {"role": "user", "text": "hi", "source_index": 1,
             "text_sha256": _s(b"hi").hexdigest()},
            {"role": "assistant", "text": "", "source_index": 2,
             "text_sha256": _s(b"").hexdigest(),
             "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "lookup", "arguments": "{}"}}],
             "tool_calls_sha256": "d" * 64},
            {"role": "tool", "text": "{}", "source_index": 3,
             "text_sha256": _s(b"{}").hexdigest(), "tool_call_id": "call_1"},
        ]
        conversation = _conversation([("user", "hi")])
        conversation["turns"] = turns
        payload = {"result": {"conversations": [conversation]}}
        self.sample.parent.mkdir(parents=True, exist_ok=True)
        self.sample.write_text(json.dumps(payload), encoding="utf-8")
        result = PLUGIN._prepare_packets(
            ToolContext("run-1", "task-1", ROOT),
            {"sample_artifact": str(self.sample.relative_to(ROOT))},
        )
        by_id = {t["turn_id"]: t for t in result["packets"][0]["turns"]}
        self.assertEqual(
            by_id["turn_0002"]["tool_calls"][0]["function"]["name"], "lookup"
        )
        self.assertEqual(by_id["turn_0003"]["tool_call_id"], "call_1")


if __name__ == "__main__":
    unittest.main()
