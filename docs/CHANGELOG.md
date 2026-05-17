# Changelog

## Unreleased

LAV-55: Day View — daily Gantt + honest worktime metrics.
- New dashboard tab **Day View** (after Cost Intelligence) with date picker, prev/next arrows and "today" shortcut. Three blocks: stat cards (Sessions, Projects, Messages, Active wall-clock, Assistant wall-clock with hover tooltips), a dedicated concurrency curve chart with Peak pill, and a per-session Gantt grouped by project with hover tooltips. Charts auto-resize on window resize (debounced).
- Two honest wall-clock metrics computed per day in `lav/queries.py:get_day_bundle()`:
  - `active_wallclock_sec`: union of per-session activity windows (consecutive messages with gap < 5 min count as continuous), deduplicated across parallel sessions.
  - `assistant_wallclock_sec`: union of (user → next assistant) intervals capped at 10 min per pair, deduplicated.
  These avoid the ~13× inflation of naïve span-sum (e.g. 12/05: span-sum 36h vs active wall-clock 4.5h).
- Reuses the LAV-46 river-session pattern (`MIN/MAX(messages.timestamp)`) so sessions straddling UTC midnight surface correctly on both days with the local slice on each.
- "Show subagents" checkbox (default off) hides sessions with `parent_session_id` from the Gantt; subagent rows render dashed when toggled on. `meta.subagent_sessions` and `meta.subagent_invocations` exposed in the bundle.
- New endpoint `GET /api/day?date=YYYY-MM-DD` with standard 4D filtering (`project`, `user`, `host`, `client`). Dashboard-only (role `both`); returns 404 for role `agent`, 400 for bad date.
- New CLI subcommand `lav day YYYY-MM-DD [--project ...] [--user ...] [--source ...] [--format json|table|brief]`. `--format brief` prints one line per session plus a worktime summary footer.
- Frontend (`lav/static/dashboard.html`): timezone-aware hour positioning via local date math (no hardcoded CEST), so sessions straddling local midnight render past the right edge with `+1` hour labels instead of folding back into the same day.
- Validated against the `agent_worktime.py` ground-truth tool on 2026-05-12: sessions 25 = 25, active wall-clock 16087s vs 16367s (−1.7%), assistant wall-clock 7454s vs 7636s (−2.4%). Drift is from LAV's `messages` table filtering tool_use/system records; the honest metrics still match the JSONL truth within ±2%.
- No schema changes, no new dependencies.

