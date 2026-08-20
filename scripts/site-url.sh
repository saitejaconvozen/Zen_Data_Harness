#!/bin/bash
# Print the review website's current public URL.
# Quick tunnels get a new hostname on every restart, so never remember a URL —
# ask for it. scripts/site-keepalive.sh keeps this file current.
cat "$(dirname "${BASH_SOURCE[0]}")/../.zen/site-url.txt" 2>/dev/null \
  || echo "no URL recorded; is scripts/site-keepalive.sh running?"
