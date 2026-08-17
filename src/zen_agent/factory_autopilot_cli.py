from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .config import load_config
from .factory_control_state import FactoryControlState
from .factory_queue import LocalFactoryQueue
from .factory_qualification import FactoryQualificationStore
from .factory_refinement_cli import (
    DOWNSTREAM_STAGES, enqueue_existing, reconcile_stalled_repairs,
)
from .factory_review import publish
from .factory_worker_pool import ParallelFactoryWorkerPool
from .plugins import load_plugins


def _stage_totals(queue: LocalFactoryQueue, run_id: str) -> tuple[int, int, int]:
    counts = queue.counts_by_stage(run_id)
    ready = sum(
        values.get("READY", 0) + values.get("LEASED", 0)
        for stage, values in counts.items()
        if stage in DOWNSTREAM_STAGES
    )
    dead = sum(
        values.get("DEAD", 0)
        for stage, values in counts.items()
        if stage in DOWNSTREAM_STAGES
    )
    terminal = counts.get("terminal", {}).get("SUCCEEDED", 0)
    return ready, dead, terminal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zen-factory-autopilot",
        description=(
            "Autonomously enqueue, refine, independently verify, repair, "
            "terminalize and publish one governed factory run"
        ),
    )
    parser.add_argument("run_id")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-items", type=int, default=5000)
    parser.add_argument("--publish-every", type=int, default=16)
    parser.add_argument("--max-repair-rounds", type=int, default=3)
    args = parser.parse_args(argv)
    if args.publish_every < 1 or args.max_items < 1:
        parser.error("publish-every and max-items must be positive")

    control = queue = qualification = None
    try:
        config = load_config(args.root)
        plugins = load_plugins(config.plugin_paths)
        control = FactoryControlState(config.state_directory / "factory-control.db")
        queue = LocalFactoryQueue(config.state_directory / "factory-queue.db")
        qualification = FactoryQualificationStore(
            config.state_directory / "factory-qualification.db"
        )
        samples = qualification.samples(args.run_id)
        if not samples:
            raise ValueError("run has no committed qualification samples")
        control.set_run_status(
            args.run_id,
            "RUNNING",
            "autonomous downstream refine, verify, repair and publication in progress",
        )
        enqueued = enqueue_existing(
            config.root,
            args.run_id,
            queue,
            qualification,
            limit=None,
            max_repair_rounds=args.max_repair_rounds,
        )
        reconciled = reconcile_stalled_repairs(config.root, args.run_id, queue)
        publish(config.root, args.run_id, args.site.resolve())
        queue.close()
        queue = None
        qualification.close()
        qualification = None

        pool = ParallelFactoryWorkerPool(config, plugins.tools, workers=args.workers)
        processed = 0
        while processed < args.max_items:
            batch = pool.run_until_idle(
                args.run_id,
                DOWNSTREAM_STAGES,
                max_items=min(args.publish_every, args.max_items - processed),
            )
            processed += len(batch)
            publish(config.root, args.run_id, args.site.resolve())
            if not batch:
                break

        queue = LocalFactoryQueue(config.state_directory / "factory-queue.db")
        ready, dead, terminal = _stage_totals(queue, args.run_id)
        complete = terminal == len(samples) and ready == 0 and dead == 0
        if dead:
            status = "NEEDS_HUMAN"
            reason = f"{dead} downstream work items dead-lettered"
        elif complete:
            status = "PAUSED"
            reason = (
                f"all {len(samples)} selected conversations reached terminal "
                "states; human release review is required"
            )
        else:
            status = "PAUSED"
            reason = (
                f"autopilot budget ended with {terminal}/{len(samples)} "
                f"terminal conversations and {ready} runnable items"
            )
        control.set_run_status(args.run_id, status, reason)
        publication = publish(config.root, args.run_id, args.site.resolve())
        result = {
            "schema_version": "zen.factory-autopilot-summary/1",
            "run_id": args.run_id,
            "status": status,
            "reason": reason,
            "model_policy": "gpt-5.6-sol-only",
            "enqueued": enqueued,
            "reconciled": reconciled,
            "processed": processed,
            "selected": len(samples),
            "terminal": terminal,
            "dead": dead,
            "runnable": ready,
            "publication": publication,
        }
        print(json.dumps(result, indent=2))
        return 0 if complete else 2
    except Exception as exc:
        if control is not None:
            try:
                control.set_run_status(
                    args.run_id,
                    "NEEDS_HUMAN",
                    f"autopilot failed: {type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
        print(
            f"zen-factory-autopilot failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    finally:
        if qualification is not None:
            qualification.close()
        if queue is not None:
            queue.close()
        if control is not None:
            control.close()


if __name__ == "__main__":
    raise SystemExit(main())
