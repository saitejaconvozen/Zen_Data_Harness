# RFC-0002: 5K Conversation Factory

## Decision

The production target is 5,000 **accepted**, source-bound conversations, not 5,000 sampled rows. The factory continuously discovers candidates until acceptance and coverage targets are met. It never represents each conversation as a permanently running agent.

The local SQLite graph runner remains a forward-test control plane. Production execution uses a transactional queue with leases, a PostgreSQL state store, content-addressed restricted artifact storage, and bounded stateless worker pools.

## Pipeline

1. Metadata scouts enumerate agent configuration versions without model access.
2. GPT-5.6-sol agent auditors assess effective prompts and configuration eligibility.
3. Trace fetchers emit opaque source bindings and immutable user/tool turns.
4. Deterministic privacy, completeness, and corruption gates reject unsafe inputs.
5. The coverage curator selects by domain, language, dialect, code-switching, outcome, difficulty, and rare taxonomy cells.
6. GPT-5.6-sol refiners produce per-turn KEEP/REPLACE decisions and taxonomy citations.
7. The trajectory gate prevents edited assistant semantics from contradicting later immutable real user turns.
8. Fresh, independent GPT-5.6-sol verifiers PASS, FAIL, or ABSTAIN.
9. FAIL enters a bounded repair loop; ABSTAIN, exhausted repair, and replay-required cases enter human review or quarantine.
10. Corpus assembly accepts only verified items with complete lineage and valid source bindings.

## Scale model

Start with a candidate floor of `target_accepted * 4` (20,000 for a 5,000 target), then adapt from measured stage yields. This is a discovery budget, not an assumption that 25% will pass. Stop only when both the acceptance target and every approved coverage floor are satisfied.

Model work is conversation-isolated. Database scans and deterministic checks are sharded (100–250 locators per shard); refinement and verification remain one conversation per model session to prevent cross-customer leakage. A bounded pool of 8 logical workers per model role is the conservative default until measured account rate limits support more.

## Required planes

- Control plane: manifests, graph definitions, budgets, pause/resume/cancel, human gates.
- Queue plane: atomic claim, lease token, heartbeat, retry, dead-letter, idempotency key.
- Worker plane: stateless role-specific workers with least-privilege inputs.
- Data plane: MongoDB read-only locators and content-addressed restricted artifacts.
- State plane: PostgreSQL run, work-item, lineage, verdict, coverage, and audit records.
- Observability plane: queue depth, latency, yield, retry, cost/token, coverage, failure class, and drift metrics.
- Review plane: authenticated reviewer UI with blinded source/proposal/verifier presentation.

## Non-negotiable invariants

- User turns and raw source artifacts are immutable.
- Refiners and verifiers have separate sessions and no shared scratch state.
- Business FAIL is a route, not an infrastructure exception.
- Every retry is bounded; every model call has a budget and idempotency key.
- An expired lease can be reclaimed, but stale workers cannot commit.
- Acceptance requires verifier PASS, deterministic validation, source binding, taxonomy validity, and coverage accounting.
- Benchmark candidates are split by source group and time before refinement; no benchmark item may enter training exports.
- Secrets and raw conversations never enter logs, queue payloads, URLs, or public artifacts.

## Promotion gates

1. Unit tests: queue CAS, leases, retries, graph cycles, schemas, invariants.
2. Synthetic soak: 10,000 locators with injected crashes and duplicate delivery.
3. Shadow run: 100 real conversations, no corpus publication.
4. Pilot: 500 candidates with human double-review and measured yield.
5. Scale: staged 1K, 5K, then continuous operation after SLO and quality sign-off.

SQLite is not approved beyond a single-host forward test. The graph engine is not production-ready until the PostgreSQL queue adapter, worker daemon, metrics exporter, authenticated review service, and disaster-recovery exercise pass their promotion gates.
