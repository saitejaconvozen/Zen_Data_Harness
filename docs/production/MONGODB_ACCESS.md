# MongoDB production access

## Required credential

Provision a dedicated identity with server-enforced read-only permission for the
approved source collections. Do not reuse an administrator, root,
`readWriteAnyDatabase`, or application-service credential.

The current legacy credential was audited on 2026-08-13 and rejected because it
has root and cross-database write privileges. Rotate it because it was embedded
in tracked legacy source files.

## Runtime injection

Supply these through a protected process environment or secret manager:

```text
MONGODB_URI
MONGODB_DATABASE=test
MONGODB_CALL_COLLECTION=call_dispositions
MONGODB_AGENT_COLLECTION=agent_base
```

Never place the real URI in Git, `zen.toml`, `ZEN.md`, a skill, a task input, a
shell-history file, an event, or an artifact.

## Required validation

Run:

```bash
zen run "Audit MongoDB credential access" --workflow golden-mongo-audit
```

The harness requests `connectionStatus` with effective privileges and refuses to
expose collections if any write-capable action is present. After it succeeds:

```bash
zen run "Inventory agents for conversation shortlisting" \
  --workflow golden-agent-inventory --input max_agents=100
```

The first inventory reads agent metadata and bounded per-agent counts. It does
not fetch transcript content or invoke a model.
