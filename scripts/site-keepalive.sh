#!/bin/bash
# Keep the review website reachable.
#
# Three processes have to be alive for the public URL to work: the status
# server, the path router in front of it, and the cloudflare tunnel. Any one
# dying takes the site down, and they have died for unrelated reasons — tmux
# tearing down the session, and the cloudflared binary being removed.
#
# The current public URL is always written to .zen/site-url.txt. A quick tunnel
# gets a NEW hostname every time cloudflared restarts, so read that file rather
# than remembering a URL.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
RUN_ID="${RUN_ID:-d068146d276e47e08403cec059d889b5}"
TOKEN="${ZEN_STATUS_TOKEN:-j9AuUM8K0IcqrYzDCQMxWDtIZyeM13x-}"
CFD="${CLOUDFLARED:-/home/ubuntu/.local/bin/cloudflared}"
mkdir -p .zen/logs

listening() { ss -ltn 2>/dev/null | grep -q ":$1 "; }

while true; do
    if ! listening 8899; then
        echo "$(date -Is) restarting status server" >> .zen/logs/keepalive.log
        setsid nohup .venv/bin/zen-factory-status "$RUN_ID" \
            --previous a530bc321a624eec871fa02bcda93509 \
            --previous a8466954628f4f96a97b91cde7e97dbc \
            --port 8899 --token "$TOKEN" \
            < /dev/null >> .zen/logs/status.out 2>&1 &
        sleep 8
    fi
    if ! listening 8900; then
        echo "$(date -Is) restarting router" >> .zen/logs/keepalive.log
        setsid nohup .venv/bin/python plugins/review-website/scripts/path_router.py \
            --port 8900 --route /status=http://127.0.0.1:8899 \
            --default http://127.0.0.1:8899 \
            < /dev/null >> .zen/logs/router.out 2>&1 &
        sleep 4
    fi
    # A live cloudflared process is not the same as a working tunnel: the
    # process can survive while its edge connection is gone, which returns 530
    # to anyone with the link. Health-check the public URL itself.
    tunnel_ok=0
    if pgrep -f "cloudflared tunnel" > /dev/null; then
        current=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" \
                  .zen/logs/cloudflared.out 2>/dev/null | tail -1)
        if [ -n "$current" ]; then
            code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 20 \
                   "$current/status/conversations?token=$TOKEN" 2>/dev/null)
            [ "$code" = "200" ] && tunnel_ok=1
        fi
    fi
    if [ "$tunnel_ok" -eq 0 ]; then
        echo "$(date -Is) tunnel unhealthy, restarting" >> .zen/logs/keepalive.log
        pkill -f "cloudflared tunnel" 2>/dev/null
        sleep 2
        : > .zen/logs/cloudflared.out
        setsid nohup "$CFD" tunnel --protocol http2 --retries 5 \
            --url http://127.0.0.1:8900 \
            < /dev/null >> .zen/logs/cloudflared.out 2>&1 &
        # The hostname is only known once cloudflared prints it.
        for _ in $(seq 30); do
            sleep 2
            url=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" \
                  .zen/logs/cloudflared.out 2>/dev/null | head -1)
            [ -n "$url" ] && break
        done
        if [ -n "${url:-}" ]; then
            printf '%s/status/conversations?token=%s\n' "$url" "$TOKEN" \
                > .zen/site-url.txt
            echo "$(date -Is) new url $url" >> .zen/logs/keepalive.log
        fi
    fi
    sleep 30
done
