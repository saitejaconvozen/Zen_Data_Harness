# Zen Data Harness — implementation guide

A complete walkthrough of how this harness works, why each piece exists, and how
to adapt it to a different dataset. Written for someone who has not seen the
codebase before.

Nothing here is aspirational. Every design note names the failure that produced
it, because most of this system's shape comes from things that went wrong in
production rather than from a plan.

---

## 1. What this is

A **data engine**. It reads recorded conversations between a voice agent and a
real caller, finds places where the agent violated its instructions or said
something untrue, corrects those turns, and emits a *golden* version suitable for
fine-tuning — with every change traced to a model decision and every user turn
byte-identical to the source.

It is **not** a chatbot, and not a general coding agent. It borrows a model as a
pure function and keeps all authority in Python.

### The core constraint

The user turns are **recorded human speech**. They cannot change. So a correction
to an assistant turn is only valid if the recorded next user turn still makes
sense as a reply. That single constraint drives most of the design.

---

## 2. Mental model

```
MongoDB                the harness                       output
────────           ───────────────────                ──────────
call_dispositions
  chat_history  ──▶ trace_fetch      sample conversations
                    prepare_packets  checksum-bind, pre-pass formatting
                    agent_audit      is this source usable at all?
                         │
                         ├─ no ──▶ terminal: QUARANTINED
                         │
                    refine          model proposes per-turn corrections
                    verify          independent model checks the proposal
                         │
                         ├─ FAIL ──▶ repair → trajectory_gate → verify_repair
                         │             (bounded rounds, then salvage)
                         │
                    terminal        VERIFIED / PARTIAL / QUARANTINED
                         │
                    qa_audit        deterministic checks + model judge
                         │
                    review ledger   REVIEW_PENDING → human decision
```

Six model roles, each in a **fresh context that cannot see the others' reasoning**:

| role | question it answers |
|---|---|
| `FACTORY_PLANNER` | which agents should we sample next? |
| `PLAN_CRITIC` | is that sampling plan sound? |
| `AGENT_AUDITOR` | is this source conversation usable at all? |
| `REFINER` | which turns are defective, and what is the corrected turn? |
| `VERIFIER` | is this proposal valid? |
| `JUDGE` | is the golden version *better* than the source? |

> **Why both a verifier and a judge.** The verifier checks *compliance* — schema,
> taxonomy paths, immutability. It was observed passing a conversation where a
> substantive answer had been replaced with "could you please repeat?", because
> nothing in the proposal was technically invalid. The judge asks a different
> question and therefore has different blind spots. Two roles asking the same
> question would just agree with each other.

---

## 3. The model boundary — read this before changing anything

```python
# src/zen_agent/model_adapter.py
codex exec --ephemeral --ignore-user-config --ignore-rules
           --skip-git-repo-check --sandbox read-only
           --model gpt-5.6-sol
           --output-schema <schema.json>
           --output-last-message <out.json>
           --cd <empty temp dir>
```

Codex is used **only as a structured-output transport**. It runs in an empty
temp directory it cannot escape, with no access to the workspace, no tools, and
a JSON schema it must satisfy. The model sees a prompt and returns one JSON
object. Python does everything else.

This matters because it makes the model *replaceable*. To swap providers you
rewrite one file and keep the entire pipeline.

The model is pinned in two places and enforced:

```python
# src/zen_agent/config.py
PINNED_MODEL = "gpt-5.6-sol"
if self.allowed_models != (PINNED_MODEL,): raise ValueError(...)
```

```toml
# zen.toml
allowed = ["gpt-5.6-sol"]
```

---

## 4. Two rules that shape everything

### 4.1 Scope every gate to the narrowest level that holds

The original harness discarded a whole conversation whenever any single turn had
a problem — via `replay_required`, `quarantine_reasons`, the trajectory gate, and
the verifier's all-or-nothing `PASS`. Four separate instances of the same
mistake. Yield was 9.9%.

Now a bad turn is excluded on its own and the conversation survives as
`PARTIAL_CANDIDATE`. Conversation-level rejection is reserved for genuine
whole-conversation blockers.

