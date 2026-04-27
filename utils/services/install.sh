#!/bin/bash
# Install LAV LaunchAgent / systemd service templates.
#
# Currently installs:
#   - lav-mcp (streamable-http MCP server, port 8765)
#
# Detects platform automatically (macOS launchd vs Linux systemd --user).
# Substitutes __HOME__ in plist files at install time.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLATFORM="$(uname -s)"

mkdir -p "$HOME/.local/bin" "$HOME/.local/logs"

# Wrapper script
install -m 755 "$SCRIPT_DIR/bin/lav-mcp.sh" "$HOME/.local/bin/lav-mcp.sh"
echo "  installed wrapper: $HOME/.local/bin/lav-mcp.sh"

if [ "$PLATFORM" = "Darwin" ]; then
    LA_DIR="$HOME/Library/LaunchAgents"
    mkdir -p "$LA_DIR"
    PLIST="$LA_DIR/com.aimax.lav-mcp.plist"

    sed "s|__HOME__|$HOME|g" "$SCRIPT_DIR/com.aimax.lav-mcp.plist" > "$PLIST"
    echo "  installed plist:   $PLIST"
    echo
    echo "Activate:"
    echo "  launchctl load $PLIST"
    echo "Verify:"
    echo "  launchctl list | grep com.aimax.lav-mcp"
elif [ "$PLATFORM" = "Linux" ]; then
    UNIT_DIR="$HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"
    install -m 644 "$SCRIPT_DIR/lav-mcp.service" "$UNIT_DIR/lav-mcp.service"
    echo "  installed unit:    $UNIT_DIR/lav-mcp.service"
    echo
    echo "Activate:"
    echo "  systemctl --user daemon-reload"
    echo "  systemctl --user enable --now lav-mcp.service"
    echo "Verify:"
    echo "  systemctl --user status lav-mcp.service"
else
    echo "ERROR: unsupported platform '$PLATFORM' (need Darwin or Linux)" >&2
    exit 1
fi
