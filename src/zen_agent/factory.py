from __future__ import annotations

from dataclasses import asdict, dataclass
from math import ceil
from typing import Any


@dataclass(frozen=True, slots=True)
class StageCapacity:
    name: str
    worker_role: str
    concurrency: int
    max_attempts: int
    shard_size: int = 1
    model: str | None = None
    independent_from: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.name or not self.worker_role:
            raise ValueError("factory stage requires name and worker_role")
        if not 1 <= self.concurrency <= 256:
            raise ValueError(f"invalid concurrency for {self.name}")
        if not 1 <= self.max_attempts <= 10:
            raise ValueError(f"invalid max_attempts for {self.name}")
        if not 1 <= self.shard_size <= 1000:
            raise ValueError(f"invalid shard_size for {self.name}")


@dataclass(frozen=True, slots=True)
class FactoryManifest:
    target_accepted: int
    candidate_floor: int
    max_repair_rounds: int
    stages: tuple[StageCapacity, ...]
    acceptance_is_fail_closed: bool = True
    preserve_user_turns: bool = True
    model_policy: str = "gpt-5.6-sol-only"

    def validate(self) -> None:
        if self.target_accepted < 1:
            raise ValueError("target_accepted must be positive")
        if self.candidate_floor < self.target_accepted:
            raise ValueError("candidate_floor cannot be below target_accepted")
        if not 1 <= self.max_repair_rounds <= 10:
            raise ValueError("max_repair_rounds must be between 1 and 10")
        names = [stage.name for stage in self.stages]
        if len(names) != len(set(names)):
            raise ValueError("factory stage names must be unique")
        for stage in self.stages:
            stage.validate()
            if stage.model not in {None, "gpt-5.6-sol"}:
                raise ValueError("model workers must use gpt-5.6-sol")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def expected_shards(self) -> dict[str, int]:
        return {
            stage.name: ceil(self.candidate_floor / stage.shard_size)
            for stage in self.stages
        }


def default_factory_manifest(
    target_accepted: int = 5000,
    candidate_multiplier: int = 4,
    model_concurrency: int = 8,
) -> FactoryManifest:
    """Return conservative defaults; concurrency must be tuned to measured account quotas."""
    if candidate_multiplier < 1:
        raise ValueError("candidate_multiplier must be positive")
    if not 1 <= model_concurrency <= 64:
        raise ValueError("model_concurrency must be between 1 and 64")
    manifest = FactoryManifest(
        target_accepted=target_accepted,
        candidate_floor=target_accepted * candidate_multiplier,
        max_repair_rounds=3,
        stages=(
            StageCapacity("metadata_scan", "MONGO_SCOUT", 16, 3, shard_size=250),
            StageCapacity("agent_audit", "AGENT_AUDITOR", model_concurrency, 2, model="gpt-5.6-sol"),
            StageCapacity("trace_fetch", "TRACE_FETCHER", 16, 3, shard_size=100),
            StageCapacity("privacy_quality_gate", "DETERMINISTIC_GATE", 32, 2, shard_size=100),
            StageCapacity("diversity_select", "COVERAGE_CURATOR", 4, 2, shard_size=250),
            StageCapacity("refine", "REFINER", model_concurrency, 2, model="gpt-5.6-sol"),
            StageCapacity("trajectory_gate", "TRAJECTORY_GATE", 32, 1),
            StageCapacity(
                "verify",
                "VERIFIER",
                model_concurrency,
                2,
                model="gpt-5.6-sol",
                independent_from=("refine", "repair"),
            ),
            StageCapacity("repair", "REPAIRER", model_concurrency, 2, model="gpt-5.6-sol"),
            StageCapacity("human_review", "HUMAN_REVIEW_ROUTER", 4, 3, shard_size=50),
            StageCapacity("publish", "CORPUS_ASSEMBLER", 4, 2, shard_size=250),
        ),
    )
    manifest.validate()
    return manifest
