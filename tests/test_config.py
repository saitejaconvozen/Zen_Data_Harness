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

    def test_runtime_imports_no_provider_sdk(self):
        """No provider SDK may be imported into the runtime.

        This once asserted the word "gemini" appeared nowhere in `src/`, which
        made sense when the harness could only call one provider. It cannot any
        more: the transport reaches Codex, Claude, or any model behind an
        OpenAI-compatible proxy, and a default model name is configuration.

        What must still hold is the reason behind the original rule — the
        runtime speaks HTTP and subprocesses, never a vendor client library, so
        adding a provider never adds a dependency or a new way to be breached.
        """
        forbidden = (
            "import openai", "from openai", "import anthropic", "from anthropic",
            "import google.generativeai", "from google.generativeai",
            "import vertexai", "from vertexai", "import litellm", "from litellm",
        )
        offenders = []
        for path in (ROOT / "src").rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token in source:
                    offenders.append(f"{path.relative_to(ROOT)} imports {token!r}")
        self.assertEqual(offenders, [], "; ".join(offenders))
