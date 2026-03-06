<p align="center">
  <h1 align="center">AI, MAX - Local Agent Viewer</h1>
  <p align="center">
    Analytics dashboard for AI coding agents across machines, users, and projects.
    <br />
    Track token usage, tool calls, file operations, and interaction history — all in one place.
  </p>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License" /></a>
  <img src="https://img.shields.io/badge/python-3.9+-3776AB.svg?logo=python&logoColor=white" alt="Python 3.9+" />
  <img src="https://img.shields.io/badge/dependencies-zero_(stdlib_only)-brightgreen.svg" alt="Zero dependencies" />
  <img src="https://img.shields.io/badge/database-SQLite-003B57.svg?logo=sqlite&logoColor=white" alt="SQLite" />
</p>

---

## Why?

AI coding agents generate a wealth of data — tokens consumed, files modified, tools invoked, interactions held — but it's scattered across JSONL files, buried in `~/.claude/` and `~/.codex/`, with no way to query or visualize it.

**LocalAgentViewer** parses all of it into a single SQLite database and serves a web dashboard. No cloud. No accounts. No dependencies. Just `lav-server`.

It supports **distributed setups** too: run an agent on each machine, and a central collector aggregates everything into one canonical view.

## Supported Agents

| Agent | Source Format | Auto-detected Location |
|-------|--------------|----------------------|
| **Claude Code** | JSONL | `~/.claude/projects/` |
| **Codex CLI** | JSONL | `~/.codex/sessions/` |
| **Claude Desktop** | JSONL | `~/Library/Application Support/Claude/local-agent-mode-sessions/` |
| **ChatGPT** | JSON export | Manual (`conversations.json` from data export) |

## Screenshots

<p align="center">
  <img src="docs/screenshots/lav1_redacted.png" width="80%" alt="Dashboard — overview with sessions, tokens, messages, and activity by project and model" />
</p>

<details>
<summary><strong>More screenshots</strong></summary>

<p align="center">
  <img src="docs/screenshots/lav4_redacted.png" width="80%" alt="Dashboard — files tab with sync panel and source filtering" />
</p>
<p align="center">
  <img src="docs/screenshots/lav2_redacted.png" width="80%" alt="Dashboard — subagent usage, MCP tool distribution" />
</p>
<p align="center">
  <img src="docs/screenshots/lav3_redacted.png" width="80%" alt="Interactions — list with classification badges, cost, and duration" />
</p>

</details>

## Installation

### 1. Clone and install

```bash
git clone https://github.com/maxturazzini/local-agent-viewer.git
cd local-agent-viewer
pip install -e .
```

This installs the core package (zero external dependencies — stdlib only). All CLI commands become available immediately.

### 2. (Optional) Install extras

```bash
pip install -e ".[classifiers]"   # AI classification (openai)
pip install -e ".[qdrant]"        # Semantic search (qdrant-client, openai, anthropic)
pip install -e ".[mcp]"           # MCP server (fastmcp)
pip install -e ".[all]"           # Everything
```

### 3. (Optional) Configure environment

Copy the example and fill in what you need:

```bash
cp .env.example .env
```

```env
# Only needed for optional features — core works without any of these
OPENAI_API_KEY=sk-...            # AI classification (lav-classify)
ANTHROPIC_API_KEY=sk-ant-...     # Qdrant KB embedding (lav-index)
QDRANT_URL=http://localhost:6333 # Qdrant server URL
CHATGPT_EXPORT_PATH=             # Path to ChatGPT conversations.json
```

## Quick Start

```bash
# Parse interactions from this machine
lav-parse

# Start the server
lav-server
```

Open **http://localhost:8764** — that's it.

The database is created automatically at `~/.local/share/local-agent-viewer/local_agent_viewer.db`. No configuration required for core functionality.

### CLI Commands

| Command | Description | Requires |
|---------|-------------|----------|
| `lav-parse` | Parse JSONL interactions (Claude Code, Codex, Desktop) | — |
| `lav-parse-chatgpt` | Parse ChatGPT export | `CHATGPT_EXPORT_PATH` |
| `lav-server` | Start the web server | — |
| `lav-classify` | Classify interactions via gpt-4.1-mini | `OPENAI_API_KEY` |
| `lav-index` | Index interactions into Qdrant | `QDRANT_URL` |
| `lav-mcp` | Start MCP server | `fastmcp` |

