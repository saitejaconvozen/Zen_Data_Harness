from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import unittest

from zen_agent.domains.golden_taxonomy import compile_taxonomy


ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "plugins" / "golden-conversations"


class GoldenTaxonomyAssetTests(unittest.TestCase):
    def test_snapshot_and_compiled_registry_are_governed(self):
        source = GOLDEN / "resources" / "taxonomy" / "zen-eval-axes-2026-q2.csv"
        registry_path = GOLDEN / "resources" / "taxonomy" / "zen-eval-taxonomy-2026-q2-v1.json"
        taxonomy = compile_taxonomy(source)
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        self.assertEqual(
            sha256(source.read_bytes()).hexdigest(),
            "e923c80119f9016c4508de2662a8dd776c4329d0615df461f9aeb4f893afc629",
        )
        self.assertEqual((taxonomy.axis_count, taxonomy.subaxis_count, taxonomy.variant_count), (10, 35, 286))
        self.assertEqual(registry["counts"], {"axes": 10, "subaxes": 35, "variants": 286, "warnings": 2})
        self.assertTrue(registry["governance"]["parent_path_validation_required"])

    def test_prompts_and_schemas_exist_and_schemas_parse(self):
        for name in (
            "agent-configuration-auditor.md", "conversation-refiner.md",
            "conversation-verifier.md", "human-review.md",
        ):
            self.assertTrue((GOLDEN / "prompts" / name).is_file())
        for path in (GOLDEN / "schemas").glob("*.json"):
            self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict)