LAV-48: New `lav-backfill-from-snapshot` CLI for SQL-direct historical backfill.
- HTTP `/api/export` pull (LAV-46) is now correct but fragile over wide historical windows (6+ months: 5+ round-trips × 1000-session limit, each subject to the 180s `timeout_seconds` and Tailscale flakiness). This adds an alternative pipeline that bypasses HTTP entirely.
- Flow: `ssh agent 'sqlite3 db ".backup snapshot.db"'` → `scp` snapshot back → ATTACH on local DB → build (proj/user/host) ID translation maps via `get_or_create_*` helpers → INSERT OR IGNORE every table for sessions where `COALESCE(MAX(messages.timestamp), c.timestamp) > since` (same river-aware filter as LAV-46) → advance `parse_state.last_pull:<agent>` with `MAX(current, snapshot_max)` so historical runs never regress a live cursor.
- New module `lav/backfill.py` (374 lines) + console script `lav-backfill-from-snapshot` in `pyproject.toml`.
- `--ssh-host` is optional: if `--snapshot-path` is pre-staged (e.g. pushed from the agent side because reverse SSH isn't configured), the fetch step is skipped.
- Bench on minimacs, 6-month window (`--since 2025-11-14T00:00:00`) against a 3.3 GB snapshot: **14 seconds total**, +19610 messages imported across 4130 active sessions. Compare to the projected 15–30 min via HTTP `/api/export` over 5 sequential rounds.
- Usage: `lav-backfill-from-snapshot --agent macchia --ssh-host 100.79.52.27 --since 2025-11-14T00:00:00` — or with a pre-staged snapshot: `... --snapshot-path /tmp/snap.db --since ...` (no `--ssh-host` needed).

LAV-46: Collector pull no longer loses messages on long-running ("river") sessions.
- Root cause: `export_sessions` filtered by `interactions.timestamp`, which is the session-birth timestamp (first message) and never moves. A session started at 06:00 that keeps appending messages until 22:00 was exported exactly once — when its birth timestamp first crossed `since` — then dropped from every subsequent pull while still gaining messages on the agent. Repro for 2026-05-12: agent DB had 2566 messages, collector DB had only 1025 (40%).
- Fix in `lav/queries.py:export_sessions`: switch the WHERE/ORDER from `c.timestamp` to `COALESCE(MAX(messages.timestamp), c.timestamp)` per session, via a `LEFT JOIN (SELECT ... GROUP BY session_id, project_id)` subquery. Child tables (messages, token_usage, file_operations, bash_commands, search_operations, skill_invocations, subagent_invocations, mcp_tool_calls) are also filtered to `timestamp > since` so a re-exported river session ships only its new tail — no bandwidth waste and no duplicate ingestion on the collector.
- Fix in `lav/server.py:pull_from_agents`: the `last_pull` cursor now advances to `max(interaction.timestamp, message.timestamp)` across the imported payload, not `max(interaction.timestamp)` alone. Otherwise the cursor would freeze at session-birth values and the new agent-side filter would never advance.
- Defense-in-depth in `lav/parsers/jsonl.py:ingest_remote_sessions`: switched the three plain `INSERT`s (`bash_commands`, `search_operations`, `mcp_tool_calls`) to `INSERT OR IGNORE`. These tables don't have UNIQUE constraints today (no-op for now); paired with a follow-up ticket to dedup + add UNIQUE indexes, this becomes effective without further code change.
- Backfill: a regular pull on the collector picks up the gap from the current `last_pull` forward automatically. To recover the pre-fix historical gap (e.g. the missing ~50% of 2026-05-12 on minimacs), run `lav sync --scope agents` with `--full` once.

LAV-43: Interactions grid now shows the AI title instead of the raw first prompt.
- Title precedence in the grid cell is now `summary` (Claude Code AI/custom title or `smart_title()` fallback) → `meta_summary` (LAV classifier abstract) → `display` (raw first user message). Previously `display` won unconditionally, so cells showed `<ide_opened_file>...` incipits even when a curated title existed (visible in the modal).
- One-line frontend change in `lav/static/interactions.html`. No backend / API change — all three fields were already in the `/api/interactions` payload.

LAV-45: Formalize dev workflow + two-environment awareness + deploy decision tree in CLAUDE.md.
- Added explicit **Two-environment awareness** block (macChia=`agent`/dev, minimacs=`both`/prod) with the local-test pattern (`lav.server._runtime_config` monkey-patch on :8765) so future sessions don't default-assume `localhost = prod`.
- Made **Development workflow** mandatory for one-line changes too (Jira ticket + CHANGELOG entry + closing comment with commit hash & test method).
- Replaced the single-line "deploy" hint with a **decision tree** branching on what changed (`lav/static/**` only / `pyproject.toml` / `lav/*.py` / `lav/mcp_server.py`). Documents the gotcha that `pgrep -f lav-server` matches only the wrapper, not the python process.
- Docs-only change (`CLAUDE.md`). No code touched.

LAV-44: Resizable columns in the interactions grid with localStorage persistence.
- Drag the right edge of any header cell to resize. Width is clamped to 50px min and saved to `localStorage` under `lav-column-widths` on mouseup.
- `loadColumnWidths()` merges saved widths into the `COLUMNS` array before first render. The `summary` column stays `1fr` until explicitly resized, then becomes fixed px.
- Pure frontend change in `lav/static/interactions.html` (CSS + JS). Reset: `localStorage.removeItem('lav-column-widths')` + reload.

ChatGPT loader: wipe `mcp_tool_calls` rows for `chatgpt` source on `--full` re-runs.
- `mcp_tool_calls` has no UNIQUE constraint, so the existing `INSERT OR IGNORE` would silently duplicate tool calls on every full re-parse. Same fix applied to the new `claude_ai` loader. Aligns with the LAV-39 pattern of wiping prior rows on full reparse.

Anthropic claude.ai account export importer (on-demand batch).
- New parser `lav.parsers.claude_ai` and CLI `lav-parse-claude-ai`. Source: `claude_ai`, host: `cloud`, project: `claude_ai` (fixed v1).
- Reads the `data-<account-uuid>-...-batch-0000/` folder produced by Anthropic's "Export account data" flow. v1 ingests `conversations.json` only; `projects.json`, `memories.json` and `design_chats/` are deliberately out of scope.
- Renders all six `content[]` types into a single message body in chronological order: `text`, `thinking` (preserved with header), `tool_use` (input JSON, capped at 2000 chars), `tool_result` (flattened text, capped at 5000 chars), `voice_note`, while skipping `token_budget` (metadata-only). The flat `msg.text` field is not authoritative (differs in ~26% of messages) and is used only as a fallback for older exports without `content[]`.
- Each `tool_use` block also produces a row in `mcp_tool_calls` (`server_name = integration_name` when present, otherwise `claude_ai`), so the dashboard tool charts pick them up automatically. `interactions.tools_used` lists the unique tool names per conversation.
- Attachments with `extracted_content` are appended to the message body (full text, no cap) so they're indexed by FTS5. Bare `files[]` entries (only `file_uuid + file_name`, no payload available in the export) are appended as a one-line reference list.
- Incremental cursor on `conv.updated_at` (parsed to `datetime`) stored in `parse_state` under key `claude_ai_last_update` with sentinel `(project_id=-1, source='claude_ai', host_id=<this host>)`. Re-runs of `--full` are idempotent thanks to deterministic message UUIDs (`claudeai:sha1(conv_uuid|msg_uuid)`) and `INSERT OR REPLACE`.
- Folder resolution: `--folder` flag → `settings.local.json: sources.claude_ai_export_path` → `CLAUDE_AI_EXPORT_PATH` env var → auto-discover the most recent `~/Downloads/data-*-batch-0000/` containing `conversations.json`.
- Schema unchanged. MCP server, CLI, classifier, Qdrant indexer and dashboard all work with the new source out of the box (filters auto-detect it).

LAV-40: Fix Claude Code title parsing for new `ai-title` and `custom-title` records.
- Recent Claude Code versions (~2.1.x+) no longer write `{"type":"summary"}` records. Titles now arrive as `{"type":"ai-title","aiTitle":"…"}` (LLM-generated) or `{"type":"custom-title","customTitle":"…"}` (user-pinned). LAV was ignoring both and falling back to `smart_title()` (truncated first prompt), so the dashboard/CLI/MCP showed raw prompt incipits instead of the curated titles visible in Claude Code's UI.
- Parser now recognises all three record types with priority `custom-title > ai-title > legacy summary > smart_title fallback`. Schema, sync/export, FTS, and other parsers untouched.
- New manual test: `python tests/test_title_parsing.py` covers the 4 priority cases against a tempdir + temp DB.
- After deploying the fix, run `lav-parse --full` to backfill existing sessions' titles.

LAV-41: Remote MCP server via streamable-http transport.
- `lav-mcp` accepts `LAV_MCP_TRANSPORT=streamable-http` (also `http`) to listen on a TCP port instead of stdio. New env vars: `LAV_MCP_HOST` (default `127.0.0.1`), `LAV_MCP_PORT` (default `8765`).
- Tool return signatures unchanged — FastMCP 3.1.0 serializes dict returns into `structured_content` for both stdio and streamable-http (verified end-to-end with smoke test). No payload wrapping needed.
- New `utils/services/`: cross-platform LaunchAgent (`com.aimax.lav-mcp.plist`) + systemd user unit (`lav-mcp.service`) + wrapper (`bin/lav-mcp.sh`) + `install.sh` that detects platform and substitutes `__HOME__`. Defaults to loopback for safety.
- New `docs/remote-mcp-server.md` reference (configuration, deployment, client setup, security, troubleshooting). README gains a "Remote MCP server" subsection under MCP Server.
- `.env.example` documents the new `LAV_MCP_TRANSPORT` / `LAV_MCP_HOST` / `LAV_MCP_PORT` env vars (commented-out, stdio remains the implicit default).
- Thread-safe lazy init of the Qdrant store via `threading.Lock` (double-checked locking). Pre-existing race in `_get_kb_store()` was harmless under stdio (single-threaded) but caused cold-start failures when concurrent HTTP requests hit the un-initialized singleton.
- No code changes outside `lav/mcp_server.py`. CLI, HTTP server, and core modules untouched.

## 0.1.5 — 2026-04-23

LAV-39: Fix double-counting of Claude Code costs/tokens on multi-block turns.
- Claude Code writes one JSONL record per content block (`thinking`, `tool_use`, text) but all blocks of a single API turn share the same `message.id` and `usage`. The parser was summing usage per row, inflating `cost_usd` and `total_tokens` by ~20–35% on Opus sessions with thinking.
- `messages` and `token_usage` gain an `api_message_id` column; new partial `UNIQUE(session_id, project_id, api_message_id)` index on `token_usage` makes `INSERT OR IGNORE` idempotent per API turn.
- `process_message_content` credits `tokens_in/tokens_out` only to the first block of each `api_message_id` (checks DB for existing non-zero row) so `update_interaction`'s `SUM(tokens_in+tokens_out)` stops double-counting.
- `lav-parse --full` now wipes prior claude_code `messages`/`token_usage` rows for the project+host before re-parsing, so old duplicates with empty `api_message_id` are cleaned up on the authoritative reparse.
- `get_session_cost_profile` exposes `api_message_id` in each timeline entry.
- After deploying the fix, run `lav-parse --full` once to rebuild existing data cleanly.

## 0.1.4 — 2026-04-07

Cost Intelligence (Alpha) — work-pattern-based token cost analysis.
- New dashboard tab "Cost Intelligence" with work patterns (hour/day/complexity), task-type costs (if classified), efficiency metrics (cache trend, cost-per-call, model/source comparison), and auto-generated key insights
- New "Cost" tab in interaction detail modal with summary cards, cumulative cost timeline chart, and message-level detail table with phase indicators
- 4 new query functions in `queries.py`: `get_session_cost_profile`, `get_work_pattern_stats`, `get_task_type_costs`, `get_efficiency_metrics`, plus `generate_insights` heuristic engine
- New `GET /api/cost-intelligence` endpoint with standard 4D filtering
- Enriched `GET /api/interaction/<id>` with `cost_profile` field
- No new tables, no new dependencies, no schema changes

## 0.1.3 — 2026-04-05

Unified `lav` CLI for query and KB management.
- New `lav/cli.py` with argparse — zero extra dependencies
- Subcommands: `search`, `show`, `kb search|status|index|remove|tags`, `sync`, `pricing list|add`
- Entry point `lav` registered in pyproject.toml
- Output formats: `--format json` (default, for piping/Claude Code), `table`, `brief`
- Auth: reads `LAV_API_KEY` / `LAV_READ_API_KEY` from env (same as MCP server)
- Reuses existing `queries.py`, `pricing.py`, `qdrant/store.py`, `qdrant/indexer.py`, `server.sync_data()`

## 0.1.2 — 2026-03-12

Separate OpenAI and Ollama classification backends + new categories.
- Split `openai_classifier.py` into shared module + dispatcher, `openai_strict.py` (OpenAI json_schema), `ollama_compat.py` (example-based prompting)
- New env var `LAV_CLASSIFY_BACKEND` (auto/openai/ollama) — auto preserves existing behavior
- New classification categories: `marketing` (sales, campaigns, outreach) and `operations` (admin, finance, HR, non-code workflows)
- Fix: Ollama path now sends system prompt and language instruction (was missing)
- Fix: Ollama path always uses `max_tokens` (local endpoints don't support `max_completion_tokens`)
- Fix: OpenAI path can now be used even when `CLASSIFY_BASE_URL` is set (via explicit `CLASSIFY_BACKEND=openai`)
- Recommended model: `gpt-4.1-mini` — best quality/cost ratio for classification (gpt-5-nano returns incomplete JSON on longer interactions)

## 0.1.1 — 2026-03-07

Classification prompt & config optimization (LAV-32).
- Fix OpenAI model cache_read pricing: use 50% of input (was incorrectly 10%, Anthropic formula)

- Reorder JSON schema fields: descriptive first, classification last (implicit chain-of-thought)
- Rewrite system prompt with disambiguation rules and examples, optimized for small models
- Filter tool_result noise from classification input
- Centralize CLASSIFICATIONS and SENSITIVITIES enums in config.py
- New env vars: `LAV_CLASSIFY_MAX_CHARS` (default 12000), `LAV_CLASSIFY_LANGUAGE` (default en)
- Raise max_tokens from 500 to 2000
- Add classification eval framework (`tests/evals/`)

## 0.1.0 — 2026-03-06

Initial public release.

- Unified DB with 4 dimensions: project, user, host, source
- Parsers: Claude Code, Codex CLI, Cowork Desktop, ChatGPT
- Agent/collector architecture for cross-machine analytics
- REST API + vanilla HTML/JS frontend (dashboard, interactions, tags)
- Classification via OpenAI Structured Outputs (gpt-4.1-mini)
- Qdrant semantic knowledge base (optional)
- FastMCP server for AI tool integration
- pip-installable package with CLI entry points
