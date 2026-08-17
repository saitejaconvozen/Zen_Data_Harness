# Promotion contract

A reusable improvement candidate progresses through these immutable stages:

`ANALYZED → PROPOSED → EVALUATED → HUMAN_APPROVED → PROMOTED_NOT_ACTIVATED`

Promotion requires all of the following:

- training and held-out conversation IDs are disjoint;
- evaluation results exactly cover the declared held-out set;
- sample size meets policy (default 30);
- absolute pass-rate improvement meets policy (default 0.02);
- there are no critical regressions;
- every source user-turn integrity check passes;
- metric coverage does not regress below policy;
- an evaluator independent of the candidate creator approves the evaluation;
- a named human explicitly approves the candidate.

Promotion does not activate a candidate. Activation is a separately reviewed repository or configuration change. Roll back by restoring the prior versioned component; do not rewrite the append-only evidence ledger.
