# Golden conversation contract

## Required evidence

- Immutable source locator and source checksum.
- Effective system prompt/configuration checksum.
- Complete ordered dialogue.
- Frozen taxonomy checksum.
- Per-assistant-turn decision and applicable metric paths.
- Refiner and verifier worker/session provenance.
- Deterministic validator results and human review status.

## Hard invariants

- Never alter, normalize, translate, or synthesize a user turn.
- Never treat metadata as a user utterance.
- `KEEP` requires exact assistant bytes.
- `CORRECTED` requires an observable source defect and a corrected pass for the
  same governed metric path.
- Do not infer real-world outcomes that are absent from the source evidence.
- Do not fill a target count by lowering quality gates.
- Automated agreement remains `VERIFICATION_PENDING`, not released.

## Current boundary

Build a reviewed golden corpus first. Training and benchmark derivations are
separate plugins with frozen split, deduplication, and leakage controls.
