# Human golden-conversation review

Review the source, proposal, taxonomy annotations and verifier findings. Confirm:

1. Every user turn is unchanged and remains authentic STT/user evidence.
2. Every assistant edit is necessary, minimal and valid in the workflow state.
3. Applicable variants are complete and use valid parent paths.
4. Facts and claimed actions are supported by available evidence.
5. Language, dialect, code-mixing, tone and conversational pacing are natural.
6. The example is safe for the domain and useful for post-training.

Choose `ACCEPT`, `EDIT`, `REJECT`, `REPAIR_REQUESTED`, `DOMAIN_ESCALATION`, or
`LANGUAGE_ESCALATION`. Automated agreement never grants release approval.
