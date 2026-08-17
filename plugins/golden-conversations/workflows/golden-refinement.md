# Golden refinement workflow

```text
source-bound sample
  -> deterministic preflight and privacy status
  -> configuration/conversation audit
  -> configuration-level qualification summary
  -> per-assistant-turn refinement and metric applicability
  -> deterministic user/hash/taxonomy validation
  -> blinded verifier session
  -> targeted repair or quarantine
  -> human domain/language review
  -> VERIFICATION_PENDING golden candidate
```

## Completion predicate

The workflow completes only when every input is represented by a reviewed golden
candidate, a quarantine record, or an explicit rejection. A model pass is not a
release. User hashes, source hashes, taxonomy checksum, prompt checksums, worker
sessions, findings and human status must all be present.
