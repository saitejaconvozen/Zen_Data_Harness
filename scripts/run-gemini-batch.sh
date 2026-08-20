#!/bin/bash
# Supervise one factory run, and keep it healthy without anyone watching.
#
# The driver exits when a pass finds nothing claimable, so a long batch needs
# something around it. Beyond restarting, this supervisor has to survive the
# failures that actually happened during development — each guard below exists
# because its absence cost hours:
#
#   * An approval gate, so a bad prompt costs one checkpoint rather than a run.
#   * A bounded pass, because the gate is only checked between invocations and
#     an unbounded driver sailed past a 500 ceiling to 809.
#   * Crash-loop detection. A driver that dies instantly gets restarted forever
#     and looks identical to a working one from outside; that ran for an hour.
#   * A dependency check. The whole pipeline stops if the model proxy is down,
#     and nothing noticed for six hours when a provider ran out of credits.
#   * Dead-letter recovery, because attempts are charged on claim, so a
#     conversation that hit three bad minutes is otherwise discarded forever.
#
# Raise the ceiling without stopping anything:
#     echo 10000 > .zen/<run_id>.approved
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
HALTED=".zen/$RUN.halted"
LOG=".zen/logs/$RUN.supervisor.log"

# Stop rather than spin: this many passes in a row completing nothing means
# something is wrong that restarting will not fix.
MAX_BARREN_PASSES="${ZEN_MAX_BARREN_PASSES:-4}"
PROXY_PORT="${LITELLM_PORT:-4000}"

mkdir -p .zen/logs
[ -f "$APPROVED" ] || echo "$CHECKPOINT_EVERY" > "$APPROVED"
rm -f "$HALTED"

note() { echo "$(date -Is) $RUN: $*" >> "$LOG"; }
stat_of() { .venv/bin/python scripts/run_stats.py "$RUN" "$1" 2>/dev/null || echo 0; }

# The model proxy is a hard dependency; without it every worker fails identically.
ensure_proxy() {
    ss -ltn 2>/dev/null | grep -q ":$PROXY_PORT " && return 0
    note "model proxy on :$PROXY_PORT is down — restarting"
    ./scripts/start-litellm.sh >> "$LOG" 2>&1
    for _ in $(seq 15); do
        sleep 2
        ss -ltn 2>/dev/null | grep -q ":$PROXY_PORT " && { note "proxy back up"; return 0; }
    done
    note "proxy did not come back"
    return 1
}

barren=0
while true; do
    left="$(stat_of outstanding)"
    have="$(stat_of candidates)"
    note "$left outstanding, $have candidates"

    if [ "${left:-0}" -eq 0 ]; then
        note "drained at $have candidates"
        break
    fi

    ceiling="$(cat "$APPROVED" 2>/dev/null || echo "$CHECKPOINT_EVERY")"
    if [ "${have:-0}" -ge "${ceiling:-500}" ]; then
        printf '%s\n' "$have" > "$HELD"
        note "HELD at $have candidates (ceiling $ceiling)"
        sleep 60
        continue
    fi
    rm -f "$HELD"

    if ! ensure_proxy; then
        # Back off rather than burning attempt budget against a dead dependency.
        note "waiting 120s for the proxy"
        sleep 120
        continue
    fi

    headroom=$(( ceiling - have ))
    [ "$headroom" -lt 50 ] && headroom=50
    .venv/bin/zen-factory-run "$RUN" \
        --inventory-artifact "$INV" --site ".zen/sites/$RUN" \
        --workers "$WORKERS" --max-acquisition-items 40000 \
        --max-refinement-items "$(( headroom * 6 ))" --max-repair-rounds 2 \
        --acquisition-per-pass 400 --publish-every 200 \
        >> ".zen/logs/$RUN.log" 2>&1

    # Transient faults — a provider hiccup, a rate limit, a bug since fixed.
    # Attempts are charged on claim, so without this a conversation that hit
    # three bad minutes is discarded permanently.
    .venv/bin/python scripts/run_stats.py "$RUN" requeue-dead >> "$LOG" 2>&1

    # Did that pass accomplish anything? "Restarted" and "made progress" are
    # different questions, and only the second one matters.
    after="$(stat_of outstanding)"
    if [ "${after:-0}" -ge "${left:-0}" ]; then
        barren=$(( barren + 1 ))
        note "pass completed no work ($barren/$MAX_BARREN_PASSES)"
        if [ "$barren" -ge "$MAX_BARREN_PASSES" ]; then
            note "HALTED — $barren consecutive passes did nothing; last driver output:"
            tail -20 ".zen/logs/$RUN.log" >> "$LOG"
            printf '%s\n' "$barren consecutive barren passes at $have candidates" > "$HALTED"
            break
        fi
        # Widen the gap each time; a rate limit clears on its own, a bug does not.
        sleep $(( barren * 30 ))
        continue
    fi
    barren=0
    sleep 10
done
