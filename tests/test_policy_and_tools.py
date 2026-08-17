from __future__ import annotations

from pathlib import Path
import unittest

from zen_agent.config import load_config
from zen_agent.models import ToolRisk
from zen_agent.policy import PolicyEngine
from zen_agent.schema import SchemaError
from zen_agent.tools import ToolContext, ToolRegistry, ToolSpec
from zen_agent.workers.codex_exec import CodexExecWorker


ROOT = Path(__file__).resolve().parents[1]


class PolicyAndToolTests(unittest.TestCase):
    def test_read_is_allowed_and_external_write_requires_human(self):
        policy = PolicyEngine(load_config(ROOT))
        self.assertEqual(policy.evaluate("fixture.read", ToolRisk.READ_ONLY).effect, "allow")
        self.assertEqual(
            policy.evaluate("fixture.publish", ToolRisk.EXTERNAL_WRITE).effect,
            "needs_approval",
        )

    def test_tool_output_is_validated_before_commit(self):
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                "fixture.invalid",
                "1.0.0",
                "Return deliberately malformed output",
                ToolRisk.READ_ONLY,
                {"type": "object", "additionalProperties": False, "properties": {}},
                {
                    "type": "object",
                    "required": ["count"],
                    "additionalProperties": False,
                    "properties": {"count": {"type": "integer"}},
                },
                lambda _context, _inputs: {"count": "not-an-integer"},
            )
        )
        with self.assertRaises(SchemaError):
            registry.invoke("fixture.invalid", ToolContext("run", "task", ROOT), {})

    def test_codex_command_is_pinned_and_structured(self):
        worker = CodexExecWorker(ROOT)
        command = worker.command(Path("schema.json"), Path("output.json"), "objective")
        self.assertIn("gpt-5.6-sol", command)
        self.assertIn("--output-schema", command)
        self.assertNotIn("gemini", " ".join(command).casefold())
