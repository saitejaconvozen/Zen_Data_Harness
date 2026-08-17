from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .config import load_config
from .factory_queue import LocalFactoryQueue
from .factory_worker_pool import ParallelFactoryWorkerPool
from .mongodb_credential import EphemeralMongoCredential
from .plugins import load_plugins


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zen-factory-drain",
        description="Drain already-approved factory work without planning new work",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("run_id")
    parser.add_argument(
        "--stage", action="append", required=True,
        choices=[
            "trace_fetch", "prepare_packets", "agent_audit", "refine", "verify",
            "repair", "trajectory_gate", "verify_repair", "terminal",
        ],
    )
    parser.add_argument("--max-items", type=int, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--prompt-mongodb-uri", action="store_true")
    args = parser.parse_args(argv)
    credential = queue = None
    try:
        if args.prompt_mongodb_uri:
            credential = EphemeralMongoCredential.prompt()
            credential.inject()
        config = load_config(args.root)
        plugins = load_plugins(config.plugin_paths)
        pool = ParallelFactoryWorkerPool(config, plugins.tools, workers=args.workers)
        results = pool.run_until_idle(
            args.run_id, tuple(args.stage), max_items=args.max_items
        )
        queue = LocalFactoryQueue(config.state_directory / "factory-queue.db")
        summary = {
            "schema_version": "zen.factory-drain-summary/1",
            "run_id": args.run_id,
            "processed": len(results),
            "results": results,
            "queue": queue.counts_by_stage(args.run_id),
            "planned_new_work": False,
        }
        print(json.dumps(summary, indent=2))
        return 0 if all(item["status"] == "SUCCEEDED" for item in results) else 2
    except Exception as exc:
        print(f"zen-factory-drain failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if queue is not None:
            queue.close()
        if credential is not None:
            credential.restore()


if __name__ == "__main__":
    raise SystemExit(main())
