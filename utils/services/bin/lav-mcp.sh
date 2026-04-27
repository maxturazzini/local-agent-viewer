#!/bin/bash
# Wrapper script for running lav-mcp under launchd / systemd.
# Customize via env vars (LAV_MCP_HOST defaults to loopback for safety).
set -euo pipefail

export LAV_MCP_TRANSPORT="${LAV_MCP_TRANSPORT:-streamable-http}"
export LAV_MCP_HOST="${LAV_MCP_HOST:-127.0.0.1}"
export LAV_MCP_PORT="${LAV_MCP_PORT:-8765}"

VENV_BIN="${LAV_VENV:-$HOME/.local/lav-venv}/bin"

if [ ! -x "$VENV_BIN/lav-mcp" ]; then
    echo "ERROR: lav-mcp not found at $VENV_BIN/lav-mcp" >&2
    echo "Install with: pip install -e \"$HOME/claude_projects/local-agent-viewer[mcp]\"" >&2
    exit 1
fi

mkdir -p "$HOME/.local/logs"

exec "$VENV_BIN/lav-mcp"