### Parser options

```bash
lav-parse                        # incremental (default, fast)
lav-parse --project myProject    # parse one project only
lav-parse --full                 # force full reparse

lav-parse-chatgpt               # parse ChatGPT export
lav-parse-chatgpt --full        # full reparse
```

## Data Pipeline

Three layers turn raw agent logs into a searchable, classified knowledge base:

```
JSONL / JSON logs
    │
    ▼
┌─────────────────────────────────────────────────┐
│  1. PARSE → SQLite                              │
│  Raw interactions: sessions, messages, tokens,  │
│  file ops, tool calls, costs, models            │
│  ─ lav-parse / lav-parse-chatgpt                │
└─────────────────┬───────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────┐
│  2. CLASSIFY → interaction_metadata (optional)  │
│  AI classification via gpt-4.1-mini:            │
│  summary, topics, people, clients, sensitivity, │
│  process type, tags                             │
│  ─ lav-classify (or auto after sync)            │
└─────────────────┬───────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────┐
│  3. INDEX → Qdrant vector DB (optional)         │
│  Semantic embeddings for meaning-based search.  │
│  Reuses SQL metadata when available (no extra   │
│  LLM call). Enables KB search in dashboard.     │
│  ─ lav-index                                    │
└─────────────────────────────────────────────────┘
```

Each layer is independent — the core works with just layer 1. Classification adds structured metadata. Qdrant adds semantic search on top.

## Features

### Analytics Dashboard
- **Overview** — sessions, messages, tokens, costs across time
- **Tokens** — input/output/cache breakdown by model and day
- **Files** — most-modified files, operations heatmap
- **Tools** — tool call frequency and distribution
- **Timeline** — activity patterns and session duration
- **Users** — per-user drill-down with 7 views
- **Knowledge Base** — semantic search across interactions

### 4D Filtering
Every query supports four independent dimensions:

| Dimension | What it filters |
|-----------|----------------|
| **Project** | Which codebase |
| **User** | Which person |
| **Host** | Which machine |
| **Source** | Which agent (claude_code, codex_cli, cowork_desktop, chatgpt) |

### Search
- **Full-text search** via SQLite FTS5 — fast, no external dependencies
- **Semantic search** via Qdrant vector DB (optional, layer 3)
- **Classification filters** — search by topic, sensitivity, process type (layer 2)

### AI Classification (optional)

```bash
# Requires OPENAI_API_KEY in .env
lav-classify              # classify unclassified interactions
lav-classify --full       # reclassify everything
lav-classify --dry-run    # preview
```

Also runs automatically after each sync when `OPENAI_API_KEY` is set.

