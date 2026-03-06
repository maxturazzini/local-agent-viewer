# Changelog

## 2026-03-02 — Agent/Collector Architecture

Distributed cross-machine architecture: each machine parses its own conversations, a central collector aggregates everything into a canonical DB.

### Changes

#### `config.py`
- **DB path**: moved from `data/local_agent_viewer.db` (shared) to `~/.local/share/local-agent-viewer/local_agent_viewer.db` (per-machine). Automatic fallback to legacy path.
- **`load_runtime_config()`**: reads `~/.local/share/local-agent-viewer/config.json` for role (`agent` / `collector` / `both`). Default: `both`.

#### `parser.py`
- **`_normalize_hostname()`**: strips `.local` / `.localdomain` to avoid duplicate host records.
- **`ingest_remote_sessions()`**: imports sessions from agents via `/api/export`. `INSERT OR IGNORE` on composite PKs for idempotency.

#### `queries.py`
- **`export_sessions(since, limit)`**: serializes local sessions with all child data for collector pull. Batch-loading to avoid N+1.

#### `server.py`
- **Role gating GET**: in `agent` mode, only `/api/health`, `/api/info`, `/api/export` served. Everything else → 404.
- **Role gating POST**: in `agent` mode, no POST endpoints (including `/api/sync`).
- **Bind address**: `agent` → `0.0.0.0` (network-reachable); `collector`/`both` → `localhost`.
- **`pull_from_agents()`**: incremental pull with `last_pull` checkpoint per agent, primary + fallback URL, configurable timeout.
- **`/api/export`**: endpoint calling `export_sessions()` for collector pull.

---

## 2026-02-22 — ChatGPT Integration + MCP Server + Dashboard

ChatGPT conversations.json parser, FastMCP server, interactive dashboard with Chart.js.

## 2026-02-13 — Multi-user, multi-host, 4D filters

Unified database with 4 independent dimensions (project, user, host, source).
