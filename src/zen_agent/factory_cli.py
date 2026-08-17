from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from uuid import uuid4

from .artifacts import ArtifactStore
from .config import load_config
from .factory import default_factory_manifest
from .factory_control_state import FactoryControlState
from .factory_planner import (
    CRITIC_SCHEMA,
    PLANNER_SCHEMA,
    IsolatedCodexRole,
    build_observation_from_inventory,
    compile_plan,
    critic_prompt,
    planner_prompt,
)
from .factory_qualification import FactoryQualificationStore
from .factory_queue import LocalFactoryQueue
from .factory_worker import FactoryWorker, factory_artifact_store
from .plugins import load_plugins


def _inside(root: Path, value: Path) -> Path:
    path = value if value.is_absolute() else root / value
    path = path.resolve()
    if root != path and root not in path.parents:
        raise PermissionError("path escapes harness workspace")
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="zen-factory", description="Adaptive conversation factory control plane"
    )
    command.add_argument("--root", type=Path, default=Path.cwd())
    sub = command.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--target", type=int, default=5000)
    create.add_argument("--candidate-multiplier", type=int, default=4)
    create.add_argument("--model-concurrency", type=int, default=8)
    plan = sub.add_parser("plan")
    plan.add_argument("run_id")
    plan.add_argument("--inventory-artifact", type=Path, required=True)
    plan.add_argument("--accepted", type=int, default=0)
    plan.add_argument("--seen", type=int, default=0)
    plan.add_argument("--coverage-gaps", type=Path)
    plan.add_argument("--dead-letter-rate", type=float, default=0.0)
    plan.add_argument("--privacy-failure-rate", type=float, default=0.0)
    plan.add_argument("--dry-run", action="store_true")
    work = sub.add_parser("work")
    work.add_argument("run_id")
    work.add_argument(
        "--stage", action="append",
        choices=["trace_fetch", "prepare_packets", "agent_audit", "refine"],
    )
    work.add_argument("--max-items", type=int, default=1)
    work.add_argument("--worker-id", default=None)
    status = sub.add_parser("status")
    status.add_argument("run_id")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    state = queue = qualification = None
    try:
        config = load_config(args.root)
        state = FactoryControlState(config.state_directory / "factory-control.db")
        queue = LocalFactoryQueue(config.state_directory / "factory-queue.db")
        qualification = FactoryQualificationStore(
            config.state_directory / "factory-qualification.db"
        )
        artifacts = ArtifactStore(config.state_directory / "factory-control-artifacts")
        if args.command == "create":
            manifest = default_factory_manifest(
                args.target, args.candidate_multiplier, args.model_concurrency
            )
            run_id = state.create_run(manifest)
            print(json.dumps({"run_id": run_id, "manifest": manifest.to_dict()}, indent=2))
            return 0
        if args.command == "status":
            print(json.dumps({
                "run": state.run(args.run_id),
                "cycles": state.cycles(args.run_id),
                "queue": queue.counts(args.run_id),
                "qualification": qualification.summary(args.run_id),
            }, indent=2))
            return 0
        if args.command == "plan":
            run = state.run(args.run_id)
            manifest = run["manifest"]
            inventory_path = _inside(config.root, args.inventory_artifact)
            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            gaps = ()
            if args.coverage_gaps:
                gaps_path = _inside(config.root, args.coverage_gaps)
                loaded = json.loads(gaps_path.read_text(encoding="utf-8"))
                if not isinstance(loaded, list):
                    raise ValueError("coverage gaps file must contain an array")
                gaps = tuple(loaded)
            cycle = state.next_cycle(args.run_id)
            observation = build_observation_from_inventory(
                args.run_id,
                cycle,
                inventory,
                target_accepted=manifest["target_accepted"],
                candidate_floor=manifest["candidate_floor"],
                accepted_count=args.accepted,
                unique_candidates_seen=args.seen,
                queue_counts=queue.counts(args.run_id),
                coverage_gaps=gaps,
                dead_letter_rate=args.dead_letter_rate,
                privacy_failure_rate=args.privacy_failure_rate,
            )
            observation_record = artifacts.put_json(observation.to_dict())
            state.start_cycle(args.run_id, cycle, observation_record.sha256)
            try:
                role = IsolatedCodexRole()
                prompt_root = config.root / "plugins" / "factory-control" / "prompts"
                proposal = role.execute(
                    planner_prompt(
                        observation,
                        (prompt_root / "factory-planner.md").read_text(encoding="utf-8"),
                    ),
                    PLANNER_SCHEMA,
                )
                critique = role.execute(
                    critic_prompt(
                        observation,
                        proposal,
                        (prompt_root / "plan-critic.md").read_text(encoding="utf-8"),
                    ),
                    CRITIC_SCHEMA,
                )
                compiled = compile_plan(observation, proposal, critique)
                proposal_record = artifacts.put_json(proposal)
                critique_record = artifacts.put_json(critique)
                compiled_record = artifacts.put_json(compiled.to_dict())
                inserted = 0
                if not args.dry_run:
                    for seed in compiled.queue_seeds:
                        inserted += queue.enqueue(**asdict(seed))
                state.finish_cycle(
                    args.run_id,
                    cycle,
                    status="COMPILED",
                    proposal_sha256=proposal_record.sha256,
                    critique_sha256=critique_record.sha256,
                    compiled_sha256=compiled_record.sha256,
                    action=compiled.action,
                )
                print(json.dumps({
                    "run_id": args.run_id,
                    "cycle": cycle,
                    "dry_run": args.dry_run,
                    "compiled_plan": compiled.to_dict(),
                    "queue_items_inserted": inserted,
                    "artifacts": {
                        "observation": observation_record.sha256,
                        "proposal": proposal_record.sha256,
                        "critique": critique_record.sha256,
                        "compiled": compiled_record.sha256,
                    },
                }, indent=2))
                return 0
            except Exception as exc:
                state.fail_cycle(args.run_id, cycle, f"{type(exc).__name__}: {exc}")
                raise
        if args.command == "work":
            plugins = load_plugins(config.plugin_paths)
            worker = FactoryWorker(
                config,
                plugins.tools,
                queue,
                factory_artifact_store(config),
                args.worker_id or f"local-{uuid4().hex[:12]}",
                qualification,
            )
            stages = tuple(
                args.stage or ["trace_fetch", "prepare_packets", "agent_audit"]
            )
            results = worker.run_until_idle(
                args.run_id, stages, max_items=args.max_items
            )
            print(json.dumps({
                "processed": len(results), "results": results,
                "queue": queue.counts(args.run_id),
            }, indent=2))
            return 0 if all(item["status"] == "SUCCEEDED" for item in results) else 2
        raise AssertionError(args.command)
    except Exception as exc:
        print(f"zen-factory failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if qualification is not None:
            qualification.close()
        if queue is not None:
            queue.close()
        if state is not None:
            state.close()


if __name__ == "__main__":
    raise SystemExit(main())
