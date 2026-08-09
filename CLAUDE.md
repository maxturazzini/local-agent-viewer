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
lav-parse --since 2026-06-01  # bounded re-read (additive, insert-only; excl. --full)
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
lav backfill tool-outcomes    # stamp tool_call_id + outcome on historical tool rows
lav backfill tool-kind        # re-derive mcp_tool_calls.kind (--reclassify after a whitelist edit)

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

**Tool outcomes** (LAV-78): the 6 tool tables (`file_operations`, `bash_commands`, `search_operations`, `skill_invocations`, `subagent_invocations`, `mcp_tool_calls`) carry `tool_call_id`/`is_error`/`duration_ms`; `bash_commands`+`mcp_tool_calls` also `error_text` (cap 2000), `bash_commands` also `exit_code`. `lav/tool_outcomes.py` is the single source of truth for those columns + index DDL (`OUTCOME_COLUMNS`/`OUTCOME_INDEX_SQL`, iterated by BOTH the SCHEMA literal and the migration), the `tool_use`→row routing, and the writers. Stdlib + `lav.config` only — `jsonl.py` imports it, so it must never import `jsonl` at module level. Backfill: `lav backfill tool-outcomes` — local DB only (no reparse, no watermark), so run it on **every node**; the sync ingest never updates an existing row.

