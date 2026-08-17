---
name: orchestrate-data-factory
description: Operate and inspect the sharded Zen conversation-data factory for large governed curation runs. Use when planning, starting, resuming, scaling, or auditing candidate discovery, agent-configuration assessment, conversation refinement, independent verification, repair loops, coverage selection, human review, or corpus assembly for hundreds to thousands of conversations.
---

# Orchestrate Data Factory

Operate the harness as a bounded, resumable factory. Treat target counts as accepted outputs, keep raw user turns immutable, and fail closed when lineage, privacy, trajectory, or verifier evidence is incomplete.

## Before a Run

1. Read `ZEN.md`, `docs/architecture/RFC-0002-conversation-factory.md`, and `docs/production/MONGODB_ACCESS.md`.
2. Inspect current manifests, queue depth, dead letters, coverage gaps, model quota, storage, and review capacity.
3. Confirm the target, approved taxonomy version, source time range, agent configuration versions, benchmark exclusion rules, and human escalation policy.
4. Generate a dry-run manifest. Do not infer that 5,000 sampled traces will yield 5,000 accepted conversations.

## Plan the Factory

- Start from `default_factory_manifest`; change concurrency only from measured rate limits and latency.
- Use deterministic shards for Mongo scanning and gates. Use one isolated conversation per GPT-5.6-sol refinement or verification session.
- Store only opaque source references in queue payloads. Put restricted content in content-addressed artifact storage.
- Separate refiners and verifiers by role, session, scratch directory, and task lease.
- Split benchmark groups before refinement and prohibit benchmark identifiers from training exports.

## Execute and Supervise

1. Enqueue idempotent work by `(run_id, source_binding, stage, revision)`.
2. Workers atomically claim leases, heartbeat long work, and commit with the same lease token.
3. Route deterministic gate failures to rejection; route verifier FAIL to bounded repair; route ABSTAIN, replay-required, and exhausted cases to quarantine or human review.
4. Refill candidate discovery when projected accepted yield or coverage floors fall short.
5. Pause dispatch when privacy failures, stale-source rates, dead-letter rates, quota errors, or quality drift cross their approved thresholds.

## Verify Completion

Do not declare success from queue emptiness. Require all of the following:

- accepted count meets the target;
- every required coverage cell meets its floor;
- no accepted item lacks source binding, taxonomy version, model/session provenance, deterministic validation, or independent verifier PASS;
- training and benchmark identities remain disjoint by source group and time policy;
- dead letters and quarantines are explained and exported for review;
- a reproducible signed manifest references immutable artifacts.

## Scaling Rule

Add worker processes only behind the durable queue. Never solve backlog by embedding thousands of conversations in one prompt, one process, one SQLite transaction, or one permanent agent tree. The local SQLite queue is for forward tests; use the production transactional adapter before multi-host execution.

## Human Control

Humans approve policy, taxonomy versions, benchmark split policy, production promotion, and adjudicated samples. The harness performs collection, refinement, verification, retries, evidence capture, and coverage accounting autonomously within those boundaries.

Read `references/acceptance-and-capacity.md` when sizing a run or deciding whether a shortfall requires more candidates, more workers, or quality-policy changes.
