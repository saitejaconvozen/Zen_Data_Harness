from __future__ import annotations

import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from zen_agent.status_server import (
    CONVERSATIONS,
    _packet_context,
    conversation_detail,
    conversation_index,
)


def conversation():
    return {
        "number": 1,
        "packet_id": "packet-1",
        "source_id": "source-short",
        "source_id_full": "source-full",
        "configuration_id": "config-1",
        "configuration_status": "QUALIFIED",
        "agent_id": "agent-1",
        "agent_version": "v3",
        "call_id": "call-1",
        "terminal": {"status": "VERIFIED_CANDIDATE", "reason": "verified"},
        "human_review": {"state": "REVIEW_PENDING"},
        "audit": {"verdict": "PASS"},
        "prompt_usable": True,
        "classification": {
            "domain": "healthcare", "primary_language": "Hindi",
            "other_languages": ["English"], "code_switching": True,
        },
        "iterations": [],
        "turns": [
            {"turn_id": "u1", "role": "user", "text": "raw user"},
            {
                "turn_id": "a1", "role": "assistant", "action": "REPLACE",
                "source_text": "source assistant", "golden_text": "golden assistant",
                "source_quality": "MAJOR_GAP", "excluded_from_golden": False,
                "metric_citations": [{
                    "axis_id": "AX1", "axis_name": "Conversation",
                    "subaxis_id": "SA1", "subaxis_name": "Grounding",
                    "variant_id": "V1", "variant_name": "No hallucination",
                }],
            },
        ],
    }


class StatusConversationTests(unittest.TestCase):
    def test_index_is_summary_only_and_exposes_facets_and_metric_hierarchy(self):
        review = {
            "disclaimer": "restricted", "counts": {"conversations": 1},
            "conversations": [conversation()],
        }
        with patch("zen_agent.status_server._review", return_value=review), patch(
            "zen_agent.status_server._judge_verdicts", return_value={}
        ):
            result = conversation_index(Path("/tmp/root"), "run")
        item = result["conversations"][0]
        self.assertIsInstance(item["turns"], int)
        self.assertNotIn("system_prompt", item)
        self.assertNotIn("source_text", item)
        self.assertNotIn("golden_text", item)
        self.assertEqual(item["agent_id"], "agent-1")
        self.assertEqual(item["review_state"], "REVIEW_PENDING")
        self.assertEqual(item["metric_keys"], ["V1"])
        self.assertEqual(result["facets"]["domains"][0]["value"], "healthcare")
        self.assertEqual(result["metric_hierarchy"][0]["subaxes"][0]["variants"][0]["variant_id"], "V1")

    def test_packet_context_keeps_prompt_out_of_list_but_available_to_detail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch = root / "packet.json"
            batch.write_text('{"result":{"packets":[{"system_prompt":"private prompt","source":{"agent_id":"agent-1","agent_version":"v3","call_id":"call-1","system_prompt_sha256":"abc"}}]}}')
            row = {
                "packet_batch": str(batch), "packet_index": 0,
                "agent_id": "agent-1", "agent_version": "v3",
                "system_prompt_sha256": "abc",
            }
            with patch("zen_agent.status_server._query", return_value=[row]):
                context = _packet_context(root, "run", "packet-1")
        self.assertEqual(context["system_prompt"], "private prompt")
        self.assertEqual(context["call_id"], "call-1")

    def test_detail_adds_source_context_without_changing_turns(self):
        source = conversation()
        review = {"conversations": [source]}
        context = {"system_prompt": "prompt", "agent_id": "agent-1"}
        with patch("zen_agent.status_server._review", return_value=review), patch(
            "zen_agent.status_server._packet_context", return_value=context
        ), patch("zen_agent.status_server._judge_verdicts", return_value={}):
            detail = conversation_detail(Path("/tmp/root"), "run", "source-short")
        self.assertEqual(detail["source_context"], context)
        self.assertEqual(detail["turns"][0]["text"], "raw user")

    def test_ui_contains_granular_filters_and_safe_prompt_panel(self):
        for identifier in (
            'id="domain"', 'id="agent"', 'id="language"', 'id="review"',
            'id="metric-browser"', "Show complete source system prompt",
            "Axis → Subaxis → Variant", "source_context",
        ):
            self.assertIn(identifier, CONVERSATIONS)
        self.assertIn("const esc=", CONVERSATIONS)
        self.assertIn("Load ${Math.min", CONVERSATIONS)


if __name__ == "__main__":
    unittest.main()
