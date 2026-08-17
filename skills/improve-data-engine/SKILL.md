---
name: improve-data-engine
description: Analyze recurring conversation-refinement failures and reviewer feedback, create bounded prompt/plugin/workflow improvement candidates, evaluate them on disjoint held-out conversations, and prepare eligible candidates for explicit human promotion. Use for self-improvement cycles, regression analysis, proposal governance, held-out A/B evaluation, or promotion readiness in the Zen data engine.
---

# Improve Data Engine

Run an evidence-bound improvement loop without allowing a data-processing run to silently rewrite shared behavior.

## Workflow

1. Read `ZEN.md` and [the promotion contract](references/promotion-contract.md).
2. Freeze the baseline version, evidence IDs, training IDs, and held-out IDs before proposing a change.
3. Run `zen-factory-self-improve` to route human feedback, re-verify repaired conversations, republish review state, and record stable gap clusters.
4. Select one coherent gap cluster or tightly related cluster group. Do not bundle unrelated fixes.
5. Create an immutable candidate with `zen-improve propose`. Limit scope to one `PROMPT`, `PLUGIN`, or `WORKFLOW` component.
6. Evaluate baseline and candidate on exactly the declared held-out IDs with an evaluator independent of the candidate author. Preserve and compare user-turn hashes.
7. Record results with `zen-improve evaluate --independent-approval` only when the independent evaluation genuinely occurred.
8. Record the human decision with `zen-improve approve`.
9. Run `zen-improve promote`. Treat `PROMOTED_NOT_ACTIVATED` as approval to prepare a normal reviewed change, not permission to alter live shared assets.
10. Apply a promoted candidate through the organization's normal code/config review, then start a new versioned baseline.

## Invariants

- Never modify source user turns.
- Never put raw transcripts in queue-control payloads when stable locators and hashes suffice.
- Keep reviewer decisions, candidate revisions, evaluations, approvals, promotions, and events append-only.
- Keep candidate-author and evaluator identities distinct.
- Reject training/held-out overlap, critical regressions, user-turn integrity failures, and insufficient evidence.
- Never activate prompt, taxonomy, skill, plugin, workflow, policy, or release-criteria changes from an ordinary data run.
- Use GPT-5.6-sol for all model-backed roles.
- Report abstentions and quarantines as outcomes, not as successful refinements.

## Commands

Use one feedback cycle:

```bash
zen-factory-self-improve RUN_ID --site .zen/sites/SITE_ID --workers 8 --max-feedback-rounds 3
```

Inspect proposal state:

```bash
zen-improve status
```

Use `zen-improve --help` and subcommand help for proposal, evaluation, approval, and promotion inputs.