### MCP Server
Expose your analytics to AI tools via the [Model Context Protocol](https://modelcontextprotocol.io). This lets Claude Code, Claude Desktop, or any MCP-compatible client query your interaction history, search the knowledge base, and trigger syncs — all through natural language.

```bash
# Requires: pip install fastmcp
lav-mcp
```

**Available tools:**

| Tool | Auth | Description |
|------|------|-------------|
| `get_interactions` | `LAV_READ_API_KEY` | List/search interactions (FTS, filters by project/user/date) |
| `get_interaction_details` | `LAV_READ_API_KEY` | Full transcript by session ID |
| `semantic_search` | `LAV_READ_API_KEY` | Qdrant vector search with classification/tag/project filters |
| `kb_status` | `LAV_READ_API_KEY` | Check if an interaction is indexed |
| `sync` | `LAV_API_KEY` | Trigger data re-parse (all, by project, or by source) |
| `kb_index` | `LAV_API_KEY` | Index an interaction into Qdrant (auto-tag or pre-metadata) |
| `kb_remove` | `LAV_API_KEY` | Remove an interaction from Qdrant |
| `kb_update_tags` | `LAV_API_KEY` | Update tags without re-embedding |

**Claude Code configuration** (`~/.claude/claude_code_config.json`):
```json
{
  "mcpServers": {
    "local-agent-viewer": {
      "command": "lav-mcp",
      "env": {
        "LAV_API_KEY": "your-write-api-key",
        "LAV_READ_API_KEY": "your-read-api-key"
      }
    }
  }
}
```

Write tools require `LAV_API_KEY`. Read tools require `LAV_READ_API_KEY` if set on the server — if not set, read access is open. Both keys are defined in `.env` and passed to MCP clients via config.

## Multi-Machine Setup

<details>
<summary><strong>Expand for distributed architecture details</strong></summary>

### Architecture

LocalAgentViewer supports a distributed agent/collector model. Each machine parses its own interactions locally. A central collector pulls from all agents into one unified database.

```
                  GET /api/export
┌──────────────┐◄──────────────────┌──────────────┐
│  Collector    │   (pull sessions) │  Agent       │
│  role: both   │                   │  role: agent │
│               │                   │              │
│  Dashboard    │                   │  Parse local │
│  Unified DB   │                   │  Local DB    │
│  All APIs     │                   │  Thin API    │
└──────────────┘                   └──────────────┘
```

### Roles

| Role | Bind | Function |
|------|------|----------|
| **agent** | `0.0.0.0:8764` | Parses local interactions, exposes `/api/export` |
| **both** (default) | `0.0.0.0:8764` | Full server: local parse + pull from agents + dashboard |

### Configuration

Each machine has a **local** config at `~/.local/share/local-agent-viewer/config.json` (not synced via git):

**Collector** (the machine with the dashboard) — see [`config.collector.example.json`](config.collector.example.json):
```json
{
  "role": "both",
  "port": 8764,
  "agents": [
    {
      "name": "laptop",
      "url": "http://laptop.local:8764",
      "fallback_url": "http://10.0.0.5:8764",
      "timeout_seconds": 10
    }
  ]
}
```

**Agent** (each remote machine) — see [`config.agent.example.json`](config.agent.example.json):
```json
{
  "role": "agent",
  "port": 8764,
  "collector_url": "http://collector.local:8764"
}
```

### Data flow

```
Agent machine (every 15 min via LaunchAgent)
  → lav-parse parses local ~/.claude/projects
  → notify_collector() → POST http://collector:8764/api/sync
  → Collector pulls from agent via GET /api/export
  → Canonical DB updated
```

Pull is **on-demand** (triggered by the agent after each parse), not periodic polling.

### Setup

**On the agent:**
```bash
mkdir -p ~/.local/share/local-agent-viewer
cp config.agent.example.json ~/.local/share/local-agent-viewer/config.json
# Edit collector_url to point to your collector machine

lav-parse
lav-server  # or install as a service (see below)
curl http://localhost:8764/api/health
```

**On the collector:**
```bash
mkdir -p ~/.local/share/local-agent-viewer
cp config.collector.example.json ~/.local/share/local-agent-viewer/config.json
# Edit agents list with your remote machines

lav-parse
lav-server
curl -X POST http://localhost:8764/api/sync -H "Content-Type: application/json" -d '{"scope":"all"}'
```

### What lives where

| What | Path | Synced? |
|------|------|---------|
| Code | `local-agent-viewer/` | Yes (git) |
| Runtime config | `~/.local/share/local-agent-viewer/config.json` | No (per-machine) |
| Database | `~/.local/share/local-agent-viewer/local_agent_viewer.db` | No (per-machine) |
| Qdrant data | `~/.local/share/local-agent-viewer/qdrant_data/` | No (per-machine) |

</details>

## Running as a Service (macOS)

<details>
<summary><strong>Expand for LaunchAgent setup</strong></summary>

Run the server and parser automatically on login with auto-restart:

```bash
# Install services
bash utils/services/install.sh

# Activate
launchctl load ~/Library/LaunchAgents/com.aimax.lav-server.plist
launchctl load ~/Library/LaunchAgents/com.aimax.lav-parser.plist

# Verify
launchctl list | grep com.aimax.lav
curl http://localhost:8764/api/health
```

The parser LaunchAgent runs incremental parsing every 15 minutes.

**After code changes:**
```bash
bash utils/services/install.sh
launchctl unload ~/Library/LaunchAgents/com.aimax.lav-server.plist
launchctl load ~/Library/LaunchAgents/com.aimax.lav-server.plist
```

**Logs:** `~/.local/logs/lav-server.log` and `lav-server-err.log`

</details>

## API Reference

<details>
<summary><strong>Expand for full API documentation</strong></summary>

### Universal endpoints (all roles)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Status, hostname, role, uptime |
| `/api/info` | GET | Sources, session count, DB size |
| `/api/export?since=T&limit=N` | GET | Telemetry package for collector pull |

### Dashboard endpoints (role: both)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/data` | GET | Full analytics with 4D filters |
| `/api/projects` | GET | Project list with stats |
| `/api/users` | GET | User list with stats |
| `/api/user/{username}` | GET | User detail |
| `/api/hosts` | GET | Host list |
| `/api/interactions` | GET | Interaction list (paginated) |
| `/api/interaction/{id}` | GET | Full interaction transcript |
| `/api/search?q=term` | GET | Full-text search |
| `/api/sync` | POST | Trigger sync |
| `/api/sync/status` | GET | Sync progress |
| `/api/classifications/stats` | GET | Classification aggregations |
| `/api/classifications/tagcloud` | GET | Topic/people/client frequencies |
| `/api/interaction/{id}/metadata` | GET | Classification metadata |
| `/api/kb/*` | GET/POST | Qdrant knowledge base |

### Query filters

All dashboard endpoints accept:

```
?project=myProject&user=john&host=laptop&client=claude_code&start=2026-01-01&end=2026-03-01
```

</details>

## Database Schema

<details>
<summary><strong>Expand for schema details</strong></summary>

Single SQLite database with composite primary keys and 4 independent filter dimensions:

| Table | Key | Contents |
|-------|-----|----------|
| `interactions` | `(session_id, project_id)` | Sessions with timestamps, cost, model |
| `messages` | `(session_id, project_id, uuid)` | Individual messages |
| `token_usage` | `(timestamp, session_id, project_id)` | Per-request token counts |
| `file_operations` | `(timestamp, session_id, project_id, tool, file_path)` | File reads/writes |
| `bash_commands` | | Shell commands executed |
| `search_operations` | | Grep/glob operations |
| `skill_invocations` | | Skill usage |
| `subagent_invocations` | | Sub-agent calls |
| `mcp_tool_calls` | | MCP tool invocations |
| `interaction_metadata` | | AI classification results |
| `parse_state` | `(key, project_id, source, host_id)` | Incremental parse cursors |

Reference tables: `projects`, `users`, `hosts`, `session_sources`.

Anti-duplicate on pull: `INSERT OR IGNORE` on composite PKs ensures idempotent ingestion across machines.

</details>

## Project Structure

```
local-agent-viewer/
├── lav/                           # Main package
│   ├── __init__.py
│   ├── config.py                  # Paths, ports, runtime config
│   ├── queries.py                 # SQL queries with 4D filters
│   ├── server.py                  # HTTP server with role-based gating
│   ├── mcp_server.py              # FastMCP server for AI tool integration
│   ├── parsers/
│   │   ├── jsonl.py               # JSONL parser (Claude Code, Codex, Desktop)
│   │   └── chatgpt.py             # ChatGPT export parser
│   ├── classifiers/
│   │   ├── openai_classifier.py   # OpenAI Structured Outputs classifier
│   │   └── sql_classifier.py      # Batch CLI classifier (gpt-4.1-mini)
│   └── qdrant/
│       ├── store.py               # Qdrant vector store client
│       ├── indexer.py              # Interaction indexer
│       └── kb_indexer.py           # CLI indexer (reuses SQL metadata)
├── static/                        # Frontend
│   ├── dashboard.html             # Analytics dashboard (Chart.js)
│   ├── interactions.html          # Interaction browser
│   └── tags.html                  # Tag cloud + stats
├── scripts/
│   └── migrate.py                 # Migration from claude-parser
├── config.agent.example.json      # Example config for agent machines
├── config.collector.example.json  # Example config for collector machine
├── pyproject.toml                 # Package config + CLI entry points
├── utils/services/                # LaunchAgent plists + install script
└── docs/
    └── CHANGELOG.md
```

## Requirements

- **Python 3.9+** — core functionality uses stdlib only
- **Optional:** `openai` for AI classification
- **Optional:** `qdrant-client` for semantic search
- **Optional:** `fastmcp` for MCP server

## Contributing

Contributions are welcome! Please open an issue first to discuss what you'd like to change.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes (`git commit -am 'Add my feature'`)
4. Push to the branch (`git push origin feature/my-feature`)
5. Open a Pull Request

## License

[MIT](LICENSE) — Max Turazzini
