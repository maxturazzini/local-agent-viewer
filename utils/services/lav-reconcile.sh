#!/bin/bash
# LAV scheduled reconcile wrapper (LAV-98).
#
# Compares this node with its remote agents and fetches what the pull cursor lost.
# The LaunchAgent fires every hour; `lav reconcile --scheduled` decides whether there
# is anything to do (at most one completed run per day, from 03:30) and exits silently
# when there is not. So one line per real run, and nothing for a skipped tick.
#
# Notifications go through LAV_NOTIFY_CMD, set in the plist.
#
# Deploy: cp utils/services/lav-reconcile.sh ~/.local/bin/lav-reconcile.sh; chmod +x.
# Schedule via ~/Library/LaunchAgents/com.aimax.lav-reconcile.plist (StartInterval 3600).

VENV="$HOME/.local/lav-venv"
LOG="$HOME/.local/logs/lav-reconcile.log"

log() { echo "$(date -Iseconds) $1" >> "$LOG"; }

mkdir -p "$(dirname "$LOG")"

if [ ! -x "$VENV/bin/lav" ]; then
    log "ERROR: lav not found in $VENV"
    exit 1
fi

# launchd never overlaps its own job, but a manual reconcile may be running. Harmless
# (inserts are guarded), only wasted: the next tick tries again.
if pgrep -f "lav-venv/bin/lav reconcile" > /dev/null; then
    exit 0
fi

OUTPUT=$("$VENV/bin/lav" reconcile --scheduled --format brief 2>&1)
EXIT_CODE=$?
if [ -n "$OUTPUT" ]; then
    while IFS= read -r line; do
        log "$line"
    done <<< "$OUTPUT"
fi
exit $EXIT_CODE
