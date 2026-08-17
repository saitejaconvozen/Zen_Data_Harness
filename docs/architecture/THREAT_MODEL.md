# Threat model

## Protected assets

Customer conversations, credentials, private prompts, taxonomy definitions,
review decisions, run state, and released training data.

## Principal threats and controls

- Prompt injection from source data: treat source content only as evidence;
  enforce policy outside the model.
- Data exfiltration: deny external writes by default; redact event payloads.
- Over-broad database authority: require read-only credentials and bounded tools.
- Artifact substitution: immutable SHA-256 blobs and manifest verification.
- Infinite retry: attempt, task, tool-call, and wall-time budgets.
- False completion: workflow-specific deterministic completion predicates.
- Reviewer leakage: blind verifier packets and separate session identifiers.
- Silent user-turn edits: exact byte/hash invariants in the conversation plugin.
- Supply-chain plugin code: explicit plugin paths and manifest validation; signed
  plugin distribution is deferred and required before third-party installation.

## Residual risk

The current runtime is single-host and does not provide a hardened container,
DLP/NER de-identification, signed plugins, or production credential attestation.
Live regulated data is therefore not enabled by this milestone.
