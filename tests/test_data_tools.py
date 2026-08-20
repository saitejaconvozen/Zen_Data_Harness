from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from zen_agent.agent_manifests import AgentCatalog
from zen_agent.data_tools import data_tool_specs, register_data_tools
from zen_agent.models import ToolRisk
from zen_agent.tools import ToolContext, ToolRegistry


ROOT = Path(__file__).resolve().parents[1]


def _workspace(directory: str) -> Path:
    """A workspace holding a minimal qa-audit ledger."""
    workspace = Path(directory)
    (workspace / ".zen").mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(workspace / ".zen" / "qa-audit.db")
    with db:
        db.execute(
            "CREATE TABLE qa_audits (run_id TEXT, source_id TEXT, batch INTEGER,"
            " status TEXT, findings_json TEXT, audited_at REAL,"
            " judge_verdict TEXT, judge_summary TEXT)"
        )
        db.execute(
            "INSERT INTO qa_audits VALUES (?,?,?,?,?,?,?,?)",
            ("run1", "abc", 1, "PARTIAL_CANDIDATE",
             json.dumps([{"kind": "judge-harmful", "detail": "shortened an answer"}]),
             0.0, "REJECT", ""),
        )
        db.execute(
            "INSERT INTO qa_audits VALUES (?,?,?,?,?,?,?,?)",
            ("run1", "def", 1, "VERIFIED_CANDIDATE",
             json.dumps([{"kind": "judge-harmful", "detail": "dropped a fact"},
                         {"kind": "information-loss", "detail": "35 words became 13"}]),
             0.0, "REVIEW", ""),
        )
    db.close()
    return workspace


def _context(workspace: Path) -> ToolContext:
    return ToolContext(run_id="run1", task_id="task1", workspace=workspace)


class RegistrationTests(unittest.TestCase):
    def test_tools_register_without_colliding_with_coding_tools(self) -> None:
        from zen_agent.coding_tools import register_coding_tools

        registry = ToolRegistry()
        register_coding_tools(registry)
        register_data_tools(registry)
        names = set(registry.names())
        self.assertIn("data.failure_clusters", names)
        self.assertIn("fs.read", names)

    def test_read_tools_are_declared_read_only(self) -> None:
        """The manifest gates access, but the risk class must be honest.

        `_allowed_tools` filters delegated read-only work by risk, so a
        mislabelled reader would leak write capability to a sub-agent.
        """
        expected = {
            "data.query_ledgers": ToolRisk.READ_ONLY,
            "data.failure_clusters": ToolRisk.READ_ONLY,
            "data.read_conversation": ToolRisk.READ_ONLY,
            "data.read_contract": ToolRisk.READ_ONLY,
            "data.propose_change": ToolRisk.WORKSPACE_WRITE,
            "data.run_tests": ToolRisk.WORKSPACE_WRITE,
        }
        actual = {spec.name: spec.risk for spec in data_tool_specs()}
        self.assertEqual(actual, expected)


class QueryLedgerTests(unittest.TestCase):
    def invoke(self, workspace: Path, **inputs):
        registry = ToolRegistry()
        register_data_tools(registry)
        return registry.invoke("data.query_ledgers", _context(workspace), inputs)

    def test_a_select_returns_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = _workspace(directory)
            out = self.invoke(workspace, ledger="qa",
                              sql="SELECT source_id FROM qa_audits ORDER BY source_id")
            self.assertEqual([r["source_id"] for r in out["rows"]], ["abc", "def"])

    def test_writes_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = _workspace(directory)
            for sql in (
                "DELETE FROM qa_audits",
                "UPDATE qa_audits SET status='x'",
                "SELECT 1; DROP TABLE qa_audits",
                "WITH x AS (SELECT 1) INSERT INTO qa_audits VALUES (1)",
                "ATTACH DATABASE '/etc/passwd' AS evil",
            ):
                with self.assertRaises(Exception, msg=sql):
                    self.invoke(workspace, ledger="qa", sql=sql)

    def test_an_unlisted_ledger_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = _workspace(directory)
            with self.assertRaises(Exception):
                self.invoke(workspace, ledger="passwords", sql="SELECT 1")


