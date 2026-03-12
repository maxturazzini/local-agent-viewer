# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

LocalAgentViewer (LAV) — local long-term memory for AI agent interactions. Parses JSONL/JSON logs from Claude Code, Codex CLI, Claude Desktop, and ChatGPT into a single SQLite database with a web dashboard, AI classification, and optional vector search.

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
lav-server                    # start server on :8764

# Optional features
lav-classify                  # AI classification (needs OPENAI_API_KEY)
lav-index                     # Qdrant vector indexing
lav-mcp                       # MCP server (needs fastmcp)
lav-pricing list              # list model pricing
lav-pricing add --model X ... # add/update pricing entry
lav-pricing seed              # insert default pricing data
```

Server at http://localhost:8764 — dashboard.html, interactions.html, tags.html.

**No unit test suite.** Manual testing via the running server and CLI commands. Classification model evals in `tests/evals/` (`eval_classify.py`), reports in `tests/evals/results/`.

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
- **Source** (`session_sources`) — which agent (claude_code, codex_cli, cowork_desktop, chatgpt)

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

### MCP Server (`lav/mcp_server.py`)

FastMCP server with 9 tools (8 original + `manage_pricing`). Read tools use `LAV_READ_API_KEY` (optional). Write tools require `LAV_API_KEY`.

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

- **Development workflow**:
  1. Pick Jira ticket → transition to **In Progress**
  2. **Plan**: propose approach and ask user for approval before coding
  3. Develop → test e2e (manual — no test suite)
  4. Update docs: CLAUDE.md (if env/architecture changed) → README (if user-facing) → .env.example (if new env vars) → `docs/CHANGELOG.md` (always — add entry under current version)
  5. Ask user about commit → commit with ticket ref (e.g. `LAV-32: ...`)
  6. Add Jira comment: decisions made, key results, Claude Code session ID
  7. Transition to **Done** (only after all above)
- **`internal_docs/`** is gitignored — private notes, not shipped
- **Jira project `LAV`** on aimaxplayground.atlassian.net tracks all TODO/backlog (epics + tasks). No local TODO files — use Jira as single source of truth
- **Sentinel values**: `parse_state` uses `project_id=-1` and `source=''` (never NULL)
- **Per-project commits** in parsers for crash resilience
- **`conversation_id`** in `chatgpt.py` is OpenAI's external field name — not a bug, don't rename
- Migration code referencing old `conversations` table in `jsonl.py` and `qdrant/store.py` is intentional

### Production deployment (this machine)

- venv: `~/.local/lav-venv/`
- LaunchAgents: `com.aimax.lav-server` (KeepAlive), `com.aimax.lav-parser` (every 15 min)
- Wrapper scripts: `~/.local/bin/lav-server.sh`, `~/.local/bin/lav-parser.sh`
- To deploy changes: `~/.local/lav-venv/bin/pip install -e .` then `kill $(pgrep -f lav-server)` (KeepAlive restarts it)
