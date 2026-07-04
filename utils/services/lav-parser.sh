#!/bin/bash
# LAV LaunchAgent / cron wrapper. Used by:
#   - macChia (role=agent): runs local incremental parse only; agent block at
#     the bottom is a no-op because there are no configured remote agents.
#   - minimacs (role=both):  local parse + HTTP pull from configured remote
#     agents via POST /api/sync scope=agent. Without this second step the
#     collector falls behind whenever a remote-agent pull is missed (no
#     push-trigger from agents is implemented; see CLAUDE.md note).
#
# Deploy: cp utils/services/lav-parser.sh ~/.local/bin/lav-parser.sh; chmod +x.
# Schedule via ~/Library/LaunchAgents/com.aimax.lav-parser.plist (StartInterval 900).

VENV="$HOME/.local/lav-venv"
LOG="$HOME/.local/logs/lav-parser.log"
ENV_FILE="$HOME/claude_projects/local-agent-viewer/.env"

log() { echo "$(date -Iseconds) $1" >> "$LOG"; }

if [ ! -f "$VENV/bin/lav-parse" ]; then
    log "ERROR: lav-parse not found in $VENV"
    exit 1
fi

# 1) Local incremental parse (always).
log "START incremental parse"
OUTPUT=$("$VENV/bin/lav-parse" --include-cowork --include-codex 2>&1)
EXIT_CODE=$?
if [ $EXIT_CODE -ne 0 ]; then
    log "ERROR (exit $EXIT_CODE): $OUTPUT"
    exit $EXIT_CODE
fi
SUMMARY=$(echo "$OUTPUT" | tail -5)
log "OK local parse: $SUMMARY"

# 2) Trigger pull from all configured remote agents via /api/sync scope=agent.
#    No-op on machines without configured agents (lav-server returns quickly).
#    Requires LAV_API_KEY for write-auth on /api/sync.
if [ -f "$ENV_FILE" ]; then
    LAV_API_KEY=$(grep -E "^LAV_API_KEY=" "$ENV_FILE" | head -1 | cut -d= -f2-)
    if [ -n "$LAV_API_KEY" ]; then
        RESP=$(curl -s -m 240 -X POST \
            -H "Authorization: Bearer $LAV_API_KEY" \
            -H "Content-Type: application/json" \
            -d '{"scope":"agent"}' \
            http://localhost:8764/api/sync 2>&1)
        log "agent sync trigger: $RESP"
    else
        log "WARN: LAV_API_KEY not found in $ENV_FILE — skipping agent sync"
    fi
else
    log "INFO: $ENV_FILE not found — skipping agent sync (likely agent-only host)"
fi
