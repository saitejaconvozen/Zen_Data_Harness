from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from uuid import uuid4

from .config import HarnessConfig
from .factory_qualification import FactoryQualificationStore
from .factory_queue import LocalFactoryQueue
from .factory_worker import FactoryWorker, factory_artifact_store
from .tools import ToolRegistry


class ParallelFactoryWorkerPool:
    """Single-host bounded worker pool; each worker owns its SQLite connections."""

    def __init__(
        self,
        config: HarnessConfig,
        tools: ToolRegistry,
        *,
        workers: int,
        stage_capacities: dict[str, int] | None = None,
    ):
        # Each worker blocks on a model subprocess that is network-bound, so the
        # useful ceiling is provider throughput, not local CPU count.
        if not 1 <= workers <= 64:
            raise ValueError("factory worker count must be between 1 and 64")
        if stage_capacities is not None and any(
            not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1
            for capacity in stage_capacities.values()
        ):
            raise ValueError("factory stage capacities must be positive integers")
        self.config = config
        self.tools = tools
        self.workers = workers
        self.stage_capacities = dict(stage_capacities or {})
        # Scheduling age gives continuously eligible stages a finite service
        # bound even when another stage has much greater backlog pressure.
        self._stage_wait_rounds: dict[str, int] = {}
        self._stage_cursor = 0
        self.queue_path = config.state_directory / "factory-queue.db"
        self.qualification_path = config.state_directory / "factory-qualification.db"
        # Create/migrate the shared schema before concurrent worker connections
        # can race on SQLite journal-mode initialization.
        qualification = FactoryQualificationStore(self.qualification_path)
        qualification.close()

    def run_until_idle(
        self,
        run_id: str,
        stages: tuple[str, ...],
        *,
        max_items: int,
    ) -> list[dict]:
        """Keep every worker busy until the queue empties or the budget is spent.

        This used to submit a wave of `workers` tasks with `pool.map` and wait
        for all of them. `map` is a barrier: one slow model call — and a call may
        run to a 900-second timeout — left the other forty-three workers idle
        until it returned. Observed concurrency decayed from 27 to 3 within an
        hour while over a thousand items sat queued.

        Now a worker that finishes immediately claims more work.
        """

        if max_items < 1:
            raise ValueError("max_items must be positive")
        results: list[dict] = []
        idle_returns = 0
        with ThreadPoolExecutor(
            max_workers=self.workers, thread_name_prefix="zen-factory-worker"
        ) as pool:
            initial = min(self.workers, max_items)
            pending = {
                pool.submit(self._run_one, run_id, assigned)
                for assigned in self._stage_assignments(run_id, stages, initial)
            }
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    try:
                        item = future.result()
                    except Exception as exc:  # a worker fault must not stop the pool
                        results.append({"stage": "?", "status": "DEAD", "error": str(exc)})
                        continue
                    if item is None:
                        # Nothing claimable right now. Stop refilling once every
                        # worker has come back empty, so the caller can re-plan.
                        idle_returns += 1
                        continue
                    idle_returns = 0
                    results.append(item)
                if idle_returns >= self.workers or len(results) >= max_items:
                    continue  # drain what is already running, submit nothing new
                refill = min(
                    self.workers - len(pending),
                    max_items - len(results) - len(pending),
                )
                for assigned in self._stage_assignments(run_id, stages, max(0, refill)):
                    pending.add(pool.submit(self._run_one, run_id, assigned))
        return results

    def _stage_assignments(
        self, run_id: str, stages: tuple[str, ...], batch_size: int
    ) -> list[tuple[str, ...]]:
        """Assign eligible capacity by backlog pressure with bounded aging."""
        if batch_size < 1:
            return []
        queue = LocalFactoryQueue(self.queue_path)
        try:
            counts = queue.counts_by_stage(run_id)
        finally:
            queue.close()
        remaining = {
            stage: counts.get(stage, {}).get("READY", 0)
            for stage in stages
        }
        available = {
            stage: max(
                0,
                self.stage_capacities.get(stage, self.workers)
                - counts.get(stage, {}).get("LEASED", 0),
            )
            for stage in stages
        }
        active = [
            stage for stage in stages
            if remaining[stage] > 0 and available[stage] > 0
        ]
        if not any(remaining.values()):
            # Claims also recover expired leases, so retain an all-stage probe.
            return [stages for _ in range(batch_size)]
        if not active:
            return []

        active_count = len(active)
        cursor = self._stage_cursor % active_count
        ordered = active[cursor:] + active[:cursor]
        order = {stage: index for index, stage in enumerate(ordered)}
        active_set = set(active)
        self._stage_wait_rounds = {
            stage: age + 1
            for stage, age in self._stage_wait_rounds.items()
            if stage in active_set
        }
        for stage in active:
            self._stage_wait_rounds.setdefault(stage, 1)
        starvation_bound = active_count
        pressure_capacity = {
            stage: self.stage_capacities.get(stage, self.workers)
            for stage in active
        }

        assignments: list[tuple[str, ...]] = []
        last_stage = ordered[0]
        while len(assignments) < batch_size:
            eligible = [
                stage for stage in ordered
                if remaining[stage] > 0 and available[stage] > 0
            ]
            if not eligible:
                break
            starved = [
                stage for stage in eligible
                if self._stage_wait_rounds[stage] >= starvation_bound
            ]
            if starved:
                chosen = max(
                    starved,
                    key=lambda stage: (
                        self._stage_wait_rounds[stage], -order[stage]
                    ),
                )
            else:
                chosen = max(
                    eligible,
                    key=lambda stage: (
                        remaining[stage] / pressure_capacity[stage],
                        remaining[stage],
                        -order[stage],
                    ),
                )
            assignments.append((chosen,))
            remaining[chosen] -= 1
            available[chosen] -= 1
            # A batch contains multiple service opportunities. Age every
            # continuously eligible stage after each one so a large batch
            # cannot starve a lower-pressure stage until the next refill.
            for stage in eligible:
                if stage == chosen:
                    self._stage_wait_rounds[stage] = 0
                else:
                    self._stage_wait_rounds[stage] += 1
            last_stage = chosen
        self._stage_cursor = (active.index(last_stage) + 1) % active_count
        return assignments

    def _run_one(self, run_id: str, stages: tuple[str, ...]) -> dict | None:
        queue = LocalFactoryQueue(self.queue_path)
        qualification = FactoryQualificationStore(self.qualification_path)
        try:
            worker = FactoryWorker(
                self.config,
                self.tools,
                queue,
                factory_artifact_store(self.config),
                f"pool-{uuid4().hex[:12]}",
                qualification,
            )
            return worker.run_one(run_id, stages)
        finally:
            qualification.close()
            queue.close()
