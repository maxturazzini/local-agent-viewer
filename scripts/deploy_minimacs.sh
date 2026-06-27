#!/usr/bin/env bash
#
# Deploy LAV to production (minimacs.local) — LAV-61/62 release runbook.
#
# Run this FROM macChia. It orchestrates the deploy over ssh per the CLAUDE.md
# decision tree, PLUS the LAV-61/62 one-off data migration (Cowork merge).
#
# Sequence:
#   1. Pre-flight: confirm the release commit is on origin/main (minimacs pulls from there).
#   2. git pull on minimacs.
#   3. pip install -e   ONLY if pyproject.toml changed (new console_scripts/version).
#   4. Data migration:  scripts/migrate_cowork_merge.py --yes  (backs up the prod DB first).
#   5. Restart server (lav/*.py changed) and lav-mcp (cli/mcp changed). KeepAlive auto-restarts.
#   6. Health check.
#
# Usage:
#   scripts/deploy_minimacs.sh            # DRY RUN — prints the plan, runs nothing remote-mutating
#   scripts/deploy_minimacs.sh --run      # execute the deploy
#
# Env overrides:
#   MINIMACS_HOST  (default: minimacs.local)
#   REPO_DIR       (default: ~/claude_projects/local-agent-viewer)
#   VENV           (default: ~/.local/lav-venv)
set -euo pipefail

HOST="${MINIMACS_HOST:-minimacs.local}"
REPO="${REPO_DIR:-\$HOME/claude_projects/local-agent-viewer}"   # expanded remotely
VENV="${VENV:-\$HOME/.local/lav-venv}"                          # expanded remotely
RUN=0
[[ "${1:-}" == "--run" ]] && RUN=1

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
remote() { ssh "$HOST" "bash -lc '$*'"; }

say "Target: $HOST   repo: $REPO   venv: $VENV   mode: $([[ $RUN == 1 ]] && echo RUN || echo DRY-RUN)"

# 1. Pre-flight — the commit must be pushed (minimacs deploys via git pull from origin).
LOCAL_HEAD="$(git rev-parse HEAD)"
say "Local HEAD: $LOCAL_HEAD"
if ! git branch -r --contains "$LOCAL_HEAD" 2>/dev/null | grep -q 'origin/main'; then
  echo "WARNING: HEAD is not on origin/main yet. Push first:  git push origin main"
  [[ $RUN == 1 ]] && { echo "Aborting: refusing to deploy an un-pushed commit."; exit 1; }
fi

# 2. Detect what changed between the deployed revision and HEAD (drives steps 3 & 5).
say "Remote currently at:"
remote "cd $REPO && git rev-parse --short HEAD && git log --oneline -1"

CHANGED="$(git diff --name-only "$(remote "cd $REPO && git rev-parse HEAD" 2>/dev/null || echo HEAD~1)" "$LOCAL_HEAD" 2>/dev/null || git show --name-only --pretty=format: "$LOCAL_HEAD")"
PYPROJECT_CHANGED=$(echo "$CHANGED" | grep -qx 'pyproject.toml' && echo 1 || echo 0)
PY_CHANGED=$(echo "$CHANGED" | grep -q '^lav/.*\.py$' && echo 1 || echo 0)
MCP_CHANGED=$(echo "$CHANGED" | grep -qx 'lav/mcp_server.py' && echo 1 || echo 0)
say "Changed files:"; echo "$CHANGED" | sed 's/^/    /'
echo "  pyproject_changed=$PYPROJECT_CHANGED  py_changed=$PY_CHANGED  mcp_changed=$MCP_CHANGED"

if [[ $RUN == 0 ]]; then
  cat <<EOF

DRY-RUN plan (nothing executed remotely):
  2) ssh $HOST 'cd $REPO && git pull'
  3) $([[ $PYPROJECT_CHANGED == 1 ]] && echo "$VENV/bin/pip install -e $REPO" || echo "(skip pip install — pyproject unchanged)")
  4) $VENV/bin/python $REPO/scripts/migrate_cowork_merge.py --yes      # backs up prod DB, purges+remerges cowork
  5) $([[ $PY_CHANGED == 1 ]] && echo "kill \$(pgrep -f 'python.*-m lav.server')   # KeepAlive restarts" || echo "(no server restart needed)")
     $([[ $MCP_CHANGED == 1 ]] && echo "pkill -f '$VENV/bin/lav-mcp'   # drops live MCP clients" || echo "(no mcp restart needed)")
  6) curl health check
Re-run with --run to execute.
EOF
  exit 0
fi

# --- REAL DEPLOY ---
say "2) git pull"
remote "cd $REPO && git pull --ff-only"

if [[ $PYPROJECT_CHANGED == 1 ]]; then
  say "3) pip install -e (pyproject changed)"
  remote "$VENV/bin/pip install -e $REPO"
else
  say "3) skip pip install (pyproject unchanged)"
fi

say "4) data migration (Cowork merge) — backs up the prod DB first"
remote "cd $REPO && $VENV/bin/python scripts/migrate_cowork_merge.py --yes"

if [[ $PY_CHANGED == 1 ]]; then
  say "5) restart lav-server (KeepAlive auto-restarts)"
  remote "pkill -f 'python.*-m lav.server' || true"
fi
if [[ $MCP_CHANGED == 1 ]]; then
  say "5b) restart lav-mcp"
  remote "pkill -f '$VENV/bin/lav-mcp' || true"
fi

say "6) health check"
sleep 3
remote "curl -s -m 5 http://localhost:8764/api/health || echo 'health check failed — inspect logs'"
say "Done. Verify the dashboard at http://$HOST:8764 (hard-refresh)."
