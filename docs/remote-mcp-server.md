# Remote MCP server

Reference for running `lav-mcp` over HTTP, so Claude Desktop / Claude Code on a different machine can use the LAV tools without ssh-stdio tunneling.

## Overview

`lav-mcp` ships two transports, selected at process start by an env var:

| Transport | Use when | Auth model |
|-----------|----------|------------|
| `stdio` (default) | Local-only client; CLI launches the binary as a subprocess | tool args carry `api_key` |
| `streamable-http` | Long-running daemon, remote clients via `mcp-remote` | tool args carry `api_key` (same) |

The transport choice does **not** change the tool surface — all 9 tools (`get_interactions`, `get_interaction_details`, `semantic_search`, `kb_status`, `sync`, `kb_index`, `kb_remove`, `kb_update_tags`, `manage_pricing`) work identically over both.

## Architecture

```
                       ┌─────────────────────────┐
Client (Claude Desktop │  npx mcp-remote         │
or Claude Code)        │   ↓ HTTP keep-alive     │
                       └────────────┬────────────┘
                                    │ http://<host>:8765/mcp
                       ┌────────────▼────────────┐
                       │  lav-mcp process        │
                       │   FastMCP                │
                       │   transport=streamable   │
                       └────────────┬────────────┘
                                    │ direct calls (in-process)
                       ┌────────────▼────────────┐
                       │  lav.queries  lav.pricing│
                       │  lav.qdrant.store        │
                       │  lav.server.sync_data    │
                       └─────────────────────────┘
                                    │
                              ~/.local/share/local-agent-viewer/
                                local_agent_viewer.db (SQLite)
                                qdrant_data/         (vector store)
```

The MCP tools are thin adapters over the same core modules used by the CLI (`lav`) and the HTTP server (`lav-server`). No business logic is duplicated.

## Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `LAV_MCP_TRANSPORT` | `stdio` | `streamable-http` enables HTTP server. `http` is accepted as alias. |
| `LAV_MCP_HOST` | `127.0.0.1` | Bind address. Use `0.0.0.0` to expose on LAN/VPN. |
| `LAV_MCP_PORT` | `8765` | TCP port for the HTTP server. |
| `LAV_API_KEY` | _(required for write tools)_ | `sync`, `kb_index`, `kb_remove`, `kb_update_tags`, `manage_pricing` add. |
| `LAV_READ_API_KEY` | _(optional)_ | If unset, read tools are open. **Set this when binding to non-loopback.** |

Env vars are read once at process start. To change them, restart `lav-mcp`.

## Deployment

### Manual (foreground)

```bash
LAV_MCP_TRANSPORT=streamable-http LAV_MCP_PORT=8765 lav-mcp
```

### macOS (launchd) / Linux (systemd)

Templates ship in [`utils/services/`](../utils/services/):

```bash
bash utils/services/install.sh
# macOS
launchctl load ~/Library/LaunchAgents/com.aimax.lav-mcp.plist
# Linux
systemctl --user daemon-reload
systemctl --user enable --now lav-mcp.service
```

Logs land in `~/.local/logs/lav-mcp.log` (and `-err.log`).

The wrapper (`~/.local/bin/lav-mcp.sh`) defaults to loopback; edit it or override env vars in the LaunchAgent / systemd unit to bind to LAN/VPN.

## Client setup

### Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "local-agent-viewer": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://<host>:8765/mcp"]
    }
  }
}
```

`<host>` is `127.0.0.1` for same-machine, or the LAN/Tailscale name/IP otherwise. Restart Claude Desktop after editing.

### Claude Code (`~/.claude/claude_code_config.json`)

Same shape. Verify with `/mcp` in a Claude Code session — `local-agent-viewer__semantic_search` and the other 8 tools should be listed.

### Passing API keys

The MCP tools read auth from a `api_key` argument in each call. The client supplies it from local env, e.g. by setting `LAV_API_KEY` and `LAV_READ_API_KEY` in your shell before launching the client. The transport itself does not authenticate the connection.

## Security

- **No TLS.** The connection is plain HTTP. Run only inside a trusted network (loopback, VPN, Tailscale, internal LAN).
- **No rate limiting.** Any client that can reach the port can call tools.
- **Auth is per-tool, not per-connection.** The server does not authenticate the HTTP session itself; each tool checks `api_key` against the relevant env var.
- **When `LAV_MCP_HOST=0.0.0.0`, set `LAV_READ_API_KEY`.** Otherwise read tools (which can return interaction transcripts) are open to any host that can reach the port.

## Troubleshooting

**Symptom: `curl` returns 406 Not Acceptable**
Streamable-http requires `Accept: application/json, text/event-stream`. The MCP client sets it automatically; if testing manually, include it.

**Symptom: port not reachable from other machine**
- `LAV_MCP_HOST=127.0.0.1` (default) only listens on loopback. Set `0.0.0.0` to expose.
- macOS firewall may prompt the first time the binary listens on a public interface — accept or pre-authorize.
- Verify `lsof -iTCP:8765 -sTCP:LISTEN` shows the process.

**Symptom: `lav-mcp` exits immediately under launchd**
Check `~/.local/logs/lav-mcp-err.log`. Usual culprits:
- Venv missing (`LAV_VENV` env var or default `~/.local/lav-venv` not present).
- `fastmcp` not installed in the venv (`pip install -e ".[mcp]"`).
- `.env` not found at the path `lav/__init__.py` expects.

**Symptom: write tool returns `Invalid or missing api_key`**
The client must pass the `api_key` argument matching the server's `LAV_API_KEY` env var. With `mcp-remote`, the client process inherits your shell env; ensure the env var is exported in the shell that launches Claude Desktop/Code.

## Limitations

- Single-process: no horizontal scaling.
- No HTTP auth header support — auth is at the tool layer only.
- No TLS / no rate limiting.
- Env vars are read once at startup; changing them requires a restart.
- Some tool payloads include `Decimal` or `datetime` (e.g. `manage_pricing list`); FastMCP serializes them through Pydantic, but clients should be tolerant of string-typed numbers.

## Related files

- [`lav/mcp_server.py`](../lav/mcp_server.py) — transport selector in `main()`.
- [`utils/services/`](../utils/services/) — LaunchAgent + systemd templates and `install.sh`.
- [`README.md`](../README.md#remote-mcp-server-http-transport) — quickstart section.
