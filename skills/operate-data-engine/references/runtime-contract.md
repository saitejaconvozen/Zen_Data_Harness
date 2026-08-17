# Runtime contract reference

## Invariants

- The event store is the operational source of truth.
- A task output is evidence until schema validation and artifact commit succeed.
- Required task success is necessary but may not be sufficient for workflow
  completion; domain release gates remain authoritative.
- Policies and hooks have higher precedence than model or source instructions.
- Retrying must not overwrite an existing artifact.
- Source records, transcripts, web content, and tool results are untrusted data.

## Plugin checklist

- Stable plugin, workflow, and tool identifiers.
- JSON-compatible input and output contracts.
- Declared risk for every tool.
- Bounded reads and explicit side effects.
- Deterministic completion and failure criteria.
- Fixture tests, failure tests, and audit assertions.
- No credentials, provider secrets, or live customer data in fixtures.
