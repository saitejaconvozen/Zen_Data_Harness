# Acceptance and Capacity

Let `T` be the accepted-conversation target and let each stage have measured yield `y_i`. The next discovery requirement is `ceil(remaining_accepts / product(y_i))`, capped by the approved scan budget. Use lower confidence bounds during early pilots; never silently lower quality gates to hit a count.

Measure separately by domain, language, dialect, code-mixing pattern, agent configuration version, outcome, duration, tool-use pattern, and taxonomy coverage. Overall yield can conceal an empty or poor-quality subgroup.

Concurrency is the minimum of provider request limits, token throughput, database read budget, artifact-store throughput, and reviewer capacity. Increase it only after p95 latency, retry rate, timeout rate, and verifier agreement remain within the approved SLO during a soak window.

For each accepted conversation retain:

- opaque immutable source binding and configuration digest;
- raw transcript digest with unmodified user turns;
- refined assistant-turn decisions and cited taxonomy paths;
- refiner and verifier model/session provenance;
- repair-round lineage and resolution ledger;
- deterministic gate reports and final verdict;
- split assignment and export-manifest identity.
