# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

LocalAgentViewer (LAV) — local long-term memory for AI agent interactions. Parses JSONL/JSON logs from Claude Code, Codex CLI, Claude Desktop, ChatGPT, and Anthropic claude.ai account exports into a single SQLite database with a web dashboard, AI classification, and optional vector search.

## Commands

```bash
# Install (zero dependencies for core, extras for optional features)
pip install -e .              # core only
pip install -e ".[all]"       # everything (qdrant, openai, fastmcp)

# Parse & serve
lav-parse                     # incremental parse from local JSONL
lav-parse --project myProject # parse one project
lav-parse --full              # full reparse
lav-parse-chatgpt             # parse ChatGPT export
lav-parse-claude-ai           # parse Anthropic claude.ai export folder (data-*-batch-0000)
lav-server                    # start server on :8764

# Unified CLI — query & KB management (zero extra deps)
lav search "query"            # FTS5 full-text search
lav search "query" --project miniMe --limit 5 --format brief
lav show <session_id>         # full interaction transcript
lav kb search "semantic query" # Qdrant vector search
lav kb status <session_id>    # check if indexed
lav kb index <session_id> --tags "tag1,tag2"
lav kb remove <session_id>
lav kb tags <session_id> --set "new,tags"
lav sync                      # trigger sync (needs LAV_API_KEY)
lav sync --scope project --project miniMe
lav pricing list              # list active pricing
lav pricing add --model X --input 5.0 --output 25.0 --from-date 2026-01-01

# Specialized CLIs (still available)
lav-classify                  # AI classification (needs OPENAI_API_KEY)
lav-index                     # Qdrant vector indexing
lav-mcp                       # MCP server (needs fastmcp)
lav-pricing list              # list model pricing (standalone)
lav-pricing add --model X ... # add/update pricing entry
lav-pricing seed              # insert default pricing data
```

Server at http://localhost:8764 — dashboard.html, interactions.html, tags.html.

**No unit test suite.** Manual testing via the running server and CLI commands. Classification model evals in `tests/evals/` (`eval_classify.py`), reports in `tests/evals/results/`.

### CLI output formats

`lav` defaults to JSON on stdout (for piping to `jq` or Claude Code Bash calls). Human-friendly alternatives:
- `--format table` — ASCII table
- `--format brief` — one line per result (session_id, project, summary)

### CLI auth

- **Read operations** (`search`, `show`, `kb search`, `kb status`, `pricing list`): require `LAV_READ_API_KEY` env var only if it's set on server side. If not set, access is open.
- **Write operations** (`sync`, `kb index`, `kb remove`, `kb tags`, `pricing add`): require `LAV_API_KEY` env var.

## Architecture

### Three-layer data pipeline

1. **Parse → SQLite** (`lav/parsers/`) — raw interactions, tokens, files, tools, costs
2. **Classify → `interaction_metadata`** (`lav/classifiers/`) — AI classification via configurable model (OpenAI, Ollama, vLLM, any OpenAI-compatible endpoint) (optional)
3. **Index → Qdrant** (`lav/qdrant/`) — vector embeddings for semantic search (optional)

Each layer is independent. The core works with just layer 1.

### Database

Single SQLite DB at `~/.local/share/local-agent-viewer/local_agent_viewer.db`.

**4 independent filter dimensions** on every query:
- **Project** (`projects`) — which codebase
- **User** (`users`) — which person
- **Host** (`hosts`) — which machine
- **Source** (`session_sources`) — which agent (claude_code, codex_cli, cowork_desktop, chatgpt, claude_ai)

Composite PK: `interactions(session_id, project_id)`. Append-only — records are never deleted.

**Cost tracking**: `model_pricing` table stores per-model prices with temporal validity (`from_date`/`to_date`). Costs are calculated at query time via LEFT JOIN — never materialized. Table is seeded automatically by `init_db()`. CLI: `lav-pricing`. MCP tool: `manage_pricing`. API: `/api/pricing`.

### Server (`lav/server.py`)

ThreadingHTTPServer with role gating:
- **agent**: thin server — only `/api/health`, `/api/info`, `/api/export`
- **both** (default): full dashboard + API + sync + MCP
- **collector**: pulls from remote agents, no local parse

Read-only connections for queries (`PRAGMA query_only=ON`), WAL mode, busy_timeout 5000ms.

### Agent/Collector distributed model

Code is shared (git). Runtime config is per-machine at `~/.local/share/local-agent-viewer/config.json` (not tracked). Example configs in repo: `config.agent.example.json`, `config.collector.example.json`.

**Data flow**: agent parses locally → notifies collector via POST → collector pulls via `/api/export`. Push-triggered pull, NOT periodic polling.

### Unified CLI (`lav/cli.py`)

argparse-based CLI (zero deps) exposing the same operations as the MCP server: `search`, `show`, `kb {search,status,index,remove,tags}`, `sync`, `pricing {list,add}`. Reuses `queries.py`, `pricing.py`, `qdrant/store.py`, `qdrant/indexer.py`, `server.sync_data()`. Copies DB connection and lazy Qdrant init patterns from `mcp_server.py`.

### MCP Server (`lav/mcp_server.py`)

FastMCP server with 9 tools (8 original + `manage_pricing`). Read tools use `LAV_READ_API_KEY` (optional). Write tools require `LAV_API_KEY`. The `lav` CLI is a faster alternative for terminal/Bash usage (no JSON-RPC overhead).

