# Changelog

## Unreleased

LAV-63 followup: Safari performance — large transcripts no longer hang.
- **Symptom**: a ~1900-message conversation (`04a68597…`) took >1 minute to show anything in **Safari** (Chromium opened it in <1s). The restyle rendered the whole transcript at once (~19.9k DOM nodes, ~1958 inline SVG icons) with two Safari-hostile costs: **`-webkit-mask-image`** on every clamped result (385 of them — each forces an offscreen compositing buffer) and full paint of all off-screen turns.
- **Fix** (`lav/static/interactions.html`, CSS only): dropped the `mask-image` fade on `.result-text.clamp` (kept the `max-height`/`overflow` hard clip + the "Show full result" button); added `content-visibility: auto; contain-intrinsic-size: auto 120px` to `.turn` so the browser skips layout/paint of off-screen turns. Mount time on Chromium dropped ~470ms → ~67ms; Safari no longer hangs.

LAV-63: Transcript popup restyle — "Quiet Canvas" + container-agnostic component (dock-ready).
- **Problem**: the transcript modal was visually noisy (≥6 competing accents — two blues `#004BFF`/`#3498db`, purple thinking, green results, red errors, plus a badge/tag rainbow), forced a click-to-expand even for 1-line content (the encrypted-thinking placeholder was one click to reveal one muted line), rendered sub-turns flat (text + thinking + N `tool_use` dumped together, `tool_use` not bound to its result — and every `tool_result` was actually shown **twice**), and the renderer was fused to the modal chrome.
- **Restyle** (`lav/static/interactions.html`): near-monochrome "Quiet Canvas" — neutral surfaces + a single brand accent `#004BFF`, red `#DC2626` reserved for errors only. Encrypted/empty (incl. `redacted_thinking`) thinking shows one muted inline line, no control; readable thinking is inline (≤4 lines) or collapsible with a `+N lines` hint. Each `tool_use` is bound to its `tool_result` on one card (no more duplicate result); short content inline, long content collapsible with a 1-line preview + size hint. `.tx-body`/`.tx-cost` use the app grey `--light-gray` (white cards on grey, matching the rest of the app).
- **Component extraction (dock-ready)**: the transcript is now a container-agnostic `mountTranscript(host, data, opts) → {root, headEl, auxEl, bodyEl, destroy}` with no overlay/fetch/ESC logic inside it; the popup shell owns `openInteraction` (same 3 parallel fetches), injects the KB band + Transcript/Cost tabs into the component's `.tx-aux` slot, and provides `onClose`/`onOpenInteraction`/`copySessionId`. A future docked side-panel can reuse the same component (v1 ships popup only; no Popup/Docked toggle).
- **Parity + hardening**: `parseMessageContent` kept verbatim (string / JSON-string / array content); full SVG tool-icon set + `mcp__*`/unknown → wrench fallback; tool name now escaped (was an XSS gap); object results `JSON.stringify`'d (no `[object Object]`); orphan/unpaired `tool_result`s still rendered; empty-message fallback preserved; breadcrumb + derived-sessions navigate via injected callback (ids via `escapeAttr`); cost chip neutral (no green); `_costProfileChart` destroy-before-recreate kept.
- **Accessibility**: modal is `role="dialog"`/`aria-modal`, with a focus trap (excludes inactive `tabindex="-1"` tabs), focus-return-to-trigger on close, a close affordance + managed focus in the loading and error states, ARIA tabs, and focusable tabpanels.
- **Deferred to LAV-64**: the KB band + Cost tab palette stay colourful for now (shared `.badge-classification`/`.kb-sensitivity` are reused by the list table).
- **Verified** live (temp `role=both` server) on a 2083-message real transcript: 1364 turns / 719 tool cards / 327 encrypted-thinking lines / 16 error results render with **zero frontend JS errors**; expand, Cost tab (chart + table), tab switch, and deep-link `?session=` auto-open all work. Static-only change → deploy is `git pull` + hard refresh (no restart).

