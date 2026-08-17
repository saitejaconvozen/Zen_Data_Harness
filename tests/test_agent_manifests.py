from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from zen_agent.agent_manifests import AgentCatalog


class AgentManifestTests(unittest.TestCase):
    def test_discovers_and_validates_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "planner.md").write_text(
                """---
name: planner
description: Plans repository work
role: planner
tools:
  - fs.read
  - fs.search
skills: [execute-coding-task]
model: gpt-5.6-sol
max_turns: 12
sandbox: read-only
memory_scope: project
---
Create a bounded plan and cite evidence.
""",
                encoding="utf-8",
            )
            catalog = AgentCatalog.discover([root])
            manifest = catalog.get("planner")
            self.assertEqual(manifest.tools, ("fs.read", "fs.search"))
            self.assertEqual(manifest.skills, ("execute-coding-task",))
            self.assertEqual(manifest.max_turns, 12)
            self.assertIn("bounded plan", manifest.instructions)

    def test_rejects_unknown_fields_and_duplicate_names(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad.md").write_text(
                "---\nname: bad\ndescription: Bad\nrole: worker\nsurprise: true\n---\nDo work.\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unknown fields"):
                AgentCatalog.discover([root])
            (root / "bad.md").unlink()
            body = "---\nname: worker\ndescription: Work\nrole: worker\n---\nDo work.\n"
            (root / "one.md").write_text(body, encoding="utf-8")
            (root / "two.md").write_text(body, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                AgentCatalog.discover([root])

    def test_rejects_manifest_symlink_escaping_root(self):
        with TemporaryDirectory() as directory, TemporaryDirectory() as outside:
            root = Path(directory)
            external = Path(outside) / "external.md"
            external.write_text(
                "---\nname: external\ndescription: External\nrole: worker\n---\nNo.\n",
                encoding="utf-8",
            )
            (root / "external.md").symlink_to(external)
            with self.assertRaisesRegex(ValueError, "escapes"):
                AgentCatalog.discover([root])


if __name__ == "__main__":
    unittest.main()
