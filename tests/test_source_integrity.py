from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TREES = ("src", "plugins", "tests")


def _python_files():
    for tree in TREES:
        for path in sorted((ROOT / tree).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


class SourceParsesTests(unittest.TestCase):
    """Every shipped Python file must parse.

    Worker scripts are launched as subprocesses, so a syntax error in one does
    not surface until a live run dead-letters against it. That happened: three
    stray dict entries were spliced into an `if` body in run_agent_auditor.py,
    and the whole agent-audit stage failed only once real work hit it.
    """

    def test_every_python_file_parses(self) -> None:
        broken = []
        for path in _python_files():
            source = path.read_text(encoding="utf-8")
            try:
                ast.parse(source, filename=str(path))
            except SyntaxError as exc:
                broken.append(f"{path.relative_to(ROOT)}:{exc.lineno}: {exc.msg}")
        self.assertEqual(broken, [], "unparseable source files: " + "; ".join(broken))

    def test_worker_scripts_expose_a_main(self) -> None:
        scripts = sorted((ROOT / "plugins").rglob("scripts/*.py"))
        self.assertTrue(scripts, "no plugin worker scripts found")
        missing = []
        for path in scripts:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            names = {
                node.name
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            if "main" not in names:
                missing.append(str(path.relative_to(ROOT)))
        self.assertEqual(missing, [], "worker scripts without main(): " + "; ".join(missing))


if __name__ == "__main__":
    unittest.main()
