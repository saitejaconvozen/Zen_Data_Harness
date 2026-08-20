"""The agent loop's four structural limits, and that they are gone.

Each of these was measured against Claude Code and Codex and found absent. They
are one refactor of the same loop, not four features:

* one tool call per turn — an agent gathering evidence from four sources spent
  four model round trips on I/O, most of a 20-turn budget
* `observations[-8:]` and nothing else — at 40 turns, amnesia for turns 1-32
* tool results inlined raw — one 400 KB query evicts the whole prompt
* budgets counted in characters — Malayalam is 1.62 bytes/char, so the same
  nominal budget is a materially smaller budget for Indic languages
"""

from __future__ import annotations

import json
import unittest

from zen_agent.agent_protocol import ACTION_SCHEMA, AgentAction
from zen_agent.coding_runtime import MAX_RESULT_CHARS, _bound_result, _digest_observations


class ParallelToolCallTests(unittest.TestCase):
    def test_a_turn_carries_several_independent_calls(self) -> None:
        action = AgentAction.from_dict({
            "kind": "tool_call", "reasoning_summary": "gather", "message": "",
            "calls": [
                {"tool": "data.failure_clusters", "arguments": '{"run_id":"r"}'},
                {"tool": "data.query_ledgers", "arguments": '{"ledger":"qa","sql":"SELECT 1"}'},
                {"tool": "data.read_contract", "arguments": '{"path":"plugins/x.md"}'},
            ],
        })
        self.assertEqual(len(action.calls), 3)
        self.assertEqual(action.calls[1].arguments["ledger"], "qa")

    def test_the_single_call_shape_still_works(self) -> None:
        """A model may emit the older shape; it must not break the run."""
        action = AgentAction.from_dict({
            "kind": "tool_call", "reasoning_summary": "r", "message": "",
            "tool": "fs.read", "arguments": '{"path":"a.py"}',
        })
        self.assertEqual([c.tool for c in action.calls], ["fs.read"])
        self.assertEqual(action.arguments, {"path": "a.py"})

    def test_a_terminal_action_names_no_tool(self) -> None:
        for kind in ("final", "ask_human"):
            action = AgentAction.from_dict({
                "kind": kind, "reasoning_summary": "r", "message": "m", "calls": [],
            })
            self.assertEqual(action.calls, ())
            self.assertIsNone(action.tool)

    def test_a_tool_call_without_calls_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            AgentAction.from_dict({
                "kind": "tool_call", "reasoning_summary": "r", "message": "", "calls": [],
            })

    def test_the_schema_advertises_the_array(self) -> None:
        calls = ACTION_SCHEMA["properties"]["calls"]
        self.assertEqual(calls["type"], "array")
        self.assertIn("calls", ACTION_SCHEMA["required"])


class BoundedResultTests(unittest.TestCase):
    def test_a_large_row_set_is_trimmed_and_says_so(self) -> None:
        result = _bound_result({"rows": [{"text": "x" * 400} for _ in range(200)],
                                "row_count": 200})
        self.assertLess(len(result["rows"]), 200)
        self.assertIn("of 200 rows", result["_truncated"])
        self.assertLessEqual(len(json.dumps(result)), MAX_RESULT_CHARS * 2)

    def test_a_small_result_passes_through_untouched(self) -> None:
        small = {"rows": [{"a": 1}], "row_count": 1}
        self.assertEqual(_bound_result(small), small)

    def test_an_unstructured_giant_becomes_a_preview(self) -> None:
        result = _bound_result("y" * (MAX_RESULT_CHARS * 3))
        self.assertIn("_truncated", result)
        self.assertLessEqual(len(result["preview"]), MAX_RESULT_CHARS)


class RollingStateTests(unittest.TestCase):
    def test_every_observation_appears_in_the_digest(self) -> None:
        observations = [
            {"tool": f"t{i}", "status": "SUCCEEDED", "result": {"row_count": i}}
            for i in range(30)
        ]
        digest = _digest_observations(observations)
        self.assertEqual(len(digest), 30, "no observation may fall out of history")
        self.assertIn("t0", digest[0])
        self.assertIn("t29", digest[-1])

    def test_parallel_calls_are_flattened_into_history(self) -> None:
        digest = _digest_observations([
            {"parallel_calls": [
                {"tool": "a", "status": "SUCCEEDED", "result": {}},
                {"tool": "b", "status": "FAILED", "error": "boom"},
            ]}
        ])
        self.assertEqual(len(digest), 2)
        self.assertIn("boom", digest[1])

    def test_the_digest_stays_small(self) -> None:
        """It must be cheap enough to send every turn."""
        observations = [{"tool": "data.query_ledgers", "status": "SUCCEEDED",
                         "result": {"row_count": 12}} for _ in range(40)]
        self.assertLess(len("\n".join(_digest_observations(observations))), 3_000)


if __name__ == "__main__":
    unittest.main()


class TokenBudgetTests(unittest.TestCase):
    """Character budgets were a measured language bias.

    Across this corpus: English 1.05 bytes/char, Hindi 1.29, Malayalam 1.62. A
    budget counted in characters is therefore a materially smaller budget for
    Indic scripts — the agent's effective context shrank for exactly the
    languages the corpus was widened to include.
    """

    def test_indic_scripts_cost_more_per_character(self) -> None:
        from zen_agent.coding_runtime import estimate_tokens

        english = "Hello, how can I help you today? " * 20
        malayalam = "നമസ്കാരം, ഞാൻ എങ്ങനെ സഹായിക്കാം? " * 20
        per_char = lambda s: estimate_tokens(s) / len(s)
        self.assertGreater(
            per_char(malayalam), per_char(english) * 2,
            "an Indic script must be charged more per character than Latin",
        )

    def test_an_empty_prompt_costs_nothing(self) -> None:
        from zen_agent.coding_runtime import estimate_tokens

        self.assertEqual(estimate_tokens(""), 0)

    def test_the_limit_is_expressed_in_tokens(self) -> None:
        from zen_agent.coding_runtime import CodingRuntimeLimits

        limits = CodingRuntimeLimits()
        self.assertTrue(hasattr(limits, "max_prompt_tokens"))
        self.assertFalse(hasattr(limits, "max_prompt_chars"))
