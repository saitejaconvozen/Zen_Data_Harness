"""End-to-end driver for one governed factory run.

`zen-factory-operate` acquires and audits conversations; `zen-factory-autopilot`
refines, verifies, repairs and publishes them. Neither alone carries a batch from
source traces to reviewable candidates, so a large unattended run had to be
babysat across two commands.

This driver alternates the two halves until both are idle or a budget is spent,
publishing as it goes. It is resumable: every stage is durable queue work, so
re-running the same run_id continues where the last invocation stopped.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

from .artifacts import ArtifactStore
from .config import load_config
from .factory_control_state import FactoryControlState
from .factory_operator import ACQUISITION_STAGES, FactoryOperator
from .factory_qualification import FactoryQualificationStore
from .factory_queue import LocalFactoryQueue
from .factory_refinement_cli import (
    DOWNSTREAM_STAGES,
    enqueue_existing,
    reconcile_stalled_repairs,
)
from .factory_review import publish
from .factory_worker_pool import ParallelFactoryWorkerPool
from .mongodb_credential import EphemeralMongoCredential
from .plugins import load_plugins


def _candidate_count(queue: LocalFactoryQueue, run_id: str) -> int:
    """Conversations that actually reached a reviewable terminal state."""

    counts = queue.terminal_status_counts(run_id)
    return counts.get("VERIFIED_CANDIDATE", 0) + counts.get("PARTIAL_CANDIDATE", 0)


def _stage_totals(queue: LocalFactoryQueue, run_id: str, stages) -> tuple[int, int]:
    counts = queue.counts_by_stage(run_id)
    ready = sum(
        values.get("READY", 0) + values.get("LEASED", 0)
        for stage, values in counts.items()
        if stage in stages
    )
    dead = sum(
        values.get("DEAD", 0)
        for stage, values in counts.items()
        if stage in stages
    )
    return ready, dead


def _workspace_file(config, value: Path) -> Path:
    path = value if value.is_absolute() else config.root / value
    path = path.resolve()
    if config.root not in path.parents or not path.is_file():
        raise PermissionError(f"{value} must be an existing file inside the workspace")
    return path


def _progress(label: str, payload: dict) -> None:
    print(f"[zen-factory-run] {label}: {json.dumps(payload)}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zen-factory-run",
        description=(
            "Carry one factory run from source traces to reviewable candidates: "
            "acquire, audit, refine, independently verify, repair and publish"
        ),
    )
    parser.add_argument("run_id")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--inventory-artifact", type=Path, required=True)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--coverage-gaps", type=Path)
    parser.add_argument("--accepted", type=int, default=0)
    parser.add_argument("--workers", type=int, default=8)
    # Budgets sized for a batch of ~1000 conversations.
    parser.add_argument("--max-planning-cycles", type=int, default=40)
    parser.add_argument("--max-acquisition-items", type=int, default=4000)
    parser.add_argument("--max-refinement-items", type=int, default=20000)
    parser.add_argument(
        "--dead-budget", type=int, default=25,
        help="permanently failed work items tolerated before escalating",
    )
    parser.add_argument("--max-repair-rounds", type=int, default=5)
    parser.add_argument("--publish-every", type=int, default=32)
    parser.add_argument(
        "--acquisition-per-pass", type=int, default=150,
        help="acquisition work items per pass before refinement gets a turn",
    )
    parser.add_argument(
        "--max-passes", type=int, default=100,
        help="acquisition/refinement alternations before stopping",
    )
    parser.add_argument("--prompt-mongodb-uri", action="store_true")
    args = parser.parse_args(argv)
    for name in ("max_acquisition_items", "max_refinement_items", "publish_every",
                 "max_passes", "max_planning_cycles", "acquisition_per_pass"):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.dead_budget < 0:
        parser.error("--dead-budget cannot be negative")

    started = time.time()
    control = queue = qualification = credential = None
    if args.prompt_mongodb_uri:
        credential = EphemeralMongoCredential.prompt()
        credential.inject()
    try:
        config = load_config(args.root)
        inventory = json.loads(
            _workspace_file(config, args.inventory_artifact).read_text(encoding="utf-8")
        )
        gaps: tuple = ()
        if args.coverage_gaps:
            loaded = json.loads(
                _workspace_file(config, args.coverage_gaps).read_text(encoding="utf-8")
            )
            if not isinstance(loaded, list):
                raise ValueError("coverage gaps must contain an array")
            gaps = tuple(loaded)

        site = args.site.resolve()
        plugins = load_plugins(config.plugin_paths)
        control = FactoryControlState(config.state_directory / "factory-control.db")
        queue = LocalFactoryQueue(config.state_directory / "factory-queue.db")
        qualification = FactoryQualificationStore(
            config.state_directory / "factory-qualification.db"
        )
        pool = ParallelFactoryWorkerPool(config, plugins.tools, workers=args.workers)
        operator = FactoryOperator(
            config.root, control, queue, qualification,
            ArtifactStore(config.state_directory / "factory-control-artifacts"),
            pool,
        )
        control.set_run_status(
            args.run_id, "RUNNING", "end-to-end factory run in progress"
        )

        acquired = refined = 0
        passes = 0
        operator_summary: dict = {}
        blocked_reason = None
        while passes < args.max_passes:
            passes += 1

            # 1. Acquire and audit whatever the planner still wants.
            if acquired < args.max_acquisition_items and blocked_reason is None:
                operator_summary = operator.operate(
                    args.run_id,
                    inventory,
                    max_planning_cycles=args.max_planning_cycles,
                    # Bound each pass so refinement runs in between. The planner
                    # stops when enough candidates exist, and candidates only
                    # appear once refinement has had a turn — sourcing the whole
                    # budget up front made that feedback arrive far too late.
                    max_work_items=max(1, min(
                        args.acquisition_per_pass,
                        args.max_acquisition_items - acquired,
                    )),
                    # Without live progress the planner believes nothing has
                    # been produced and keeps sourcing far past the target.
                    accepted_count=args.accepted + _candidate_count(queue, args.run_id),
                    coverage_gaps=gaps,
                    dead_budget=args.dead_budget,
                )
                acquired += operator_summary.get("work_items_processed", 0)
                _progress("acquisition", {
                    "pass": passes,
                    "status": operator_summary.get("operator_status"),
                    "acquired": acquired,
                })
                if operator_summary.get("operator_status") == "BLOCKED":
                    # Stop acquiring, but still finish work already acquired —
                    # a planning problem must not strand refined conversations.
                    blocked_reason = operator_summary.get("reason")

            # 2. Promote audited conversations into the refinement pipeline.
            enqueue_existing(
                config.root, args.run_id, queue, qualification,
                limit=None, max_repair_rounds=args.max_repair_rounds,
            )
            reconcile_stalled_repairs(config.root, args.run_id, queue)

            # 3. Drain refinement, publishing incrementally.
            drained = 0
            while refined < args.max_refinement_items:
                batch = pool.run_until_idle(
                    args.run_id,
                    DOWNSTREAM_STAGES,
                    max_items=min(
                        args.publish_every, args.max_refinement_items - refined
                    ),
                )
                if not batch:
                    break
                refined += len(batch)
                drained += len(batch)
                publish(config.root, args.run_id, site)
                terminal_now = queue.counts_by_stage(args.run_id).get(
                    "terminal", {}
                ).get("SUCCEEDED", 0)
                _progress("drain", {
                    "pass": passes,
                    "refined": refined,
                    "terminal": terminal_now,
                    "elapsed_s": round(time.time() - started),
                })
            _progress("refinement", {"pass": passes, "drained": drained, "refined": refined})

            acq_ready, acq_dead = _stage_totals(queue, args.run_id, ACQUISITION_STAGES)
            ref_ready, ref_dead = _stage_totals(queue, args.run_id, DOWNSTREAM_STAGES)
            if blocked_reason is not None and not ref_ready and not drained:
                break
            if acq_dead + ref_dead > args.dead_budget:
                blocked_reason = (
                    f"{acq_dead + ref_dead} work items dead-lettered, "
                    f"exceeding the budget of {args.dead_budget}"
                )
                break
            budget_left = (
                acquired < args.max_acquisition_items
                and refined < args.max_refinement_items
            )
            if not acq_ready and not ref_ready and not drained and not budget_left:
                break
            if not acq_ready and not ref_ready and not drained:
                break

        samples = qualification.samples(args.run_id)
        acq_ready, acq_dead = _stage_totals(queue, args.run_id, ACQUISITION_STAGES)
        ref_ready, ref_dead = _stage_totals(queue, args.run_id, DOWNSTREAM_STAGES)
        terminal = queue.counts_by_stage(args.run_id).get("terminal", {}).get(
            "SUCCEEDED", 0
        )
        complete = (
            bool(samples)
            and terminal == len(samples)
            and not acq_ready
            and not ref_ready
        )
        if blocked_reason:
            status, reason = "NEEDS_HUMAN", blocked_reason
        elif complete:
            status = "PAUSED"
            reason = (
                f"all {len(samples)} conversations reached terminal states; "
                "human release review is required"
            )
        else:
            status = "PAUSED"
            reason = (
                f"run budget ended with {terminal}/{len(samples)} terminal "
                f"conversations and {acq_ready + ref_ready} runnable items"
            )
        control.set_run_status(args.run_id, status, reason)
        publication = publish(config.root, args.run_id, site)
        result = {
            "schema_version": "zen.factory-run-summary/1",
            "run_id": args.run_id,
            "status": status,
            "reason": reason,
            "model_policy": "gpt-5.6-sol-only",
            "passes": passes,
            "selected": len(samples),
            "terminal": terminal,
            "acquisition_items": acquired,
            "refinement_items": refined,
            "runnable": acq_ready + ref_ready,
            "dead": acq_dead + ref_dead,
            "elapsed_seconds": round(time.time() - started, 1),
            "publication": publication,
        }
        print(json.dumps(result, indent=2))
        return 0 if complete else 2
    except Exception as exc:
        if control is not None:
            try:
                control.set_run_status(
                    args.run_id, "NEEDS_HUMAN",
                    f"end-to-end run failed: {type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
        print(f"zen-factory-run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if credential is not None:
            credential.restore()
        if qualification is not None:
            qualification.close()
        if queue is not None:
            queue.close()
        if control is not None:
            control.close()


if __name__ == "__main__":
    raise SystemExit(main())
