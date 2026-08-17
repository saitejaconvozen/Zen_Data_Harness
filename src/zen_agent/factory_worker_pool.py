from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
    ):
        # Each worker blocks on a model subprocess that is network-bound, so the
        # useful ceiling is provider throughput, not local CPU count.
        if not 1 <= workers <= 64:
            raise ValueError("factory worker count must be between 1 and 64")
        self.config = config
        self.tools = tools
        self.workers = workers
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
        if max_items < 1:
            raise ValueError("max_items must be positive")
        results: list[dict] = []
        with ThreadPoolExecutor(
            max_workers=self.workers, thread_name_prefix="zen-factory-worker"
        ) as pool:
            while len(results) < max_items:
                batch_size = min(self.workers, max_items - len(results))
                assignments = self._stage_assignments(
                    run_id, stages, batch_size
                )
                batch = list(pool.map(
                    lambda assigned: self._run_one(run_id, assigned),
                    assignments,
                ))
                completed = [item for item in batch if item is not None]
                results.extend(completed)
                if not completed:
                    break
        return results

    def _stage_assignments(
        self, run_id: str, stages: tuple[str, ...], batch_size: int
    ) -> list[tuple[str, ...]]:
        """Fairly reserve workers across ready stages to prevent starvation."""
        queue = LocalFactoryQueue(self.queue_path)
        try:
            counts = queue.counts_by_stage(run_id)
        finally:
            queue.close()
        remaining = {
            stage: counts.get(stage, {}).get("READY", 0)
            for stage in stages
        }
        active = [stage for stage in stages if remaining[stage] > 0]
        if not active:
            # Claims also recover expired leases, so retain an all-stage probe.
            return [stages for _ in range(batch_size)]
        assignments: list[tuple[str, ...]] = []
        while len(assignments) < batch_size and active:
            next_active = []
            for stage in active:
                if len(assignments) >= batch_size:
                    break
                assignments.append((stage,))
                remaining[stage] -= 1
                if remaining[stage] > 0:
                    next_active.append(stage)
            active = next_active
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
