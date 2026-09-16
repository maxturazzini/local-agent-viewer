#!/bin/bash
# LAV scheduled classification wrapper (LAV-73).
#
# Classifies interactions that have no metadata yet (lav-classify is
# incremental by default), using whatever backend/model is configured in .env
# (LAV_CLASSIFY_BACKEND etc.). This replaces the removed in-server
# auto-classification: classification only ever runs as this explicit job or
# a manual lav-classify invocation.
#
# Deploy: cp utils/services/lav-classify.sh ~/.local/bin/lav-classify.sh; chmod +x.
# Schedule via ~/Library/LaunchAgents/com.aimax.lav-classify.plist (StartInterval 3600).

VENV="$HOME/.local/lav-venv"
LOG="$HOME/.local/logs/lav-classify-cron.log"

log() { echo "$(date -Iseconds) $1" >> "$LOG"; }

if [ ! -f "$VENV/bin/lav-classify" ]; then
    log "ERROR: lav-classify not found in $VENV"
    exit 1
fi

# Never overlap another classification run (a previous slow cron tick, or a
# manual bulk/reclassification in progress).
if pgrep -f "lav-venv/bin/lav-classify" > /dev/null; then
    log "SKIP: another lav-classify is already running"
    exit 0
fi

log "START incremental classify"
OUTPUT=$("$VENV/bin/lav-classify" --min-messages 2 2>&1)
EXIT_CODE=$?
if [ $EXIT_CODE -ne 0 ]; then
    log "ERROR (exit $EXIT_CODE): $(echo "$OUTPUT" | tail -5)"
    exit $EXIT_CODE
fi
log "OK: $(echo "$OUTPUT" | tail -3 | tr '\n' ' ')"

# LAV-93: refresh the entity graph for the last two days (no model calls, seconds).
# Two days, not one: a session classified while still running is rebuilt the next hour.
if [ -x "$VENV/bin/lav" ]; then
    SINCE=$(date -v-2d +%Y-%m-%d 2>/dev/null || date -d "2 days ago" +%Y-%m-%d)
    if EOUT=$("$VENV/bin/lav" entities build --since "$SINCE" 2>&1); then
        log "OK entities build since $SINCE: $(echo "$EOUT" | "$VENV/bin/python" -c 'import json,sys; d=json.load(sys.stdin); print(d["interactions"], "interactions,", d["mentions"], "mentions,", d["relations"].get("edges_created", 0), "new works_for proposals")' 2>/dev/null)"
    else
        log "ERROR entities build: $(echo "$EOUT" | tail -3 | tr '\n' ' ')"
    fi
fi
