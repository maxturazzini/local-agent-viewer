# Changelog

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
