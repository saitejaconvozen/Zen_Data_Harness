from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from zen_agent.factory_acceptance import (
    AcceptanceReportError,
    evaluate_acceptance,
    evaluate_review_document,
    render_markdown,
    validate_run_id,
    write_report_bundle,
)
from zen_agent.factory_acceptance_cli import main as acceptance_cli_main


RUN_ID = "a530bc321a624eec871fa02bcda93509"


def complete_evidence() -> dict[str, object]:
    return {
        "target_count": 2,
        "selected_count": 2,
        "quality_qualified_selected_count": 2,
        "total_count": 2,
        "terminal_count": 2,
        "source_user_turns_preserved_count": 4,
        "source_user_turns_checked_count": 4,
        "complete_independent_citation_count": 2,
        "citation_required_count": 2,
        "queue_dead_count": 1,
        "queue_failure_count": 1,
        "queue_dead_explained_count": 1,
        "queue_failure_explained_count": 1,
        "verified_count": 1,
        "quarantined_count": 1,
        "human_review_required_count": 2,
        "human_review_completed_count": 2,
        "languages": ["en", "es"],
        "domains": ["support", "travel"],
        "code_switch_count": 1,
        "minimum_languages": 2,
        "minimum_domains": 2,
        "minimum_code_switch_count": 1,
    }


