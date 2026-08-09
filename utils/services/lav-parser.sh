#!/bin/bash
# LAV LaunchAgent / cron wrapper. Used by:
#   - macChia (role=agent): runs local incremental parse only; agent block at
#     the bottom is a no-op because there are no configured remote agents.
#   - minimacs (role=both):  local parse + HTTP pull from configured remote
#     agents via POST /api/sync scope=agent. Without this second step the
#     collector falls behind whenever a remote-agent pull is missed (no
#     push-trigger from agents is implemented; see CLAUDE.md note).
#
# Both roles then run the LAV-78 tool-outcome heal (step 3) — it is local-only
# and needs no server, no API key and no network.
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

# 3) Heal tool outcomes (LAV-78).
#    A tool_result that lands after its tool_use was already parsed leaves
#    is_error/duration_ms NULL on the row. The agent repairs its own row on a
#    later parse, but that repair never reaches the collector: /api/export pages
#    child rows by the tool call's OWN timestamp (already behind the cursor) and
#    the ingest is NOT EXISTS-guarded, so an existing row is never updated.
#    So each node heals itself from messages.content, which both already hold
#    verbatim — no reparse, no re-sync, no watermark touched.
#
#    Incremental on purpose: only sessions active in the lookback window. The
#    window must cover the worst realistic PULL LAG, not just this schedule
#    interval, because on the collector the timestamps are the agent's own
#    (when the work happened), not when it was ingested — rows that arrive late
#    still carry old timestamps. 72h covers a weekend of laptop sleep or a
#    collector outage. Widen it after a longer outage with
#    LAV_BACKFILL_LOOKBACK_HOURS, or run the command once without --since.
#
#    Never fatal: the parse and the sync above already succeeded and must stay
#    counted. Silently skipped on a node whose checkout predates the command,
#    so this file is safe to deploy on either node first.
BACKFILL_HOURS="${LAV_BACKFILL_LOOKBACK_HOURS:-72}"
if [ -x "$VENV/bin/lav" ] && "$VENV/bin/lav" backfill tool-outcomes --help > /dev/null 2>&1; then
    SINCE=$(date -u -v-"${BACKFILL_HOURS}"H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
            || date -u -d "${BACKFILL_HOURS} hours ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)
    if [ -z "$SINCE" ]; then
        log "WARN: could not compute a --since window — skipping tool-outcomes backfill"
    else
        BF_OUTPUT=$("$VENV/bin/lav" backfill tool-outcomes \
            --since "$SINCE" --progress-every 0 --format brief 2>&1)
        BF_EXIT=$?
        if [ $BF_EXIT -ne 0 ]; then
            log "WARN tool-outcomes backfill failed (exit $BF_EXIT): $(echo "$BF_OUTPUT" | tail -3 | tr '\n' ' ')"
        else
            log "OK tool-outcomes backfill (since $SINCE): $(echo "$BF_OUTPUT" | grep -m1 '^APPLIED' || echo "$BF_OUTPUT" | tail -1)"
        fi
    fi
else
    log "INFO: lav backfill tool-outcomes unavailable in $VENV — skipping (pre-LAV-78 checkout)"
fi
