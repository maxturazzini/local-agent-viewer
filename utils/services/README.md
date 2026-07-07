# Service templates

Deployment templates for LAV components. Currently:

- **lav-mcp** — MCP server in `streamable-http` mode for remote consumption (port 8765 by default).
- **lav-parser** — scheduled incremental parse (+ remote-agent pull trigger on collector hosts).
- **lav-classify** — scheduled incremental classification (LAV-73). There is NO in-server auto-classification: this hourly job (or a manual `lav-classify`) is the only way rows get classified. Uses the backend configured in `.env` (`LAV_CLASSIFY_BACKEND` etc.); skips if another `lav-classify` is already running.

## Files

| File | Purpose |
|------|---------|
| `bin/lav-mcp.sh` | Wrapper script — sets `LAV_MCP_TRANSPORT`, exec the venv binary |
| `com.aimax.lav-mcp.plist` | macOS LaunchAgent template (placeholder `__HOME__`) |
| `lav-mcp.service` | Linux systemd `--user` unit |
| `lav-parser.sh` | Parse wrapper — incremental parse + agent-pull trigger, schedule every 15 min |
| `lav-classify.sh` | Classify wrapper — incremental `lav-classify --min-messages 2`, concurrency guard |
| `com.aimax.lav-classify.plist` | macOS LaunchAgent template for the classify job (hourly, placeholder `__HOME__`) |
| `install.sh` | Detects platform, copies wrapper + service file, prints activation commands |

## lav-classify quick install (macOS)

```bash
cp utils/services/lav-classify.sh ~/.local/bin/lav-classify.sh && chmod +x ~/.local/bin/lav-classify.sh
sed "s|__HOME__|$HOME|g" utils/services/com.aimax.lav-classify.plist > ~/Library/LaunchAgents/com.aimax.lav-classify.plist
launchctl load ~/Library/LaunchAgents/com.aimax.lav-classify.plist
# log: ~/.local/logs/lav-classify-cron.log
```

## Quick install

```bash
bash utils/services/install.sh
# Then activate as instructed (launchctl on macOS, systemctl --user on Linux).
```

## Defaults & customization

The wrapper defaults are chosen to be **safe** — bound to loopback only:

- `LAV_MCP_TRANSPORT=streamable-http`
- `LAV_MCP_HOST=127.0.0.1`
- `LAV_MCP_PORT=8765`

To expose on LAN/VPN, override before launching (e.g. in the LaunchAgent
`EnvironmentVariables` block, or by editing the wrapper):

```bash
LAV_MCP_HOST=0.0.0.0 LAV_MCP_PORT=8765 \
  ~/.local/bin/lav-mcp.sh
```

When binding to `0.0.0.0`, **set `LAV_READ_API_KEY`** in your `.env` so read
operations are not open to anyone on the network. See [docs/remote-mcp-server.md](../../docs/remote-mcp-server.md).

## Logs

- macOS: `~/.local/logs/lav-mcp.log`, `~/.local/logs/lav-mcp-err.log`
- Linux: same files, plus `journalctl --user -u lav-mcp.service`

## Venv assumption

The wrapper assumes the LAV venv is at `~/.local/lav-venv`. Override with:

```bash
LAV_VENV=/path/to/venv bash utils/services/install.sh
```

Or edit `~/.local/bin/lav-mcp.sh` after install.
