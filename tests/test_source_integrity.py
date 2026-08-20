from __future__ import annotations

import ast
import builtins
import re
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
        # Modules whose name starts with "_" are shared helpers imported by the
        # worker scripts, not entry points the plugin layer launches.
        scripts = [
            path
            for path in sorted((ROOT / "plugins").rglob("scripts/*.py"))
            if not path.name.startswith("_")
        ]
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


class WorkerScriptImportTests(unittest.TestCase):
    """Every name a worker script uses at module scope must be imported.

    Worker scripts run as subprocesses, so a missing import surfaces only when
    live work hits it — 27 audits dead-lettered on `NameError: name 'sys' is
    not defined` before anyone noticed.
    """

    def test_scripts_that_touch_sys_path_import_sys(self) -> None:
        broken = []
        for path in sorted((ROOT / "plugins").rglob("scripts/*.py")):
            if "__pycache__" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            if "sys.path" in source and not re.search(r"^import sys$", source, re.M):
                broken.append(str(path.relative_to(ROOT)))
        self.assertEqual(broken, [], "scripts using sys without importing it: " + "; ".join(broken))

    def test_scripts_importing_the_shared_transport_can_find_it(self) -> None:
        """`_transport` lives with the golden-conversations workers.

        A script in another plugin that inserts only its own directory on
        sys.path fails at runtime with ModuleNotFoundError — 29 audits died
        that way.
        """
        transport = ROOT / "plugins" / "golden-conversations" / "scripts"
        broken = []
        for path in sorted((ROOT / "plugins").rglob("scripts/*.py")):
            if "__pycache__" in path.parts or path.parent == transport:
                continue
            source = path.read_text(encoding="utf-8")
            if "from _transport import" not in source:
                continue
            if "golden-conversations" not in source:
                broken.append(str(path.relative_to(ROOT)))
        self.assertEqual(
            broken, [], "scripts import _transport without reaching it: " + "; ".join(broken)
        )

    def test_terminal_status_enum_covers_every_status_the_worker_emits(self) -> None:
        """The graph schema and the classifier must agree on the status set.

        A status the worker can emit but the schema rejects dead-letters the
        conversation at the very last stage, after all its model calls are paid
        for — which is exactly what NOT_SELECTED did.
        """
        worker = (ROOT / "src/zen_agent/factory_worker.py").read_text(encoding="utf-8")
        graph = (ROOT / "plugins/golden-graph/plugin.py").read_text(encoding="utf-8")
        emitted = set(re.findall(r'_enqueue_terminal\(\s*\n?\s*run_id, work, "([A-Z_]+)"', worker))
        emitted |= set(re.findall(r'return "([A-Z_]+)", ', worker)) - {"REPAIR"}
        enum_line = re.search(r'"terminal_status"\] = \{[^}]*"enum": \[([^\]]*)\]', graph)
        self.assertIsNotNone(enum_line, "terminal_status enum not found")
        allowed = set(re.findall(r'"([A-Z_]+)"', enum_line.group(1)))
        missing = sorted(emitted - allowed)
        self.assertEqual(missing, [], f"worker emits statuses the schema rejects: {missing}")

    def test_worker_scripts_reference_no_undefined_local_names(self) -> None:
        """Catch NameErrors that only surface when live work hits the script.

        Two shipped this way — `output_path` and `schema_path`, each left behind
        by a refactor that removed the variable but not its use. Neither is
        visible until a subprocess runs, and both dead-lettered real work after
        its model calls were already paid for.
        """
        # Module dunders and names bound by enclosing scopes (nested functions,
        # methods) are always available; this check is aimed only at plain local
        # references to variables that no longer exist.
        builtins_names = set(dir(builtins)) | {
            "__file__", "__name__", "__doc__", "__package__", "self", "cls",
        }
        broken = []
        for path in sorted((ROOT / "plugins").rglob("scripts/*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            module_level = {
                node.id
                for stmt in tree.body
                for node in ast.walk(stmt)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
            }
            for stmt in tree.body:
                if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                    module_level |= {(a.asname or a.name).split(".")[0] for a in stmt.names}
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    module_level.add(stmt.name)

            for func in ast.walk(tree):
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                bound = set(module_level) | builtins_names
                # Names bound anywhere in an enclosing function are visible here.
                for outer in ast.walk(tree):
                    if isinstance(outer, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if func is not outer and func in ast.walk(outer):
                            for node in ast.walk(outer):
                                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                                    bound.add(node.id)
                            for group in (outer.args.posonlyargs, outer.args.args,
                                          outer.args.kwonlyargs):
                                bound |= {a.arg for a in group}
                args = func.args
                for group in (args.posonlyargs, args.args, args.kwonlyargs):
                    bound |= {a.arg for a in group}
                for extra in (args.vararg, args.kwarg):
                    if extra:
                        bound.add(extra.arg)
                for node in ast.walk(func):
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                        bound.add(node.id)
                    elif isinstance(node, (ast.Import, ast.ImportFrom)):
                        bound |= {(a.asname or a.name).split(".")[0] for a in node.names}
                    elif isinstance(node, ast.ExceptHandler) and node.name:
                        bound.add(node.name)
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        bound.add(node.name)
                    elif isinstance(node, (ast.comprehension,)):
                        for sub in ast.walk(node.target):
                            if isinstance(sub, ast.Name):
                                bound.add(sub.id)
                # Only this function's own scope. Walking into nested defs would
                # report their parameters as undefined here.
                own: list[ast.AST] = []
                stack = list(ast.iter_child_nodes(func))
                while stack:
                    node = stack.pop()
                    own.append(node)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                         ast.ClassDef, ast.Lambda)):
                        continue
                    stack.extend(ast.iter_child_nodes(node))
                for node in own:
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                        if node.id not in bound:
                            broken.append(
                                f"{path.relative_to(ROOT)}:{node.lineno} "
                                f"{func.name}() uses undefined {node.id!r}"
                            )
        self.assertEqual(broken, [], "undefined names: " + "; ".join(sorted(set(broken))))

    def test_transport_only_stamps_fields_a_schema_declares(self) -> None:
        """Fields added to a decision must be permitted by its own schema.

        Three outages came from this: `zen_model_id`, then `decision_id`, each
        stamped on every role while several role schemas set
        additionalProperties=false. The driver then died at startup, not at the
        stage that produced the value.
        """
        transport = (
            ROOT / "plugins/golden-conversations/scripts/_transport.py"
        ).read_text(encoding="utf-8")
        body = transport[transport.index("def run_model("):]
        for field in ("decision_id",):
            if f'decision["{field}"]' in body:
                self.assertIn(
                    "declares_id",
                    body,
                    f"{field} is stamped without checking the schema declares it",
                )
        self.assertNotIn('decision.setdefault("zen_model_id"', body)
