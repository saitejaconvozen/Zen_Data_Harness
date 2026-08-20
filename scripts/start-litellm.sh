#!/bin/bash
# Start (or inspect) the LiteLLM proxy the factory talks to.
#
#   scripts/start-litellm.sh                 start on :4000
#   scripts/start-litellm.sh --list-upstream ask Google which models exist
#   scripts/start-litellm.sh --check         is the proxy answering?
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
[ -f .zen/factory.env ] && source .zen/factory.env
LITELLM=/home/ubuntu/.local/bin/litellm
PORT="${LITELLM_PORT:-4000}"

case "${1:-start}" in
--list-upstream)
    # The single most common setup error is inventing a model id. Ask Google.
    if [ -z "${GEMINI_API_KEY:-}" ]; then
        echo "GEMINI_API_KEY is not set; add it to .zen/factory.env" >&2; exit 78
    fi
    curl -s "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY" \
      | grep -o '"name": *"models/[^"]*"' | sed 's/.*models\///;s/"//' | sort
    ;;
--check)
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
           -H "Authorization: Bearer ${LITELLM_MASTER_KEY:-}" \
           "http://127.0.0.1:$PORT/v1/models")
    echo "proxy on :$PORT -> HTTP ${code:-no response}"
    [ "$code" = "200" ] && curl -s -H "Authorization: Bearer ${LITELLM_MASTER_KEY:-}" \
        "http://127.0.0.1:$PORT/v1/models" | head -c 600
    ;;
*)
    for v in GEMINI_API_KEY LITELLM_MASTER_KEY; do
        [ -z "${!v:-}" ] && { echo "$v is not set; add it to .zen/factory.env" >&2; exit 78; }
    done
    mkdir -p .zen/logs
    # Detached: the proxy must outlive the shell that started it.
    setsid nohup "$LITELLM" --config litellm-config.yaml --port "$PORT" \
        < /dev/null >> .zen/logs/litellm.log 2>&1 &
    echo "starting litellm on :$PORT (log: .zen/logs/litellm.log)"
    for _ in $(seq 30); do
        sleep 2
        ss -ltn 2>/dev/null | grep -q ":$PORT " && { echo "listening"; exit 0; }
    done
    echo "did not come up; last log lines:" >&2
    tail -20 .zen/logs/litellm.log >&2
    exit 1
    ;;
esac
