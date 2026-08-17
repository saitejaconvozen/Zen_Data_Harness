from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .improvement import (
    GovernanceError,
    ImprovementStore,
    PromotionBlockedError,
    PromotionPolicy,
    aggregate_gap_clusters,
)


def _load(path: Path | None, default: Any) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path else default


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zen-improve",
        description="Governed, append-only self-improvement candidate manager",
    )
    parser.add_argument("--db", type=Path, default=Path(".zen/improvement.db"))
    commands = parser.add_subparsers(dest="command", required=True)

    analyze = commands.add_parser("analyze", help="deterministically cluster failure evidence")
    analyze.add_argument("--run-id", required=True)
    analyze.add_argument("--failures", type=Path)
    analyze.add_argument("--citations", type=Path)
    analyze.add_argument("--feedback", type=Path)
    analyze.add_argument("--idempotency-key", required=True)

    commands.add_parser("status", help="show proposal lifecycle totals")

    propose = commands.add_parser("propose", help="record an immutable candidate")
    propose.add_argument("--scope", choices=("prompt", "plugin", "workflow"), required=True)
    propose.add_argument("--component", required=True)
    propose.add_argument("--baseline-version", required=True)
    propose.add_argument("--candidate-version", required=True)
    propose.add_argument("--change", type=Path, required=True)
    propose.add_argument("--gap-ids", type=Path)
    propose.add_argument("--training-ids", type=Path)
    propose.add_argument("--created-by", required=True)
    propose.add_argument("--idempotency-key", required=True)

    evaluate = commands.add_parser("evaluate", help="record held-out A/B results")
    evaluate.add_argument("--proposal-id", required=True)
    evaluate.add_argument("--held-out-ids", type=Path, required=True)
    evaluate.add_argument("--results", type=Path, required=True)
    evaluate.add_argument("--evaluator-id", required=True)
    evaluate.add_argument("--independent-approval", action="store_true")
    evaluate.add_argument("--idempotency-key", required=True)

    approve = commands.add_parser("approve", help="record an explicit human decision")
    approve.add_argument("--proposal-id", required=True)
    approve.add_argument("--approver-id", required=True)
    approve.add_argument("--decision", choices=("approve", "reject"), required=True)
    approve.add_argument("--reason", required=True)
    approve.add_argument("--idempotency-key", required=True)

    promote = commands.add_parser("promote", help="apply the deterministic promotion gate")
    promote.add_argument("--proposal-id", required=True)
    promote.add_argument("--minimum-sample-size", type=int, default=30)
    promote.add_argument("--minimum-improvement", type=float, default=0.02)
    promote.add_argument("--minimum-coverage-delta", type=int, default=0)
    promote.add_argument("--maximum-regressions", type=int)
    promote.add_argument("--idempotency-key", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        with ImprovementStore(args.db) as store:
            if args.command == "analyze":
                clusters = aggregate_gap_clusters(
                    _load(args.failures, []), _load(args.citations, []),
                    _load(args.feedback, []),
                )
                analysis_id = store.record_analysis(
                    args.run_id, clusters, idempotency_key=args.idempotency_key
                )
                _print({"analysis_id": analysis_id, "clusters": clusters})
            elif args.command == "status":
                _print(store.status())
            elif args.command == "propose":
                proposal_id = store.create_proposal(
                    scope=args.scope, component=args.component,
                    baseline_version=args.baseline_version,
                    candidate_version=args.candidate_version,
                    change=_load(args.change, {}), gap_ids=_load(args.gap_ids, []),
                    training_ids=_load(args.training_ids, []), created_by=args.created_by,
                    idempotency_key=args.idempotency_key,
                )
                _print(store.proposal(proposal_id))
            elif args.command == "evaluate":
                evaluation_id = store.record_evaluation(
                    args.proposal_id, held_out_ids=_load(args.held_out_ids, []),
                    results=_load(args.results, []), evaluator_id=args.evaluator_id,
                    independent_evaluator_approved=args.independent_approval,
                    idempotency_key=args.idempotency_key,
                )
                _print(store.evaluation(evaluation_id))
            elif args.command == "approve":
                approval_id = store.approve(
                    args.proposal_id, approver_id=args.approver_id,
                    decision=args.decision, reason=args.reason,
                    idempotency_key=args.idempotency_key,
                )
                _print({"approval_id": approval_id, "status": store.proposal_status(args.proposal_id)})
            elif args.command == "promote":
                policy = PromotionPolicy(
                    minimum_sample_size=args.minimum_sample_size,
                    minimum_absolute_improvement=args.minimum_improvement,
                    minimum_coverage_delta=args.minimum_coverage_delta,
                    maximum_noncritical_regressions=args.maximum_regressions,
                )
                promotion_id = store.promote(
                    args.proposal_id, policy=policy,
                    idempotency_key=args.idempotency_key,
                )
                _print({
                    "promotion_id": promotion_id,
                    "status": store.proposal_status(args.proposal_id),
                    "activated": False,
                })
        return 0
    except PromotionBlockedError as exc:
        parser.exit(2, f"promotion blocked: {exc}\n")
    except (GovernanceError, KeyError, json.JSONDecodeError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())

