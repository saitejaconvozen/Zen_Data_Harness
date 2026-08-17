# Task and run lifecycle

Runs use `PLANNED`, `RUNNING`, `NEEDS_HUMAN`, `SUCCEEDED`, `FAILED`, `BLOCKED`,
or `QUARANTINED`. Tasks use `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`,
`BLOCKED`, `QUARANTINED`, or `SKIPPED`.

```text
PENDING -> RUNNING -> SUCCEEDED
                 |-> PENDING       retry with remaining budget
                 |-> FAILED        attempts exhausted
                 |-> BLOCKED       missing input/authority/dependency
                 |-> QUARANTINED   unsafe or unassessable output
```

Every transition is appended to the event log before dependent work is made
runnable. A run succeeds only when all required tasks succeeded and the workflow
completion predicate passes. An interrupted `RUNNING` task becomes retryable on
resume; committed artifacts are never regenerated under a different checksum.
