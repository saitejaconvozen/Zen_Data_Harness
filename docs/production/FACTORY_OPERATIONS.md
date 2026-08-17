# Conversation Factory Operations

## Current milestone

The harness now has an adaptive GPT-5.6-sol planner, an isolated GPT-5.6-sol plan critic, a deterministic compiler, durable queue leases, sharded Mongo scouts, source-bound packet preparation, parallel agent-auditor workers, and configuration-level qualification.

The current forward-test run is `509d13857e01475ca7250a62067e888f`. Inspect it with:

```bash
.venv/bin/zen-factory --root . status 509d13857e01475ca7250a62067e888f
```

## Create a run

```bash
.venv/bin/zen-factory --root . create \
  --target 5000 \
  --candidate-multiplier 4 \
  --model-concurrency 8
```

This creates durable state only. It does not fetch data or invoke models.

## Plan or replan

```bash
.venv/bin/zen-factory --root . plan RUN_ID \
  --inventory-artifact PATH_TO_INVENTORY_ARTIFACT \
  --accepted 0 \
  --seen 0
```

Each invocation creates a new planning cycle. The planner proposes a bounded action, the critic independently approves/rejects it, and deterministic compiler guards enforce model/session identities, known agents, scan budgets, completion predicates, failure-rate pauses, and at most 100 selected conversations per shard.

## Run workers

Inject Mongo credentials only into scout processes:

```bash
.venv/bin/zen-factory-operate --root . RUN_ID \
  --inventory-artifact PATH_TO_INVENTORY_ARTIFACT \
  --prompt-mongodb-uri --workers 8 \
  --max-planning-cycles 3 --max-work-items 100
```

Do not place the URI in scripts, command history, queue payloads, logs, or artifacts in production. Use the deployment secret manager or an owner-only environment file outside the repository.

Then run deterministic packet workers and isolated auditors:

```bash
.venv/bin/zen-factory --root . work RUN_ID \
  --stage prepare_packets --max-items 10 --worker-id packet-01

.venv/bin/zen-factory --root . work RUN_ID \
  --stage agent_audit --max-items 1 --worker-id auditor-01
```

Start multiple worker processes for concurrency. Queue claims are exclusive and use lease tokens; do not share one in-memory worker object across processes.

## Qualification behavior

Effective configuration identity is the digest of agent ID, agent version, and source-bound system-prompt digest. Metadata duplicates are normalized for fetching but never treated as one configuration audit.

A configuration is considered only after its registered audit sample is complete. Defaults are:

- at least three audited conversations;
- at least 80% PASS;
- 95% Wilson lower bound at least 0.35;
- zero critical prompt/workflow failures.

Insufficient evidence returns `PENDING` or `NEED_MORE`; it never silently qualifies an agent. Configuration qualification remains a strict source-quality and release signal. Individually audited conversations with `conversation_usable=true` may enter corrective refinement even when their configuration is not qualified; they retain the configuration status, must pass independent verification, and cannot silently qualify the agent. Unusable conversations terminate as `QUARANTINED`.

Corrective refinement eligibility and agent-configuration qualification are deliberately separate decisions.

## Autonomous bounded operation

Use one command to drain acquisition work and replan whenever the acquisition queue becomes idle:

```bash
.venv/bin/zen-factory-operate --root . RUN_ID \
  --inventory-artifact PATH_TO_INVENTORY_ARTIFACT \
  --prompt-mongodb-uri --workers 8 \
  --max-planning-cycles 3 --max-work-items 100
```

The operator stops at completion, planner pause, an infrastructure blocker, or either explicit budget. Resume by running the same command again; durable state prevents duplicate work.

## Autonomous downstream factory and review UI

After acquisition and agent auditing, the harness owns all remaining work:

```bash
.venv/bin/zen-factory-autopilot RUN_ID --root . \
  --site .zen/sites/SITE_ID \
  --workers 8 --max-repair-rounds 3
```

This single command idempotently queues every committed audit, sends usable
conversations through fresh GPT-5.6-sol refinement, independent verification,
bounded verifier-guided repair, trajectory safety, and independent
re-verification. It terminalizes every selected conversation, refreshes the
protected website at bounded checkpoints, and pauses for human release review.

The review studio displays exact source user turns, source/refined assistant
comparisons, KEEP/REPLACE actions, iteration history, and searchable
Axis → Subaxis → Variant mappings by conversation and turn. Only
`VERIFIED_CANDIDATE` means automated gates passed; it is not human release.

Use `zen-factory-refine` only as a lower-level recovery/drain command. Use
`zen-factory-review` only to rebuild the website from committed artifacts.


## Production boundary

The current SQLite implementation is a single-host forward-test backend. Before multi-host or 5,000-conversation production execution, complete the PostgreSQL adapter from `deploy/postgres/001_factory_control_plane.sql`, worker heartbeats, service supervision, metrics export, authenticated review, backup/restore, and staged soak tests.
