from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterable, Mapping, Sequence


_SCOPES = {"PROMPT", "PLUGIN", "WORKFLOW"}
_FAIL_VALUES = {"FAIL", "FAILED", "REJECT", "REJECTED", "REQUEST_REPAIR", "EDIT"}


class GovernanceError(ValueError):
    """Raised when an improvement action violates a governance invariant."""


class PromotionBlockedError(GovernanceError):
    """Raised when a candidate does not satisfy the deterministic promotion gate."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _as_tuple(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({str(value) for value in values if str(value)}))


def _field(row: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return default


def _is_failure(row: Mapping[str, Any], *, feedback: bool = False) -> bool:
    names = ("decision", "action", "verdict", "outcome", "status") if feedback else (
        "golden_verdict", "final_verdict", "verdict", "outcome", "status"
    )
    value = str(_field(row, *names, default="FAIL" if feedback else "")).upper()
    return value in _FAIL_VALUES


def aggregate_gap_clusters(
    run_failures: Iterable[Mapping[str, Any]] = (),
    metric_citations: Iterable[Mapping[str, Any]] = (),
    reviewer_feedback: Iterable[Mapping[str, Any]] = (),
) -> tuple[dict[str, Any], ...]:
    """Build stable gap clusters from run evidence.

    Passing citations and reviewer approvals are deliberately excluded.  Cluster
    identities depend only on the taxonomy path and defect code, while each
    cluster's evidence checksum binds the exact input evidence used to create it.
    """

    rows: list[tuple[str, Mapping[str, Any]]] = []
    rows.extend(("run_failure", row) for row in run_failures)
    rows.extend(
        ("metric_citation", row) for row in metric_citations if _is_failure(row)
    )
    rows.extend(
        ("reviewer_feedback", row)
        for row in reviewer_feedback
        if _is_failure(row, feedback=True)
    )
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for source, row in rows:
        axis = str(_field(row, "axis_id", "axis", default="unclassified"))
        subaxis = str(_field(row, "subaxis_id", "subaxis", "sub_axis", default="unclassified"))
        variant = str(_field(row, "variant_id", "variant", default="unclassified"))
        defect = str(_field(row, "defect_code", "failure_type", "finding_code", default="unspecified"))
        conversation = str(
            _field(row, "conversation_id", "source_id", "packet_id", default="unknown")
        )
        turn = str(_field(row, "turn_id", default="unknown"))
        severity = str(_field(row, "severity", default="NORMAL")).upper()
        evidence = {
            "source": source,
            "conversation_id": conversation,
            "turn_id": turn,
            "severity": severity,
            "reason": str(_field(row, "reason", "summary", "comment", default="")),
        }
        evidence["evidence_id"] = _digest(evidence)
        grouped[(axis, subaxis, variant, defect)].append(evidence)

    clusters = []
    for key, evidence in grouped.items():
        axis, subaxis, variant, defect = key
        evidence = sorted(evidence, key=lambda row: row["evidence_id"])
        identity = {
            "axis_id": axis,
            "subaxis_id": subaxis,
            "variant_id": variant,
            "defect_code": defect,
        }
        sources = Counter(row["source"] for row in evidence)
        cluster = {
            "cluster_id": "gap-" + _digest(identity)[:24],
            **identity,
            "evidence_count": len(evidence),
            "affected_conversation_ids": list(
                _as_tuple(row["conversation_id"] for row in evidence)
            ),
            "affected_turn_ids": list(_as_tuple(row["turn_id"] for row in evidence)),
            "critical_count": sum(row["severity"] == "CRITICAL" for row in evidence),
            "source_counts": dict(sorted(sources.items())),
            "evidence_sha256": _digest(evidence),
        }
        clusters.append(cluster)
    return tuple(sorted(clusters, key=lambda row: row["cluster_id"]))


@dataclass(frozen=True)
class PromotionPolicy:
    minimum_sample_size: int = 30
    minimum_absolute_improvement: float = 0.02
    minimum_coverage_delta: int = 0
    maximum_noncritical_regressions: int | None = None

    def validate(self) -> None:
        if self.minimum_sample_size < 1:
            raise GovernanceError("minimum_sample_size must be positive")
        if not 0 <= self.minimum_absolute_improvement <= 1:
            raise GovernanceError("minimum_absolute_improvement must be between zero and one")
        if self.maximum_noncritical_regressions is not None and self.maximum_noncritical_regressions < 0:
            raise GovernanceError("maximum_noncritical_regressions cannot be negative")


@dataclass(frozen=True)
class EvaluationSummary:
    sample_size: int
    baseline_pass_count: int
    candidate_pass_count: int
    baseline_pass_rate: float
    candidate_pass_rate: float
    absolute_improvement: float
    regressions: int
    critical_regressions: int
    baseline_coverage: int
    candidate_coverage: int
    coverage_delta: int
    user_turn_integrity_failures: int
    independent_evaluator_approved: bool


class ImprovementStore:
    """Append-only governed state for improvement proposals and evaluations."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS improvement_analyses (
              id TEXT PRIMARY KEY, source_run_id TEXT NOT NULL,
              clusters_json TEXT NOT NULL, content_sha256 TEXT NOT NULL,
              created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS improvement_proposals (
              id TEXT PRIMARY KEY, scope TEXT NOT NULL, component TEXT NOT NULL,
              baseline_version TEXT NOT NULL, candidate_version TEXT NOT NULL,
              change_json TEXT NOT NULL, gap_ids_json TEXT NOT NULL,
              training_ids_json TEXT NOT NULL, created_by TEXT NOT NULL,
              content_sha256 TEXT NOT NULL, created_at REAL NOT NULL,
              UNIQUE(scope, component, candidate_version)
            );
            CREATE TABLE IF NOT EXISTS improvement_evaluations (
              id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL REFERENCES improvement_proposals(id),
              evaluator_id TEXT NOT NULL, held_out_ids_json TEXT NOT NULL,
              results_json TEXT NOT NULL, summary_json TEXT NOT NULL,
              content_sha256 TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS improvement_approvals (
              id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL REFERENCES improvement_proposals(id),
              approver_id TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
              content_sha256 TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS improvement_promotions (
              id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE REFERENCES improvement_proposals(id),
              evaluation_id TEXT NOT NULL REFERENCES improvement_evaluations(id),
              approval_id TEXT NOT NULL REFERENCES improvement_approvals(id),
              policy_json TEXT NOT NULL, activated INTEGER NOT NULL DEFAULT 0,
              content_sha256 TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS improvement_events (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
              entity_id TEXT NOT NULL, payload_json TEXT NOT NULL,
              created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS improvement_idempotency (
              operation TEXT NOT NULL, idempotency_key TEXT NOT NULL,
              request_sha256 TEXT NOT NULL, entity_id TEXT NOT NULL,
              created_at REAL NOT NULL,
              PRIMARY KEY(operation, idempotency_key)
            );
            """
        )
        self.db.commit()
        for database_file in (self.path, self.path.with_name(self.path.name + "-wal"), self.path.with_name(self.path.name + "-shm")):
            if database_file.exists():
                database_file.chmod(0o600)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "ImprovementStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _existing_idempotent(
        self, operation: str, key: str, request_sha256: str
    ) -> str | None:
        if not key.strip():
            raise GovernanceError("idempotency_key is required")
        row = self.db.execute(
            "SELECT request_sha256,entity_id FROM improvement_idempotency WHERE operation=? AND idempotency_key=?",
            (operation, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_sha256:
            raise GovernanceError("idempotency key reused with different request")
        return str(row["entity_id"])

    def _record_idempotency(
        self, operation: str, key: str, request_sha256: str, entity_id: str
    ) -> None:
        self.db.execute(
            "INSERT INTO improvement_idempotency VALUES (?,?,?,?,?)",
            (operation, key, request_sha256, entity_id, time.time()),
        )

    def _event(self, event_type: str, entity_id: str, payload: Mapping[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO improvement_events(event_type,entity_id,payload_json,created_at) VALUES (?,?,?,?)",
            (event_type, entity_id, _canonical(payload), time.time()),
        )

    def record_analysis(
        self,
        source_run_id: str,
        clusters: Sequence[Mapping[str, Any]],
        *,
        idempotency_key: str,
    ) -> str:
        ordered = sorted((dict(row) for row in clusters), key=lambda row: str(row["cluster_id"]))
        request = {"source_run_id": source_run_id, "clusters": ordered}
        checksum = _digest(request)
        existing = self._existing_idempotent("ANALYZE", idempotency_key, checksum)
        if existing:
            return existing
        analysis_id = "analysis-" + checksum[:24]
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO improvement_analyses VALUES (?,?,?,?,?)",
                (analysis_id, source_run_id, _canonical(ordered), checksum, time.time()),
            )
            self._record_idempotency("ANALYZE", idempotency_key, checksum, analysis_id)
            self._event("improvement.analysis_recorded", analysis_id, request)
        return analysis_id

    def create_proposal(
        self,
        *,
        scope: str,
        component: str,
        baseline_version: str,
        candidate_version: str,
        change: Mapping[str, Any],
        gap_ids: Iterable[str],
        training_ids: Iterable[str] = (),
        created_by: str,
        idempotency_key: str,
    ) -> str:
        scope = scope.upper()
        if scope not in _SCOPES:
            raise GovernanceError("proposal scope must be PROMPT, PLUGIN, or WORKFLOW")
        if not all((component.strip(), baseline_version.strip(), candidate_version.strip(), created_by.strip())):
            raise GovernanceError("component, versions, and created_by are required")
        if baseline_version == candidate_version:
            raise GovernanceError("candidate_version must differ from baseline_version")
        request = {
            "scope": scope,
            "component": component,
            "baseline_version": baseline_version,
            "candidate_version": candidate_version,
            "change": dict(change),
            "gap_ids": list(_as_tuple(gap_ids)),
            "training_ids": list(_as_tuple(training_ids)),
            "created_by": created_by,
        }
        checksum = _digest(request)
        existing = self._existing_idempotent("PROPOSE", idempotency_key, checksum)
        if existing:
            return existing
        proposal_id = "proposal-" + checksum[:24]
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO improvement_proposals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        proposal_id, scope, component, baseline_version, candidate_version,
                        _canonical(request["change"]), _canonical(request["gap_ids"]),
                        _canonical(request["training_ids"]), created_by, checksum, time.time(),
                    ),
                )
                self._record_idempotency("PROPOSE", idempotency_key, checksum, proposal_id)
                self._event("improvement.proposed", proposal_id, request)
        except sqlite3.IntegrityError as exc:
            raise GovernanceError("candidate version already exists or proposal is duplicated") from exc
        return proposal_id

    def proposal(self, proposal_id: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM improvement_proposals WHERE id=?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise KeyError(proposal_id)
        value = dict(row)
        for name in ("change_json", "gap_ids_json", "training_ids_json"):
            value[name.removesuffix("_json")] = json.loads(value.pop(name))
        value["status"] = self.proposal_status(proposal_id)
        return value

    def record_evaluation(
        self,
        proposal_id: str,
        *,
        held_out_ids: Iterable[str],
        results: Sequence[Mapping[str, Any]],
        evaluator_id: str,
        independent_evaluator_approved: bool,
        idempotency_key: str,
    ) -> str:
        proposal = self.proposal(proposal_id)
        held_out = _as_tuple(held_out_ids)
        training = set(proposal["training_ids"])
        overlap = training.intersection(held_out)
        if overlap:
            raise GovernanceError(
                "held-out IDs overlap proposal training IDs: " + ", ".join(sorted(overlap))
            )
        if not held_out:
            raise GovernanceError("at least one held-out ID is required")
        if evaluator_id == proposal["created_by"]:
            raise GovernanceError("evaluator must be independent from proposal creator")
        by_id: dict[str, dict[str, Any]] = {}
        for raw in results:
            row = dict(raw)
            item_id = str(_field(row, "id", "conversation_id", "held_out_id", default=""))
            if not item_id or item_id in by_id:
                raise GovernanceError("evaluation result IDs must be present and unique")
            required = {"baseline_pass", "candidate_pass", "user_turn_integrity"}
            if not required.issubset(row):
                raise GovernanceError("each result requires baseline_pass, candidate_pass, and user_turn_integrity")
            by_id[item_id] = {
                "id": item_id,
                "baseline_pass": bool(row["baseline_pass"]),
                "candidate_pass": bool(row["candidate_pass"]),
                "critical": bool(row.get("critical", False)),
                "baseline_covered": bool(row.get("baseline_covered", True)),
                "candidate_covered": bool(row.get("candidate_covered", True)),
                "user_turn_integrity": bool(row["user_turn_integrity"]),
            }
        if set(by_id) != set(held_out):
            raise GovernanceError("evaluation results must exactly match held-out IDs")
        ordered = [by_id[item_id] for item_id in held_out]
        baseline_passes = sum(row["baseline_pass"] for row in ordered)
        candidate_passes = sum(row["candidate_pass"] for row in ordered)
        regressions = [
            row for row in ordered if row["baseline_pass"] and not row["candidate_pass"]
        ]
        baseline_coverage = sum(row["baseline_covered"] for row in ordered)
        candidate_coverage = sum(row["candidate_covered"] for row in ordered)
        sample_size = len(ordered)
        summary = EvaluationSummary(
            sample_size=sample_size,
            baseline_pass_count=baseline_passes,
            candidate_pass_count=candidate_passes,
            baseline_pass_rate=baseline_passes / sample_size,
            candidate_pass_rate=candidate_passes / sample_size,
            absolute_improvement=(candidate_passes - baseline_passes) / sample_size,
            regressions=len(regressions),
            critical_regressions=sum(row["critical"] for row in regressions),
            baseline_coverage=baseline_coverage,
            candidate_coverage=candidate_coverage,
            coverage_delta=candidate_coverage - baseline_coverage,
            user_turn_integrity_failures=sum(not row["user_turn_integrity"] for row in ordered),
            independent_evaluator_approved=bool(independent_evaluator_approved),
        )
        request = {
            "proposal_id": proposal_id,
            "evaluator_id": evaluator_id,
            "held_out_ids": list(held_out),
            "results": ordered,
            "summary": asdict(summary),
        }
        checksum = _digest(request)
        existing = self._existing_idempotent("EVALUATE", idempotency_key, checksum)
        if existing:
            return existing
        evaluation_id = "evaluation-" + checksum[:24]
        with self.db:
            self.db.execute(
                "INSERT INTO improvement_evaluations VALUES (?,?,?,?,?,?,?,?)",
                (
                    evaluation_id, proposal_id, evaluator_id, _canonical(list(held_out)),
                    _canonical(ordered), _canonical(asdict(summary)), checksum, time.time(),
                ),
            )
            self._record_idempotency("EVALUATE", idempotency_key, checksum, evaluation_id)
            self._event("improvement.evaluated", evaluation_id, request)
        return evaluation_id

    def evaluation(self, evaluation_id: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM improvement_evaluations WHERE id=?", (evaluation_id,)
        ).fetchone()
        if row is None:
            raise KeyError(evaluation_id)
        value = dict(row)
        for name in ("held_out_ids_json", "results_json", "summary_json"):
            value[name.removesuffix("_json")] = json.loads(value.pop(name))
        return value

    def approve(
        self,
        proposal_id: str,
        *,
        approver_id: str,
        decision: str,
        reason: str,
        idempotency_key: str,
    ) -> str:
        self.proposal(proposal_id)
        decision = decision.upper()
        if decision not in {"APPROVE", "REJECT"}:
            raise GovernanceError("approval decision must be APPROVE or REJECT")
        if not approver_id.strip() or not reason.strip():
            raise GovernanceError("approver_id and reason are required")
        request = {
            "proposal_id": proposal_id,
            "approver_id": approver_id,
            "decision": decision,
            "reason": reason,
        }
        checksum = _digest(request)
        existing = self._existing_idempotent("APPROVE", idempotency_key, checksum)
        if existing:
            return existing
        approval_id = "approval-" + checksum[:24]
        with self.db:
            self.db.execute(
                "INSERT INTO improvement_approvals VALUES (?,?,?,?,?,?,?)",
                (approval_id, proposal_id, approver_id, decision, reason, checksum, time.time()),
            )
            self._record_idempotency("APPROVE", idempotency_key, checksum, approval_id)
            self._event("improvement.human_decision_recorded", approval_id, request)
        return approval_id

    def _latest_evaluation(self, proposal_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT id FROM improvement_evaluations WHERE proposal_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
        return self.evaluation(row["id"]) if row else None

    def _latest_approval(self, proposal_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM improvement_approvals WHERE proposal_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
        return dict(row) if row else None

    def promotion_readiness(
        self, proposal_id: str, policy: PromotionPolicy = PromotionPolicy()
    ) -> dict[str, Any]:
        policy.validate()
        self.proposal(proposal_id)
        evaluation = self._latest_evaluation(proposal_id)
        approval = self._latest_approval(proposal_id)
        blockers: list[str] = []
        if evaluation is None:
            blockers.append("no evaluation recorded")
        else:
            summary = evaluation["summary"]
            if summary["sample_size"] < policy.minimum_sample_size:
                blockers.append("evaluation sample is below minimum")
            if summary["critical_regressions"] != 0:
                blockers.append("critical regressions detected")
            if summary["user_turn_integrity_failures"] != 0:
                blockers.append("source user-turn integrity violation detected")
            if summary["absolute_improvement"] < policy.minimum_absolute_improvement:
                blockers.append("minimum improvement not achieved")
            if summary["coverage_delta"] < policy.minimum_coverage_delta:
                blockers.append("coverage regressed below policy")
            if (
                policy.maximum_noncritical_regressions is not None
                and summary["regressions"] > policy.maximum_noncritical_regressions
            ):
                blockers.append("noncritical regressions exceed policy")
            if not summary["independent_evaluator_approved"]:
                blockers.append("independent evaluator did not approve")
        if approval is None or approval["decision"] != "APPROVE":
            blockers.append("explicit human approval is missing")
        return {
            "eligible": not blockers,
            "blockers": blockers,
            "evaluation_id": evaluation["id"] if evaluation else None,
            "approval_id": approval["id"] if approval else None,
        }

    def promote(
        self,
        proposal_id: str,
        *,
        policy: PromotionPolicy = PromotionPolicy(),
        idempotency_key: str,
    ) -> str:
        readiness = self.promotion_readiness(proposal_id, policy)
        request = {
            "proposal_id": proposal_id,
            "policy": asdict(policy),
            "evaluation_id": readiness["evaluation_id"],
            "approval_id": readiness["approval_id"],
        }
        checksum = _digest(request)
        existing = self._existing_idempotent("PROMOTE", idempotency_key, checksum)
        if existing:
            return existing
        if not readiness["eligible"]:
            raise PromotionBlockedError("; ".join(readiness["blockers"]))
        promotion_id = "promotion-" + checksum[:24]
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO improvement_promotions VALUES (?,?,?,?,?,?,?,?)",
                    (
                        promotion_id, proposal_id, readiness["evaluation_id"],
                        readiness["approval_id"], _canonical(asdict(policy)), 0,
                        checksum, time.time(),
                    ),
                )
                self._record_idempotency("PROMOTE", idempotency_key, checksum, promotion_id)
                self._event(
                    "improvement.promoted_not_activated",
                    promotion_id,
                    {**request, "activated": False},
                )
        except sqlite3.IntegrityError as exc:
            raise GovernanceError("proposal was already promoted") from exc
        return promotion_id

    def proposal_status(self, proposal_id: str) -> str:
        if self.db.execute(
            "SELECT 1 FROM improvement_promotions WHERE proposal_id=?", (proposal_id,)
        ).fetchone():
            return "PROMOTED_NOT_ACTIVATED"
        approval = self._latest_approval(proposal_id)
        if approval and approval["decision"] == "REJECT":
            return "REJECTED"
        if approval and approval["decision"] == "APPROVE":
            return "HUMAN_APPROVED"
        if self._latest_evaluation(proposal_id):
            return "EVALUATED"
        return "CANDIDATE"

    def status(self) -> dict[str, int]:
        result: Counter[str] = Counter()
        ids = self.db.execute("SELECT id FROM improvement_proposals ORDER BY id").fetchall()
        for row in ids:
            result[self.proposal_status(row["id"])] += 1
        return dict(sorted(result.items()))

    def events(self) -> list[dict[str, Any]]:
        values = []
        for row in self.db.execute("SELECT * FROM improvement_events ORDER BY sequence"):
            value = dict(row)
            value["payload"] = json.loads(value.pop("payload_json"))
            values.append(value)
        return values