```python
# src/zen_agent/factory_worker.py — _classify_proposal
if proposal["prompt_usable"] is not True:        return "QUARANTINED", ...
if proposal["conversation_assessable"] is False: return "QUARANTINED", ...
excluded = divergent_turns | unassessable_turns
if not excluded:                    return "VERIFIED_CANDIDATE", ...
if len(excluded) >= total:          return "QUARANTINED", ...
return "PARTIAL_CANDIDATE", f"{len(excluded)} of {total} turns excluded"
```

**When you add a gate, ask: does this problem affect one turn or the whole
call?** Getting that wrong is the most expensive mistake available here.

### 4.2 Correct, don't reject

A validator that raises kills the conversation and burns the retry budget. 63
conversations died that way. Where the harness knows the right answer, it fixes
the decision instead:

| model does | harness does |
|---|---|
| replaces a turn on `MINOR_GAP` | coerces to `KEEP` with source text |
| returns a no-op replacement | coerces to `KEEP` |
| mislabels `TERMINAL_TURN` | sets the label it already computed |
| drifts bytes on a `KEEP` | restores the source |
| invents a tool call | strips it, marks `evidence_status: INSUFFICIENT` |
| writes "calling tool" | restores the source text |

Reject only what the harness genuinely cannot decide.

---

## 5. Stage by stage

### 5.1 Ingestion — `src/zen_agent/adapters/mongodb.py`

`bind_conversation()` turns a Mongo document into a checksum-bound conversation.

**The bug worth knowing about.** It originally required every turn's `content` to
be a string. A tool-calling assistant turn has `content: null` and its action in
`tool_calls`. So every tool-using conversation was either rejected outright or —
worse — passed through with the tool call silently deleted, leaving an empty
assistant turn where an invocation belonged. For a dataset meant to teach tool
use, that is the worst possible failure: the data looks fine and teaches the
wrong thing.

```python
tool_calls = item.get("tool_calls")
raw_content = item.get("content")
if not isinstance(raw_content, str):
    if tool_calls: raw_content = ""      # a tool-only turn speaks no words
    else: raise ValueError(...)
```

Tool calls are normalised to the OpenAI shape, hashed into provenance, and `tool`
result turns are carried through with their `tool_call_id`.

**Scaling note.** Counting conversations per agent with `$group` over 46M
documents times out. Use a bounded early-exit count — shortlisting needs a floor,
not an exact total:

```python
calls.count_documents({"agent_id": aid}, limit=cap, hint="agent_id_-1")
```

### 5.2 Packet preparation — `plugins/golden-refinement/plugin.py`

Builds the immutable unit of work: system prompt, all turns with checksums, the
checksum-bound taxonomy, and the SHA of every prompt and schema used. `packet_id`
is derived from those identities, so a packet is reproducible.

This is also where **deterministic pre-passes** run. Anything a regex can decide
should never cost a model call:

```python
format_findings = analyze_conversation(system_prompt, normalized_turns)
```

`src/zen_agent/turn_format.py` detects mandatory language tags (`<|ENGLISH|>`).
On the real corpus, 64% of the refiner's replacements had been nothing but adding
a tag. Moving that to code freed the entire budget for actual quality work.

> Latin-script Hindi ("Kya aap baat kar sakte hain") is not English. Script
> detection alone got 3 of 6 cases wrong, so the model now *declares*
> `response_language` and the harness trusts that, with a lexical fallback of
> Hindi function words that have no English homograph.

### 5.3 Refinement — `plugins/golden-conversations/prompts/conversation-refiner.md`

The contract is the most important file in the repository. Its central rule:

> Correct **defects**. You are not rewriting the call, not improving its phrasing,
> and not producing the response you would have preferred.

Per turn the model sets:

| field | meaning |
|---|---|
| `source_quality` | `PERFECT` / `MINOR_GAP` / `MAJOR_GAP` / `CRITICAL_GAP` |
| `action` | `KEEP` or `REPLACE` |
| `downstream_coherence` | `TERMINAL_TURN` / `PRESERVED` / `DIVERGENT` |
| `evidence_status` | `SUFFICIENT` / `INSUFFICIENT` |
| `golden_tool_calls` | corrected calls, or `null` |
| `response_language` | authoritative language tag |

