from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .artifacts import ArtifactStore
from .config import load_config
from .factory_control_state import FactoryControlState
from .factory_operator import FactoryOperator
from .factory_qualification import FactoryQualificationStore
from .factory_queue import LocalFactoryQueue
from .factory_worker_pool import ParallelFactoryWorkerPool
from .mongodb_credential import EphemeralMongoCredential
from .plugins import load_plugins


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zen-factory-operate")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("run_id")
    parser.add_argument("--inventory-artifact", type=Path, required=True)
    parser.add_argument("--coverage-gaps", type=Path)
    parser.add_argument("--accepted", type=int, default=0)
    parser.add_argument("--max-planning-cycles", type=int, default=3)
    parser.add_argument("--max-work-items", type=int, default=100)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--prompt-mongodb-uri", action="store_true")
    args = parser.parse_args(argv)
    state = queue = qualification = credential = None
    if args.prompt_mongodb_uri:
        credential = EphemeralMongoCredential.prompt()
        credential.inject()
    try:
        config = load_config(args.root)
        inventory_path = args.inventory_artifact
        if not inventory_path.is_absolute():
            inventory_path = config.root / inventory_path
        inventory_path = inventory_path.resolve()
        if config.root not in inventory_path.parents or not inventory_path.is_file():
            raise PermissionError("inventory artifact must be inside the harness workspace")
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        gaps = ()
        if args.coverage_gaps:
            gaps_path = args.coverage_gaps
            if not gaps_path.is_absolute():
                gaps_path = config.root / gaps_path
            gaps_path = gaps_path.resolve()
            if config.root not in gaps_path.parents or not gaps_path.is_file():
                raise PermissionError("coverage gaps must be inside the harness workspace")
            loaded = json.loads(gaps_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, list):
                raise ValueError("coverage gaps must contain an array")
            gaps = tuple(loaded)
        state = FactoryControlState(config.state_directory / "factory-control.db")
        queue = LocalFactoryQueue(config.state_directory / "factory-queue.db")
        qualification = FactoryQualificationStore(config.state_directory / "factory-qualification.db")
        plugins = load_plugins(config.plugin_paths)
        worker = ParallelFactoryWorkerPool(
            config, plugins.tools, workers=args.workers
        )
        operator = FactoryOperator(
            config.root,
            state,
            queue,
            qualification,
            ArtifactStore(config.state_directory / "factory-control-artifacts"),
            worker,
        )
        summary = operator.operate(
            args.run_id,
            inventory,
            max_planning_cycles=args.max_planning_cycles,
            max_work_items=args.max_work_items,
            accepted_count=args.accepted,
            coverage_gaps=gaps,
        )
        print(json.dumps(summary, indent=2))
        return 0 if summary["operator_status"] in {"SUCCEEDED", "PAUSED", "BUDGET_EXHAUSTED"} else 2
    except Exception as exc:
        print(f"zen-factory-operate failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if credential is not None:
            credential.restore()
        if qualification is not None:
            qualification.close()
        if queue is not None:
            queue.close()
        if state is not None:
            state.close()


if __name__ == "__main__":
    raise SystemExit(main())
