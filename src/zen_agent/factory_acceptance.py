from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
PROTECTED_FIELDS = frozenset(
    {
        "credentials",
        "evidence_quote",
        "golden_text",
        "source_text",
        "transcript",
        "transcript_text",
    }
)


class AcceptanceReportError(ValueError):
    """Raised when acceptance evidence cannot be handled safely."""


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise AcceptanceReportError("run id must be exactly 32 lowercase hexadecimal characters")
    return run_id


def workspace_path(path: str | Path) -> Path:
    """Return a normalized workspace-relative path without resolving symlinks."""
    raw = str(path)
    candidate = PurePosixPath(raw)
    if not raw or candidate.is_absolute() or ".." in candidate.parts:
        raise AcceptanceReportError("path must be workspace-relative and may not traverse parents")
    if candidate.parts and candidate.parts[0] == ".venv":
        raise AcceptanceReportError("access to .venv is prohibited")
    normalized = Path(*candidate.parts)
    current = Path()
    for part in normalized.parts:
        current /= part
        if current.is_symlink():
            raise AcceptanceReportError(f"symlink path component is not allowed: {current.as_posix()}")
    return normalized


def assert_aggregate_only(value: Any, *, location: str = "$") -> None:
    """Reject protected text fields before report serialization."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            if key_text.casefold() in PROTECTED_FIELDS:
                raise AcceptanceReportError(f"protected field is not permitted in aggregate output at {location}")
            assert_aggregate_only(child, location=f"{location}.{key_text}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            assert_aggregate_only(child, location=f"{location}[{index}]")


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    assert_aggregate_only(value)
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path) -> str:
    safe_path = workspace_path(path)
    digest = hashlib.sha256()
    with safe_path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _ensure_private_directory(path: Path) -> None:
    current = Path()
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise AcceptanceReportError(f"symlink directory is not allowed: {current.as_posix()}")
        if current.exists():
            if not current.is_dir():
                raise AcceptanceReportError(f"report parent is not a directory: {current.as_posix()}")
            os.chmod(current, stat.S_IRWXU)
        else:
            current.mkdir(mode=stat.S_IRWXU)


def write_owner_only(path: str | Path, payload: bytes) -> str:
    """Atomically write a regular owner-only file and return its SHA-256."""
    safe_path = workspace_path(path)
    _ensure_private_directory(safe_path.parent)
    if safe_path.is_symlink():
        raise AcceptanceReportError(f"report destination may not be a symlink: {safe_path.as_posix()}")
    if safe_path.exists() and not safe_path.is_file():
        raise AcceptanceReportError(f"report destination is not a regular file: {safe_path.as_posix()}")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{safe_path.name}.",
        suffix=".tmp",
        dir=safe_path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, safe_path)
        os.chmod(safe_path, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path.exists():
            temporary_path.unlink()

    return sha256_bytes(payload)


GATE_NAMES = (
    "target_selected",
    "terminalization",
    "source_user_turn_preservation",
    "independent_taxonomy_citations",
    "queue_integrity",
    "diversity",
    "verified_quarantined_yield",
    "human_review_coverage",
)
VALID_GATE_STATUSES = frozenset({"PASS", "FAIL", "NEEDS_HUMAN"})


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AcceptanceReportError(f"{name} must be a non-negative integer")
    return value


def _string_set(value: Any, name: str) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise AcceptanceReportError(f"{name} must be a list of non-empty strings")
    return set(value)


def _gate(status: str, reason: str, metrics: Mapping[str, Any]) -> dict[str, Any]:
    if status not in VALID_GATE_STATUSES:
        raise AcceptanceReportError(f"invalid gate status: {status}")
    result = {"status": status, "reason": reason, "metrics": dict(metrics)}
    assert_aggregate_only(result)
    return result


def _identity(iteration: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = iteration.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _has_explicit_independent_pass(iteration: Mapping[str, Any]) -> bool:
    """Require a PASS plus explicitly distinct author/verifier provenance."""
    if iteration.get("verifier_decision") != "PASS":
        return False
    proposal_session = _identity(
        iteration, "proposal_session_id", "refiner_session_id"
    )
    verifier_session = _identity(iteration, "verifier_session_id")
    proposal_agent = _identity(
        iteration, "proposal_agent_id", "refiner_agent_id"
    )
    verifier_agent = _identity(iteration, "verifier_agent_id")
    return bool(
        (
            proposal_session is not None
            and verifier_session is not None
            and proposal_session != verifier_session
        )
        or (
            proposal_agent is not None
            and verifier_agent is not None
            and proposal_agent != verifier_agent
        )
    )


def evaluate_acceptance(evidence: Mapping[str, Any], *, run_id: str) -> dict[str, Any]:
    """Evaluate pre-aggregated, non-sensitive Milestone 1 evidence.

    Evidence is deliberately explicit: missing or malformed facts are errors, and
    facts needing adjudication produce NEEDS_HUMAN rather than inferred success.
    """
    validate_run_id(run_id)
    assert_aggregate_only(evidence)

    target = _integer(evidence.get("target_count"), "target_count")
    selected = _integer(evidence.get("selected_count"), "selected_count")
    quality_qualified = _integer(
        evidence.get("quality_qualified_selected_count"),
        "quality_qualified_selected_count",
    )
    total = _integer(evidence.get("total_count"), "total_count")
    terminal = _integer(evidence.get("terminal_count"), "terminal_count")
    preserved = _integer(
        evidence.get("source_user_turns_preserved_count"),
        "source_user_turns_preserved_count",
    )
    checked_turns = _integer(
        evidence.get("source_user_turns_checked_count"),
        "source_user_turns_checked_count",
    )
    cited = _integer(
        evidence.get("complete_independent_citation_count"),
        "complete_independent_citation_count",
    )
    citations_required = _integer(
        evidence.get("citation_required_count"),
        "citation_required_count",
    )
    dead = _integer(evidence.get("queue_dead_count"), "queue_dead_count")
    failures = _integer(evidence.get("queue_failure_count"), "queue_failure_count")
    explained_dead = _integer(
        evidence.get("queue_dead_explained_count"),
        "queue_dead_explained_count",
    )
    explained_failures = _integer(
        evidence.get("queue_failure_explained_count"),
        "queue_failure_explained_count",
    )
    verified = _integer(evidence.get("verified_count"), "verified_count")
    quarantined = _integer(evidence.get("quarantined_count"), "quarantined_count")
    review_required = _integer(
        evidence.get("human_review_required_count"),
        "human_review_required_count",
    )
    review_completed = _integer(
        evidence.get("human_review_completed_count"),
        "human_review_completed_count",
    )
    languages = _string_set(evidence.get("languages"), "languages")
    domains = _string_set(evidence.get("domains"), "domains")
    code_switch = _integer(evidence.get("code_switch_count"), "code_switch_count")
    minimum_languages = _integer(evidence.get("minimum_languages"), "minimum_languages")
    minimum_domains = _integer(evidence.get("minimum_domains"), "minimum_domains")
    minimum_code_switch = _integer(
        evidence.get("minimum_code_switch_count"),
        "minimum_code_switch_count",
    )

    if any(count > total for count in (selected, quality_qualified, terminal, verified, quarantined)):
        raise AcceptanceReportError("aggregate item counts may not exceed total_count")
    if quality_qualified > selected:
        raise AcceptanceReportError("quality-qualified selected count may not exceed selected count")
    if preserved > checked_turns or cited > citations_required:
        raise AcceptanceReportError("successful evidence counts may not exceed checked counts")
    if explained_dead > dead or explained_failures > failures:
        raise AcceptanceReportError("explained queue counts may not exceed queue counts")
    if review_completed > review_required:
        raise AcceptanceReportError("completed human reviews may not exceed required reviews")

    gates: dict[str, dict[str, Any]] = {}
    count_ok = selected >= target and quality_qualified == selected
    gates["target_selected"] = _gate(
        "PASS" if count_ok else "FAIL",
        "target met by quality-qualified selections" if count_ok else "target shortfall or selected items lack quality qualification",
        {"target_count": target, "selected_count": selected, "quality_qualified_selected_count": quality_qualified},
    )
    terminal_ok = terminal == total
    gates["terminalization"] = _gate(
        "PASS" if terminal_ok else "FAIL",
        "all items are terminal" if terminal_ok else "one or more items are non-terminal",
        {"total_count": total, "terminal_count": terminal},
    )
    preservation_ok = checked_turns > 0 and preserved == checked_turns
    gates["source_user_turn_preservation"] = _gate(
        "PASS" if preservation_ok else "FAIL",
        "all checked source user turns match exactly" if preservation_ok else "source user-turn checks are missing or mismatched",
        {"checked_count": checked_turns, "preserved_count": preserved},
    )
    citation_ok = cited == citations_required
    gates["independent_taxonomy_citations"] = _gate(
        "PASS" if citation_ok else "FAIL",
        "all required citations have independent axis, subaxis, and variant paths" if citation_ok else "one or more required independent citation paths are incomplete",
        {"required_count": citations_required, "complete_count": cited},
    )
    queue_ok = dead == explained_dead and failures == explained_failures
    gates["queue_integrity"] = _gate(
        "PASS" if queue_ok else "FAIL",
        "all dead and failed queue records are accounted for" if queue_ok else "unexplained dead or failed queue records exist",
        {"dead_count": dead, "dead_explained_count": explained_dead, "failure_count": failures, "failure_explained_count": explained_failures},
    )
    diversity_ok = (
        len(languages) >= minimum_languages
        and len(domains) >= minimum_domains
        and code_switch >= minimum_code_switch
    )
    gates["diversity"] = _gate(
        "PASS" if diversity_ok else "FAIL",
        "language, domain, and code-switch floors are met" if diversity_ok else "one or more diversity floors are not met",
        {"language_count": len(languages), "minimum_languages": minimum_languages, "domain_count": len(domains), "minimum_domains": minimum_domains, "code_switch_count": code_switch, "minimum_code_switch_count": minimum_code_switch},
    )
    yield_ok = verified + quarantined == total
    gates["verified_quarantined_yield"] = _gate(
        "PASS" if yield_ok else "FAIL",
        "every item is verified or quarantined" if yield_ok else "verified and quarantined outcomes do not cover the run",
        {"total_count": total, "verified_count": verified, "quarantined_count": quarantined},
    )
    reviews_pending = review_completed < review_required
    gates["human_review_coverage"] = _gate(
        "NEEDS_HUMAN" if reviews_pending else "PASS",
        "required human reviews are pending" if reviews_pending else "all required human reviews are complete",
        {"required_count": review_required, "completed_count": review_completed, "pending_count": review_required - review_completed},
    )

    statuses = {gate["status"] for gate in gates.values()}
    verdict = (
        "NEEDS_HUMAN"
        if reviews_pending
        else ("FAIL" if "FAIL" in statuses else ("NEEDS_HUMAN" if "NEEDS_HUMAN" in statuses else "PASS"))
    )
    report = {
        "schema_version": 1,
        "milestone": "1",
        "run_id": run_id,
        "verdict": verdict,
        "gates": gates,
    }
    assert_aggregate_only(report)
    return report


def evaluate_review_document(
    document: Mapping[str, Any], *, run_id: str
) -> dict[str, Any]:
    """Aggregate Milestone 1 evidence from a protected factory review document.

    Only structural and aggregate fields are inspected. Protected transcript and
    evidence text is neither copied nor serialized. Policy thresholds absent from
    the review schema remain NEEDS_HUMAN rather than being inferred.
    """
    validate_run_id(run_id)
    if document.get("run_id") != run_id:
        raise AcceptanceReportError("review input run ID does not match requested run ID")

    conversations_value = document.get("conversations")
    coverage_value = document.get("metric_coverage")
    counts_value = document.get("counts")
    if not isinstance(conversations_value, list):
        raise AcceptanceReportError("review conversations must be an array")
    if not isinstance(coverage_value, list):
        raise AcceptanceReportError("review metric coverage must be an array")
    if not isinstance(counts_value, Mapping):
        raise AcceptanceReportError("review counts must be an object")
    conversations: list[Mapping[str, Any]] = []
    for value in conversations_value:
        if not isinstance(value, Mapping):
            raise AcceptanceReportError("each review conversation must be an object")
        conversations.append(value)

    selected = len(conversations)
    declared_selected = counts_value.get("conversations")
    if _integer(declared_selected, "counts.conversations") != selected:
        raise AcceptanceReportError("declared conversation count does not match review rows")

    terminal_statuses = {
        "VERIFIED_CANDIDATE", "PARTIAL_CANDIDATE", "QUARANTINED", "REJECTED_SOURCE",
        "NOT_SELECTED",
    }
    terminal_count = 0
    verified_count = 0
    quarantined_count = 0
    checked_turns = 0
    preserved_turns = 0
    mismatched_turns = 0
    unassessable_turns = 0
    languages: set[str] = set()
    domains: set[str] = set()
    code_switch_count = 0
    review_required = 0
    review_completed = 0
    independently_verified_numbers: set[int] = set()

    for index, conversation in enumerate(conversations, 1):
        number = conversation.get("number", index)
        if not isinstance(number, int) or isinstance(number, bool):
            raise AcceptanceReportError("conversation number must be an integer")

        iterations = conversation.get("iterations")
        if not isinstance(iterations, list):
            raise AcceptanceReportError("conversation iterations must be an array")
        independent_pass = any(
            isinstance(iteration, Mapping)
            and _has_explicit_independent_pass(iteration)
            for iteration in iterations
        )
        if independent_pass:
            independently_verified_numbers.add(number)

        terminal = conversation.get("terminal")
        if not isinstance(terminal, Mapping):
            raise AcceptanceReportError("conversation terminal state must be an object")
        terminal_status = terminal.get("status")
        if terminal_status in terminal_statuses:
            terminal_count += 1
        if terminal_status in {"VERIFIED_CANDIDATE", "PARTIAL_CANDIDATE"} and independent_pass:
            verified_count += 1
        elif terminal_status in {"QUARANTINED", "REJECTED_SOURCE"}:
            quarantined_count += 1

        turns = conversation.get("turns")
        if not isinstance(turns, list):
            raise AcceptanceReportError("conversation turns must be an array")
        for turn in turns:
            if not isinstance(turn, Mapping):
                raise AcceptanceReportError("each conversation turn must be an object")
            if turn.get("role") == "user":
                checked_turns += 1
                source_value = turn.get("source_text")
                current_value = turn.get("text")
                if isinstance(source_value, str) and isinstance(current_value, str):
                    if source_value == current_value:
                        preserved_turns += 1
                    else:
                        mismatched_turns += 1
                else:
                    unassessable_turns += 1

        classification = conversation.get("classification")
        if not isinstance(classification, Mapping):
            raise AcceptanceReportError("conversation classification must be an object")
        language = classification.get("primary_language")
        domain = classification.get("domain")
        if isinstance(language, str) and language and language != "Unknown":
            languages.add(language)
        if isinstance(domain, str) and domain and domain != "Unclassified":
            domains.add(domain)
        if classification.get("code_switching") is True:
            code_switch_count += 1

        human_review = conversation.get("human_review")
        if not isinstance(human_review, Mapping):
            raise AcceptanceReportError("conversation human review must be an object")
        review_required += 1
        if human_review.get("latest_decision") is not None:
            review_completed += 1

    citation_required = len(coverage_value)
    complete_citations = 0
    structurally_complete_citations = 0
    citations_missing_independent_provenance = 0
    for row in coverage_value:
        if not isinstance(row, Mapping):
            raise AcceptanceReportError("each metric coverage row must be an object")
        path_complete = all(
            isinstance(row.get(field), str) and bool(row.get(field))
            for field in ("axis_id", "subaxis_id", "variant_id")
        )
        evidence_turn_ids = row.get("evidence_turn_ids")
        evidence_complete = (
            isinstance(evidence_turn_ids, list)
            and bool(evidence_turn_ids)
            and all(isinstance(value, str) and bool(value) for value in evidence_turn_ids)
        )
        if path_complete and evidence_complete:
            structurally_complete_citations += 1
            if row.get("conversation_number") in independently_verified_numbers:
                complete_citations += 1
            else:
                citations_missing_independent_provenance += 1

    queue_value = counts_value.get("queue")
    if not isinstance(queue_value, list):
        raise AcceptanceReportError("counts.queue must be an array")
    dead_count = 0
    failure_count = 0
    for row in queue_value:
        if not isinstance(row, Mapping):
            raise AcceptanceReportError("each queue count row must be an object")
        count = _integer(row.get("count"), "queue count")
        status = row.get("status")
        if status == "DEAD":
            dead_count += count
        elif status == "FAILED":
            failure_count += count

    gates: dict[str, dict[str, Any]] = {}
    target_value = document.get("target_count")
    if target_value is None:
        gates["target_selected"] = _gate(
            "NEEDS_HUMAN",
            "the protected review schema does not declare the governed count target",
            {
                "target_count_available": False,
                "selected_count": selected,
                "quality_qualified_selected_count": verified_count,
            },
        )
    else:
        target = _integer(target_value, "target_count")
        count_ok = verified_count >= target
        gates["target_selected"] = _gate(
            "PASS" if count_ok else "FAIL",
            "target met by independently verified candidates" if count_ok else "independently verified candidates do not meet the target",
            {
                "target_count_available": True,
                "target_count": target,
                "selected_count": selected,
                "quality_qualified_selected_count": verified_count,
            },
        )

    terminal_ok = terminal_count == selected
    gates["terminalization"] = _gate(
        "PASS" if terminal_ok else "FAIL",
        "all selected conversations are terminal" if terminal_ok else "one or more selected conversations are non-terminal",
        {"total_count": selected, "terminal_count": terminal_count},
    )
    preservation_ok = checked_turns > 0 and preserved_turns == checked_turns
    gates["source_user_turn_preservation"] = _gate(
        "PASS" if preservation_ok else "FAIL",
        "all source user turns are marked exactly preserved" if preservation_ok else "source user-turn preservation evidence is missing or mismatched",
        {"checked_count": checked_turns, "preserved_count": preserved_turns},
    )
    citation_ok = complete_citations == citation_required
    citation_structure_ok = structurally_complete_citations == citation_required
    if citation_ok:
        citation_status = "PASS"
        citation_reason = (
            "all citation rows have complete paths, evidence turns, and explicit "
            "independent verifier provenance"
        )
    elif citation_structure_ok and citations_missing_independent_provenance > 0:
        citation_status = "NEEDS_HUMAN"
        citation_reason = (
            "citation paths and evidence turns are complete, but explicit distinct "
            "proposal/refiner and verifier provenance is unavailable"
        )
    else:
        citation_status = "FAIL"
        citation_reason = "one or more citation rows lack a complete path or evidence turns"
    gates["independent_taxonomy_citations"] = _gate(
        citation_status,
        citation_reason,
        {
            "required_count": citation_required,
            "structurally_complete_count": structurally_complete_citations,
            "complete_count": complete_citations,
            "missing_independent_provenance_count": (
                citations_missing_independent_provenance
            ),
        },
    )
    queue_ok = dead_count == 0 and failure_count == 0
    gates["queue_integrity"] = _gate(
        "PASS" if queue_ok else "FAIL",
        "the aggregate queue has no dead or failed work" if queue_ok else "dead or failed work requires integrity review",
        {"dead_count": dead_count, "failure_count": failure_count},
    )

    diversity_policy = document.get("diversity_minimums")
    diversity_metrics = {
        "language_count": len(languages),
        "domain_count": len(domains),
        "code_switch_count": code_switch_count,
    }
    if not isinstance(diversity_policy, Mapping):
        gates["diversity"] = _gate(
            "NEEDS_HUMAN",
            "the protected review schema does not declare governed diversity floors",
            {**diversity_metrics, "minimums_available": False},
        )
    else:
        minimum_languages = _integer(diversity_policy.get("languages"), "diversity_minimums.languages")
        minimum_domains = _integer(diversity_policy.get("domains"), "diversity_minimums.domains")
        minimum_code_switch = _integer(diversity_policy.get("code_switch"), "diversity_minimums.code_switch")
        diversity_ok = (
            len(languages) >= minimum_languages
            and len(domains) >= minimum_domains
            and code_switch_count >= minimum_code_switch
        )
        gates["diversity"] = _gate(
            "PASS" if diversity_ok else "FAIL",
            "all governed diversity floors are met" if diversity_ok else "one or more governed diversity floors are not met",
            {
                **diversity_metrics,
                "minimums_available": True,
                "minimum_languages": minimum_languages,
                "minimum_domains": minimum_domains,
                "minimum_code_switch_count": minimum_code_switch,
            },
        )

    yield_ok = verified_count + quarantined_count == selected
    gates["verified_quarantined_yield"] = _gate(
        "PASS" if yield_ok else "FAIL",
        "every selected conversation is verified or quarantined" if yield_ok else "verified and quarantined outcomes do not cover all selected conversations",
        {
            "total_count": selected,
            "verified_count": verified_count,
            "quarantined_count": quarantined_count,
        },
    )
    reviews_pending = review_completed < review_required
    gates["human_review_coverage"] = _gate(
        "NEEDS_HUMAN" if reviews_pending else "PASS",
        "required human reviews are pending" if reviews_pending else "all required human reviews are complete",
        {
            "required_count": review_required,
            "completed_count": review_completed,
            "pending_count": review_required - review_completed,
        },
    )

    statuses = {gate["status"] for gate in gates.values()}
    verdict = (
        "NEEDS_HUMAN"
        if reviews_pending
        else ("FAIL" if "FAIL" in statuses else ("NEEDS_HUMAN" if "NEEDS_HUMAN" in statuses else "PASS"))
    )
    report = {
        "schema_version": 1,
        "milestone": "1",
        "run_id": run_id,
        "verdict": verdict,
        "gates": gates,
    }
    assert_aggregate_only(report)
    return report


def evaluate_unassessable_acceptance(
    *, run_id: str, reason: str = "required aggregate evidence is unavailable"
) -> dict[str, Any]:
    """Return a complete fail-closed report when protected input lacks safe aggregates.

    The report deliberately carries no source content and does not infer successful
    gates from incomplete evidence. Human review is required before acceptance.
    """
    validate_run_id(run_id)
    if not isinstance(reason, str) or not reason or "\n" in reason:
        raise AcceptanceReportError("unassessable reason must be a non-empty single line")
    gates = {
        name: _gate(
            "NEEDS_HUMAN",
            reason,
            {"evidence_available": False},
        )
        for name in GATE_NAMES
    }
    report = {
        "schema_version": 1,
        "milestone": "1",
        "run_id": run_id,
        "verdict": "NEEDS_HUMAN",
        "gates": gates,
    }
    assert_aggregate_only(report)
    return report


def render_markdown(report: Mapping[str, Any]) -> bytes:
    assert_aggregate_only(report)
    lines = [
        "# Milestone 1 Acceptance Report",
        "",
        f"Run: `{report['run_id']}`",
        "",
        f"Verdict: **{report['verdict']}**",
        "",
        "| Gate | Status | Reason |",
        "|---|---|---|",
    ]
    gates = report.get("gates")
    if not isinstance(gates, Mapping):
        raise AcceptanceReportError("report gates must be an object")
    for name in GATE_NAMES:
        gate = gates.get(name)
        if not isinstance(gate, Mapping):
            raise AcceptanceReportError(f"missing gate: {name}")
        reason = str(gate.get("reason", "")).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {name} | {gate.get('status')} | {reason} |")
    lines.append("")
    return ("\n".join(lines)).encode("utf-8")


def write_report_bundle(report: Mapping[str, Any]) -> dict[str, str]:
    run_id = validate_run_id(str(report.get("run_id", "")))
    output_dir = Path(".zen") / "milestones" / run_id
    json_path = output_dir / "acceptance.json"
    markdown_path = output_dir / "acceptance.md"
    manifest_path = output_dir / "sha256.json"
    json_payload = canonical_json_bytes(report)
    markdown_payload = render_markdown(report)
    hashes = {
        json_path.name: write_owner_only(json_path, json_payload),
        markdown_path.name: write_owner_only(markdown_path, markdown_payload),
    }
    manifest = {"algorithm": "SHA-256", "files": hashes, "run_id": run_id}
    hashes[manifest_path.name] = write_owner_only(manifest_path, canonical_json_bytes(manifest))
    return {
        "json": json_path.as_posix(),
        "markdown": markdown_path.as_posix(),
        "hash_manifest": manifest_path.as_posix(),
        "json_sha256": hashes[json_path.name],
        "markdown_sha256": hashes[markdown_path.name],
        "hash_manifest_sha256": hashes[manifest_path.name],
    }
