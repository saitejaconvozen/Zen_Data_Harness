from __future__ import annotations

import json
from pathlib import Path
from statistics import median
from time import perf_counter_ns
from unittest.mock import patch

from zen_agent.factory_worker_pool import ParallelFactoryWorkerPool


STAGES = ("refine", "verify")
COUNTS = {
    "refine": {"READY": 1200, "LEASED": 0},
    "verify": {"READY": 120, "LEASED": 0},
}
CAPACITIES = {"refine": 16, "verify": 8}
WORKERS = 24
BATCH_SIZE = 16
ROUNDS = 5000
REPETITIONS = 5


class FixedCountsQueue:
    def __init__(self, _path: Path):
        pass

    def counts_by_stage(self, _run_id: str) -> dict[str, dict[str, int]]:
        return {stage: dict(statuses) for stage, statuses in COUNTS.items()}

    def close(self) -> None:
        pass


def adaptive_pool() -> ParallelFactoryWorkerPool:
    pool = object.__new__(ParallelFactoryWorkerPool)
    pool.workers = WORKERS
    pool.stage_capacities = dict(CAPACITIES)
    pool._stage_wait_rounds = {}
    pool._stage_cursor = 0
    pool.queue_path = Path("unused-fixed-counts.db")
    return pool


def equal_rotation(batch_size: int) -> list[tuple[str, ...]]:
    remaining = {stage: COUNTS[stage]["READY"] for stage in STAGES}
    available = {stage: CAPACITIES[stage] - COUNTS[stage]["LEASED"] for stage in STAGES}
    active = [stage for stage in STAGES if remaining[stage] and available[stage]]
    assignments: list[tuple[str, ...]] = []
    while len(assignments) < batch_size and active:
        next_active = []
        for stage in active:
            if len(assignments) >= batch_size:
                break
            assignments.append((stage,))
            remaining[stage] -= 1
            available[stage] -= 1
            if remaining[stage] and available[stage]:
                next_active.append(stage)
        active = next_active
    return assignments


def summarize(sequence: list[str], elapsed_ns: int) -> dict[str, float | int]:
    total = len(sequence)
    refine = sequence.count("refine")
    verify_positions = [index for index, stage in enumerate(sequence) if stage == "verify"]
    gaps = [right - left for left, right in zip(verify_positions, verify_positions[1:])]
    target_refine_share = COUNTS["refine"]["READY"] / sum(
        row["READY"] for row in COUNTS.values()
    )
    return {
        "assignments": total,
        "refine_assignments": refine,
        "verify_assignments": total - refine,
        "refine_share": round(refine / total, 6),
        "backlog_pressure_error_percentage_points": round(
            abs(refine / total - target_refine_share) * 100, 6
        ),
        "max_verify_dispatch_gap": max(gaps, default=0),
        "elapsed_ns": elapsed_ns,
        "assignments_per_second": round(total * 1_000_000_000 / elapsed_ns, 2),
    }


def run_equal() -> tuple[list[str], int]:
    sequence: list[str] = []
    started = perf_counter_ns()
    for _ in range(ROUNDS):
        sequence.extend(stage for stage, in equal_rotation(BATCH_SIZE))
    return sequence, perf_counter_ns() - started


def run_adaptive() -> tuple[list[str], int]:
    sequence: list[str] = []
    pool = adaptive_pool()
    started = perf_counter_ns()
    for _ in range(ROUNDS):
        sequence.extend(
            stage for stage, in pool._stage_assignments("benchmark", STAGES, BATCH_SIZE)
        )
    return sequence, perf_counter_ns() - started


def main() -> int:
    raw: dict[str, list[dict[str, float | int]]] = {"equal_rotation": [], "adaptive": []}
    with patch("zen_agent.factory_worker_pool.LocalFactoryQueue", FixedCountsQueue):
        for _ in range(REPETITIONS):
            equal_sequence, equal_ns = run_equal()
            adaptive_sequence, adaptive_ns = run_adaptive()
            raw["equal_rotation"].append(summarize(equal_sequence, equal_ns))
            raw["adaptive"].append(summarize(adaptive_sequence, adaptive_ns))

    aggregate = {}
    for policy, rows in raw.items():
        aggregate[policy] = {
            "median_assignments_per_second": median(
                row["assignments_per_second"] for row in rows
            ),
            "backlog_pressure_error_percentage_points": rows[0][
                "backlog_pressure_error_percentage_points"
            ],
            "max_verify_dispatch_gap": rows[0]["max_verify_dispatch_gap"],
            "refine_share": rows[0]["refine_share"],
        }
    print(json.dumps({
        "workload": {
            "counts": COUNTS, "capacities": CAPACITIES, "workers": WORKERS,
            "batch_size": BATCH_SIZE, "rounds": ROUNDS,
            "repetitions": REPETITIONS, "assignments_per_repetition": ROUNDS * BATCH_SIZE,
        },
        "raw": raw,
        "aggregate": aggregate,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
