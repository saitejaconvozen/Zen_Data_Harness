from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore
from .factory_control_state import FactoryControlState
from .factory_planner import (
    CRITIC_SCHEMA,
    PLANNER_SCHEMA,
    PlanNotUsable,
    IsolatedCodexRole,
    build_observation_from_inventory,
    compile_plan,
    critic_prompt,
    planner_prompt,
)
from .factory_qualification import FactoryQualificationStore
from .factory_queue import LocalFactoryQueue
from .factory_worker import FactoryWorker


ACQUISITION_STAGES = ("trace_fetch", "prepare_packets", "agent_audit")


class FactoryOperator:
    """Bounded observe-plan-criticize-compile-work-replan loop."""

    def __init__(
        self,
        root: Path,
        state: FactoryControlState,
        queue: LocalFactoryQueue,
        qualification: FactoryQualificationStore,
        control_artifacts: ArtifactStore,
        worker: FactoryWorker,
        role: IsolatedCodexRole | None = None,
    ):
        self.root = root.resolve()
        self.state = state
        self.queue = queue
        self.qualification = qualification
        self.control_artifacts = control_artifacts
        self.worker = worker
        self.role = role or IsolatedCodexRole()

    def operate(
        self,
        run_id: str,
        inventory: dict[str, Any],
        *,
        max_planning_cycles: int,
        max_work_items: int,
        accepted_count: int = 0,
        coverage_gaps: tuple[dict[str, Any], ...] = (),
        dead_budget: int = 0,
        max_consecutive_rejections: int = 3,
    ) -> dict[str, Any]:
        """Run the bounded acquisition loop.

        ``dead_budget`` is how many permanently-failed work items the run may
        absorb before escalating. Transient failures that the queue will retry
        never halt the run; over a long unattended batch they are expected.
        """

        if max_planning_cycles < 1 or max_work_items < 1:
            raise ValueError("operator budgets must be positive")
        if dead_budget < 0:
            raise ValueError("dead_budget cannot be negative")
        planned = processed = 0
        dead_seen = rejected = 0
        actions: list[dict[str, Any]] = []
        while planned < max_planning_cycles and processed < max_work_items:
            stage_counts = self.queue.counts_by_stage(run_id)
            acquisition_ready = sum(
                stage_counts.get(stage, {}).get("READY", 0)
                for stage in ACQUISITION_STAGES
            )
            if acquisition_ready:
                results = self.worker.run_until_idle(
                    run_id,
                    ACQUISITION_STAGES,
                    max_items=min(max_work_items - processed, acquisition_ready),
                )
                processed += len(results)
                actions.append({"kind": "WORK", "results": results})
                # A retryable failure is re-queued by the worker and is not a
                # blocker; only permanently dead work counts against the budget.
                dead_seen += sum(1 for item in results if item["status"] == "DEAD")
                if dead_seen > dead_budget:
                    blocker = (
                        f"{dead_seen} acquisition work items dead-lettered, "
                        f"exceeding the budget of {dead_budget}"
                    )
                    self.state.set_run_status(run_id, "NEEDS_HUMAN", blocker)
                    return self._summary(
                        run_id, "BLOCKED", planned, processed, actions,
                        blocker,
                    )
                continue

            run = self.state.run(run_id)
            if run["status"] in {"PAUSED", "SUCCEEDED", "FAILED", "CANCELLED"}:
                return self._summary(
                    run_id, run["status"], planned, processed, actions,
                    run.get("reason") or "factory reached terminal control state",
                )
            qualification = self.qualification.summary(run_id)
            seen = sum(qualification["conversation_audits"].values())
            manifest = run["manifest"]
            cycle = self.state.next_cycle(run_id)
            observation = build_observation_from_inventory(
                run_id,
                cycle,
                inventory,
                target_accepted=manifest["target_accepted"],
                candidate_floor=manifest["candidate_floor"],
                accepted_count=accepted_count,
                unique_candidates_seen=seen,
                queue_counts=self.queue.counts(run_id),
                coverage_gaps=coverage_gaps,
            )
            observation_record = self.control_artifacts.put_json(observation.to_dict())
            self.state.start_cycle(run_id, cycle, observation_record.sha256)
            try:
                prompt_root = self.root / "plugins" / "factory-control" / "prompts"
                proposal = self.role.execute(
                    planner_prompt(
                        observation,
                        (prompt_root / "factory-planner.md").read_text(encoding="utf-8"),
                    ),
                    PLANNER_SCHEMA,
                )
                critique = self.role.execute(
                    critic_prompt(
                        observation,
                        proposal,
                        (prompt_root / "plan-critic.md").read_text(encoding="utf-8"),
                    ),
                    CRITIC_SCHEMA,
                )
                compiled = compile_plan(observation, proposal, critique)
                proposal_record = self.control_artifacts.put_json(proposal)
                critique_record = self.control_artifacts.put_json(critique)
                compiled_record = self.control_artifacts.put_json(compiled.to_dict())
                inserted = sum(self.queue.enqueue(**asdict(seed)) for seed in compiled.queue_seeds)
                self.state.finish_cycle(
                    run_id,
                    cycle,
                    status="COMPILED",
                    proposal_sha256=proposal_record.sha256,
                    critique_sha256=critique_record.sha256,
                    compiled_sha256=compiled_record.sha256,
                    action=compiled.action,
                )
                planned += 1
                actions.append({
                    "kind": "PLAN", "cycle": cycle, "action": compiled.action,
                    "queue_items_inserted": inserted,
                    "compiled_sha256": compiled_record.sha256,
                })
                if compiled.action in {"PAUSE", "COMPLETE"}:
                    return self._summary(
                        run_id,
                        "PAUSED" if compiled.action == "PAUSE" else "SUCCEEDED",
                        planned,
                        processed,
                        actions,
                        compiled.rationale,
                    )
                if inserted == 0:
                    return self._summary(
                        run_id, "BLOCKED", planned, processed, actions,
                        "approved fetch compiled no new idempotent work",
                    )
            except PlanNotUsable as exc:
                # The gate did its job: no work was seeded. Re-plan within the
                # cycle budget instead of failing the whole batch.
                self.state.fail_cycle(run_id, cycle, f"{type(exc).__name__}: {exc}")
                planned += 1
                rejected += 1
                actions.append({"kind": "PLAN_REJECTED", "cycle": cycle, "reason": str(exc)})
                if rejected > max_consecutive_rejections:
                    blocker = (
                        f"{rejected} consecutive planning cycles produced no usable plan; "
                        "last reason: " + str(exc)
                    )
                    self.state.set_run_status(run_id, "NEEDS_HUMAN", blocker)
                    return self._summary(
                        run_id, "BLOCKED", planned, processed, actions, blocker,
                    )
                continue
            except Exception as exc:
                self.state.fail_cycle(run_id, cycle, f"{type(exc).__name__}: {exc}")
                raise
            rejected = 0
        return self._summary(
            run_id, "BUDGET_EXHAUSTED", planned, processed, actions,
            "bounded operator budget reached; resume with another operate command",
        )

    def _summary(
        self,
        run_id: str,
        status: str,
        planned: int,
        processed: int,
        actions: list[dict[str, Any]],
        reason: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": "zen.factory-operator-summary/1",
            "run_id": run_id,
            "operator_status": status,
            "reason": reason,
            "planning_cycles": planned,
            "work_items_processed": processed,
            "queue": self.queue.counts_by_stage(run_id),
            "qualification": self.qualification.summary(run_id),
            "actions": actions,
        }
