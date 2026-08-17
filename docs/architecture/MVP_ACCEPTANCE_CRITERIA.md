# MVP acceptance criteria

The foundation milestone is accepted only when automated tests demonstrate:

1. A selected workflow produces a bounded task plan.
2. Run and task state survives process restart.
3. A failed retry never destroys an already committed artifact.
4. Tool policy denies an undeclared or disallowed operation.
5. Tool input and output schemas reject malformed values.
6. Run success requires task success plus a completion predicate.
7. Artifact verification detects changed bytes.
8. Trace output explains every state transition.
9. A second fixture plugin installs without kernel changes.
10. Model configuration rejects every identifier except `gpt-5.6-sol`.
11. Source contains no Gemini or Vertex execution dependency.
12. Golden bootstrap validates the real axes CSV without reading MongoDB.

Before a live-data milestone, add crash injection, prompt-injection, privacy,
user-turn immutability, blinded verification, and human-review release tests.
