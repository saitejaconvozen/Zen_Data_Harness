from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from zen_agent.coding_runtime import CodingRuntime, CodingRuntimeLimits
from zen_agent.coding_state import CodingStateStore
from zen_agent.memory import MemoryStore
from zen_agent.model_adapter import ScriptedModelAdapter


ROOT = Path(__file__).resolve().parents[1]


def action(kind, *, tool=None, arguments=None, message="", summary="next"):
    return {
        "kind": kind,
        "reasoning_summary": summary,
        "tool": tool,
        "arguments": arguments or {},
        "message": message,
    }


class CodingRuntimeTests(unittest.TestCase):
    def test_executor_is_reinvoked_with_independent_verifier_feedback(self):
        with TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            original = "message = 'hello'\n"
            (workspace / "app.py").write_text(original, encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            subprocess.run(["git", "add", "app.py"], cwd=workspace, check=True)
            subprocess.run(
                [
                    "git", "-c", "user.name=Zen Test", "-c", "user.email=zen@example.invalid",
                    "commit", "-qm", "baseline",
                ],
                cwd=workspace,
                check=True,
            )
            digest = hashlib.sha256(original.encode()).hexdigest()
            responses = [
                {
                    "summary": "Change the value and test it.",
                    "steps": [
                        {
                            "id": "edit",
                            "description": "Change hello to hi.",
                            "verification": "Import app and assert the value.",
                        }
                    ],
                    "risks": [],
                },
                action("tool_call", tool="fs.read", arguments={"path": "app.py"}),
                action(
                    "tool_call",
                    tool="fs.replace",
                    arguments={
                        "path": "app.py",
                        "old": "'hello'",
                        "new": "'hi'",
                        "expected_sha256": digest,
                    },
                ),
                action(
                    "tool_call",
                    tool="process.run",
                    arguments={
                        "argv": [
                            sys.executable,
                            "-c",
                            "import app; assert app.message == 'hi'",
                        ]
                    },
                ),
                action("final", message="Changed and tested app.message."),
                {
                    "verdict": "FAIL",
                    "summary": "Inspect the file once more.",
                    "findings": ["Final file content was not reread after testing."],
                    "recommended_actions": ["Read app.py and resubmit evidence."],
                },
                action("tool_call", tool="fs.read", arguments={"path": "app.py"}),
                action("final", message="Reread app.py; it contains hi and the check passed."),
                {
                    "verdict": "PASS",
                    "summary": "The requested change and check are evidenced.",
                    "findings": [],
                    "recommended_actions": [],
                },
            ]
            adapter = ScriptedModelAdapter(responses)
            with CodingStateStore(Path(directory) / "state.db") as state, MemoryStore(
                Path(directory) / "memory.db"
            ) as memory:
                runtime = CodingRuntime(
                    harness_root=ROOT,
                    workspace=workspace,
                    state=state,
                    model=adapter,
                    limits=CodingRuntimeLimits(max_executor_turns=10, max_verification_cycles=3),
                    memory=memory,
                )
                session_id = runtime.start("Change app.message from hello to hi and verify it")
                session = state.get_session(session_id)
                self.assertEqual(session["status"], "SUCCEEDED")
                self.assertEqual((workspace / "app.py").read_text(), "message = 'hi'\n")
                verdicts = [
                    event["payload"]["verdict"]
                    for event in state.list_events(session_id)
                    if event["event_type"] == "verification.completed"
                ]
                self.assertEqual(verdicts, ["FAIL", "PASS"])
                executor_prompts = [
                    call["prompt"] for call in adapter.calls if call["role"] == "executor"
                ]
                self.assertIn("Final file content was not reread", executor_prompts[-1])
                self.assertTrue(memory.query("requested change", scope="episodic"))

    def test_unknown_tool_is_denied_and_returned_as_observation(self):
        with TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            adapter = ScriptedModelAdapter(
                [
                    {
                        "summary": "Inspect.",
                        "steps": [{"id": "one", "description": "Inspect.", "verification": "Evidence."}],
                        "risks": [],
                    },
                    action("tool_call", tool="network.fetch", arguments={"url": "https://example.com"}),
                    action("ask_human", message="Network access is not available."),
                ]
            )
            with CodingStateStore(Path(directory) / "state.db") as state:
                runtime = CodingRuntime(
                    harness_root=ROOT, workspace=workspace, state=state, model=adapter
                )
                session_id = runtime.start("Inspect a remote service")
                self.assertEqual(state.get_session(session_id)["status"], "WAITING_FOR_HUMAN")
                self.assertFalse(state.list_tool_calls(session_id))


if __name__ == "__main__":
    unittest.main()
