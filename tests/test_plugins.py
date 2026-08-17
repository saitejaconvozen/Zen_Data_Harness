from __future__ import annotations

from pathlib import Path
import unittest

from zen_agent.config import load_config
from zen_agent.plugins import load_plugins
from zen_agent.skills import SkillCatalog


ROOT = Path(__file__).resolve().parents[1]


class PluginTests(unittest.TestCase):
    def test_required_plugins_install_without_kernel_changes(self):
        config = load_config(ROOT)
        registry = load_plugins(config.plugin_paths)
        self.assertTrue(
            {"csv-profile", "golden-conversations", "golden-mongodb", "golden-refinement"}
            <= set(registry.manifests)
        )
        for workflow in (
            "csv-profile", "golden-bootstrap", "golden-agent-inventory",
            "golden-conversation-sample", "golden-prepare-refinement",
        ):
            self.assertIn(workflow, registry.workflows)

    def test_skill_metadata_uses_progressive_disclosure(self):
        catalog = SkillCatalog.discover([ROOT / "skills"])
        names = {item.name for item in catalog.list()}
        self.assertEqual(
            names,
            {
                "operate-data-engine", "refine-golden-conversations",
                "orchestrate-data-factory", "improve-data-engine",
                "execute-coding-task", "verify-code-change",
            },
        )
        self.assertIn(
            "Preserve every user turn",
            catalog.load_body("refine-golden-conversations"),
        )
