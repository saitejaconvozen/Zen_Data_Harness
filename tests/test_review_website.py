from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from zen_agent.artifacts import ArtifactStore
from zen_agent.config import load_config
from zen_agent.plugins import load_plugins
from zen_agent.runtime import Supervisor
from zen_agent.state import EventStore


ROOT = Path(__file__).resolve().parents[1]


def _review() -> dict:
    return {
        "schema_version": "zen.golden-review-batch/1",
        "run_id": "source-run",
        "counts": {"total": 1, "READY_FOR_HUMAN_REVIEW": 1, "QUARANTINED": 0},
        "conversations": [
            {
                "packet_index": 0,
                "packet_id": "rp_" + "a" * 64,
                "status": "READY_FOR_HUMAN_REVIEW",
                "source": {"agent_id": "agent-test"},
                "classification": {"domain": "test", "primary_language": "English"},
                "prompt_usable": True,
                "prompt_issues": [],
                "replay_required": False,
                "quarantine_reasons": [],
                "verifier": {"decision": "PASS", "findings": [], "replay_required": False},
                "turns": [
                    {
                        "turn_id": "turn_0000",
                        "role": "user",
                        "text": "hello",
                        "text_sha256": "unused",
                        "source_preserved": True,
                    },
                    {
                        "turn_id": "turn_0001",
                        "role": "assistant",
                        "source_text": "hello",
                        "golden_text": "Hello. How may I help?",
                        "action": "REPLACE",
                        "semantic_delta": "STYLE_ONLY",
                        "correction_reason": "Improve conversational clarity.",
                        "metric_citations": [
                            {
                                "axis_name": "Communication",
                                "subaxis_name": "Clarity",
                                "variant_name": "Clear response",
                                "source_verdict": "FAIL",
                                "golden_verdict": "PASS",
                                "confidence": 0.9,
                            }
                        ],
                    },
                ],
            }
        ],
    }


class ReviewWebsiteTests(unittest.TestCase):
    def test_builds_owner_only_site_through_supervisor(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            (root / "plugins").symlink_to(ROOT / "plugins", target_is_directory=True)
            (root / "ZEN.md").write_text("# Test harness\n", encoding="utf-8")
            review = root / "review.json"
            review.write_text(json.dumps(_review()), encoding="utf-8")
            (root / "zen.toml").write_text(
                """[project]\nname = \"test\"\ninstruction_file = \"ZEN.md\"\n[runtime]\nstate_directory = \".zen\"\nmax_tasks_per_run = 10\nmax_attempts_per_task = 1\nmax_tool_calls_per_run = 10\n[models]\nexecution_adapter = \"codex_exec\"\nallowed = [\"gpt-5.6-sol\"]\ndefault = \"gpt-5.6-sol\"\n[plugins]\npaths = [\"plugins\"]\n[policy]\ndefault_tool_effect = \"deny\"\nallowed_risks = [\"read_only\", \"workspace_write\"]\napproval_risks = [\"external_write\", \"production_write\", \"destructive\"]\n""",
                encoding="utf-8",
            )
            config = load_config(root)
            registry = load_plugins(config.plugin_paths)
            state = EventStore(config.state_directory / "state.db")
            try:
                supervisor = Supervisor(config, registry, state, ArtifactStore(config.state_directory / "artifacts"))
                run_id = supervisor.start(
                    "Build review website for verified generated conversations",
                    {"review_batch": "review.json"},
                    "golden-review-website",
                )
                self.assertEqual(state.get_run(run_id)["status"], "SUCCEEDED")
                site = root / ".zen" / "sites" / run_id
                self.assertTrue((site / "site-manifest.json").is_file())
                self.assertEqual(site.stat().st_mode & 0o777, 0o700)
                self.assertEqual((site / "review.json").stat().st_mode & 0o777, 0o600)
            finally:
                state.close()

    def test_frontend_does_not_render_transcript_as_html(self):
        javascript = (ROOT / "plugins" / "review-website" / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", javascript)
        self.assertNotIn("insertAdjacentHTML", javascript)
        self.assertIn("textContent", javascript)

    def test_ngrok_secrets_are_not_command_arguments_or_config_writes(self):
        launcher = (ROOT / "plugins" / "review-website" / "scripts" / "publish_ngrok.py").read_text(encoding="utf-8")
        self.assertNotIn('"--authtoken"', launcher)
        self.assertNotIn("add-authtoken", launcher)
        self.assertIn("getpass.getpass", launcher)
        self.assertIn('tunnel_env["NGROK_AUTHTOKEN"]', launcher)


if __name__ == "__main__":
    unittest.main()
