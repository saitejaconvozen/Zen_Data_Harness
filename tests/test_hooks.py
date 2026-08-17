from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from zen_agent.hooks import HookConfig, HookRunner


class HookTests(unittest.TestCase):
    def test_command_hook_receives_json_and_can_block(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "guard.py"
            script.write_text(
                """import json, sys
request = json.load(sys.stdin)
blocked = request["payload"].get("path", "").endswith(".env")
print(json.dumps({"decision": "block" if blocked else "allow", "feedback": "secret file" if blocked else "checked"}))
""",
                encoding="utf-8",
            )
            config_path = root / "hooks.json"
            config_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "command": [sys.executable, str(script)],
                                    "matcher": "fs.*",
                                    "timeout_seconds": 2,
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            runner = HookRunner(HookConfig.load(config_path), root)
            ignored = runner.emit("PreToolUse", {"path": ".env"}, subject="process.run")
            blocked = runner.emit("PreToolUse", {"path": ".env"}, subject="fs.read")
            self.assertTrue(ignored.allowed)
            self.assertEqual(ignored.executions, ())
            self.assertFalse(blocked.allowed)
            self.assertEqual(blocked.feedback, ("secret file",))

    def test_timeout_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "slow.py"
            script.write_text("import time; time.sleep(1)\n", encoding="utf-8")
            config_path = root / "hooks.json"
            config_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "BeforeComplete": [
                                {"command": [sys.executable, str(script)], "timeout_seconds": 0.02}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            result = HookRunner(HookConfig.load(config_path), root).emit(
                "BeforeComplete", {"session_id": "s1"}
            )
            self.assertFalse(result.allowed)
            self.assertIn("timed out", result.feedback[0])

    def test_config_rejects_shell_command_strings(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.json"
            path.write_text(
                json.dumps({"hooks": {"SessionStart": [{"command": "echo unsafe"}]}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "string list"):
                HookConfig.load(path)


if __name__ == "__main__":
    unittest.main()