**`MINOR_GAP` is explicitly not a defect.** Under an earlier contract that told
the model to produce "the ideal response", 73% of turns were replaced and 34% of
those were pure style — it was adding localities, product details and sales
rationale the agent never said. Now `MINOR_GAP` is recorded and the turn is kept
byte-identical, enforced in the validator.

**Never invent a tool call.** Adding one invents an action that never happened
and a result that never existed. A genuinely missing call is annotated and marked
`INSUFFICIENT` — never written for the agent.

### 5.4 Verification, repair, terminal

Verifier runs in a fresh session that must differ from the refiner's. On `FAIL`
the conversation enters a bounded repair loop:

```
repair → trajectory_gate → verify_repair → (PASS | FAIL → next round | EXHAUSTED)
```

When rounds run out, turns the verifier still objects to are excluded and the
rest is kept — rather than discarding everything.

Findings carry a `scope`: `GOLDEN_TEXT`, `ANNOTATION`, or `TURN_LABEL`. **Only
`GOLDEN_TEXT` findings force a rewrite.** 62% of blocking findings turned out to
be about taxonomy-citation precision, not the conversation — rewriting a good
answer to satisfy a bookkeeping objection makes the data worse.

### 5.5 Quality assurance — `src/zen_agent/qa_audit.py`

Two independent layers, because each catches what the other cannot.

**Deterministic checks** (no model, cannot be argued with):

| check | catches |
|---|---|
| `false-preserved` | reply can't follow the replacement |
| `answer-to-clarification` | substantive answer became "please repeat" |
| `replaced-without-defect` | rewritten on `PERFECT`/`MINOR_GAP` |
| `keep-not-identical` | a kept turn isn't byte-identical |
| `user-turn-modified` | immutability broken |
| `possible-fabrication` | specifics appearing nowhere in the call |

`src/zen_agent/dialogue_act.py` holds the sharpest one. If a replacement drops
the source's question *and* the recorded reply opens with a bare acknowledgement
("yeah", "ji", "haan"), the reply demonstrably answered the old turn. That is
mechanically provable, and it caught a case both the refiner and the independent
verifier had passed.

**The model judge** — `prompts/refinement-judge.md`. Asks only: *is the golden
version better than the source, or did the pipeline make it worse?* Verdicts are
`IMPROVED` / `UNNECESSARY` / `HARMFUL` / `UNCERTAIN`, plus a `tool_call_verdict`
where `FABRICATED` forces a rejection.

The prompt carries real calibration examples with their correct verdicts, and one
instruction that matters more than the rest:

> Be willing to say the pipeline did badly. A judge that approves everything is
> worth nothing.

Sampling is 20% of every batch of 50, deterministic so a re-run audits the same
conversations, with a durable ledger so batches advance.

---

## 6. Operating it

```bash
# one-time
python -m venv zen-harness && zen-harness/bin/pip install -e .
echo "export MONGODB_URI='mongodb://…'" > .zen/factory.env && chmod 600 .zen/factory.env

# build an agent shortlist
zen-harness/bin/python -m zen_agent.cli run "shortlist agents" --input max_agents=4000

# create and run a batch
zen-harness/bin/zen-factory create --target 500          # prints a run_id
scripts/factory-tmux.sh start  <run_id>
scripts/factory-tmux.sh ui     <run_id> 8899
scripts/factory-tmux.sh status

# quality
zen-harness/bin/zen-factory-audit <run_id> --judge
zen-harness/bin/zen-factory-audit <run_id> --full --judge   # sweep everything
```

`scripts/factory-watch.sh` runs continuously: logs progress, auto-runs the audit
every 50 new candidates, releases stale leases when the queue stalls, and
**restarts the run if it exits with work still queued**.

### Operational lessons paid for in downtime

