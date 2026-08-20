from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

from .config import load_config
from .factory_qualification import FactoryQualificationStore
from .factory_queue import LocalFactoryQueue
from .factory_worker_pool import ParallelFactoryWorkerPool
from .plugins import load_plugins


DOWNSTREAM_STAGES = (
    "refine", "verify", "human_feedback_repair", "repair", "trajectory_gate",
    "verify_repair", "terminal",
)


def _audit_decision(root: Path, run_id: str, packet_id: str) -> dict | None:
    """Read a committed audit, or None when its artifact is gone.

    A missing artifact used to abort the whole run. It means only that this one
    conversation cannot be re-derived from disk, which the caller heals by
    re-auditing it.
    """
    path = root / ".zen" / "factory-jobs" / run_id / packet_id / "agent-audit.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))["decision"]
    except (ValueError, KeyError):
        return None


def enqueue_existing(
    root: Path,
    run_id: str,
    queue: LocalFactoryQueue,
    qualification: FactoryQualificationStore,
    *,
    limit: int | None,
    max_repair_rounds: int,
) -> dict[str, int]:
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    if not 1 <= max_repair_rounds <= 10:
        raise ValueError("max_repair_rounds must be between 1 and 10")
    counts: Counter[str] = Counter()
    samples = qualification.samples(run_id)
    if limit is not None:
        samples = samples[:limit]
    # Conversations that already have work in flight need nothing re-derived.
    active_keys = {
        row["job_key"]
        for row in queue.items_for_run(run_id)
        if row["stage"] != "agent_audit"
    }
    for sample in samples:
        audit = _audit_decision(root, run_id, sample["packet_id"])
        job_key = f"conversation-{sample['source_content_sha256']}"
        if audit is None:
            # The decision cannot be read back. If the conversation is already
            # moving through the pipeline there is nothing to do; otherwise
            # re-audit it rather than silently dropping it.
            if job_key in active_keys:
                counts["audit_artifact_missing_in_flight"] += 1
            else:
                queue.enqueue(
                    run_id, job_key, "agent_audit",
                    {
                        "tool": "factory.audit_conversation",
                        "inputs": {
                            "packet_batch": sample["packet_batch"],
                            "packet_index": sample["packet_index"],
                        },
                        "source_content_sha256": sample["source_content_sha256"],
                        "packet_id": sample["packet_id"],
                    },
                    max_attempts=2, priority=80,
                )
                counts["audit_requeued"] += 1
            continue
        common = {
            "source_content_sha256": sample["source_content_sha256"],
            "packet_id": sample["packet_id"],
            "configuration_key": sample["configuration_key"],
            "qualification_status": sample["configuration_status"],
            "agent_audit_sha256": sample["decision_sha256"],
            "max_repair_rounds": max_repair_rounds,
        }
        inputs = {
            "packet_batch": sample["packet_batch"],
            "packet_index": sample["packet_index"],
        }
        common["conversation_job_key"] = job_key
        if audit["conversation_usable"]:
            inserted = queue.enqueue(
                run_id, job_key, "refine",
                {"tool": "golden.refine_one", "inputs": inputs, **common},
                max_attempts=2, priority=70,
            )
            counts["refine_inserted" if inserted else "refine_existing"] += 1
            continue
        terminal_inputs = {
            **inputs,
            "packet_id": sample["packet_id"],
            "source_decision_run_id": run_id,
            "round_number": 0,
            "terminal_status": "QUARANTINED",
        }
        inserted = queue.enqueue(
            run_id, job_key, "terminal",
            {
                "tool": "golden.graph_terminal",
                "inputs": terminal_inputs,
                "terminal_reason": "source conversation is not usable for corrective refinement",
                **common,
            },
            max_attempts=1, priority=30,
        )
        counts["quarantine_inserted" if inserted else "quarantine_existing"] += 1
    counts["considered"] = len(samples)
    return dict(counts)


