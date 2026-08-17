#!/usr/bin/env bash
# Launch one governed factory run to completion.
#
# The run is resumable: all stages are durable queue work, so re-running the
# same RUN_ID continues where the last invocation stopped. Safe to restart after
# a crash, a reboot, or a deliberate stop.
#
#   scripts/run-factory.sh RUN_ID [--workers 16] [any zen-factory-run flag]
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ $# -lt 1 ]]; then
    echo "usage: $0 RUN_ID [zen-factory-run flags...]" >&2
    exit 64
fi
RUN_ID="$1"; shift

# Credentials live outside the repo tree's tracked files.
if [[ -f .zen/factory.env ]]; then
    # shellcheck disable=SC1091
    source .zen/factory.env
fi
if [[ -z "${MONGODB_URI:-}" ]]; then
    echo "MONGODB_URI is not set. Put it in .zen/factory.env or export it." >&2
    exit 78
fi

# Newest inventory artifact unless one is passed through.
INVENTORY="${ZEN_INVENTORY:-}"
if [[ -z "$INVENTORY" ]]; then
    INVENTORY="$(ls -t .zen/artifacts/blobs/*/* 2>/dev/null \
        | xargs -r grep -l '"agents_returned"' 2>/dev/null | head -1 || true)"
fi
if [[ -z "$INVENTORY" || ! -f "$INVENTORY" ]]; then
    echo "No agent inventory artifact found. Build one first:" >&2
    echo "  .venv/bin/python -m zen_agent.cli run 'shortlist agents' --input max_agents=4000" >&2
    exit 66
fi

# Fail loudly on a placeholder or typo instead of running against nothing.
# Exit code 2 from the progress CLI means "dead work exists", not "unknown
    # run", so membership in --list is the only correct existence check.
    if ! .venv/bin/zen-factory-progress --list 2>/dev/null | grep -qx "  $RUN_ID"; then
    echo "Unknown run_id: $RUN_ID" >&2
    .venv/bin/zen-factory-progress --list >&2 || true
    echo >&2
    echo "Create one with: .venv/bin/zen-factory create --target 1000" >&2
    exit 64
fi

SITE=".zen/sites/$RUN_ID"
LOG=".zen/logs/$RUN_ID.log"
mkdir -p "$SITE" "$(dirname "$LOG")"

echo "run_id    : $RUN_ID"
echo "inventory : $INVENTORY"
echo "site      : $SITE"
echo "log       : $LOG"
echo "started   : $(date -Is)"
echo

# Unbuffered so the log and the tmux pane fill in real time rather than in
# 4KB bursts, which made a live run look hung.
export PYTHONUNBUFFERED=1

# Budgets sized for a ~1000-conversation batch; override any of them by
# passing the same flag again after RUN_ID.
# Run to completion. A long unattended batch will hit transient faults; the
# queue is durable, so releasing stale leases and re-entering resumes exactly
# where it stopped. Stops early when every selected conversation is terminal.
# The target lives on disk, not in the environment. tmux sessions inherit the
# tmux *server* environment rather than the caller's, so an exported variable
# silently vanished and the run would have processed all 1,460 sourced
# conversations instead of stopping at the requested 500.
TARGET_FILE=".zen/${RUN_ID}.target"
if [[ -z "${ZEN_TARGET_TERMINAL:-}" && -f "$TARGET_FILE" ]]; then
    ZEN_TARGET_TERMINAL="$(tr -dc '0-9' < "$TARGET_FILE")"
    export ZEN_TARGET_TERMINAL
fi
[[ -n "${ZEN_TARGET_TERMINAL:-}" ]] \
    && echo "target      : stop at ${ZEN_TARGET_TERMINAL} terminal conversations" \
    || echo "target      : none (runs until every sourced conversation is terminal)"

ATTEMPTS="${ZEN_MAX_ATTEMPTS:-40}"
for attempt in $(seq 1 "$ATTEMPTS"); do
    if [[ $attempt -gt 1 ]]; then
        echo
        echo "--- attempt $attempt/$ATTEMPTS at $(date -Is): releasing stale leases ---"
        sqlite3 .zen/factory-queue.db "UPDATE factory_work SET status='READY',
            lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL
            WHERE run_id='$RUN_ID' AND status='LEASED';" || true
        sleep 5
    fi
    set +e
    .venv/bin/zen-factory-run "$RUN_ID" \
    --inventory-artifact "$INVENTORY" \
    --site "$SITE" \
    --workers 16 \
    --max-acquisition-items 4000 \
    --max-refinement-items 20000 \
        --dead-budget 40 \
        "$@" 2>&1 | tee -a "$LOG"
    code=${PIPESTATUS[0]}
    set -e
    if [[ $code -eq 0 ]]; then
        echo "Run $RUN_ID completed at $(date -Is)."
        exit 0
    fi
    selected=$(sqlite3 .zen/factory-queue.db "SELECT COUNT(*) FROM factory_work
        WHERE run_id='$RUN_ID' AND stage='agent_audit';" 2>/dev/null || echo 0)
    terminal=$(sqlite3 .zen/factory-queue.db "SELECT COUNT(*) FROM factory_work
        WHERE run_id='$RUN_ID' AND stage='terminal' AND status='SUCCEEDED';" 2>/dev/null || echo 0)
    echo "attempt $attempt exited $code | terminal $terminal of $selected sourced"
    if [[ -n "${ZEN_TARGET_TERMINAL:-}" && $terminal -ge ${ZEN_TARGET_TERMINAL} ]]; then
        echo "Reached target of ${ZEN_TARGET_TERMINAL} terminal conversations."
        exit 0
    fi
done
echo "Exhausted $ATTEMPTS attempts; see $LOG." >&2
exit 2
