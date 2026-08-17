from __future__ import annotations

import unittest

from zen_agent.agent_protocol import AgentAction
from zen_agent.config import PINNED_MODEL
from zen_agent.model_adapter import CodexExecAdapter, ScriptedModelAdapter


class AgentProtocolTests(unittest.TestCase):
    def test_action_requires_tool_only_for_tool_calls(self):
        action = AgentAction.from_dict({
            "kind": "tool_call",
            "reasoning_summary": "Inspect the file first.",
            "tool": "fs.read",
            "arguments": {"path": "README.md"},
            "message": "",
        })
        self.assertEqual(action.tool, "fs.read")
        encoded = AgentAction.from_dict({
            "kind": "tool_call", "reasoning_summary": "Inspect.", "tool": "fs.read",
            "arguments": "{\"path\":\"README.md\"}", "message": "",
        })
        self.assertEqual(encoded.arguments, {"path": "README.md"})
        with self.assertRaises(ValueError):
            AgentAction.from_dict({
                "kind": "final", "reasoning_summary": "done", "tool": "fs.read",
                "arguments": {}, "message": "done",
            })

    def test_model_policy_is_pinned(self):
        self.assertEqual(CodexExecAdapter().model, PINNED_MODEL)
        with self.assertRaises(ValueError):
            CodexExecAdapter(model="some-other-model")

    def test_scripted_adapter_records_role_without_executing_tools(self):
        response = {"summary": "plan", "steps": [], "risks": []}
        adapter = ScriptedModelAdapter([response])
        self.assertEqual(adapter.generate(role="planner", prompt="objective", schema={}), response)
        self.assertEqual(adapter.calls[0]["role"], "planner")


if __name__ == "__main__":
    unittest.main()
