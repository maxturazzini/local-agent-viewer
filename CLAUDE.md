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
```

Server at http://localhost:8764 — dashboard.html, interactions.html, tags.html.

**No test suite exists.** Manual testing via the running server and CLI commands.

## Architecture

### Three-layer data pipeline

1. **Parse → SQLite** (`lav/parsers/`) — raw interactions, tokens, files, tools, costs
2. **Classify → `interaction_metadata`** (`lav/classifiers/`) — AI classification via gpt-4.1-mini (optional)
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

FastMCP server with 8 tools. Read tools use `LAV_READ_API_KEY` (optional). Write tools require `LAV_API_KEY`.

### Frontend (`lav/static/`)

Vanilla HTML/JS/CSS + Chart.js CDN. Three pages: dashboard (6 sub-tabs), interactions list, tags. Filters auto-disable when only one value exists.

### Environment & config

- `.env` in project root — loaded by `lav/__init__.py` via `os.environ.setdefault`
- `lav/config.py` — reads all config from env vars at import time
- `lav/__init__.py` must be imported before `lav.config` (enforced by import order in server.py)
- Version lives in `pyproject.toml` only, read via `importlib.metadata` in `lav/__init__.__version__`

### Key conventions

- **`internal_docs/`** is gitignored — private notes and TODO, not shipped
- **Sentinel values**: `parse_state` uses `project_id=-1` and `source=''` (never NULL)
- **Per-project commits** in parsers for crash resilience
- **`conversation_id`** in `chatgpt.py` is OpenAI's external field name — not a bug, don't rename
- Migration code referencing old `conversations` table in `jsonl.py` and `qdrant/store.py` is intentional

### Production deployment (this machine)

- venv: `~/.local/lav-venv/`
- LaunchAgents: `com.aimax.lav-server` (KeepAlive), `com.aimax.lav-parser` (every 15 min)
- Wrapper scripts: `~/.local/bin/lav-server.sh`, `~/.local/bin/lav-parser.sh`
- To deploy changes: `~/.local/lav-venv/bin/pip install -e .` then `kill $(pgrep -f lav-server)` (KeepAlive restarts it)