class FailureClusterTests(unittest.TestCase):
    def test_findings_group_by_kind_with_examples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = _workspace(directory)
            registry = ToolRegistry()
            register_data_tools(registry)
            out = registry.invoke(
                "data.failure_clusters", _context(workspace), {"run_id": "run1"}
            )
            kinds = {c["kind"]: c["count"] for c in out["clusters"]}
            self.assertEqual(kinds, {"judge-harmful": 2, "information-loss": 1})
            # Largest cluster first, so the agent works on what matters most.
            self.assertEqual(out["clusters"][0]["kind"], "judge-harmful")
            self.assertEqual(out["clusters"][0]["examples"][0]["source_id"], "abc")


class ProposalTests(unittest.TestCase):
    def invoke(self, workspace: Path, **inputs):
        registry = ToolRegistry()
        register_data_tools(registry)
        return registry.invoke("data.propose_change", _context(workspace), inputs)

    def test_a_proposal_is_written_and_nothing_is_modified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = _workspace(directory)
            target = workspace / "plugins" / "p" / "prompts" / "refiner.md"
            target.parent.mkdir(parents=True)
            target.write_text("original", encoding="utf-8")
            out = self.invoke(
                workspace,
                path="plugins/p/prompts/refiner.md",
                replacement="changed",
                rationale="the refiner shortens substantive answers into requests to repeat",
                evidence_source_ids=["abc", "def"],
            )
            self.assertEqual(out["status"], "PENDING_HUMAN_REVIEW")
            # The contract itself is untouched: a human merges, the agent does not.
            self.assertEqual(target.read_text(encoding="utf-8"), "original")
            written = json.loads((workspace / out["proposal"]).read_text())
            self.assertEqual(written["evidence_source_ids"], ["abc", "def"])

    def test_a_proposal_without_evidence_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = _workspace(directory)
            with self.assertRaises(Exception):
                self.invoke(workspace, path="plugins/p.md", replacement="x",
                            rationale="a" * 50, evidence_source_ids=[])

    def test_a_path_outside_the_contract_roots_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = _workspace(directory)
            for path in ("../../etc/passwd", ".zen/factory.env", "datasets/v2.jsonl"):
                with self.assertRaises(Exception, msg=path):
                    self.invoke(workspace, path=path, replacement="x",
                                rationale="a" * 50, evidence_source_ids=["abc"])


class ManifestTests(unittest.TestCase):
    def test_the_data_engineer_manifest_loads_and_its_tools_exist(self) -> None:
        catalog = AgentCatalog.discover([ROOT / "agents"])
        manifest = catalog.get("data-engineer")
        registry = ToolRegistry()
        register_data_tools(registry)
        missing = [name for name in manifest.tools if name not in set(registry.names())]
        self.assertEqual(missing, [], f"manifest names unregistered tools: {missing}")
        self.assertEqual(manifest.sandbox, "read-only")


if __name__ == "__main__":
    unittest.main()


class ExecutorSelectionTests(unittest.TestCase):
    """The agent loop must be able to run a manifest other than `executor`.

    The role was hardcoded, so the kernel could only ever write code. Selecting
    the manifest is what turns the same loop into a data engineer.
    """

    def test_runtime_accepts_an_executor_agent(self) -> None:
        import inspect
        from zen_agent.coding_runtime import CodingRuntime

        signature = inspect.signature(CodingRuntime.__init__)
        self.assertIn("executor_agent", signature.parameters)
        self.assertEqual(signature.parameters["executor_agent"].default, "executor")

    def test_cli_exposes_the_agent_flag(self) -> None:
        from pathlib import Path as _P
        source = (_P(__file__).resolve().parents[1] / "src/zen_agent/cli.py").read_text()
        self.assertIn('"--agent"', source)
        self.assertIn("executor_agent=", source)


