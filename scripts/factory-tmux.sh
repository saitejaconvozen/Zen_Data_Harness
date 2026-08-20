#!/usr/bin/env bash
# Start, inspect, or stop the factory inside a durable tmux session.
#
#   scripts/factory-tmux.sh start  RUN_ID [extra zen-factory-run flags...]
#   scripts/factory-tmux.sh ui     RUN_ID [PORT]
#   scripts/factory-tmux.sh status
#   scripts/factory-tmux.sh attach
#   scripts/factory-tmux.sh stop | stop-ui
#
# The pane is kept alive after the run exits (remain-on-exit) so the final
# summary and any traceback stay readable instead of vanishing with the session.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=_env.sh
source "$ROOT/scripts/_env.sh"
SESSION="${ZEN_TMUX_SESSION:-zen-factory}"
UI_SESSION="${ZEN_UI_SESSION:-zen-status}"

usage() {
    sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
    exit 64
}

case "${1:-}" in
start)
    shift
    RUN_ID="${1:-}"; [[ -n "$RUN_ID" ]] || usage
    shift || true
    # Validate before touching tmux: remain-on-exit keeps a dead session alive,
    # so "session exists" is not evidence the run actually started.
    # Exit code 2 from the progress CLI means "dead work exists", not "unknown
    # run", so membership in --list is the only correct existence check.
    if ! "$ZEN_BIN"/zen-factory-progress --list 2>/dev/null | grep -qx "  $RUN_ID"; then
        echo "Unknown run_id: $RUN_ID" >&2
        echo >&2
        "$ZEN_BIN"/zen-factory-progress --list >&2 || true
        echo >&2
        echo "Create one with: ${ZEN_BIN}/zen-factory create --target 1000" >&2
        exit 64
    fi
    if tmux has-session -t "$SESSION" 2>/dev/null; then
        # A live run must never be silently replaced.
        if pgrep -f "zen-factory-run $RUN_ID" >/dev/null 2>&1; then
            echo "Run $RUN_ID is already active in session '$SESSION'." >&2
            echo "Attach with: scripts/factory-tmux.sh attach" >&2
            exit 1
        fi
        echo "Removing finished session '$SESSION'."
        tmux kill-session -t "$SESSION"
    fi
    # A tmux session inherits the tmux *server* environment, not this shell's,
    # so run-control variables must be passed explicitly or they vanish.
    tmux new-session -d -s "$SESSION" -c "$ROOT" \
        -e "ZEN_TARGET_TERMINAL=${ZEN_TARGET_TERMINAL:-}" \
        -e "ZEN_MAX_ATTEMPTS=${ZEN_MAX_ATTEMPTS:-40}" \
        -e "ZEN_MODEL_PROVIDER=${ZEN_MODEL_PROVIDER:-codex}" \
        -e "MONGODB_URI=${MONGODB_URI:-}" \
        "scripts/run-factory.sh $RUN_ID $*"
    # Keep the pane after the command exits so output survives.
    tmux set-option -t "$SESSION" remain-on-exit on \; \
         set-option -t "$SESSION" history-limit 200000 >/dev/null
    sleep 3
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
        echo "Session died immediately. Last output:" >&2
        tail -20 ".zen/logs/$RUN_ID.log" 2>/dev/null >&2 || true
        exit 1
    fi
    echo "Started run $RUN_ID in tmux session '$SESSION'."
    echo "  attach : scripts/factory-tmux.sh attach     (detach with Ctrl-B then D)"
    echo "  status : scripts/factory-tmux.sh status"
    echo "  log    : tail -f .zen/logs/$RUN_ID.log"
    ;;
ui)
    shift
    RUN_ID="${1:-}"; [[ -n "$RUN_ID" ]] || usage
    PORT="${2:-8899}"
    # Exit code 2 from the progress CLI means "dead work exists", not "unknown
    # run", so membership in --list is the only correct existence check.
    if ! "$ZEN_BIN"/zen-factory-progress --list 2>/dev/null | grep -qx "  $RUN_ID"; then
        echo "Unknown run_id: $RUN_ID" >&2
        "$ZEN_BIN"/zen-factory-progress --list >&2 || true
        exit 64
    fi
    tmux kill-session -t "$UI_SESSION" 2>/dev/null || true
    tmux new-session -d -s "$UI_SESSION" -c "$ROOT" \
        "${ZEN_BIN}/zen-factory-status $RUN_ID --port $PORT"
    tmux set-option -t "$UI_SESSION" remain-on-exit on >/dev/null
    for _ in $(seq 1 15); do
        if curl -sf -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/"; then
            echo "Status UI live: http://127.0.0.1:$PORT/"
            echo "  JSON  : http://127.0.0.1:$PORT/api/status"
            echo "  stop  : tmux kill-session -t $UI_SESSION"
            echo
            echo "From your laptop, either:"
            echo "  - VS Code Remote: open http://localhost:$PORT (ports are forwarded for you)"
            echo "  - or:  ssh -N -L $PORT:127.0.0.1:$PORT ubuntu@\$(hostname)"
            exit 0
        fi
        sleep 1
    done
    echo "UI did not come up. Pane output:" >&2
    tmux capture-pane -p -t "$UI_SESSION" 2>/dev/null | tail -15 >&2
    exit 1
    ;;
status)
    if tmux has-session -t "$SESSION" 2>/dev/null; then
        echo "tmux session '$SESSION': ALIVE"
    else
        echo "tmux session '$SESSION': not running"
    fi
    pid="$(pgrep -f 'bin/zen-factory-run' | head -1 || true)"
    if [[ -n "$pid" ]]; then
        echo "run process   : pid $pid, up $(ps -p "$pid" -o etimes= | tr -d ' ')s"
    else
        echo "run process   : none"
    fi
    calls=0
    for p in $(pgrep -f "codex exec" 2>/dev/null || true); do
        if tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -q "gpt-5.6-sol"; then
            calls=$((calls + 1))
        fi
    done
    echo "model calls   : $calls concurrent gpt-5.6-sol"
    echo
    tmux capture-pane -p -t "$SESSION" 2>/dev/null | grep -v '^$' | tail -12 || true
    ;;
attach)
    tmux attach -t "$SESSION"
    ;;
stop)
    # Stopping the run must not blind the operator: the UI reads durable state
    # and stays useful while the run is down. Use "stop-ui" to take it down.
    tmux kill-session -t "$SESSION" 2>/dev/null && echo "Stopped '$SESSION'." \
        || echo "No session '$SESSION'."
    if tmux has-session -t "$UI_SESSION" 2>/dev/null; then
        echo "Status UI left running (scripts/factory-tmux.sh stop-ui to close it)."
    fi
    echo "Queue state is durable; restart with: scripts/factory-tmux.sh start RUN_ID"
    ;;
stop-ui)
    tmux kill-session -t "$UI_SESSION" 2>/dev/null && echo "Stopped '$UI_SESSION'." \
        || echo "No session '$UI_SESSION'."
    ;;
*)
    usage
    ;;
esac