class FactoryAcceptanceTests(unittest.TestCase):
    def test_all_acceptance_gates_pass_with_complete_evidence(self) -> None:
        report = evaluate_acceptance(complete_evidence(), run_id=RUN_ID)

        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(
            {gate["status"] for gate in report["gates"].values()},
            {"PASS"},
        )

    def test_material_gate_defects_fail_closed(self) -> None:
        cases = (
            ({"quality_qualified_selected_count": 1}, "target_selected"),
            ({"terminal_count": 1}, "terminalization"),
            ({"source_user_turns_preserved_count": 3}, "source_user_turn_preservation"),
            ({"complete_independent_citation_count": 1}, "independent_taxonomy_citations"),
            ({"queue_dead_explained_count": 0}, "queue_integrity"),
            ({"languages": ["en"]}, "diversity"),
            ({"verified_count": 0}, "verified_quarantined_yield"),
        )
        for changes, gate in cases:
            with self.subTest(gate=gate):
                evidence = complete_evidence()
                evidence.update(changes)

                report = evaluate_acceptance(evidence, run_id=RUN_ID)

                self.assertEqual(report["verdict"], "FAIL")
                self.assertEqual(report["gates"][gate]["status"], "FAIL")

    def test_pending_required_reviews_need_human(self) -> None:
        evidence = complete_evidence()
        evidence["human_review_completed_count"] = 1

        report = evaluate_acceptance(evidence, run_id=RUN_ID)

        self.assertEqual(report["verdict"], "NEEDS_HUMAN")
        gate = report["gates"]["human_review_coverage"]
        self.assertEqual(gate["status"], "NEEDS_HUMAN")
        self.assertEqual(gate["metrics"]["pending_count"], 1)

    def test_pending_reviews_take_precedence_without_hiding_fail_gates(self) -> None:
        evidence = complete_evidence()
        evidence["human_review_required_count"] = 91
        evidence["human_review_completed_count"] = 0
        evidence["terminal_count"] = 1

        report = evaluate_acceptance(evidence, run_id=RUN_ID)

        self.assertEqual(report["verdict"], "NEEDS_HUMAN")
        self.assertEqual(report["gates"]["terminalization"]["status"], "FAIL")
        review_gate = report["gates"]["human_review_coverage"]
        self.assertEqual(review_gate["status"], "NEEDS_HUMAN")
        self.assertEqual(review_gate["metrics"]["pending_count"], 91)

    def test_user_turn_proof_equality_tampering_and_unavailable_fail_closed(self) -> None:
        protected_text = "protected-user-turn-sentinel"

        def document_with(source_value: object, current_value: object) -> dict[str, object]:
            turn = {"role": "user"}
            if source_value is not None:
                turn["source_text"] = source_value
            if current_value is not None:
                turn["text"] = current_value
            return {
                "run_id": RUN_ID,
                "counts": {
                    "conversations": 1,
                    "queue": [{"status": "SUCCEEDED", "count": 1}],
                },
                "conversations": [
                    {
                        "number": 1,
                        "terminal": {"status": "VERIFIED_CANDIDATE"},
                        "turns": [turn],
                        "classification": {
                            "primary_language": "en",
                            "domain": "support",
                            "code_switching": False,
                        },
                        "iterations": [
                            {
                                "refiner_session_id": "refiner-session",
                                "verifier_session_id": "verifier-session",
                                "verifier_decision": "PASS",
                            }
                        ],
                        "human_review": {"latest_decision": None},
                    }
                ],
                "metric_coverage": [
                    {
                        "conversation_number": 1,
                        "axis_id": "axis",
                        "subaxis_id": "subaxis",
                        "variant_id": "variant",
                        "evidence_turn_ids": ["turn-1"],
                    }
                ],
            }

        cases = (
            (protected_text, protected_text, "PASS"),
            (protected_text, protected_text + "-tampered", "FAIL"),
            (protected_text, None, "FAIL"),
            (None, protected_text, "FAIL"),
        )
        for source_value, current_value, expected_status in cases:
            with self.subTest(expected_status=expected_status, current=current_value):
                report = evaluate_review_document(
                    document_with(source_value, current_value), run_id=RUN_ID
                )
                self.assertEqual(
                    report["gates"]["source_user_turn_preservation"]["status"],
                    expected_status,
                )
                serialized = json.dumps(report, sort_keys=True)
                markdown = render_markdown(report).decode("utf-8")
                self.assertNotIn(protected_text, serialized)
                self.assertNotIn(protected_text, markdown)

    def test_independence_provenance_and_abstain_yield(self) -> None:
        def report_for(iteration: dict[str, object]) -> dict[str, object]:
            document = {
                "run_id": RUN_ID,
                "counts": {
                    "conversations": 1,
                    "queue": [{"status": "SUCCEEDED", "count": 1}],
                },
                "conversations": [
                    {
                        "number": 1,
                        "terminal": {"status": "VERIFIED_CANDIDATE"},
                        "turns": [
                            {"role": "user", "source_text": "same", "text": "same"}
                        ],
                        "classification": {
                            "primary_language": "en",
                            "domain": "support",
                            "code_switching": False,
                        },
                        "iterations": [iteration],
                        "human_review": {"latest_decision": "APPROVE"},
                    }
                ],
                "metric_coverage": [
                    {
                        "conversation_number": 1,
                        "axis_id": "axis",
                        "subaxis_id": "subaxis",
                        "variant_id": "variant",
                        "evidence_turn_ids": ["turn-1"],
                    }
                ],
            }
            return evaluate_review_document(document, run_id=RUN_ID)

        independent_cases = (
            {
                "refiner_session_id": "refiner-session",
                "verifier_session_id": "verifier-session",
                "verifier_decision": "PASS",
            },
            {
                "refiner_agent_id": "refiner-agent",
                "verifier_agent_id": "verifier-agent",
                "verifier_decision": "PASS",
            },
        )
        for iteration in independent_cases:
            with self.subTest(independent=iteration):
                report = report_for(iteration)
                self.assertEqual(
                    report["gates"]["independent_taxonomy_citations"]["status"],
                    "PASS",
                )
                self.assertEqual(
                    report["gates"]["verified_quarantined_yield"]["metrics"]["verified_count"],
                    1,
                )

        non_independent_cases = (
            {"verifier_decision": "PASS"},
            {
                "refiner_session_id": "same-session",
                "verifier_session_id": "same-session",
                "verifier_decision": "PASS",
            },
            {
                "refiner_session_id": "refiner-session",
                "verifier_session_id": "verifier-session",
                "verifier_decision": "ABSTAIN",
            },
        )
        for iteration in non_independent_cases:
            with self.subTest(non_independent=iteration):
                report = report_for(iteration)
                self.assertEqual(
                    report["gates"]["independent_taxonomy_citations"]["status"],
                    "NEEDS_HUMAN",
                )
                self.assertEqual(
                    report["gates"]["verified_quarantined_yield"]["metrics"]["verified_count"],
                    0,
                )
                self.assertEqual(
                    report["gates"]["verified_quarantined_yield"]["status"],
                    "FAIL",
                )

    def test_protected_fields_and_invalid_run_ids_are_rejected(self) -> None:
        evidence = complete_evidence()
        evidence["source_text"] = "not serialized"

        with self.assertRaisesRegex(AcceptanceReportError, "protected field"):
            evaluate_acceptance(evidence, run_id=RUN_ID)
        for invalid in ("../escape", "A" * 32, "0" * 31, "0" * 33):
            with self.subTest(run_id=invalid):
                with self.assertRaises(AcceptanceReportError):
                    validate_run_id(invalid)

    def test_review_document_aggregates_observed_schema_evidence(self) -> None:
        document = {
            "run_id": RUN_ID,
            "counts": {
                "conversations": 2,
                "queue": [{"status": "SUCCEEDED", "count": 2}],
            },
            "conversations": [
                {
                    "number": 1,
                    "terminal": {"status": "VERIFIED_CANDIDATE"},
                    "turns": [
                        {
                            "role": "user",
                            "source_text": "protected fixture user turn one",
                            "text": "protected fixture user turn one",
                        }
                    ],
                    "classification": {
                        "primary_language": "en",
                        "domain": "support",
                        "code_switching": False,
                    },
                    "iterations": [
                        {
                            "proposal_role": "refiner",
                            "refiner_session_id": "refiner-session-1",
                            "verifier_session_id": "verifier-session-1",
                            "verifier_decision": "PASS",
                        }
                    ],
                    "human_review": {"latest_decision": None},
                },
                {
                    "number": 2,
                    "terminal": {"status": "QUARANTINED"},
                    "turns": [
                        {
                            "role": "user",
                            "source_text": "protected fixture user turn two",
                            "text": "protected fixture user turn two",
                        }
                    ],
                    "classification": {
                        "primary_language": "es",
                        "domain": "travel",
                        "code_switching": True,
                    },
                    "iterations": [
                        {"proposal_role": "refiner", "verifier_decision": "ABSTAIN"}
                    ],
                    "human_review": {"latest_decision": "QUARANTINE"},
                },
            ],
            "metric_coverage": [
                {
                    "conversation_number": number,
                    "axis_id": "axis",
                    "subaxis_id": "subaxis",
                    "variant_id": "variant",
                    "evidence_turn_ids": [f"turn-{number}"],
                }
                for number in (1, 2)
            ],
        }

        report = evaluate_review_document(document, run_id=RUN_ID)

        self.assertEqual(report["verdict"], "NEEDS_HUMAN")
        gates = report["gates"]
        self.assertEqual(gates["target_selected"]["status"], "NEEDS_HUMAN")
        self.assertEqual(gates["diversity"]["status"], "NEEDS_HUMAN")
        for name in (
            "terminalization",
            "source_user_turn_preservation",
            "queue_integrity",
            "verified_quarantined_yield",
        ):
            self.assertEqual(gates[name]["status"], "PASS")
        self.assertEqual(
            gates["independent_taxonomy_citations"]["status"],
            "NEEDS_HUMAN",
        )
        self.assertEqual(
            gates["independent_taxonomy_citations"]["metrics"],
            {
                "required_count": 2,
                "structurally_complete_count": 2,
                "complete_count": 1,
                "missing_independent_provenance_count": 1,
            },
        )
        self.assertEqual(
            gates["diversity"]["metrics"],
            {
                "language_count": 2,
                "domain_count": 2,
                "code_switch_count": 1,
                "minimums_available": False,
            },
        )
        self.assertEqual(
            gates["human_review_coverage"]["metrics"]["pending_count"],
            1,
        )

    def test_output_is_deterministic_sanitized_and_json_markdown_parity_holds(self) -> None:
        report = evaluate_acceptance(complete_evidence(), run_id=RUN_ID)
        first_json = json.dumps(report, indent=2, sort_keys=True) + "\n"
        second_json = json.dumps(
            evaluate_acceptance(complete_evidence(), run_id=RUN_ID),
            indent=2,
            sort_keys=True,
        ) + "\n"
        first_markdown = render_markdown(report)
        second_markdown = render_markdown(
            evaluate_acceptance(complete_evidence(), run_id=RUN_ID)
        )

        self.assertEqual(first_json, second_json)
        self.assertEqual(first_markdown, second_markdown)
        markdown_text = first_markdown.decode("utf-8")
        self.assertIn(f"Verdict: **{report['verdict']}**", markdown_text)
        for gate_name, gate in report["gates"].items():
            self.assertIn(
                f"| {gate_name} | {gate['status']} |",
                markdown_text,
            )
        for sensitive_name in (
            "source_text",
            "text",
            "prompt",
            "messages",
            "transcript",
        ):
            self.assertNotIn(sensitive_name, first_json)
            self.assertNotIn(sensitive_name, markdown_text)

    def test_cli_accepts_regular_file_and_reports_pending_review(self) -> None:
        protected_text = "cli-protected-user-turn-sentinel"
        document = {
            "schema_version": "zen.factory-golden-review/1",
            "run_id": RUN_ID,
            "counts": {
                "conversations": 1,
                "queue": [{"status": "SUCCEEDED", "count": 1}],
                "terminal": {"VERIFIED_CANDIDATE": 1},
            },
            "conversations": [
                {
                    "number": 1,
                    "source_id": "source-1",
                    "terminal": {"status": "VERIFIED_CANDIDATE"},
                    "turns": [
                        {
                            "role": "user",
                            "source_text": protected_text,
                            "text": protected_text,
                        }
                    ],
                    "classification": {
                        "primary_language": "en",
                        "other_languages": [],
                        "domain": "support",
                        "code_switching": False,
                    },
                    "iterations": [
                        {
                            "refiner_session_id": "refiner-session",
                            "verifier_session_id": "verifier-session",
                            "verifier_decision": "PASS",
                        }
                    ],
                    "human_review": {
                        "state": "REVIEW_PENDING",
                        "latest_decision": None,
                    },
                }
            ],
            "metric_coverage": [
                {
                    "source_id": "source-1",
                    "conversation_number": 1,
                    "axis_id": "axis",
                    "subaxis_id": "subaxis",
                    "variant_id": "variant",
                    "evidence_turn_ids": ["turn-1"],
                    "missing_evidence": [],
                }
            ],
        }
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory(dir=original_cwd) as temporary_directory:
            try:
                os.chdir(temporary_directory)
                review_path = Path("review.json")
                review_path.write_text(json.dumps(document), encoding="utf-8")
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = acceptance_cli_main(
                        [RUN_ID, "--review", str(review_path)]
                    )

                self.assertEqual(exit_code, 2)
                self.assertEqual(stderr.getvalue(), "")
                summary = json.loads(stdout.getvalue())
                self.assertEqual(summary["run_id"], RUN_ID)
                self.assertEqual(summary["verdict"], "NEEDS_HUMAN")
                self.assertNotIn(protected_text, stdout.getvalue())
                report_path = Path(summary["artifacts"]["json"])
                report_text = report_path.read_text(encoding="utf-8")
                self.assertNotIn(protected_text, report_text)
                self.assertEqual(json.loads(report_text)["verdict"], "NEEDS_HUMAN")
            finally:
                os.chdir(original_cwd)

    def test_cli_rejects_traversal_and_symlink_inputs(self) -> None:
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory(dir=original_cwd) as temporary_directory:
            try:
                os.chdir(temporary_directory)
                regular = Path("review.json")
                regular.write_text("{}", encoding="utf-8")
                symlink = Path("review-link.json")
                symlink.symlink_to(regular.name)
                for review_path in ("../review.json", str(symlink)):
                    with self.subTest(review_path=review_path):
                        stdout = io.StringIO()
                        stderr = io.StringIO()
                        with redirect_stdout(stdout), redirect_stderr(stderr):
                            exit_code = acceptance_cli_main(
                                [RUN_ID, "--review", review_path]
                            )
                        self.assertEqual(exit_code, 3)
                        self.assertEqual(stdout.getvalue(), "")
                        self.assertIn("acceptance reporter error:", stderr.getvalue())
            finally:
                os.chdir(original_cwd)

    def test_report_bundle_is_owner_only_and_hashes_recompute(self) -> None:
        report = evaluate_acceptance(complete_evidence(), run_id=RUN_ID)
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            try:
                os.chdir(temporary_directory)
                artifacts = write_report_bundle(report)

                for key in ("json", "markdown", "hash_manifest"):
                    path = Path(artifacts[key])
                    self.assertTrue(path.is_file())
                    self.assertFalse(path.is_symlink())
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    expected = artifacts[f"{key}_sha256"]
                    self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected)
                output_dir = Path(artifacts["json"]).parent
                self.assertEqual(stat.S_IMODE(output_dir.stat().st_mode), 0o700)
                manifest = json.loads(
                    Path(artifacts["hash_manifest"]).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    manifest["files"]["acceptance.json"],
                    artifacts["json_sha256"],
                )
                self.assertEqual(
                    manifest["files"]["acceptance.md"],
                    artifacts["markdown_sha256"],
                )
            finally:
                os.chdir(original_cwd)


if __name__ == "__main__":
    unittest.main()
