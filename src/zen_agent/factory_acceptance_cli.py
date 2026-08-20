from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from zen_agent.factory_acceptance import (
    AcceptanceReportError,
    evaluate_acceptance,
    evaluate_review_document,
    validate_run_id,
    workspace_path,
    write_report_bundle,
)


EXIT_BY_VERDICT = {"PASS": 0, "FAIL": 1, "NEEDS_HUMAN": 2}
TERMINAL_STATUSES = frozenset(
    {"VERIFIED_CANDIDATE", "PARTIAL_CANDIDATE", "QUARANTINED", "REJECTED_SOURCE",
     "NOT_SELECTED"}
)
# Statuses that yield reviewable golden turns.
CANDIDATE_STATUSES = frozenset({"VERIFIED_CANDIDATE", "PARTIAL_CANDIDATE"})
VERIFIER_DECISIONS = frozenset({"PASS", "FAIL", "ABSTAIN"})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zen-factory-acceptance",
        description="Generate the aggregate Milestone 1 factory acceptance report.",
    )
    parser.add_argument("run_id", help="32-character lowercase hexadecimal factory run ID")
    parser.add_argument(
        "--review",
        required=True,
        help="workspace-relative protected review JSON path",
    )
    return parser


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AcceptanceReportError(f"{name} must be a JSON object")
    return value


