# LocalAgentViewer

Analytics tool for monitoring AI coding agents (Claude Code, Codex CLI, Claude Desktop, ChatGPT) across multiple users, hosts, and projects.

## Quick Start

```bash
cd local-agent-viewer
pip install -e .

# Parse conversations from this host (incremental)
lav-parse
# or: python3 -m lav.parsers.jsonl

# Parse only one project
lav-parse --project myProject

# Force full reparse
lav-parse --full

# Parse ChatGPT export (conversations.json)
lav-parse-chatgpt
# or: python3 -m lav.parsers.chatgpt

# Start server
lav-server
# or: python3 -m lav.server

# Classify conversations (requires OPENAI_API_KEY in .env)
lav-classify

# Index into Qdrant KB
lav-index

# MCP server (requires fastmcp)
lav-mcp

# Migrate from claude-parser (one-time)
python3 scripts/migrate.py
```

**Server**: http://localhost:8764
- Dashboard: http://localhost:8764/dashboard.html
- Interactions: http://localhost:8764/interactions.html
- Tags: http://localhost:8764/tags.html

## Structure

```
local-agent-viewer/
├── lav/                       # Main package
│   ├── __init__.py            # PROJECT_ROOT + .env loading
│   ├── config.py              # Configuration (paths, ports, Qdrant)
│   ├── queries.py             # SQL queries with 4D filters + metadata
│   ├── server.py              # ThreadingHTTPServer + REST API + auto-classification
│   ├── mcp_server.py          # FastMCP server for AI tool integration
│   ├── parsers/
│   │   ├── jsonl.py           # JSONL parser → unified DB (Claude Code, Codex, Cowork)
│   │   └── chatgpt.py         # ChatGPT export parser (conversations.json) → DB
│   ├── classifiers/
│   │   ├── openai_classifier.py  # OpenAI Structured Outputs classifier
│   │   └── sql_classifier.py    # Batch CLI for classification (gpt-4.1-mini)
│   └── qdrant/
│       ├── store.py           # Qdrant vector store client
│       ├── indexer.py         # Conversation indexer with auto-tagging
│       └── kb_indexer.py      # CLI indexer (reuses SQL metadata if available)
├── static/                    # Frontend
│   ├── dashboard.html         # Analytics dashboard (Chart.js)
│   ├── interactions.html      # Conversation list with classification badges
│   └── tags.html              # Tag cloud + classification stats
├── scripts/
│   └── migrate.py             # Migration from claude-parser (~40 DBs → 1)
├── pyproject.toml             # Package config + entry points
├── docs/
│   └── CHANGELOG.md
└── data/                      # (legacy, DB moved outside project)
```

## Architecture

### DB

Single `local_agent_viewer.db` with **4 independent dimensions**:

| Dimension | Ref table | Filter |
|-----------|-----------|--------|
| **Project** | `projects` | Which codebase |
| **User** | `users` | Which person |
| **Host** | `hosts` | Which machine |
| **Source** | `session_sources` | Which tool (claude_code/codex_cli/cowork_desktop/chatgpt) |

Composite PK conversations: `(session_id, project_id)`
Composite PK parse_state: `(key, project_id, source)`

**DB path**: `~/.local/share/local-agent-viewer/local_agent_viewer.db` (per-machine, outside project dir). Legacy fallback: `data/local_agent_viewer.db`.

### Agent/Collector Architecture

Distributed architecture for cross-machine analytics. Code is shared (git). **Runtime configurations** are per-machine.

#### Roles

| Role | Bind | Function |
|------|------|----------|
| **agent** | `0.0.0.0:8764` | Thin server: local parse, exposes `/api/export`, `/api/health`, `/api/info` |
| **collector** | `localhost:8764` | Pulls from remote agents, no local parse |
| **both** (default) | `localhost:8764` | Agent + collector: pull + local parse + dashboard + MCP |

#### Data flow (push-triggered pull)

```
laptop lav-parser (every 15 min)
  → parse ~/.claude/projects (local JSONL)
  → local DB (~/.local/share/local-agent-viewer/)
  → notify_collector() in lav/parsers/jsonl.py
      → POST http://server.local:8764/api/sync (scope=agent)
          → server pulls from laptop via /api/export
          → canonical DB updated
```

**IMPORTANT**: Pull happens ON DEMAND via HTTP trigger from the agent.
NOT periodic polling.

#### Per-machine config

Each machine has its own `~/.local/share/local-agent-viewer/config.json` (local, not synced):

```json
// server — collector + dashboard
{
  "role": "both",
  "port": 8764,
  "agents": [
    {"name": "laptop", "url": "http://laptop.local:8764", "fallback_url": "http://10.0.0.5:8764", "timeout_seconds": 10}
  ]
}

// laptop — agent (with collector_url for push-triggered pull)
{
  "role": "agent",
  "port": 8764,
  "collector_url": "http://server.local:8764"
}
```

