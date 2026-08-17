from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from zen_agent.context import WorkspaceContextCompiler
from zen_agent.memory import MemoryStore


class WorkspaceContextTests(unittest.TestCase):
    def test_loads_only_applicable_instruction_hierarchy(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ZEN.md").write_text("root contract", encoding="utf-8")
            (root / "AGENTS.md").write_text("root agent rules", encoding="utf-8")
            target = root / "src" / "service"
            target.mkdir(parents=True)
            (root / "src" / "AGENTS.md").write_text("source rules", encoding="utf-8")
            (target / "AGENTS.md").write_text("service rules", encoding="utf-8")
            (target / "module.py").write_text("pass\n", encoding="utf-8")
            unrelated = root / "docs"
            unrelated.mkdir()
            (unrelated / "AGENTS.md").write_text("documentation rules", encoding="utf-8")

            compiled = WorkspaceContextCompiler(root).compile(
                "Fix service", ("src/service/module.py",)
            )
            self.assertIn("root contract", compiled)
            self.assertIn("root agent rules", compiled)
            self.assertIn("source rules", compiled)
            self.assertIn("service rules", compiled)
            self.assertNotIn("documentation rules", compiled)

    def test_rejects_escape_and_omits_whole_sections_at_budget(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ZEN.md").write_text("contract", encoding="utf-8")
            (root / "AGENTS.md").write_text("nested-unique-" + "x" * 500, encoding="utf-8")
            compiler = WorkspaceContextCompiler(root, max_chars=160)
            compiled = compiler.compile("small objective")
            self.assertLessEqual(len(compiled), 160)
            self.assertNotIn("nested-unique", compiled)
            with self.assertRaisesRegex(ValueError, "escapes"):
                compiler.discover((Path("../outside.txt"),))


class MemoryTests(unittest.TestCase):
    def test_persists_and_queries_project_and_episodic_memory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "memory.db"
            with MemoryStore(db) as store:
                store.append("project", "Use unittest for this repository", actor="worker")
                store.append_episode("session-1", "The parser rejects unknown fields", actor="verifier")
                store.append_episode("session-2", "A separate observation", actor="verifier")
            with MemoryStore(db) as reopened:
                matches = reopened.query("parser unknown", scope="episodic")
                self.assertEqual([item.session_id for item in matches], ["session-1"])
                self.assertEqual(matches[0].actor, "verifier")

    def test_curated_memory_requires_proposal_and_explicit_approval(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with MemoryStore(root / "memory.db") as store:
                proposal = store.propose_curated(
                    "Always run focused tests.", actor="worker", rationale="Repeated verification gap"
                )
                self.assertEqual(store.read_curated(), "")
                expected = store.curated_sha256()
                digest = store.approve_curated(
                    proposal, reviewer="human-reviewer", expected_sha256=expected
                )
                self.assertEqual(store.read_curated(), "Always run focused tests.")
                self.assertEqual(store.list_proposals(status="APPROVED")[0].reviewed_by, "human-reviewer")
                self.assertEqual(digest, store.curated_sha256())

    def test_curated_approval_detects_stale_review(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            curated = root / "project.md"
            with MemoryStore(root / "memory.db", curated_path=curated) as store:
                proposal = store.propose_curated("new", actor="worker", rationale="reason")
                old_hash = store.curated_sha256()
                curated.write_text("human edit", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "changed since review"):
                    store.approve_curated(proposal, reviewer="human", expected_sha256=old_hash)


if __name__ == "__main__":
    unittest.main()
