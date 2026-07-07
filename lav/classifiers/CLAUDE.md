# lav/classifiers/

AI classification layer: turns raw interaction messages into structured `interaction_metadata`
(summary, abstract, process, topics, people, clients, classification, data_sensitivity,
sensitive_data_types). See root [CLAUDE.md](../../CLAUDE.md) for the pipeline overview and
`LAV_CLASSIFY_*` env vars.

## Files

- **`openai_classifier.py`** — shared core, not a backend itself. Owns:
  - `CLASSIFICATION_SCHEMA` (the 9-field JSON schema, enums sourced from `config.CLASSIFICATIONS`/`config.SENSITIVITIES`)
  - `prepare_messages_for_classification()` — builds classifier input text from raw messages; strips base64 blobs and injected system-wrapper tags; default mode keeps user intent + assistant first line only, `LAV_CLASSIFY_RICH=1` includes full assistant text + tool names + truncated tool results
  - `_sanitize_result()` / `_parse_json_response()` — defensive parsing shared by every backend
  - `sensitivity_floor()` / `apply_sensitivity_floor()` — deterministic MINIMUM sensitivity from regex detectors (credentials, IBAN, health terms) + extracted entities, opt-in via `LAV_SENSITIVITY_FLOOR=1`; can only raise sensitivity, never lower the model's guess
  - `classify_interaction()` — the dispatcher. Routes to a backend module based on `_resolve_backend()`.
- **`openai_strict.py`** — backend `openai`. OpenAI native `response_format=json_schema` strict mode. Renders its system prompt from `lav/taxonomy.py`.
- **`ollama_compat.py`** — backend `ollama`. Example-based prompting (no json_schema support on Ollama/vLLM), embeds a sample JSON in the user message, strips `<think>` tags.
- **`sql_classifier.py`** — the `lav-classify` CLI entrypoint. SQLite candidate selection (incremental vs `--full`), calls `classify_interaction()`, upserts into `interaction_metadata`.
- **`foundry/`** — backend `foundry` (LAV-72, A/B test vs. `openai_strict`). Isolated on purpose so cloud-model quirks don't touch the working paths.
  - `client.py` — builds a plain `openai.OpenAI` client pointed at an Azure AI Foundry endpoint (Azure OpenAI or serverless MaaS, both speak chat-completions). Config via `LAV_FOUNDRY_ENDPOINT`/`_KEY`/`_API_VERSION`, with per-deployment overrides suffixed by the uppercased deployment name (e.g. `LAV_FOUNDRY_ENDPOINT_GPT_OSS_120B`).
  - `classify.py` — reuses `openai_strict.SYSTEM_PROMPT` and `CLASSIFICATION_SCHEMA` verbatim (task definition never drifts between backends). Handles Foundry-specific call quirks: `max_completion_tokens` with generous headroom (reasoning models burn hidden tokens before the JSON), optional `reasoning_effort` (`LAV_FOUNDRY_REASONING_EFFORT`), and a `json_schema` → `json_object` fallback for endpoints without strict structured-output support. Records per-call token usage in module-level `USAGE` (ground truth from the API response, read by the eval's cost/task table).

## Backend resolution

`LAV_CLASSIFY_BACKEND` = `auto` (default) | `openai` | `ollama` | `foundry`.
`auto` picks `ollama` if `LAV_CLASSIFY_BASE_URL` is set, else `openai`. `foundry` is never
auto-selected — it's opt-in only, set it explicitly.

## Taxonomy

Classification categories, sensitivity levels, field descriptions, and prompt fragments are
centralized in `lav/taxonomy.py` (LAV-70) — not duplicated per backend. `lav/taxonomy.example.json`
is the example taxonomy shipped in the repo; real taxonomies are per-deployment config.

## Evals

`tests/evals/eval_classify.py` is the multi-model comparison harness — gold-set based, reports
accuracy/macro-F1 per field per model, results under `tests/evals/results/`. When the `foundry`
backend was exercised in a run, it also prints a token/cost-per-task table sourced from
`foundry.classify.USAGE`, projected onto a full ~17k-interaction batch. Per-model EUR/MTok pricing
is a hardcoded dict in the eval (`PRICE`), not in `lav/pricing.py` — update it there when Foundry
pricing changes or a new deployment is added.

Known conclusion from the LAV-70 eval round: the gold set was the bottleneck, not the model —
Haiku beat gemma once the gold set itself was fixed.
