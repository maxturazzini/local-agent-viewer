# LocalAgentViewer

Analytics tool for monitoring AI coding agents (Claude Code, Codex CLI, Claude Desktop, ChatGPT) across multiple users, hosts, and projects.

Parses conversation JSONL files into a unified SQLite database, serves a web dashboard, and supports distributed multi-machine setups via an agent/collector architecture.

## Features

- **Unified database** with 4 independent dimensions: project, user, host, source
- **Web dashboard** with interactive charts (Chart.js), filters, and drill-down
- **Multi-machine** agent/collector pull architecture
- **Full-text search** across all conversations (SQLite FTS5)
- **Semantic search** via Qdrant vector knowledge base (optional)
- **MCP server** for integration with Claude Code and other AI tools
- **Incremental parsing** — only processes new data on each run

## Requirements

- Python 3.9+
- No pip dependencies for core functionality (stdlib only)
- Optional: `openai` + `qdrant-client` for semantic KB features
- Optional: `fastmcp` for MCP server

## Quick Start

### Single machine (standalone)

```bash
# Clone / copy the project
cd local-agent-viewer

# Parse conversations from this host
python3 parser.py

# Start the server
python3 server.py
```

Open http://localhost:8764 for the dashboard.

The database is created automatically at `~/.local/share/local-agent-viewer/local_agent_viewer.db`.

### What gets parsed

| Source | Location | Format |
|--------|----------|--------|
| Claude Code | `~/.claude/projects/` | JSONL |
| Codex CLI | `~/.codex/sessions/` | JSONL |
| Claude Desktop | `~/Library/Application Support/Claude/local-agent-mode-sessions/` | JSONL |
| ChatGPT | Exported `conversations.json` | JSON |

```bash
# Parse specific source
python3 parser.py --project myProject
python3 parser.py --full  # force full reparse

# Parse ChatGPT export
python3 parser_chatgpt.py
```

## Multi-Machine Setup

LocalAgentViewer supports a distributed agent/collector architecture for aggregating data from multiple machines into a single dashboard.

### Concepts

| Role | Bind address | Function |
|------|-------------|----------|
| **agent** | `0.0.0.0:8764` | Thin server: parses local conversations, exposes `/api/export` for pull |
| **both** (default) | `0.0.0.0:8764` | Full server: local parse + pull from agents + dashboard + API |

The **collector** (role `both`) periodically pulls data from **agents** via HTTP. Each machine has its own local SQLite database. The collector merges everything into its own DB.

### Architecture

```
┌─────────────┐         GET /api/export          ┌─────────────┐
│  Machine A  │◄─────────────────────────────────│  Machine B  │
│  role: both │         (pull sessions)           │ role: agent │
│             │                                   │             │
│  Dashboard  │                                   │  Parse only │
│  Unified DB │                                   │  Local DB   │
│  Parse local│                                   │  Thin API   │
└─────────────┘                                   └─────────────┘
```

### Configuration

Each machine has a **local config file** (not synced):

```
~/.local/share/local-agent-viewer/config.json
```

#### Collector machine (role: both)

```json
{
  "role": "both",
  "port": 8764,
  "agents": [
    {
      "name": "workstation",
      "url": "http://workstation.local:8764",
      "fallback_url": "http://10.0.0.5:8764",
      "timeout_seconds": 10
    }
  ]
}
```

#### Agent machine

```json
{
  "role": "agent",
  "port": 8764
}
```

### Setup steps

**On the agent machine:**

```bash
# 1. Create local config
mkdir -p ~/.local/share/local-agent-viewer
echo '{"role": "agent", "port": 8764}' > ~/.local/share/local-agent-viewer/config.json

# 2. Parse local conversations to seed the DB
python3 parser.py

# 3a. Quick test (foreground, dies when you close the terminal)
python3 server.py

# 3b. Persistent service via LaunchAgent (recommended)
bash utils/services/install.sh
launchctl load ~/Library/LaunchAgents/com.aimax.lav-server.plist

# 4. Verify
curl http://localhost:8764/api/health
# {"status":"ok","hostname":"...","role":"agent","uptime":...,"version":1}
```

**On the collector machine:**

```bash
# 1. Create local config with agent reference
mkdir -p ~/.local/share/local-agent-viewer
cat > ~/.local/share/local-agent-viewer/config.json << 'EOF'
{
  "role": "both",
  "port": 8764,
  "agents": [
    {"name": "workstation", "url": "http://workstation.local:8764", "timeout_seconds": 10}
  ]
}
EOF

# 2. Parse local conversations
python3 parser.py

# 3a. Quick test (foreground)
python3 server.py

# 3b. Persistent service via LaunchAgent (recommended)
bash utils/services/install.sh
launchctl load ~/Library/LaunchAgents/com.aimax.lav-server.plist

# 4. Trigger a sync (pulls from agents + parses local)
curl -X POST http://localhost:8764/api/sync -H "Content-Type: application/json" -d '{"scope":"all"}'

# 5. Verify cross-machine data
sqlite3 ~/.local/share/local-agent-viewer/local_agent_viewer.db \
  "SELECT h.hostname, COUNT(*) FROM conversations c JOIN hosts h ON h.id=c.host_id GROUP BY h.hostname"
```

