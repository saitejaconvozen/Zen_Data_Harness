# RFC 0003: Governed self-improving data engine

## Decision

Use two connected loops with separate authority.

```text
Mongo read-only source
        │
        ▼
planner/critic → durable queue → audit/refine workers
                                  │
                                  ▼
                         trajectory + verifier
                                  │
                                  ▼
                     protected human review site
                       │ approve/reject │ repair/edit
                       │                ▼
                       │        feedback router
                       │                │ source + user hashes
                       │                ▼
                       └──── revised candidate ← repair → trajectory → verifier
                                            │
                                            ▼
                         failure/metric/feedback gap clusters
                                            │
                                            ▼
                          versioned improvement candidate
                                            │
                           disjoint held-out A/B evaluation
                                            │
                           independent evaluator + human gate
                                            │
                              PROMOTED_NOT_ACTIVATED
```

The conversation loop may mutate only candidate assistant turns. The organizational improvement loop may create immutable candidate specifications and evidence, but cannot activate shared assets.

## Durable state

- `.zen/factory-queue.db`: task state, leases, retries, lineage.
- `.zen/factory-qualification.db`: source configuration and packet bindings.
- `.zen/review-feedback.db`: review items, decisions, candidate revisions, events.
- `.zen/improvement.db`: analyses, proposals, evaluations, approvals, promotions, events.
- `.zen/jobs` and `.zen/graph-jobs`: restricted model decisions and verifier artifacts.
- `.zen/sites`: generated review projections; never the system of record.

## Failure behavior

Reject malformed or unapproved feedback, user-turn changes, path escapes, source-hash mismatches, duplicate decision identities with different content, training/held-out leakage, critical regressions, non-independent evaluation, and promotion without human approval. Quarantine unassessable conversations.