#### What's local vs shared

| What | Path | Sync |
|------|------|------|
| Code | `local-agent-viewer/` | Shared (git) |
| Runtime config | `~/.local/share/local-agent-viewer/config.json` | **Local** per machine |
| SQLite database | `~/.local/share/local-agent-viewer/local_agent_viewer.db` | **Local** per machine |
| Qdrant data | `~/.local/share/local-agent-viewer/qdrant_data/` | **Local** per machine |

### Server

- **Port**: 8764
- **ThreadingHTTPServer** for concurrency
- Read-only connections for queries (`PRAGMA query_only=ON`)
- WAL mode + busy_timeout 5000ms
- Granular sync in background thread
- **Role gating**: in agent mode, only thin endpoints (health/info/export)

### API Endpoints

| Endpoint | Method | Agent | Both/Collector | Description |
|----------|--------|-------|----------------|-------------|
| `/api/health` | GET | yes | yes | Status, hostname, role, uptime |
| `/api/info` | GET | yes | yes | Detailed info (sources, sessions, DB size) |
| `/api/export` | GET | yes | yes | Telemetry package for pull (`?since=T&limit=N`) |
| `/api/projects` | GET | -- | yes | Project list |
| `/api/users` | GET | -- | yes | User list with stats |
| `/api/user/{username}` | GET | -- | yes | User detail |
| `/api/hosts` | GET | -- | yes | Host list |
| `/api/data` | GET | -- | yes | Analytics with 4D filters |
| `/api/conversations` | GET | -- | yes | Conversation list |
| `/api/conversation/{id}` | GET | -- | yes | Conversation detail |
| `/api/search` | GET | -- | yes | Full-text search |
| `/api/sync` | POST | -- | yes | Granular sync (includes pull from agents) |
| `/api/sync/status` | GET | -- | yes | Sync status |
| `/api/retention/status` | GET | -- | yes | JSONL stats |
| `/api/kb/*` | GET/POST | -- | yes | Qdrant knowledge base |

Query filters: `?project=myProject&user=john&host=laptop&client=claude_code&start=2026-01-01&end=2026-02-11`

## Frontend

- **Supported sources**: `claude_code`, `codex_cli`, `cowork_desktop`, `chatgpt`
- **Vanilla HTML/JS/CSS** + Chart.js CDN
- Filters **auto-disable** (grayed out if only one value, never hidden)
- Dashboard sub-tabs: Overview, Tokens, Files, Tools, Timeline, Users
- Users tab with full drill-down (7 views)
- Sync panel in toolbar

## Qdrant (Semantic Knowledge Base)

**Architecture**: Qdrant HTTP server runs on your always-on machine (port 6333). All clients connect via HTTP.

**Configuration**: Set `QDRANT_URL=http://your-server:6333` in the `.env` file.

**Indexer** (indexes conversations from canonical DB):
```bash
lav-index --dry-run   # preview
lav-index --limit 50  # test with 50
lav-index             # all
```

**Fallback file mode**: if `QDRANT_URL` is not set, uses local file in `~/.local/share/local-agent-viewer/qdrant_data/`.

## Classification & Indexing

Two complementary systems for enriching conversations with metadata:

### SQL Classification (table `conversation_metadata`)

Structured classification via **gpt-4.1-mini** (OpenAI Structured Outputs). Independent from Qdrant.

**Schema**: summary, abstract, process, classification, data_sensitivity, sensitive_data_types, topics, people, clients, tags.

**When it runs**:
1. **Automatic on sync** — After each pull/parse in `lav/server.py`, newly imported conversations are classified automatically (requires `OPENAI_API_KEY` in `.env`).
2. **Manual batch** — Via CLI for backlog or reclassification:
```bash
lav-classify                                  # incremental (unclassified only)
lav-classify --full --since 2026-01-01        # reclassify from date
lav-classify --limit 50                       # test on 50
lav-classify --dry-run                        # preview without writing
```

### Qdrant KB (semantic search)

Vector indexing for meaning-based search. Generates embeddings + payload.

**SQL integration**: `lav/qdrant/kb_indexer.py` checks if a conversation already has SQL metadata in `conversation_metadata`. If so, uses it as `pre_metadata` (skips Haiku call → cost savings). Otherwise, generates with Haiku.

### Classification API endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/classifications/stats` | Aggregations by classification and sensitivity |
| `GET /api/classifications/tagcloud` | Frequency of topics, people, clients, processes |
| `GET /api/conversation/{id}/metadata` | SQL metadata for a single conversation |
| `GET /api/search?classification=X&sensitivity=Y&topic=Z` | Classification filters in search |

## Technical Notes

- **Append-only**: DB records are never deleted
- **Per-project commits**: resilience to partial crashes during parsing
- **Sentinel values**: parse_state uses `project_id=-1` and `source=''` (never NULL)
- **Cross-platform**: detect_user/detect_host work on Mac, Linux, Windows