LAV-62: Cowork project inference — stop creating junk projects from output folders and filenames.
- **Problem**: Cowork has no project working directory — it runs in an ephemeral sandbox (`/sessions/...`) and writes results into generic folders (`outputs/`, `uploads/`) or names files directly. The old inference (`extract_project_name`, which returns the last path segment) therefore produced junk "projects" like `outputs` (29 conversations), `SKILL.md` (10), and bare filenames (`CLAUDE.md`, `todo.md`, `*.html`, `*.pdf`, `*.csv`). LAV-61's one-project-per-conversation merge made these junk names visible as distinct project rows.
- **Fix** (`lav/parsers/jsonl.py`): new `infer_cowork_project()` used by `parse_cowork_sessions`' `infer_project_from_event` (Cowork-only; `extract_project_name` is unchanged and still used by the Claude Code parser). It rejects sandbox/scratch/OS paths (`/sessions/...`, `local-agent-mode-sessions`, `/var/folders`, `/tmp`), skips generic segments (`outputs`, `uploads`, `mnt`, `cache`, `library`, `cloudstorage`, …) and filenames, and extracts a real project root — the segment after a marker (`/mnt/<project>`, `/Claude_Coworks/<project>`, `/Artifacts/<project>`) or the first meaningful dir under `/Users/<user>/`. No meaningful root → `None` → `cowork_default`.
- **Effect** (local reparse, 96 cowork conversations): junk projects (`outputs`, `SKILL.md`, filenames — ~50 conversations) → **0**; `miniMe` 9 → **25**; honest `cowork_default` 20 → 43; real projects (`demo_Finance_1`, `Zenato`, `AiMaxAcademy`, `CooPilot`, …) correctly attributed. `claude_code`/`codex` project inference untouched.
- **Empty projects hidden** (`lav/queries.py` `get_projects_list`): the `projects` table is append-only, so the old junk project rows survive a reparse with 0 interactions. Added `HAVING COUNT(DISTINCT c.session_id) > 0` so the projects list / filter dropdown only shows projects that actually have conversations — the orphaned junk names no longer appear (dropdown went from ~106 to 81 real projects).
- **Migration**: requires the same Cowork purge + `lav-parse --full --include-cowork` as LAV-61 (project attribution is materialized in `interactions.project_id`, so historical rows need a reparse). The empty-project rows can be left in place (now hidden by the query) or optionally `DELETE FROM projects WHERE id NOT IN (SELECT DISTINCT project_id FROM interactions)`.

LAV-61: Cowork conversations merge into one session; Claude Code subagents roll up under their master.
- **Problem**: a conversation's cost/tokens were split or under-reported. Two distinct causes, two distinct fixes:
  - **Cowork** logs ONE conversation as two session_ids inside a single `local_<uuid>/audit.jsonl`: a folder-uuid *shell* (only the human turns) and an inner *agent* session (the human turns echoed + all assistant work + tokens). The old parser kept them apart, so a conversation showed as a 0-token "prompt only" row plus a separate row holding the real work. They are the **same dialogue**, not a master/subagent pair.
  - **Claude Code** has *genuine* subagents (`agent-*.jsonl`, linked via `parent_session_id`), but their cost was never rolled up to the master, so master rows under-reported the true tree cost.
- **Parser — Cowork MERGE** (`lav/parsers/jsonl.py`, `parse_cowork_sessions`): one `audit.jsonl` = one conversation. The canonical id is the folder uuid (`local_<uuid>` → `cowork:<uuid>`; `local_ditto_<uuid>` strips only `local_`). When an inner agent session exists (`has_inner`), every event is relabeled to that master id and the shell's duplicate turns are dropped; the row is titled with the human's first prompt. Cowork rows therefore have `parent_session_id = NULL` (no slaves). Slaves of a Cowork conversation do **not** exist. Also: the whole conversation inherits one project (pre-scan), and the dead `full_reparse` flag was fixed so `lav-parse --full` re-emits historical events.
- **Queries** (`lav/queries.py`): `get_interactions_list` gains `grouped=True` (default). Grouped shows only masters (`parent_session_id IS NULL`, orphan-promotion so nothing disappears) and rolls up `cost_usd`/`total_tokens` over the master + all descendants (recursive), plus `derived_count`. This rollup is what surfaces **Claude Code** subagent trees under one master row. `grouped=False` = exact prior behavior (every session, per-session cost). New `get_interaction_children()` lists a master's direct children; `get_interaction_detail` attaches them as `children`. **Search stays flat**: `lav search` (CLI) and MCP search pass `grouped=False`.
- **Frontend** (`lav/static/interactions.html`) + **server** (`lav/server.py`): `/api/interactions` reads a `grouped` query param; the list has a **Grouped ⇄ Flat** toggle persisted in `localStorage` (`lav.listMode`). Master rows with real subagents (Claude Code) show a `↳ N agents` pill (expandable inline); the detail modal lists derived sessions (isolated `renderDerivedSessions`, reuses `.subagents` CSS).
- **⚠️ Migration (REQUIRED — purge before reparse)**: the Cowork MERGE **changes session_ids**, so a plain `lav-parse --full` over an existing DB would leave the OLD per-event Cowork rows as orphans (append-only `INSERT OR REPLACE` only overwrites the same id) → the list would show both the merged master AND stale split rows, and cost/token aggregates would **double-count** (old + new `token_usage`). The migration MUST purge Cowork rows first. On minimacs, in a transaction: `DELETE FROM <t> WHERE session_id LIKE 'cowork:%'` for every table with a `session_id` column (`interactions, messages, token_usage, file_operations, bash_commands, search_operations, skill_invocations, subagent_invocations, mcp_tool_calls, interaction_metadata, conversations, session_sources`) **and** `DELETE FROM parse_state WHERE source='cowork_desktop'`; then `lav-parse --full --include-cowork`. `messages_fts` stays consistent via existing triggers. Claude Code data is untouched by this purge.
- **Verified** on a unified test DB (real local DB: claude_code 4144 + cowork purged & re-merged): cowork 96 masters / **0 slaves**; a sample cowork conversation is one unified row with 273 assistant + 121 user messages and a human-prompt title; Claude Code master `3e8d411d` rolls up 40 descendants to `$24.28 / 17.7M tokens` matching an independent recursive closure; FLAT total (5789) == raw count, grouped (5723) < flat (no regression).
- **Reference**: see `docs/DATA_MODEL.md` for the full data-model explanation (two conversation models, grouped vs flat, what the queries return).

