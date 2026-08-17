from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from zen_agent.coding_tools import coding_tool_catalog, coding_tool_specs, register_coding_tools
from zen_agent.tools import ToolContext, ToolRegistry
from zen_agent.workspace import Workspace, WorkspaceError


class CodingToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.context = ToolContext("run", "task", self.root)
        self.tools = ToolRegistry()
        register_coding_tools(self.tools)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def invoke(self, name: str, **inputs: object) -> dict[str, object]:
        return self.tools.invoke(name, self.context, inputs)

    def test_specs_have_expected_risk_and_names(self) -> None:
        specs = {item.name: item for item in coding_tool_specs()}
        self.assertEqual(set(specs), {"fs.read", "fs.write", "fs.replace", "fs.list", "fs.search", "process.run", "git.status", "git.diff"})
        self.assertEqual(specs["fs.read"].risk.value, "read_only")
        self.assertEqual(specs["process.run"].risk.value, "workspace_write")
        catalog = {item["name"]: item for item in coding_tool_catalog()}
        self.assertEqual(catalog["fs.write"]["risk"], "workspace_write")
        self.assertIn("input_schema", catalog["fs.write"])

    def test_write_read_and_optimistic_replace(self) -> None:
        created = self.invoke("fs.write", path="hello.txt", content="hello world")
        read = self.invoke("fs.read", path="hello.txt")
        self.assertEqual(read["content"], "hello world")
        self.assertEqual(read["sha256"], created["sha256"])
        replaced = self.invoke("fs.replace", path="hello.txt", old="world", new="Zen", expected_sha256=read["sha256"])
        self.assertNotEqual(replaced["sha256"], read["sha256"])
        self.assertEqual((self.root / "hello.txt").read_text(), "hello Zen")
        with self.assertRaisesRegex(WorkspaceError, "changed since"):
            self.invoke("fs.replace", path="hello.txt", old="Zen", new="agent", expected_sha256=read["sha256"])

    def test_overwrite_requires_matching_hash(self) -> None:
        (self.root / "value.txt").write_text("old")
        with self.assertRaisesRegex(WorkspaceError, "required"):
            self.invoke("fs.write", path="value.txt", content="new")
        digest = self.invoke("fs.read", path="value.txt")["sha256"]
        self.invoke("fs.write", path="value.txt", content="new", expected_sha256=digest)
        self.assertEqual((self.root / "value.txt").read_text(), "new")
        self.invoke("fs.write", path="created.txt", content="created", expected_sha256=None)

    def test_traversal_absolute_and_symlink_escape_are_rejected(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside.txt"
        outside.write_text("secret")
        self.addCleanup(outside.unlink, missing_ok=True)
        (self.root / "escape").symlink_to(outside)
        workspace = Workspace(self.root)
        for path in ("../outside", str(outside), "escape"):
            with self.subTest(path=path), self.assertRaises(WorkspaceError):
                workspace.file(path)
        with self.assertRaises(WorkspaceError):
            self.invoke("fs.write", path="escape", content="overwrite", expected_sha256="0" * 64)

    def test_list_and_literal_search(self) -> None:
        (self.root / "src").mkdir()
        (self.root / "src" / "one.py").write_text("alpha\nneedle here\n")
        (self.root / "src" / "two.py").write_text("needle too\n")
        listed = self.invoke("fs.list", path="src", recursive=True)
        self.assertEqual([item["path"] for item in listed["entries"]], ["src/one.py", "src/two.py"])
        searched = self.invoke("fs.search", pattern="needle", path="src", limit=1)
        self.assertEqual(len(searched["matches"]), 1)
        self.assertTrue(searched["truncated"])
        self.assertEqual(searched["matches"][0]["line"], 2)

    def test_process_run_does_not_invoke_shell_and_is_bounded(self) -> None:
        literal = self.invoke("process.run", argv=[sys.executable, "-c", "import sys; print(sys.argv[1])", "$(touch pwned)"], timeout_seconds=2)
        self.assertEqual(literal["returncode"], 0)
        self.assertIn("$(touch pwned)", literal["stdout"])
        self.assertFalse((self.root / "pwned").exists())
        timed = self.invoke("process.run", argv=[sys.executable, "-c", "import time; time.sleep(2)"], timeout_seconds=1)
        self.assertTrue(timed["timed_out"])
        bounded = self.invoke("process.run", argv=[sys.executable, "-c", "print('x' * 10000)"], max_output_bytes=1024)
        self.assertTrue(bounded["truncated"])
        self.assertLessEqual(len(bounded["stdout"].encode()), 1024)
        with self.assertRaises(WorkspaceError):
            self.invoke("process.run", argv=["true"], cwd="..")

    def test_git_status_and_diff(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.root, check=True)
        (self.root / "tracked.txt").write_text("before\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=self.root, check=True)
        (self.root / "tracked.txt").write_text("after\n")
        status = self.invoke("git.status")
        self.assertIn("tracked.txt", status["status"])
        diff = self.invoke("git.diff")
        self.assertIn("-before", diff["diff"])
        self.assertIn("+after", diff["diff"])

    def test_git_cannot_discover_repository_above_workspace(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        child = self.root / "child"
        child.mkdir()
        child_context = ToolContext("run", "task", child)
        with self.assertRaisesRegex(WorkspaceError, "escapes workspace"):
            self.tools.invoke("git.status", child_context, {})


if __name__ == "__main__":
    unittest.main()