**Tool kind** (LAV-85): `mcp_tool_calls` is NOT "MCP calls" — it is the catch-all for tool calls with no dedicated table, so it also holds ChatGPT's and claude.ai's own **built-in** tools (38% of the rows on prod, all with `is_error` NULL by design: those exports carry no linkable `tool_result`). `kind TEXT DEFAULT ''` (`'mcp'` | `'builtin_host'`) + `idx_mcp_kind(kind, server_name)` separates them. Derived by `tool_outcomes.tool_kind(session_id, server_name, tool_name)`, which is **total** (never returns `''` — a `''` on a DB `init_db()` touched means the migration FAILED) and **conjunctive**: never classify by `server_name` alone. `server_name='claude_ai'` is a MIXED bucket (~1.100 real connector calls that lost their `integration_name`), and `claude_ai_Atlassian`/`claude_ai_ms365`/`claude_ai_Lovable` are REAL MCP servers seen from Claude Code — a `LIKE 'claude_ai%'` rule breaks in both directions. The whitelist `CLAUDE_AI_BUILTIN_TOOLS` is deliberately conservative: a tool that ALSO appears under an explicit server name is a connector tool (that is why `search_notes`/`read_notes` are OUT — they also show up under `mcp-obsidian`). Derived in-place by `_migrate_add_tool_kind` on every `init_db()`, so no per-node backfill is needed; `lav backfill tool-kind --reclassify` exists only to apply an EDITED whitelist to already-classified rows. Sync ingest RECOMPUTES it locally (same rule as `cmd_name`).

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
- `LAV_CLASSIFY_BACKEND` — `auto` (default), `openai`, `ollama`, `foundry` (opt-in only, never auto-selected). Auto: openai when no BASE_URL, ollama otherwise.
- `LAV_CLASSIFY_MODEL` — model name (default: `gpt-4.1-mini`; prod runs `deepseek-v4-flash` on foundry)
- `LAV_CLASSIFY_BASE_URL` — OpenAI-compatible endpoint for Ollama/vLLM (empty = OpenAI default; foundry uses `LAV_FOUNDRY_*` instead)
- `LAV_CLASSIFY_SYSTEM_PROMPT` — custom prompt: inline text or file path (empty = built-in)
- `LAV_CLASSIFY_MAX_CHARS` — max chars of interaction text sent to the model (default: `12000`)
- `LAV_CLASSIFY_LANGUAGE` — language for summary/abstract/process output (default: `en`)
- `LAV_SENSITIVITY_FLOOR` — `1` enables the deterministic minimum-sensitivity floor (regex + entity detectors; can only raise the model's guess)
- `LAV_FOUNDRY_ENDPOINT` / `LAV_FOUNDRY_KEY` / `LAV_FOUNDRY_API_VERSION` — Azure AI Foundry endpoint config (per-deployment overrides: suffix the uppercased deployment name)
- `LAV_FOUNDRY_TIMEOUT` / `LAV_FOUNDRY_MAX_RETRIES` — per-request deadline in seconds (default `40`) and SDK retries (default `3`)
- **No auto-classification in the code**: the parse-time hook (`jsonl.py`) is disabled and the post-sync path (`_auto_classify_new` in server.py) was removed entirely (LAV-73 — it used to classify on every agent pull AND sweep the whole unclassified backlog). Classification runs are always explicit via `lav-classify` (incremental by default; `--meta-since`/`--meta-model` for surgical reclassification of already-classified rows). Scheduled classification = the hourly `com.aimax.lav-classify` LaunchAgent (templates in `utils/services/`), which just runs `lav-classify` with a concurrency guard.

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
- **`is_error` semantics** (LAV-78): `NULL` = no `tool_result` ever seen (truncated transcript, or a parser that reads none — claude_ai/chatgpt/codex are all NULL), `0` = ok (a result exists and the `is_error` key is ABSENT — absence means success), `1` = error. NULL is never "assumed success": every rate query must exclude NULL from the DENOMINATOR. `duration_ms` is wall clock `tool_use`→`tool_result`, so it INCLUDES permission-prompt wait and background time (p99 157s, max 76min observed) — not a latency metric, and not clamped
- **`bash_commands` IS all Bash** (LAV-80 — the old "24,8% only, never call it the Bash error rate" caveat is RETIRED): the `is_file_related_bash` gate is gone, any non-empty command is stored (+47.316 rows, 3,05x). True Bash error rate **5,79%** (was 4,99% visible). `file_operations` unchanged — still fed only by file-related commands, proven byte-identical. New `cmd_name TEXT DEFAULT ''` + `idx_bash_cmd_name`: the program actually run (`cd /tmp && grep …`→`grep`, `sudo -u x systemctl`→`systemctl`, `FOO=1 python3`→`python3`), 303 buckets, `cd`=0, 99,86% usable (`''` = derived-undecidable, part of the 0,14% residual). **GROUP BY `cmd_name`, never the first word.** Gate removal only affects NEWLY parsed content — history needs `lav-parse --since <ISO>` **per node**
- **`--since` semantics** (LAV-80): temporarily LOWERS the read watermark, insert-only, all inserts duplicate-guarded → idempotent. Never lowers a persisted watermark (write-back is seeded from the STORED value, and `min()` means a `--since` newer than stored is ignored). Rejected together with `--full`. Recovered rows keep their OLD timestamps, so they sit behind the collector's export cursor and do NOT propagate over sync — run it on every node (`notify_collector` still fires; harmless, achieves nothing)
- **`is_error` NULL is a coverage CAP, not drift** (LAV-80): after a FULL `lav backfill tool-outcomes`, **26,1%** of claude_code `bash_commands` rows are still `is_error IS NULL` — 14.795 of them have NO `tool_use` block in `messages` at all (append-only rows from an earlier parser era, unrecoverable). It is SYMMETRIC between agent and collector (both derive from their own `messages`), so it is not agent/collector divergence. Separately: the scheduled self-heal in `lav-parser.sh` uses a BOUNDED lookback (`LAV_BACKFILL_LOOKBACK_HOURS`, default 72h) — an outage longer than the window leaves those rows NULL forever until someone runs the backfill once without `--since`
- **Outcome drift → backfill on every node** (LAV-78): `export_sessions()` filters CHILD rows by the tool call's OWN timestamp, so an outcome stamped in a later run sits behind the export cursor and never re-ships (and the collector's `NOT EXISTS` ingest would skip it anyway). The scheduled parser (`utils/services/lav-parser.sh`) therefore runs `lav backfill tool-outcomes` after each parse on BOTH nodes — the collector re-derives outcomes locally instead of receiving them
- **Canonical hostname** (LAV-68): `socket.gethostname()` is volatile on macOS (transiently `Mac`/mojibake), so host identity comes from `_canonical_hostname()` in `jsonl.py` — precedence `LAV_HOSTNAME` env → `config.json` `"hostname"` key → validated socket name → `unknown`. **Set a stable `"hostname"` in each node's `config.json`** (dev machine → `dev-host`, prod machine → `prod-host`) or new host rows split one machine's sessions. Corrupted/generic names are rejected by `_is_valid_hostname()` and never inserted.
- **Synthetic subagent session ids**: Claude Code agent files (`subagents/**/agent-*.jsonl`) reuse the parent's `sessionId`; the parser rekeys them as `<parent_session_id>::agent-<agentId>` (LAV-66). A `session_id` containing `::agent-` is a subagent child conversation, linked via `parent_session_id`.
- **Per-project commits** in parsers for crash resilience
- **`conversation_id`** in `chatgpt.py` is OpenAI's external field name — not a bug, don't rename
- Migration code referencing old `conversations` table in `jsonl.py` and `qdrant/store.py` is intentional

### Production deployment

Machine layout (venv, LaunchAgents, wrapper scripts), roles and the deploy decision tree: [docs/infrastructure.md](docs/infrastructure.md). Real host names, ssh targets and copy-paste deploy commands: `internal_docs/infra.md` (gitignored).
