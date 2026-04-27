# Service templates

Long-running deployment templates for LAV components. Currently:

- **lav-mcp** — MCP server in `streamable-http` mode for remote consumption (port 8765 by default).

## Files

| File | Purpose |
|------|---------|
| `bin/lav-mcp.sh` | Wrapper script — sets `LAV_MCP_TRANSPORT`, exec the venv binary |
| `com.aimax.lav-mcp.plist` | macOS LaunchAgent template (placeholder `__HOME__`) |
| `lav-mcp.service` | Linux systemd `--user` unit |
| `install.sh` | Detects platform, copies wrapper + service file, prints activation commands |

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
