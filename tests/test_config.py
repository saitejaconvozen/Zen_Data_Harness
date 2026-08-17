from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import unittest

from zen_agent.config import PINNED_MODEL, load_config
from zen_agent.workers.codex_exec import CodexExecWorker


ROOT = Path(__file__).resolve().parents[1]


class ModelPolicyTests(unittest.TestCase):
    def test_configuration_is_pinned_to_sol(self):
        config = load_config(ROOT)
        self.assertEqual(config.allowed_models, (PINNED_MODEL,))
        self.assertEqual(config.default_model, PINNED_MODEL)

    def test_worker_rejects_other_model(self):
        with self.assertRaises(ValueError):
            CodexExecWorker(ROOT, model="another-model")

    def test_runtime_source_has_no_gemini_or_vertex_dependency(self):
        source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "src").rglob("*.py")).casefold()
        self.assertNotIn("gemini", source)
        self.assertNotIn("vertex", source)
