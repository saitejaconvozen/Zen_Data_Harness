# Zen_Data_Harness

This Harness acts as a Data Engine for the Zen Project.

It reads recorded voice-agent conversations, corrects defects in the assistant
turns, and emits fine-tuning data with full provenance — every user turn
byte-identical to the source, every change traced to a model decision, and every
candidate held for human review before release.

GPT-5.6-sol is used only as a structured-output transport. The harness owns every
tool call, file write, and state transition.

**New here? Read [`docs/GUIDE.md`](docs/GUIDE.md).** It walks the whole system
from the model boundary down to the implementation, and explains how to adapt it
to a different dataset.

## Quick start

Python 3.11 or newer:

```bash
python -m venv .venv && .venv/bin/pip install -e .

# credentials live outside version control
mkdir -p .zen && echo "export MONGODB_URI='mongodb://…'" > .zen/factory.env
chmod 600 .zen/factory.env

.venv/bin/python -m zen_agent.cli plugins
```

## Run a data batch end to end

`zen-factory-run` carries one run from source traces to reviewable candidates —
acquire, audit, refine, independently verify, repair, terminalize and publish —
with no human step in the loop. It is resumable: re-running the same `run_id`
continues from the durable queue.

```bash
# build an agent shortlist (once)
.venv/bin/python -m zen_agent.cli run "shortlist agents" --input max_agents=4000

# create a run, then drive it
.venv/bin/zen-factory create --target 500        # prints a run_id
scripts/factory-tmux.sh start  <run_id>          # supervised, auto-restarting
scripts/factory-tmux.sh ui     <run_id> 8899     # dashboard + conversation browser
scripts/factory-tmux.sh status
```

`scripts/factory-watch.sh` monitors continuously: it logs progress, runs the QA
audit on every new batch of 50 candidates, releases stale leases when the queue
stalls, and restarts the run if it exits with work still queued.

Terminal conversations land in the review ledger as `REVIEW_PENDING`; the run
never blocks waiting on a reviewer.

## Quality assurance

Two independent layers, because each catches what the other cannot.

```bash
.venv/bin/zen-factory-audit <run_id> --judge           # sample 20% of each batch of 50
.venv/bin/zen-factory-audit <run_id> --full --judge    # sweep every candidate
```

**Deterministic checks** run in Python and cannot be argued with: a replacement
that breaks the recorded next user turn, a substantive answer turned into
"please repeat", a kept turn that is not byte-identical, a fabricated tool call,
specifics appearing nowhere in the call.

**The model judge** asks a different question from the verifier — *is the golden
version better than the source, or did the pipeline make it worse?* — from a
fresh context, and is willing to say the pipeline did badly.

## Design principles

- **Scope every gate to the narrowest level that holds.** A problematic turn is
  excluded on its own; the conversation survives as a partial candidate.
- **Correct rather than reject.** Where the harness knows the right answer it
  fixes the model's decision instead of failing the packet.
- **Defects only.** Stylistic preference is recorded, never rewritten.
- **Never fabricate.** No invented facts, and no tool call the agent did not make.
- **The model is a pure function.** It sees a prompt and returns JSON, in an
  empty read-only sandbox, with no workspace access.

## Kernel boundaries

- Single-host durable supervisor with bounded parallel delegation.
- SQLite event/task state and content-addressed immutable artifacts.
- Tool risk policies fail closed.
- Exact SHA-256 edit guards, path containment, argument-array processes,
  independent verification, lifecycle hooks, append-only session events.
- No production writes; human release approval is never automatic.
- `process.run` provides no kernel-level network isolation — run on a trusted
  host and use `PreToolUse` hooks or an outer sandbox for untrusted input.

## Testing

```bash
.venv/bin/python -m unittest discover -s tests -q
```

See also `docs/architecture/RFC-0004-autonomous-coding-kernel.md` and
`docs/architecture/MVP_ACCEPTANCE_CRITERIA.md` before enabling live data.
