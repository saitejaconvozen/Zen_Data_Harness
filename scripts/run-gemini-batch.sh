#!/bin/bash
# Supervise one factory run on the Gemini/LiteLLM transport.
#
# Two jobs beyond restarting the driver:
#
#   * The driver ends its own process when a pass finds nothing claimable, so a
#     long batch needs something around it rather than one long-lived process.
#   * An approval gate. The batch halts every CHECKPOINT_EVERY candidates and
#     waits for a human to raise the ceiling, so a bad prompt or policy costs at
#     most one checkpoint's spend instead of the whole run.
#
# Raise the ceiling without stopping anything:
#     echo 1000 > .zen/<run_id>.approved
#
#   scripts/run-gemini-batch.sh <run_id>
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
source .zen/factory.env
RUN="${1:?usage: run-gemini-batch.sh <run_id>}"
WORKERS="${ZEN_WORKERS:-12}"
CHECKPOINT_EVERY="${ZEN_CHECKPOINT_EVERY:-500}"
INV="$(cat .zen/inventory-artifact.txt)"
APPROVED=".zen/$RUN.approved"
HELD=".zen/$RUN.awaiting-approval"
LOG=".zen/logs/$RUN.supervisor.log"
mkdir -p .zen/logs
[ -f "$APPROVED" ] || echo "$CHECKPOINT_EVERY" > "$APPROVED"

note() { echo "$(date -Is) $RUN: $*" >> "$LOG"; }

outstanding() {
    .venv/bin/python scripts/run_stats.py "$RUN" outstanding 2>/dev/null || echo 0
}
candidates() {
    .venv/bin/python scripts/run_stats.py "$RUN" candidates 2>/dev/null || echo 0
}

while true; do
    left="$(outstanding)"
    note "$left items outstanding"
    if [ "${left:-0}" -eq 0 ]; then
        note "drained"
        break
    fi

    have="$(candidates)"
    ceiling="$(cat "$APPROVED" 2>/dev/null || echo "$CHECKPOINT_EVERY")"
    if [ "${have:-0}" -ge "${ceiling:-500}" ]; then
        printf '%s\n' "$have" > "$HELD"
        note "HELD at $have candidates (approved ceiling $ceiling)"
        sleep 60
        continue
    fi
    rm -f "$HELD"

    .venv/bin/zen-factory-run "$RUN" \
        --inventory-artifact "$INV" --site ".zen/sites/$RUN" \
        --workers "$WORKERS" --max-acquisition-items 40000 \
        --max-refinement-items 80000 --max-repair-rounds 2 \
        --acquisition-per-pass 400 --publish-every 500 \
        >> ".zen/logs/$RUN.log" 2>&1

    # Recover anything that dead-lettered on a transient fault before retrying.
    .venv/bin/python scripts/run_stats.py "$RUN" requeue-dead >> "$LOG" 2>&1
    sleep 10
done
