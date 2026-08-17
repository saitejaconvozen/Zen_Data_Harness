---
name: verify-code-change
description: Independently verify a repository change after implementation by inspecting evidence, diff scope, requirements, and test results. Use for completion gates, regression review, failed-attempt feedback, and deciding whether a coding task must be re-planned.
---

# Verify Code Change

Evaluate the result from a fresh context. Do not accept the implementer's claim as evidence.

## Verification procedure

1. Restate the objective as observable acceptance criteria.
2. Inspect the relevant final files and complete diff.
3. Check for unrelated changes, unsafe behavior, missing error handling, and instruction violations.
4. Run or inspect focused tests for changed behavior.
5. Run the proportionate regression suite and record exact outcomes.
6. Classify the result as `PASS`, `FAIL`, or `NEEDS_HUMAN`.

## Verdict rules

- Return `PASS` only when every material criterion has direct evidence and required checks pass.
- Return `FAIL` with specific, actionable findings when repair is possible within the original objective.
- Return `NEEDS_HUMAN` only for a genuine authority, product-choice, secret, or external-state dependency.
- Never repair code while acting as the independent verifier. Send findings back to the executor for a bounded new iteration.

For every finding, identify the criterion, evidence, affected path or check, and recommended action. Keep uncertainty explicit.
