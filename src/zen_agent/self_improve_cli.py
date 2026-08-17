from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from .config import load_config
from .factory_qualification import FactoryQualificationStore
from .factory_queue import LocalFactoryQueue
from .factory_review import publish
from .factory_worker_pool import ParallelFactoryWorkerPool
from .feedback_router import FeedbackRouter, FeedbackRoutingError, SCHEMA_VERSION
from .improvement import ImprovementStore, aggregate_gap_clusters
from .plugins import load_plugins
from .review_feedback import ReviewFeedbackStore


FEEDBACK_STAGES = (
    "human_feedback_repair", "trajectory_gate", "verify_repair", "repair", "terminal",
)


def _targets(decision: dict[str, Any]) -> list[dict[str, str]]:
    if decision["action"] == "EDIT":
        return [
            {
                "turn_id": row["turn_id"],
                "instruction": (
                    "Use this reviewer-approved assistant response exactly unless it violates "
                    "the repair safety contract: " + json.dumps(row["text"], ensure_ascii=False)
                ),
            }
            for row in decision["assistant_edits"]
        ]
    changes = decision["feedback"].get("requested_changes", [])
    targets = [row for row in changes if row.get("turn_id")]
    if targets:
        return targets
    summary = decision["feedback"]["summary"]
    return [
        {"turn_id": turn_id, "instruction": summary}
        for turn_id in decision["feedback"].get("evidence_turn_ids", [])
    ]


def _router_decision(item: dict[str, Any], sample: dict[str, Any]) -> dict[str, Any]:
    decision = item["repair_decision"]
    targets = _targets(decision)
    if not targets:
        raise FeedbackRoutingError("review feedback must identify at least one assistant turn")
    return {
        "schema_version": SCHEMA_VERSION,
        "decision_id": decision["id"],
        "run_id": item["run_id"],
        "packet_id": item["conversation_id"],
        "source_content_sha256": item["source_content_sha256"],
        "packet_locator": {
            "packet_batch": sample["packet_batch"],
            "packet_index": sample["packet_index"],
        },
        "approval": {
            "status": "APPROVED",
            "reviewer_id": decision["reviewer_identity"],
            "approved_at": datetime.fromtimestamp(
                decision["created_at"], timezone.utc
            ).isoformat(),
        },
        "feedback": {"action": "REQUEST_REPAIR", "targets": targets},
    }


def _analysis(review: dict[str, Any], decisions: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    failures = [
        {
            "packet_id": row["packet_id"],
            "status": row["terminal"]["status"],
            "failure_type": row["terminal"].get("reason") or "terminal_failure",
            "reason": row["terminal"].get("reason") or "",
        }
        for row in review["conversations"]
        if row["terminal"]["status"] not in {"VERIFIED_CANDIDATE", "PARTIAL_CANDIDATE"}
    ]
    citations = [
        row for row in review["metric_coverage"]
        if row.get("golden_verdict") == "FAIL"
    ]
    feedback = []
    for decision in decisions:
        for citation in decision["feedback"].get("metric_citations", []) or [{}]:
            feedback.append({
                **citation,
                "action": decision["action"],
                "summary": decision["feedback"]["summary"],
                "turn_id": citation.get("turn_id") or next(
                    iter(decision["feedback"].get("evidence_turn_ids", [])), "unknown"
                ),
            })
    return aggregate_gap_clusters(failures, citations, feedback)


def run_cycle(
    root: Path,
    run_id: str,
    site: Path,
    *,
    workers: int,
    max_items: int,
    max_feedback_rounds: int,
) -> dict[str, Any]:
    config = load_config(root)
    plugins = load_plugins(config.plugin_paths)
    # Publishing also creates source-bound review items for every conversation.
    before = publish(config.root, run_id, site)
    qualification = FactoryQualificationStore(config.state_directory / "factory-qualification.db")
    queue = LocalFactoryQueue(config.state_directory / "factory-queue.db")
    routed, routing_errors = [], []
    try:
        samples = {row["packet_id"]: row for row in qualification.samples(run_id)}
        with ReviewFeedbackStore(config.state_directory / "review-feedback.db") as reviews:
            pending = reviews.pending_repair_requests(run_id=run_id)
        router = FeedbackRouter(
            config.root, queue, run_id, max_feedback_rounds=max_feedback_rounds
        )
        for item in pending:
            try:
                sample = samples[item["conversation_id"]]
                route = router.route(_router_decision(item, sample))
                routed.append({
                    "packet_id": route.packet_id,
                    "decision_id": route.review_decision_id,
                    "feedback_round": route.round_number,
                    "enqueued": route.enqueued,
                })
            except (KeyError, FeedbackRoutingError) as exc:
                routing_errors.append({"item_id": item["id"], "error": str(exc)})
    finally:
        qualification.close()
        queue.close()

    pool = ParallelFactoryWorkerPool(config, plugins.tools, workers=workers)
    processed = pool.run_until_idle(run_id, FEEDBACK_STAGES, max_items=max_items)
    after = publish(config.root, run_id, site)
    review = json.loads((site / "review.json").read_text(encoding="utf-8"))
    with ReviewFeedbackStore(config.state_directory / "review-feedback.db") as reviews:
        decisions = []
        for item in reviews.list_items(run_id=run_id, limit=10_000):
            decisions.extend(reviews.list_decisions(item["id"]))
    clusters = _analysis(review, decisions)
    analysis_key = "cycle-" + hashlib.sha256(
        json.dumps(clusters, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with ImprovementStore(config.state_directory / "improvement.db") as improvements:
        analysis_id = improvements.record_analysis(
            run_id, clusters, idempotency_key=analysis_key
        )
        improvement_status = improvements.status()
    final_queue = LocalFactoryQueue(config.state_directory / "factory-queue.db")
    try:
        queue_counts = final_queue.counts_by_stage(run_id)
    finally:
        final_queue.close()
    return {
        "schema_version": "zen.self-improvement-cycle/1",
        "run_id": run_id,
        "model_policy": "gpt-5.6-sol-only",
        "review_items_before": before["conversations"],
        "routed_feedback": routed,
        "routing_errors": routing_errors,
        "processed": processed,
        "review_items_after": after["conversations"],
        "gap_analysis_id": analysis_id,
        "gap_clusters": list(clusters),
        "improvement_lifecycle": improvement_status,
        "queue": queue_counts,
        "governance": {
            "user_turns_immutable": True,
            "independent_reverification_required": True,
            "shared_asset_auto_mutation": False,
            "promotion_requires_held_out_evaluation_and_human_approval": True,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zen-factory-self-improve",
        description="Run one governed feedback, repair, re-verification, and learning cycle",
    )
    parser.add_argument("run_id")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-items", type=int, default=1000)
    parser.add_argument("--max-feedback-rounds", type=int, default=3)
    args = parser.parse_args(argv)
    try:
        result = run_cycle(
            args.root.resolve(), args.run_id, args.site.resolve(),
            workers=args.workers, max_items=args.max_items,
            max_feedback_rounds=args.max_feedback_rounds,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 2 if result["routing_errors"] else 0
    except Exception as exc:
        print(f"self-improvement cycle failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
