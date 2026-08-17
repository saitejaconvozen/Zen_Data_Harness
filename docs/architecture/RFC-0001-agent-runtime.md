# RFC-0001: Zen agent runtime

Status: accepted for implementation baseline

## Decision

Build Zen as a generic, durable agent runtime. Domain behavior is installed by
plugins that register schemas, tools, workflows, validators, and skills. The
golden-conversation system is the first plugin, not the runtime itself.

## Required properties

- Objective-driven: an operator supplies an objective and governed inputs.
- Resumable: committed work survives process interruption.
- Auditable: task transitions, tool calls, results, and decisions are events.
- Verifiable: artifacts are immutable and content-addressed.
- Governed: deterministic policy and approval checks dominate instructions.
- Extensible: a second plugin installs without changes to the kernel.
- Honest: completion is a predicate, not a model assertion.

## Components

1. The context compiler resolves repository instructions, workflow contracts,
   selected skill bodies, task inputs, and prior evidence within a budget.
2. The planner selects a registered workflow and produces a bounded task DAG.
3. The supervisor advances tasks through the lifecycle and enforces budgets.
4. The tool registry validates named, schema-described operations.
5. The policy engine permits, denies, or pauses operations by risk and scope.
6. The event store is the durable source of operational state.
7. The artifact store commits immutable, checksum-addressed results.
8. Validators decide whether a task result can be committed.
9. Human review can accept, edit, reject, or return an artifact for repair.
10. Worker adapters execute bounded model tasks. The first adapter is Codex CLI.

## Initial technology choices

- Python 3.11+ and standard-library-first implementation.
- SQLite with WAL for a single-node coordinator.
- JSON-compatible tool and artifact contracts.
- Project-local Agent Skills directories using `SKILL.md`.
- `codex exec` process adapter pinned to `gpt-5.6-sol`; no Gemini client.

## Explicitly deferred

- Distributed scheduling, remote worker queues, and multi-agent teams.
- Production MongoDB execution.
- Autonomous modification of skills or policies.
- Training, preference, reward, RL, and benchmark artifact production.
- Declaring model-refined conversations released without human review.

## Migration boundary

The existing `/home/ubuntu/Sai_Teja/src/zen_data_engine` package remains intact.
The plugin will first call it through narrow adapter tools. Code is moved only
after parity tests demonstrate that the new boundary preserves its invariants.