- **tmux inherits the tmux *server* environment, not your shell's.** An exported
  variable silently vanishes. Put run control on disk (`.zen/<run>.target`).
- Everything is resumable. Kill anything, restart with the same `run_id`.
- After an unclean kill, release stale leases:
  ```sql
  UPDATE factory_work SET status='READY', lease_owner=NULL,
    lease_token=NULL, lease_expires_at=NULL
  WHERE run_id='…' AND status='LEASED';
  ```
- Read live SQLite with `mode=ro`, never `immutable=1` — immutable caches the
  schema and will not see new columns or rows.
- ~135 MB per concurrent model call; ~128 s per call. Budget accordingly.

---

## 7. Adapting it to your data

Most of the harness is domain-agnostic. Four things are not.

**1. The source adapter** — `src/zen_agent/adapters/mongodb.py`.
Replace `bind_conversation()` to emit `{system_prompt, turns[], source_content_sha256}`.
Keep the checksums; provenance depends on them.

**2. The taxonomy** — `plugins/golden-conversations/resources/taxonomy/*.json`.
Axis → sub-axis → variant, checksum-pinned. Update the expected SHA and counts in
`plugins/golden-refinement/plugin.py`.

**3. The contracts** — `plugins/golden-conversations/prompts/*.md`.
What counts as a defect in *your* domain. This is where most of your effort goes.

**4. The deterministic pre-passes** — `src/zen_agent/turn_format.py`.
Language tags are specific to these voice agents. Replace with whatever mechanical
rules your domain has, and delete what does not apply.

### Two schemas must stay in sync

`refiner-response-v1` drives generation; `refiner-decision-v1` drives validation.
Editing one and not the other fails silently — the model keeps generating against
the old shape. `tests/test_schema_parity.py` exists because this happened.

Structured-output schemas require **every property listed in `required`**; use
nullable types for optional fields.

---

## 8. Testing

```bash
zen-harness/bin/python -m unittest discover -s tests -q
```

215 tests. Notable ones and the incident behind each:

| test | exists because |
|---|---|
| `test_source_integrity` | a syntax error meant a whole stage had never run |
| `test_schema_parity` | two schemas drifted silently |
| `test_dialogue_act` | a good answer was replaced and both models approved |
| `test_qa_audit` | three of the audit's own checks were false-positive machines |
| `test_packet_preparation` | tool calls were being dropped at ingestion |

---

## 9. Honest limits

- **Quality is model-judged, not human-judged.** No conversation in this system
  has a human label. The judge rejects a large share of what the pipeline
  approves, and its own false-negative rate is unmeasured. Hand-label ~30
  conversations and score the judge against them before trusting it as a gate.
- **Source data caps the ceiling.** Conversations whose backend actions were
  never logged cannot be verified, and the auditor correctly refuses them.
  Better upstream logging raises the ceiling more than any harness change.
- **Single host.** SQLite + one machine. Fine to a few thousand conversations;
  beyond that you need a real queue and object storage.
- **Two orchestrators still exist** — `GraphSupervisor` and
  `FactoryWorker._transition` implement the same loop. Only the latter runs.
- **The self-improvement loop has never closed.** `improvement.db` is empty.

---

## 10. Where things live

```
src/zen_agent/
  model_adapter.py      the model boundary — start here
  factory_worker.py     stage routing and terminal classification
  factory_run_cli.py    end-to-end driver
  qa_audit.py           deterministic quality checks
  dialogue_act.py       coherence proof from the recorded reply
  turn_format.py        language-tag pre-pass
  status_server.py      dashboard + conversation browser
  adapters/mongodb.py   source ingestion

plugins/golden-conversations/
  prompts/*.md          the contracts — the real logic
  schemas/*.json        generation and validation shapes
  scripts/run_*.py      one model role each

scripts/
  run-factory.sh        supervisor with retry
  factory-tmux.sh       start / ui / status / attach / stop
  factory-watch.sh      continuous monitor and self-heal
```

Read in this order: `model_adapter.py` → `conversation-refiner.md` →
`factory_worker.py::_classify_proposal` → `qa_audit.py`.
