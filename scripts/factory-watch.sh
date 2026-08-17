#!/usr/bin/env bash
# Continuous monitor for a running factory batch.
#
#   scripts/factory-watch.sh RUN_ID [INTERVAL_SECONDS]
#
# Appends a progress line to .zen/logs/RUN_ID.watch.log every interval, runs the
# sampling QA audit whenever another 50 candidates have accumulated, and shouts
# when the run stalls, dies, or starts dead-lettering. Designed to run in its own
# tmux session so monitoring survives the session that started it.
#
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=_env.sh
source "$ROOT/scripts/_env.sh"

RUN_ID="${1:-}"
INTERVAL="${2:-300}"
if [[ -z "$RUN_ID" ]]; then
    echo "usage: $0 RUN_ID [INTERVAL_SECONDS]" >&2
    exit 64
fi
TARGET="${ZEN_TARGET_TERMINAL:-500}"
LOG=".zen/logs/${RUN_ID}.watch.log"
mkdir -p "$(dirname "$LOG")"

q() { sqlite3 .zen/factory-queue.db "$1" 2>/dev/null || echo 0; }
say() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

say "watching $RUN_ID (target $TARGET, interval ${INTERVAL}s)"
last_terminal=-1
stalls=0

while true; do
    terminal=$(q "SELECT COUNT(*) FROM factory_work WHERE run_id='$RUN_ID' AND stage='terminal' AND status='SUCCEEDED';")
    verified=$(q "SELECT COUNT(*) FROM factory_work WHERE run_id='$RUN_ID' AND stage='terminal' AND status='SUCCEEDED' AND json_extract(payload_json,'\$.inputs.terminal_status')='VERIFIED_CANDIDATE';")
    partial=$(q "SELECT COUNT(*) FROM factory_work WHERE run_id='$RUN_ID' AND stage='terminal' AND status='SUCCEEDED' AND json_extract(payload_json,'\$.inputs.terminal_status')='PARTIAL_CANDIDATE';")
    dead=$(q "SELECT COUNT(*) FROM factory_work WHERE run_id='$RUN_ID' AND status='DEAD';")
    ready=$(q "SELECT COUNT(*) FROM factory_work WHERE run_id='$RUN_ID' AND status IN ('READY','LEASED');")
    calls=0
    for p in $(pgrep -f "codex exec" 2>/dev/null); do
        tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -q "gpt-5.6-sol" && calls=$((calls + 1))
    done
    alive=$(pgrep -f "bin/zen-factory-run" >/dev/null 2>&1 && echo yes || echo NO)

    say "terminal $terminal/$TARGET (verified $verified, partial $partial) | runnable $ready | dead $dead | calls $calls | process $alive"

    # The run exits cleanly when its target is met or the queue momentarily
    # empties. If work is queued and nothing is processing it, restart — that
    # silent stop is what leaves conversations unfinished.
    if [[ "$alive" == "NO" && "$ready" -gt 0 ]]; then
        say "!! $ready items queued with no run process — restarting"
        sqlite3 .zen/factory-queue.db "UPDATE factory_work SET status='READY',
            lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL
            WHERE run_id='$RUN_ID' AND status='LEASED';" 2>/dev/null
        scripts/factory-tmux.sh start "$RUN_ID" --workers 32 --max-repair-rounds 5             --max-refinement-items 20000 --max-acquisition-items 1             --acquisition-per-pass 1 --publish-every 320 >>"$LOG" 2>&1             && say "   restarted" || say "   restart failed"
    elif [[ "$alive" == "NO" ]]; then
        say "run process idle; no queued work remains"
    fi
    if [[ "$terminal" -eq "$last_terminal" && "$calls" -eq 0 && "$ready" -gt 0 ]]; then
        stalls=$((stalls + 1))
        say "!! no progress and no model calls (${stalls} consecutive checks)"
        if [[ $stalls -ge 3 ]]; then
            say "!! releasing stale leases to unstick the queue"
            sqlite3 .zen/factory-queue.db "UPDATE factory_work SET status='READY',
                lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL
                WHERE run_id='$RUN_ID' AND status='LEASED';" 2>/dev/null
            stalls=0
        fi
    else
        stalls=0
    fi
    last_terminal=$terminal

    # Audit each new batch of 50 approved conversations without being asked.
    audit=$("$ZEN_BIN"/zen-factory-audit "$RUN_ID" --all-batches --judge --judge-workers 6 2>&1)
    if ! grep -q "No full batch" <<<"$audit"; then
        say "QA audit ran:"
        printf '%s\n' "$audit" | tail -30 | tee -a "$LOG"
    fi

    if [[ "$ready" -eq 0 && "$alive" == "NO" ]]; then
        say "== queue drained: $terminal terminal (verified $verified, partial $partial) =="
        exit 0
    fi
    sleep "$INTERVAL"
done
