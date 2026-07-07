# Infrastructure & deployment

How LocalAgentViewer (LAV) is meant to run across machines. This document uses
**placeholder host names** (`dev-host`, `prod-host`) — the real host names, ssh
targets and copy-paste commands for a given installation are operator-private and
live outside the repo (see [Operator runbook](#operator-runbook) below).

> Same pattern as `config.agent.example.json` / `config.json` and
> `lav/taxonomy.example.json` / `lav/taxonomy.json`: the generic shape is
> committed, the real values are not.

## Two-environment model

LAV runs as a small distributed system. Code is shared via git; **runtime config
is per-machine** at `~/.local/share/local-agent-viewer/config.json` (gitignored).
A typical setup is two machines:

| Role | `config.json` `role` | Exposes on `:8764` | Purpose |
|---|---|---|---|
| **`dev-host`** (dev) | `agent` | `/api/health\|info\|export` only — **no dashboard** | Local development; parses its own JSONL |
| **`prod-host`** (prod) | `both` | Full dashboard + API + sync + MCP | Long-running server + collector |

**Always run `hostname` first** before touching "prod" or a running server, so you
know which machine you are on.

### Testing UI changes on an `agent` machine

An `agent`-role server has no dashboard. To test UI changes on a dev machine, spin
up a temporary `lav-server` with the role overridden to `both` on a different port
(e.g. `:8765`) by monkey-patching `lav.server._runtime_config` from a one-off
Python launcher. Never edit the prod agent config to do this.

## Agent / collector data flow

Push-triggered pull, **not** periodic polling:

1. The **agent** parses its local JSONL into its own SQLite DB.
2. The agent notifies the **collector** via `POST`.
3. The collector pulls new rows via `/api/export`.

Both machines share the same git repo path via editable install, so a `git pull`
updates code on either side. See also [DATA_MODEL.md](DATA_MODEL.md) for the DB
shape and [remote-mcp-server.md](remote-mcp-server.md) for exposing `lav-mcp` over
HTTP.

## Deploy decision tree

Branch on **what changed in the diff**. Deploy is `git pull` on `prod-host`, plus a
conditional step:

| Changed in diff | Extra step after `git pull` |
|---|---|
| Only `lav/static/**` | Nothing — browser hard refresh (static files re-read each request) |
| `pyproject.toml` (e.g. new `console_scripts`) | `pip install -e .` in the prod venv |
| Any `lav/*.py` (server code) | Restart: `kill $(pgrep -f "python.*-m lav.server")` — KeepAlive auto-restarts. The wrapper bash + tee don't need killing. **Note**: `pgrep -f lav-server` matches only the wrapper; use `python.*lav.server`. |
| `lav/mcp_server.py` | Also restart `lav-mcp` (`pgrep -f "lav-venv/bin/lav-mcp"`) — this drops live MCP client connections |
| `pyproject.toml` version bump | Tag the release after push |

## Production deployment layout

On the `prod-host` machine:

- **venv**: `~/.local/lav-venv/`
- **LaunchAgents**: a KeepAlive server, a parser (every 15 min), a classifier
  (hourly, incremental — the only automatic classification; LAV-73), and an MCP
  server (if streamable-http is enabled). See
  [utils/services/README.md](../utils/services/README.md) for the plist templates.
- **Wrapper scripts**: `~/.local/bin/lav-server.sh`, `~/.local/bin/lav-parser.sh`,
  `~/.local/bin/lav-classify.sh`

## Canonical hostname

`socket.gethostname()` is volatile on macOS (transiently `Mac`/mojibake), so host
identity comes from `_canonical_hostname()` in `jsonl.py` (LAV-68) — precedence:
`LAV_HOSTNAME` env → `config.json` `"hostname"` key → validated socket name →
`unknown`. **Set a stable `"hostname"` in each node's `config.json`** (dev machine →
e.g. `dev-host`, prod machine → e.g. `prod-host`) or new host rows will split one
machine's sessions. Corrupted/generic names are rejected by `_is_valid_hostname()`
and never inserted.

## Operator runbook

The concrete host names, ssh targets, org-specific service labels and copy-paste
deploy commands for **this** installation are operator-private (they identify real
machines) and are **not committed** — they live in `internal_docs/infra.md`
(gitignored). If you cloned this repo, create your own from this document.