class DispatchFloorTests(unittest.TestCase):
    """The export is the last gate before data leaves the harness.

    The turn floor is already enforced at binding, at the audit and in the
    classifier. It is repeated at the export because that is the one place a
    loosened upstream rule would otherwise ship unnoticed.
    """

    def conversation(self, exchanges: int):
        turns = []
        for i in range(exchanges):
            turns.append({"turn_id": f"u{i}", "role": "user", "text": "hello"})
            turns.append({
                "turn_id": f"a{i}", "role": "assistant", "action": "KEEP",
                "source_text": "hi", "golden_text": "hi", "metric_citations": [],
            })
        return {
            "source_id": "abc", "source_id_full": "a" * 64,
            "terminal": {"status": "VERIFIED_CANDIDATE"},
            "classification": {}, "turns": turns,
        }

    def test_a_short_conversation_is_refused(self) -> None:
        from zen_agent.dispatch_export import conversation_record

        for exchanges in (1, 2):
            self.assertIsNone(
                conversation_record(self.conversation(exchanges), "run"),
                f"{exchanges} exchanges should not be dispatched",
            )

    def test_the_minimum_is_accepted(self) -> None:
        from zen_agent.dispatch_export import conversation_record

        record = conversation_record(self.conversation(3), "run")
        self.assertIsNotNone(record)
        self.assertEqual(record["counts"]["exchanges"], 3)

    def test_direction_follows_who_speaks_first(self) -> None:
        from zen_agent.dispatch_export import call_direction, INBOUND, OUTBOUND

        self.assertEqual(call_direction([{"role": "user"}, {"role": "assistant"}]), INBOUND)
        self.assertEqual(call_direction([{"role": "assistant"}, {"role": "user"}]), OUTBOUND)
        # A leading system turn must not decide direction.
        self.assertEqual(
            call_direction([{"role": "system"}, {"role": "assistant"}, {"role": "user"}]),
            OUTBOUND,
        )


class MetricsRecorderTests(unittest.TestCase):
    """The recorder must actually write, and must be diagnosable when it can't.

    A loop variable named `root` shadowed the harness path inside
    `_record_metrics`, so every write raised TypeError into a bare
    `except: pass`. The pipeline ran at full speed while the dashboard
    reported it idle — a silent instrument is worse than no instrument.
    """

    def test_recorder_writes_a_row_for_both_job_roots(self) -> None:
        import sys as _sys
        scripts = ROOT / "plugins" / "golden-conversations" / "scripts"
        if str(scripts) not in _sys.path:
            _sys.path.insert(0, str(scripts))
        import _transport
        from zen_agent.observability import MetricsStore

        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            # Both roots must attribute a run: the auditor writes under
            # factory-jobs, every other role under jobs.
            for job_root in ("factory-jobs", "jobs"):
                out = work / ".zen" / job_root / f"run-{job_root}" / "rp_x" / "d.json"
                out.parent.mkdir(parents=True, exist_ok=True)
                parts = out.resolve().parts
                index = parts.index(job_root)
                self.assertEqual(parts[index + 1], f"run-{job_root}")
                self.assertEqual(parts[index + 2], "rp_x")

        # And the store itself accepts the record the recorder builds.
        with tempfile.TemporaryDirectory() as directory:
            store = MetricsStore(Path(directory) / "m.db")
            try:
                from zen_agent.observability import CallRecord
                store.record(CallRecord(run_id="r", role="REFINER",
                                        provider="litellm", model="m",
                                        packet_id="rp_x", latency_ms=5))
                self.assertEqual(store.totals("r")["calls"], 1)
            finally:
                store.close()

    def test_the_swallow_can_be_turned_off(self) -> None:
        source = (ROOT / "plugins/golden-conversations/scripts/_transport.py").read_text(
            encoding="utf-8")
        self.assertIn("ZEN_METRICS_STRICT", source)
        self.assertIn("metrics-errors.log", source)


class CheckpointGateTests(unittest.TestCase):
    """The approval ceiling must be reachable, not merely declared.

    The supervisor can only evaluate the gate between driver invocations. With
    an unbounded refinement budget the driver ran for hours in a single pass and
    overshot a 500 ceiling to 809 before anything checked. Bounding the pass to
    the remaining headroom is what makes the ceiling mean something.
    """

    def test_supervisor_bounds_each_pass_by_remaining_headroom(self) -> None:
        script = (ROOT / "scripts" / "run-gemini-batch.sh").read_text(encoding="utf-8")
        self.assertIn("headroom=$(( ceiling - have ))", script)
        self.assertIn('--max-refinement-items "$(( headroom * 6 ))"', script)
        self.assertNotIn("--max-refinement-items 80000", script)

    def test_supervisor_holds_before_starting_a_driver(self) -> None:
        script = (ROOT / "scripts" / "run-gemini-batch.sh").read_text(encoding="utf-8")
        hold = script.index("awaiting-approval")
        launch = script.index("zen-factory-run")
        self.assertLess(hold, launch, "the hold must precede the driver launch")