def reconcile_stalled_repairs(
    root: Path,
    run_id: str,
    queue: LocalFactoryQueue,
) -> dict[str, int]:
    """Recover legacy FAIL transitions blocked by non-round-scoped queue keys."""
    rows = queue.items_for_run(run_id)
    terminal_packets = {
        row["payload"].get("packet_id")
        for row in rows
        if row["stage"] == "terminal"
    }
    latest: dict[str, dict] = {}
    for row in rows:
        if row["stage"] != "verify_repair" or row["status"] != "SUCCEEDED":
            continue
        packet_id = row["payload"].get("packet_id")
        if not packet_id or packet_id in terminal_packets:
            continue
        round_number = int(row["payload"]["inputs"]["round_number"])
        current = latest.get(packet_id)
        if current is None or round_number > int(current["payload"]["inputs"]["round_number"]):
            latest[packet_id] = row

    counts: Counter[str] = Counter()
    for packet_id, row in latest.items():
        round_number = int(row["payload"]["inputs"]["round_number"])
        verifier_path = (
            root / ".zen" / "graph-jobs" / run_id / packet_id
            / f"round-{round_number:02d}" / "verifier.json"
        )
        if not verifier_path.is_file():
            counts["missing_verifier_artifact"] += 1
            continue
        decision = json.loads(verifier_path.read_text(encoding="utf-8"))["decision"]
        if decision.get("decision") != "FAIL":
            continue
        payload = row["payload"]
        max_rounds = int(payload.get("max_repair_rounds", 3))
        next_round = round_number + 1
        conversation_job_key = payload.get(
            "conversation_job_key",
            row["job_key"].split(":repair-round-", 1)[0],
        )
        common = {
            key: payload[key]
            for key in (
                "source_content_sha256", "packet_id", "configuration_key",
                "qualification_status", "agent_audit_sha256", "max_repair_rounds",
            )
            if key in payload
        }
        common["conversation_job_key"] = conversation_job_key
        base_inputs = payload["inputs"]
        if next_round >= max_rounds:
            inserted = queue.enqueue(
                run_id,
                conversation_job_key,
                "terminal",
                {
                    **common,
                    "tool": "golden.graph_terminal",
                    "inputs": {
                        "packet_batch": base_inputs["packet_batch"],
                        "packet_index": base_inputs["packet_index"],
                        "packet_id": packet_id,
                        "source_decision_run_id": run_id,
                        "round_number": round_number,
                        "terminal_status": "QUARANTINED",
                    },
                    "terminal_reason": "maximum repair rounds exhausted",
                },
                max_attempts=1,
                priority=30,
            )
            counts["terminal_inserted" if inserted else "terminal_existing"] += 1
            continue
        inserted = queue.enqueue(
            run_id,
            f"{conversation_job_key}:repair-round-{next_round:02d}",
            "repair",
            {
                **common,
                "tool": "golden.graph_repair",
                "inputs": {
                    "packet_batch": base_inputs["packet_batch"],
                    "packet_index": base_inputs["packet_index"],
                    "packet_id": packet_id,
                    "source_decision_run_id": run_id,
                    "round_number": next_round,
                },
            },
            max_attempts=2,
            priority=50,
        )
        counts["repair_inserted" if inserted else "repair_existing"] += 1
    counts["stalled_candidates"] = len(latest)
    return dict(counts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zen-factory-refine",
        description="Run the durable refine, verify, repair and terminal factory stages",
    )
    parser.add_argument("run_id")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--enqueue-existing", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-items", type=int, default=2000)
    parser.add_argument("--max-repair-rounds", type=int, default=3)
    args = parser.parse_args(argv)

    queue = qualification = None
    try:
        config = load_config(args.root)
        plugins = load_plugins(config.plugin_paths)
        queue = LocalFactoryQueue(config.state_directory / "factory-queue.db")
        qualification = FactoryQualificationStore(
            config.state_directory / "factory-qualification.db"
        )
        enqueued = {}
        reconciled = {}
        if args.enqueue_existing:
            enqueued = enqueue_existing(
                config.root, args.run_id, queue, qualification,
                limit=args.limit, max_repair_rounds=args.max_repair_rounds,
            )
            reconciled = reconcile_stalled_repairs(config.root, args.run_id, queue)
        queue.close()
        queue = None
        qualification.close()
        qualification = None

        pool = ParallelFactoryWorkerPool(config, plugins.tools, workers=args.workers)
        results = pool.run_until_idle(
            args.run_id, DOWNSTREAM_STAGES, max_items=args.max_items
        )
        queue = LocalFactoryQueue(config.state_directory / "factory-queue.db")
        stage_counts = queue.counts_by_stage(args.run_id)
        dead = sum(values.get("DEAD", 0) for values in stage_counts.values())
        ready = sum(values.get("READY", 0) for values in stage_counts.values())
        summary = {
            "schema_version": "zen.factory-refinement-summary/1",
            "run_id": args.run_id,
            "model_policy": "gpt-5.6-sol-only",
            "enqueued": enqueued,
            "reconciled": reconciled,
            "processed": len(results),
            "queue": stage_counts,
            "dead": dead,
            "ready": ready,
        }
        print(json.dumps(summary, indent=2))
        return 0 if dead == 0 and ready == 0 else 2
    except Exception as exc:
        print(
            f"zen-factory-refine failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    finally:
        if qualification is not None:
            qualification.close()
        if queue is not None:
            queue.close()


if __name__ == "__main__":
    raise SystemExit(main())