### Frontend (`lav/static/`)

Vanilla HTML/JS/CSS + Chart.js CDN. Three pages: dashboard (6 sub-tabs), interactions list, tags. Filters auto-disable when only one value exists.

### Environment & config

- `.env` in project root — loaded by `lav/__init__.py` via `os.environ.setdefault`
- `lav/config.py` — reads all config from env vars at import time
- `lav/__init__.py` must be imported before `lav.config` (enforced by import order in server.py)
- Version lives in `pyproject.toml` only, read via `importlib.metadata` in `lav/__init__.__version__`

**Classification env vars** (all optional, in `.env`):
- `LAV_CLASSIFY_BACKEND` — `auto` (default), `openai`, `ollama`. Auto: openai when no BASE_URL, ollama otherwise.
- `LAV_CLASSIFY_MODEL` — model name (default: `gpt-4.1-mini`)
- `LAV_CLASSIFY_BASE_URL` — OpenAI-compatible endpoint for Ollama/vLLM/Azure (empty = OpenAI default)
- `LAV_CLASSIFY_SYSTEM_PROMPT` — custom prompt: inline text or file path (empty = built-in)
- `LAV_CLASSIFY_MAX_CHARS` — max chars of interaction text sent to the model (default: `12000`)
- `LAV_CLASSIFY_LANGUAGE` — language for summary/abstract/process output (default: `en`)

### Key conventions

- **Two-environment awareness — ALWAYS run `hostname` first** before anything that touches "prod" or a running server. Two machines: **`dev-host`** (`role: agent`, **no dashboard** — `:8764` serves only `/api/health|info|export`; to test UI, spin up a temp `lav-server` with role `both` on `:8765` via monkey-patched `lav.server._runtime_config`) and **`prod-host`** (`role: both`, full dashboard on `:8764`). `dev-host`/`prod-host` are placeholders — full infra, roles and deploy detail: [docs/infrastructure.md](docs/infrastructure.md); real host names, ssh targets and copy-paste runbook: `internal_docs/infra.md` (gitignored).
- **Development workflow** — mandatory even for one-line changes:
  1. Pick (or create) Jira ticket → transition to **In Progress**
  2. **Plan**: propose approach and ask user for approval before coding
  3. Develop → test e2e (manual — no test suite). For UI: use the temp `lav-server` on dev-host.
  4. Update docs: CLAUDE.md (if env/architecture changed) → README (if user-facing) → .env.example (if new env vars) → `docs/CHANGELOG.md` (**always** — entry under `## Unreleased` with `LAV-XX:` prefix)
  5. Ask user about commit → commit with ticket ref (e.g. `LAV-32: ...`). Multiple tickets in one commit is OK if the changes are coupled (e.g. `LAV-43, LAV-44: ...`)
  6. Push to `origin/main`
  7. **Deploy on prod-host** (see decision tree below)
  8. Add Jira comment per ticket: commit hash, test method (mention dev-host temp server if UI), deploy notes
  9. Transition to **Done** (only after deploy verified)
- **Deploy decision tree** — branch on what changed in the diff (full table with commands: [docs/infrastructure.md](docs/infrastructure.md#deploy-decision-tree)): static-only → `git pull` + browser refresh, no restart; `pyproject.toml` → also `pip install -e .`; any `lav/*.py` → also restart the server python process (`kill $(pgrep -f "python.*-m lav.server")`, KeepAlive restarts it — `pgrep -f lav-server` matches only the wrapper, use `python.*lav.server`); `lav/mcp_server.py` → also restart `lav-mcp` (drops live MCP clients); version bump → tag the release.
- **`internal_docs/`** is gitignored — private notes, not shipped
- **Jira project `LAV`** on aimaxplayground.atlassian.net tracks all TODO/backlog (epics + tasks). No local TODO files — use Jira as single source of truth
- **Sentinel values**: `parse_state` uses `project_id=-1` and `source=''` (never NULL)
- **Canonical hostname** (LAV-68): `socket.gethostname()` is volatile on macOS (transiently `Mac`/mojibake), so host identity comes from `_canonical_hostname()` in `jsonl.py` — precedence `LAV_HOSTNAME` env → `config.json` `"hostname"` key → validated socket name → `unknown`. **Set a stable `"hostname"` in each node's `config.json`** (dev machine → `dev-host`, prod machine → `prod-host`) or new host rows split one machine's sessions. Corrupted/generic names are rejected by `_is_valid_hostname()` and never inserted.
- **Synthetic subagent session ids**: Claude Code agent files (`subagents/**/agent-*.jsonl`) reuse the parent's `sessionId`; the parser rekeys them as `<parent_session_id>::agent-<agentId>` (LAV-66). A `session_id` containing `::agent-` is a subagent child conversation, linked via `parent_session_id`.
- **Per-project commits** in parsers for crash resilience
- **`conversation_id`** in `chatgpt.py` is OpenAI's external field name — not a bug, don't rename
- Migration code referencing old `conversations` table in `jsonl.py` and `qdrant/store.py` is intentional

### Production deployment

Machine layout (venv, LaunchAgents, wrapper scripts), roles and the deploy decision tree: [docs/infrastructure.md](docs/infrastructure.md). Real host names, ssh targets and copy-paste deploy commands: `internal_docs/infra.md` (gitignored).