LAV-58: Add missing `claude-opus-4-8` (and `claude-opus-4-7`) model pricing.
- `claude-opus-4-8` (the current model) was absent from `model_pricing`, so every opus-4-8 interaction was costed at $0.0000 — the most expensive model in a session was silently ignored. On a /deep-research session (`fc00f15f…`) this halved the reported cost ($13.50 vs ~$26 real).
- Added both `claude-opus-4-8` and `claude-opus-4-7` to `DEFAULT_PRICING` in `lav/pricing.py` (`$5 in / $25 out`, cache write $6.25 = 1.25×, cache read $0.50 = 0.1× per Mtok — identical to opus-4-6). Permanent for fresh installs (seeded via `INSERT OR IGNORE` in `init_db`).
- Live DBs: insert via `lav pricing add --model claude-opus-4-8 --provider anthropic --input 5.0 --output 25.0 --cache-write 6.25 --cache-read 0.5 --from-date 2024-01-01`. Costs are computed at query time (LEFT JOIN, never materialized), so the row fixes **all** historical opus-4-8 data retroactively with no re-parse.

LAV-57: Cost Profile — token + USD cost totals block above Message-Level Detail.
- `get_session_cost_profile` (`lav/queries.py`) now returns a per-category cost breakdown in `summary.cost` (`input`/`output`/`cache_write`/`cache_read`/`total`) plus `summary.tokens.total`, computed from the same `token_usage` rows as the existing token totals (the 4 prices already live in `_COST_EXPR`). `cost.total` equals the existing `cost_usd` (consistency check).
- Frontend (`lav/static/interactions.html` `renderCostProfile`): new "Totals" card rendered above the message table — a 3-column table (Category | Tokens | Cost USD) with rows Input / Output / Cache In (write) / Cache Out (read) and a bold TOTAL. Reuses existing formatters; falls back to $0 for sessions without pricing. Makes the cost breakdown legible at a glance (e.g. cache *write* dominates, not the large cache *read*).

LAV-59: `interactions.total_tokens` is now cache-inclusive — one consistent token total everywhere.
- The materialized `interactions.total_tokens` column was cache-*excluded* (`SUM(tokens_in+tokens_out)` from `messages`), so the Interactions grid and the detail-popup header showed numbers ~50× smaller than the Cost Profile total for cached sessions (e.g. `fc00f15f`: 339,617 vs 19,356,374). Two different numbers for the same session.
- `lav/parsers/jsonl.py` `update_interaction()`: `total_tokens` is now computed from `token_usage` (`input+output+cache_creation+cache_read`, deduped by `api_message_id`, includes subagent rows under the same session_id → matches Cost Profile). Falls back to `messages.tokens_in+tokens_out` only for sources without `token_usage` (ChatGPT / claude.ai exports).
- Existing rows backfilled in place: `UPDATE interactions SET total_tokens = (SELECT SUM(...) FROM token_usage WHERE session/project match)` where `token_usage` exists. Grid, popup header and the user/host/project aggregates all read the now-correct column. **Semantic change (intentional, not silent):** historical token totals increase by including cache tokens.

LAV-60: Transcript — thinking blocks stay collapsed; encrypted signatures are never dumped.
- Opus 4.7/4.8 `thinking` blocks carry an empty `thinking` field (text omitted by default) plus an encrypted `signature`. The render branch was gated on `item.thinking` being truthy, so empty-thinking blocks fell through to the raw-JSON fallback and spilled the base64 signature into the transcript.
- `lav/static/interactions.html`: the branch now matches `thinking` / `redacted_thinking` regardless of text and always routes through `renderThinking()`, which shows a muted `(encrypted — no readable thinking content)` placeholder instead of the signature. Blocks remain collapsed by default, expandable on click.

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
