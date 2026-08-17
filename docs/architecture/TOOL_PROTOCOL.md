# Tool protocol

Every tool declares a stable name, version, description, input schema, output
schema, risk class, and callable implementation. Inputs and outputs must be JSON
compatible. Unknown fields fail validation by default.

Risk classes are `read_only`, `workspace_write`, `external_write`,
`production_write`, and `destructive`. Policy is checked immediately before each
invocation. A model cannot grant approval. Tool calls emit requested, started,
succeeded, denied, or failed events with secrets redacted.

Tools should be narrow, idempotent where practical, and explicit about side
effects. A tool result is observation data until a validator accepts it and the
artifact store commits it.