### What lives where

| What | Path | Shared? |
|------|------|---------|
| Code (server.py, parser.py, etc.) | `local-agent-viewer/` | Yes (git/sync) |
| Runtime config | `~/.local/share/local-agent-viewer/config.json` | **No** (per-machine) |
| Database | `~/.local/share/local-agent-viewer/local_agent_viewer.db` | **No** (per-machine) |

## API Reference

### Agent endpoints (available in all roles)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Status, hostname, role, uptime, version |
| `/api/info` | GET | Sources, session count, last parse, DB size |
| `/api/export?since=T&limit=N` | GET | Telemetry package for pull (sessions + all child data) |

### Collector endpoints (role: both only)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/data` | GET | Full analytics with 4D filters |
| `/api/projects` | GET | Project list with stats |
| `/api/users` | GET | User list with stats |
| `/api/user/{username}` | GET | User detail |
| `/api/hosts` | GET | Host list with stats |
| `/api/conversations` | GET | Conversation list (paginated) |
| `/api/conversation/{id}` | GET | Full conversation transcript |
| `/api/search?q=term` | GET | Full-text search |
| `/api/sync` | POST | Trigger sync (pull + local parse) |
| `/api/sync/status` | GET | Sync progress |
| `/api/kb/*` | GET/POST | Qdrant knowledge base |

### Filters

All collector endpoints support query string filters:

```
?project=myProject&user=john&host=workstation&client=claude_code&start=2026-01-01&end=2026-03-01
```

## macOS LaunchAgent (recommended)

Run the server automatically on login with auto-restart (`KeepAlive`).

`install.sh` copies all necessary files (Python, HTML, qdrant/) from OneDrive to `~/.local/lav/` (local, outside OneDrive — required by macOS privacy restrictions on CloudStorage), and installs the LaunchAgent plists.

```bash
# Install all services (copies code to ~/.local/lav/, scripts to ~/.local/bin/, plists to LaunchAgents)
bash utils/services/install.sh

# Activate LAV server
launchctl load ~/Library/LaunchAgents/com.aimax.lav-server.plist

# Activate LAV parser (incremental parse every 15 min)
launchctl load ~/Library/LaunchAgents/com.aimax.lav-parser.plist

# Verify
launchctl list | grep com.aimax.lav
curl http://localhost:8764/api/health
```

**After code changes**: re-run `bash utils/services/install.sh` to update `~/.local/lav/`, then reload:
```bash
launchctl unload ~/Library/LaunchAgents/com.aimax.lav-server.plist
launchctl load ~/Library/LaunchAgents/com.aimax.lav-server.plist
```

**Logs**: `~/.local/logs/lav-server.log` and `lav-server-err.log`

## Project Structure

```
local-agent-viewer/
├── server.py          # HTTP server with role-based gating
├── parser.py          # JSONL parser (Claude Code, Codex, Desktop)
├── parser_chatgpt.py  # ChatGPT conversations.json parser
├── config.py          # Configuration (paths, ports, runtime config)
├── queries.py         # SQL queries with 4D filters + export
├── mcp_server.py      # FastMCP server for AI tool integration
├── dashboard.html     # Analytics dashboard (vanilla JS + Chart.js)
├── interactions.html  # Conversation browser
├── qdrant/            # Optional vector knowledge base
│   ├── store.py
│   └── indexer.py
├── utils/services/    # LaunchAgent plists and wrapper scripts
└── docs/              # Architecture plans
```

## Database Schema

The unified SQLite database uses composite primary keys and 4 independent dimensions:

- **conversations** — PK: `(session_id, project_id)`
- **messages** — UNIQUE: `(session_id, project_id, uuid)`
- **token_usage** — UNIQUE: `(timestamp, session_id, project_id)`
- **file_operations** — UNIQUE: `(timestamp, session_id, project_id, tool, file_path)`
- **bash_commands**, **search_operations**, **skill_invocations**, **subagent_invocations**, **mcp_tool_calls**
- **parse_state** — PK: `(key, project_id, source, host_id)` — tracks incremental cursors

Anti-duplicate on pull: `INSERT OR IGNORE` on composite PKs ensures idempotent ingestion.

## License

MIT
