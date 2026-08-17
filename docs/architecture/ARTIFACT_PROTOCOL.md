# Artifact protocol

Artifacts are immutable byte strings identified by SHA-256. The store writes a
blob once, records byte length and media type, and attaches it to a run/task
through a manifest record. Reusing identical bytes is idempotent; overwriting a
logical output with different bytes is forbidden.

Manifests record producer tool and version, input task, timestamp, checksum, and
verification state. Protected conversation artifacts must use a restricted store
and must never be copied into logs. Verification recomputes every checksum and
checks the task/run completion contract.
