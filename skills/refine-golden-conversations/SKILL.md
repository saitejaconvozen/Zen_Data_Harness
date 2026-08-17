---
name: refine-golden-conversations
description: Assess, shortlist, annotate, refine, verify, and review multilingual real-world voice-agent conversations while preserving user turns exactly. Use for system-prompt adherence, assistant-turn correction, axis/subaxis/variant applicability, code-switching or dialect quality, and creation of human-reviewable golden conversation candidates.
---

# Refine Golden Conversations

Read `references/golden-contract.md` before handling source text. Read
`references/execution-workflow.md` before running or changing the auditor,
refiner, verifier or human-review stages.

## Execute

1. Verify immutable source and prompt bindings.
2. Load the complete checksum-bound active taxonomy.
3. Assess prompt/workflow adherence and conversation usability.
4. Inspect every assistant turn in full multi-turn context.
5. Keep exact correct text; minimally replace only observable defects.
6. Attach applicable axis, subaxis and variant as separate IDs with evidence.
7. Preserve every user turn exactly, including real STT errors, language,
   dialect and code-mixing.
8. Quarantine unsupported facts and replay-sensitive corrections.
9. Verify in a fresh GPT-5.6-sol session.
10. Emit human-review candidates, never automatically released training data.

Do not derive SFT, preference, reward, RL or benchmark artifacts in this workflow.
