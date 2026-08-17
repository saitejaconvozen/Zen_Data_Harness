# Execution workflow

## Governed inputs

- Source-bound conversation artifact.
- `zen-eval-axes/2026-q2-v1` registry.
- Agent-auditor, refiner and verifier prompt checksums.
- Structured output schemas.

## Role order

1. `AGENT_AUDITOR`: determine prompt coherence, triggered workflow adherence,
   critical failures and usability from observable evidence.
2. `REFINER`: decide `KEEP` or minimal `REPLACE` for every assistant turn and
   annotate every applicable metric path.
3. Deterministic validator: verify user bytes, turn coverage, taxonomy paths,
   checksums, placeholders and output schema.
4. `VERIFIER`: independently judge the full proposal in a fresh session.
5. Human reviewer: accept, edit, reject, request repair, or escalate.

## Required annotation

Store axis ID, subaxis ID and variant ID separately. Record turn or multi-turn
scope, trigger/evidence turn IDs, a target-specific source quote, source verdict,
golden verdict, expected/observed behavior, severity, confidence and missing
evidence. Do not annotate variants merely to improve coverage.
