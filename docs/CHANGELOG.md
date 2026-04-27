# Changelog

## Unreleased

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
