from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from zen_agent.artifacts import ArtifactStore
from zen_agent.config import load_config
from zen_agent.plugins import load_plugins
from zen_agent.runtime import Supervisor
from zen_agent.state import EventStore


ROOT = Path(__file__).resolve().parents[1]
LEGACY_ROOT = ROOT.parent


class RuntimeTests(unittest.TestCase):
    def _runtime(self, directory):
        config = replace(load_config(ROOT), state_directory=Path(directory) / ".zen")
        registry = load_plugins(config.plugin_paths)
        state = EventStore(config.state_directory / "state.db")
        artifacts = ArtifactStore(config.state_directory / "artifacts")
        return config, registry, state, Supervisor(config, registry, state, artifacts)

    def test_csv_workflow_completes_and_verifies(self):
        with TemporaryDirectory() as directory:
            _, _, state, supervisor = self._runtime(directory)
            try:
                run_id = supervisor.start("Profile this CSV", {"path": str(ROOT / "tests" / "fixtures" / "sample.csv")})
                self.assertEqual(state.get_run(run_id)["status"], "SUCCEEDED")
                self.assertTrue(supervisor.verify(run_id)["valid"])
                event_types = [item["event_type"] for item in state.trace(run_id)]
                self.assertIn("policy.evaluated", event_types)
                self.assertIn("artifact.committed", event_types)
            finally:
                state.close()

    def test_golden_bootstrap_validates_real_taxonomy_without_mongo(self):
        with TemporaryDirectory() as directory:
            _, _, state, supervisor = self._runtime(directory)
            try:
                inputs = {
                    "legacy_root": str(LEGACY_ROOT),
                    "taxonomy_csv": str(LEGACY_ROOT / "DSE OKR 2026 Q2 - Zen Eval Axes.csv"),
                }
                run_id = supervisor.start("Inspect golden conversations and taxonomy", inputs)
                self.assertEqual(state.get_run(run_id)["status"], "SUCCEEDED")
                tasks = state.list_tasks(run_id)
                self.assertEqual([item["status"] for item in tasks], ["SUCCEEDED", "SUCCEEDED"])
                self.assertTrue(supervisor.verify(run_id)["valid"])
            finally:
                state.close()
