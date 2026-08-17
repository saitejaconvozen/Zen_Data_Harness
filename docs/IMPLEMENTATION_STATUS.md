# Implementation status

Status here is evidence-based. A stage is "exercised" only when the state
stores contain rows produced by a real run, and "implemented, unexercised" when
the code and tests exist but no production data has passed through it.

## Autonomous coding kernel — exercised

- Pinned GPT-5.6 Sol structured model transport with no target-workspace access.
- Durable coding sessions, turns, tool calls, feedback, cancellation, and append-only events.
- Strict planner, executor, investigator, and verifier manifests.
- Contained reads, checksum-guarded writes, search, bounded non-shell processes, and Git evidence.
- Bounded parallel read-only delegation and fresh verification with repair cycles.
- Lifecycle hooks, context hierarchy, skills, episodic memory, approved curated memory, and a localhost gateway.

Observed: 18 coding sessions (13 SUCCEEDED, 3 FAILED, 2 WAITING_FOR_HUMAN).

## Conversation factory — exercised

- Read-only MongoDB source ingestion and source-bound packet preparation.
- Deterministic assistant-turn format analysis (`zen_agent.turn_format`) resolving
  mandatory language tags before any model call.
- Planner/critic planning, durable queueing, and parallel GPT-5.6-sol workers.
- Turn-quality refinement with per-turn `source_quality` and `downstream_coherence`,
  taxonomy citations, and independent verification.
- Protected review website showing source/refined turns and Axis → Sub-axis → Variant evidence.

Observed: 2 runs, 91 conversations reaching a terminal state.

## Human review and self-improvement — implemented, unexercised

The code paths and tests exist; no production data has passed through them.

- Authenticated human approve/edit/reject/request-repair decisions in an append-only SQLite ledger.
- Feedback routing that verifies packet identity and every immutable user-turn hash.
- Isolated human-feedback repair, trajectory safety, independent re-verification, and revised-candidate reopening.
- Deterministic failure and reviewer-feedback clustering.
- Immutable prompt/plugin/workflow proposals, disjoint held-out evaluations, independent-evaluator gates, human approval, and promotion records.
- Unified `zen-factory-self-improve` operator and reusable `improve-data-engine` skill.

Observed: 91 `review_items`, all `REVIEW_PENDING`. 0 `review_decisions`.
0 `improvement_proposals`. **The loop has never closed end to end.** Until a
human works through a review batch, every claim about clustering, candidate
evaluation, and promotion is untested against real reviewer behaviour.

## Deliberate governance boundary

"Self-improving" means the harness can discover gaps, produce and evaluate versioned candidates, and recommend promotion. An ordinary data run cannot silently rewrite or activate shared prompts, plugins, workflows, skills, taxonomy, policy, or release criteria. Eligible candidates end in `PROMOTED_NOT_ACTIVATED` and enter normal organizational review. This boundary prevents dataset feedback from becoming unreviewed executable policy.

## Known calibration debt

The refiner has never been scored against human labels. Before scaling, hand-label
a held-out set of at least 20 conversations and measure agreement on
`source_quality` and `downstream_coherence`. Yield and replace-rate telemetry are
not a substitute for that measurement.

## Remaining scale-hardening work

Before treating 5,000+ conversations as routine, add distributed queue/database backends, object storage, tenant-scoped encryption and retention controls, reviewer SSO/RBAC, held-out split registry automation, model-cost/concurrency budgets, observability/SLOs, disaster recovery, and repeated adversarial calibration.

For mature coding-harness parity, PTY/browser tools, MCP-compatible external tool
servers, Git worktree isolation for concurrent writers, cron/event triggers, and
OS-enforced network sandboxing remain extensions. Zen does not claim
feature-for-feature parity with Codex, Hermes, or OpenClaw.

## Running the tests

```bash
.venv/bin/python -m unittest discover -s tests -q
```
