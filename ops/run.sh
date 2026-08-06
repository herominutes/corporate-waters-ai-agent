#!/usr/bin/env bash
# Launch The Goodman Protocol
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Ensure PYTHONPATH includes our src directory
export PYTHONPATH="$SCRIPT_DIR/src:${PYTHONPATH:-}"

# Append-mode logging so logrotate copytruncate works without sparse holes
exec >> "$SCRIPT_DIR/logs/bot.log" 2>&1

MAX_BACKOFF=60
backoff=5

while true; do
    echo "[$(date)] Starting bot..."
    start_ts=$(date +%s)
    python3 -m goodman.bot && exit_code=$? || exit_code=$?
    elapsed=$(( $(date +%s) - start_ts ))
    echo "[$(date)] Bot exited with code $exit_code after ${elapsed}s, restarting in ${backoff}s..."
    sleep "$backoff"
    if [ "$elapsed" -gt 300 ]; then
        backoff=5
    else
        backoff=$((backoff < MAX_BACKOFF ? backoff * 2 : MAX_BACKOFF))
    fi
done
