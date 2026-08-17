---
name: operate-data-engine
description: Plan, execute, verify, resume, and audit governed data-engine jobs with Zen. Use for objective-driven dataset inventory, validation, transformation, corpus construction, artifact review, failure recovery, or development of new Zen workflows and plugins.
---

# Operate Data Engine

Use the repository `ZEN.md` as the governing contract. Read
`references/runtime-contract.md` when changing task states, tool policies,
artifacts, workflow completion, or human-review behavior.

## Execute a job

1. Restate the objective as observable completion predicates.
2. Select an installed workflow; stop if none owns the objective.
3. Validate governed inputs before creating a run.
4. Produce a bounded task DAG with explicit dependencies and attempts.
5. Execute only registered tools that pass deterministic policy checks.
6. Validate outputs, commit immutable artifacts, and record state events.
7. Replan or retry only with changed evidence or strategy.
8. Verify all required artifacts and predicates before reporting success.
9. Return `NEEDS_HUMAN`, `BLOCKED`, or `QUARANTINED` honestly when automation
   lacks authority or reliable evidence.

## Extend the harness

Keep new domain behavior inside a plugin. Register narrow tools and workflows;
do not add domain branches to the supervisor. Add acceptance tests proving the
plugin installs without kernel edits. Keep production writes approval-gated.