def _array(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise AcceptanceReportError(f"{name} must be a JSON array")
    return value


def _count(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AcceptanceReportError(f"{name} must be a non-negative integer")
    return value


def _load_review(path: str | Path) -> Mapping[str, Any]:
    safe_path = workspace_path(path)
    if safe_path.is_symlink() or not safe_path.is_file():
        raise AcceptanceReportError("review input must be a regular non-symlink file")
    try:
        with safe_path.open("r", encoding="utf-8") as stream:
            document = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AcceptanceReportError("review input is not readable valid UTF-8 JSON") from exc
    return _object(document, "review input")


def _identity(iteration: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = iteration.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _has_independent_verifier(conversation: Mapping[str, Any]) -> bool:
    for raw_iteration in _array(conversation.get("iterations"), "conversation iterations"):
        iteration = _object(raw_iteration, "conversation iteration")
        if iteration.get("verifier_decision") != "PASS":
            continue
        proposal_session = _identity(
            iteration, "proposal_session_id", "refiner_session_id"
        )
        verifier_session = _identity(iteration, "verifier_session_id")
        proposal_agent = _identity(
            iteration, "proposal_agent_id", "refiner_agent_id"
        )
        verifier_agent = _identity(iteration, "verifier_agent_id")
        if (
            proposal_session is not None
            and verifier_session is not None
            and proposal_session != verifier_session
        ) or (
            proposal_agent is not None
            and verifier_agent is not None
            and proposal_agent != verifier_agent
        ):
            return True
    return False


def derive_acceptance_evidence(
    document: Mapping[str, Any], *, run_id: str
) -> dict[str, Any]:
    """Derive non-sensitive Milestone 1 aggregates from review schema v1."""
    if document.get("schema_version") != "zen.factory-golden-review/1":
        raise AcceptanceReportError("unsupported protected review schema")
    if document.get("run_id") != run_id:
        raise AcceptanceReportError("review run ID does not match requested run ID")

    counts = _object(document.get("counts"), "review counts")
    conversations_raw = _array(document.get("conversations"), "review conversations")
    coverage_raw = _array(document.get("metric_coverage"), "metric coverage")
    declared_total = _count(counts.get("conversations"), "counts.conversations")
    if declared_total != len(conversations_raw):
        raise AcceptanceReportError("declared conversation count does not match review rows")

    terminal_count = 0
    verified_count = 0
    quarantined_count = 0
    checked_turns = 0
    preserved_turns = 0
    review_required = 0
    review_completed = 0
    languages: set[str] = set()
    domains: set[str] = set()
    code_switch_count = 0
    independent_sources: set[str] = set()

    for raw_conversation in conversations_raw:
        conversation = _object(raw_conversation, "conversation")
        source_id = conversation.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            raise AcceptanceReportError("conversation source ID is missing")
        if _has_independent_verifier(conversation):
            independent_sources.add(source_id)

        terminal = _object(conversation.get("terminal"), "conversation terminal")
        terminal_status = terminal.get("status")
        if terminal_status in TERMINAL_STATUSES:
            terminal_count += 1
        if terminal_status in CANDIDATE_STATUSES:
            verified_count += 1
        elif terminal_status in {"QUARANTINED", "REJECTED_SOURCE"}:
            quarantined_count += 1

        for raw_turn in _array(conversation.get("turns"), "conversation turns"):
            turn = _object(raw_turn, "conversation turn")
            if turn.get("role") == "user":
                checked_turns += 1
                if turn.get("source_preserved") is True:
                    preserved_turns += 1

        classification = _object(
            conversation.get("classification"), "conversation classification"
        )
        primary = classification.get("primary_language")
        if isinstance(primary, str) and primary.strip() and primary.casefold() != "unknown":
            languages.add(primary.strip())
        for other in _array(
            classification.get("other_languages"),
            "classification other_languages",
        ):
            if isinstance(other, str) and other.strip() and other.casefold() != "unknown":
                languages.add(other.strip())
        domain = classification.get("domain")
        if isinstance(domain, str) and domain.strip() and domain.casefold() != "unclassified":
            domains.add(domain.strip())
        if classification.get("code_switching") is True:
            code_switch_count += 1

        human_review = _object(
            conversation.get("human_review"), "conversation human review"
        )
        review_required += 1
        state = human_review.get("state")
        latest_decision = human_review.get("latest_decision")
        if state != "REVIEW_PENDING" and latest_decision is not None:
            review_completed += 1

    complete_citations = 0
    for raw_row in coverage_raw:
        row = _object(raw_row, "metric coverage row")
        path_complete = all(
            isinstance(row.get(key), str) and bool(row.get(key))
            for key in ("axis_id", "subaxis_id", "variant_id")
        )
        evidence_turn_ids = row.get("evidence_turn_ids")
        evidence_complete = (
            isinstance(evidence_turn_ids, list)
            and bool(evidence_turn_ids)
            and all(isinstance(value, str) and bool(value) for value in evidence_turn_ids)
        )
        missing_evidence = row.get("missing_evidence")
        no_missing_evidence = isinstance(missing_evidence, list) and not missing_evidence
        if (
            path_complete
            and evidence_complete
            and no_missing_evidence
            and row.get("source_id") in independent_sources
        ):
            complete_citations += 1

    queue_dead = 0
    queue_failures = 0
    for raw_row in _array(counts.get("queue"), "counts.queue"):
        row = _object(raw_row, "queue count row")
        count = _count(row.get("count"), "queue count")
        status = row.get("status")
        if status == "DEAD":
            queue_dead += count
        if status in {"DEAD", "FAILED"}:
            queue_failures += count

    declared_terminal = _object(counts.get("terminal"), "counts.terminal")
    declared_verified = _count(
        declared_terminal.get("VERIFIED_CANDIDATE", 0),
        "counts.terminal.VERIFIED_CANDIDATE",
    )
    declared_quarantined = sum(
        _count(declared_terminal.get(status, 0), f"counts.terminal.{status}")
        for status in ("QUARANTINED", "REJECTED_SOURCE")
    )
    if declared_verified != verified_count or declared_quarantined != quarantined_count:
        raise AcceptanceReportError("declared terminal counts do not match conversation rows")

    return {
        # The frozen review population is the Milestone 1 selection target.
        # Quality qualification remains separate, so count pressure cannot pass
        # quarantined or rejected items as accepted selections.
        "target_count": declared_total,
        "selected_count": declared_total,
        "quality_qualified_selected_count": verified_count,
        "total_count": declared_total,
        "terminal_count": terminal_count,
        "source_user_turns_preserved_count": preserved_turns,
        "source_user_turns_checked_count": checked_turns,
        "complete_independent_citation_count": complete_citations,
        "citation_required_count": len(coverage_raw),
        "queue_dead_count": queue_dead,
        "queue_failure_count": queue_failures,
        # Aggregate queue rows carry no explanation records. Any failure therefore
        # remains unexplained and fails closed; the zero-failure case is complete.
        "queue_dead_explained_count": 0,
        "queue_failure_explained_count": 0,
        "verified_count": verified_count,
        "quarantined_count": quarantined_count,
        "human_review_required_count": review_required,
        "human_review_completed_count": review_completed,
        "languages": sorted(languages),
        "domains": sorted(domains),
        "code_switch_count": code_switch_count,
        "minimum_languages": 2,
        "minimum_domains": 2,
        "minimum_code_switch_count": 1,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        run_id = validate_run_id(args.run_id)
        document = _load_review(args.review)
        report = evaluate_review_document(document, run_id=run_id)
        bundle = write_report_bundle(report)
    except AcceptanceReportError as exc:
        print(f"acceptance reporter error: {exc}", file=sys.stderr)
        return 3

    summary = {
        "artifacts": bundle,
        "run_id": run_id,
        "verdict": report["verdict"],
    }
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return EXIT_BY_VERDICT[report["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
