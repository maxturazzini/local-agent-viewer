# Matrice — `gen_ai.*` × schema LAV × 5 sorgenti

Legenda celle sorgente:
- ✅ **nel file e letto da LAV** (con destinazione DB)
- 🟡 **nel file ma LAV cieco** — debito di parsing, recuperabile
- ⚪ **assente nel file** — limite del provider, non recuperabile da questi log
- ◐ parziale (dettaglio in nota) · *(pl)* = assenza plausible, non confermata full-corpus

Colonna **LAV DB**: colonna/tabella che oggi ospita il dato (— = nessuna).
Livelli OTel: **Req** required · **CondReq** conditionally required · **Rec** recommended · **OptIn** opt-in.
Spec: semantic-conventions-genai @ main, 2026-07-24 (tutte Development salvo `error.type`, `server.*` stable).

## 1. Identità e contesto

| Attributo OTel (livello) | LAV DB | claude_code | codex | cowork | chatgpt | claude_ai |
|---|---|---|---|---|---|---|
| `gen_ai.conversation.id` (CondReq) | `session_id` | ✅ `sessionId` | ✅ `payload.id` | ✅ `session_id` | ✅ `conversation_id` | ✅ `uuid` |
| `gen_ai.provider.name` (Req) | — (solo `model_pricing.provider`) | ⚪ implicito (anthropic) | 🟡 `model_provider`='openai' | ⚪ implicito | ⚪ implicito | ⚪ implicito |
| `gen_ai.operation.name` (Req) | — (implicito nel routing tabellare) | 🟡 derivabile da `type` record | 🟡 derivabile da `type`/`payload.type` | 🟡 derivabile da `type`+`subtype` | 🟡 derivabile da role/recipient | 🟡 derivabile da content type |
| `gen_ai.request.model` (CondReq) | `interactions.model`/`messages.model` (unica colonna) | ◐ solo response; requested solo in `model_refusal_fallback.originalModel` (raro) | ✅ `turn_context.payload.model` | ✅ `system.init.model` → meta_json | ✅ `default_model_slug` + 🟡 `requested_model_slug` (routing) | ⚪ **nessun modello nell'export** |
| `gen_ai.response.model` (Rec) | idem (non distinta) | ✅ `message.model` | ⚪ nessun echo response *(pl)* | ✅ `message.model` | ✅ `metadata.model_slug` per-messaggio | ⚪ |
| `gen_ai.response.id` (Rec) | `api_message_id` (solo dedup) | ✅ `message.id` + 🟡 `requestId` | ⚪ solo id item-level (msg_/fc_) | ✅ `message.id` + 🟡 `request_id` (933) | 🟡 `metadata.request_id` (104k, Cloudflare Ray + colo edge) | ⚪ solo UUID export |
| `gen_ai.agent.id/name` (CondReq) | `agent_id`, `parent_session_id` | ✅ `agentId` + 🟡 `agentName`, `attribution*` | 🟡 `agent_nickname/path`, `parent_thread_id`, `sub_agent_activity` | 🟡 `parent_tool_use_id`, `tool_use_result.agentId/agentType` | ◐ `gizmo_id` ✅ (→project) + 🟡 agent mode markers | ⚪ |
| `service.name` (resource) | `projects.name` (inferito) | ✅ da `cwd` | ✅ da `cwd` | ✅ inferito dai tool | ✅ da `gizmo_*` | ◐ projects.json non linkato alle conv |
| user/account/org id | `users` (da path home) | ⚪ (solo path in cwd) | ⚪ (solo `plan_type`) | 🟡 **account+org UUID nel path del file** | 🟡 in `user.json` fratello (user id, email, plus) | 🟡 `account.uuid` su ogni conv + users.json |
| `gen_ai.conversation.compacted` (Rec, 2026) | — (file acompact esclusi) | 🟡 `compact_boundary`+`compactMetadata` (pre/postTokens, durationMs) | 🟡 record `compacted` + `context_compacted` | 🟡 `compact_boundary` (`pre_tokens`) | 🟡 `turn_summary` (debole) | ⚪ (`token_budget` sempre null) |

## 2. Usage e costi

| Attributo OTel (livello) | LAV DB | claude_code | codex | cowork | chatgpt | claude_ai |
|---|---|---|---|---|---|---|
| `gen_ai.usage.input_tokens` (Rec) | `token_usage.input_tokens` | ✅ ⚠️ semantica: non-cache | ✅ ⚠️ salvato **netto** dei cached (OTel lo vuole inclusivo) | ✅ | ⚪ zero token nell'export (unica eccezione: 2 record `app_pairing`) | ⚪ zero token |
| `gen_ai.usage.output_tokens` (Rec) | `token_usage.output_tokens` | ✅ | ✅ | ✅ | ⚪ | ⚪ |
| `gen_ai.usage.cache_creation.input_tokens` (Rec, 2026) ➕ | `cache_creation_tokens` | ✅ + 🟡 breakdown TTL 5m/1h | 🟡 **`cache_write_input_tokens` esiste (build recenti), LAV hardcoda 0** (jsonl.py:1527) | ✅ + 🟡 breakdown TTL | ⚪ | ⚪ |
| `gen_ai.usage.cache_read.input_tokens` (Rec, 2026) ➕ | `cache_read_tokens` | ✅ | ✅ `cached_input_tokens` | ✅ | ⚪ | ⚪ |
| `gen_ai.usage.reasoning.output_tokens` (Rec, 2026) | — | ⚪ (testo thinking sì, conteggio no) | 🟡 **`reasoning_output_tokens` presente e ignorato** | ◐ solo stima live `estimated_tokens` | ⚪ | ⚪ |
| Costo | ➕ **query-time via `model_pricing`** (OTel non lo modella) | ✅ calcolato | ✅ calcolato | ✅ calcolato + 🟡 `total_cost_usd`/`modelUsage.costUSD` **dal client** (validazione incrociata!) | — (senza token non calcolabile) | — |
| context window | — | ⚪ | 🟡 `model_context_window` | 🟡 `modelUsage.contextWindow` | ⚪ | ⚪ |

## 3. Parametri di richiesta — il muro del provider

Assenza **verificata full-corpus in tutte e 5 le sorgenti** (l'unica parziale eccezione è il reasoning effort):

| Attributo OTel (livello) | claude_code | codex | cowork | chatgpt | claude_ai |
|---|---|---|---|---|---|
| `gen_ai.request.temperature/top_p/top_k` (Rec) | ⚪ | ⚪ | ⚪ | ⚪ | ⚪ |
| `gen_ai.request.max_tokens` (Rec) | ⚪ (ma l'evento limite sì: `stop_reason=max_tokens` ×22) | ⚪ | ⚪ (`maxOutputTokens` è il limite modello) | ⚪ (`finish_details.type=max_tokens` ×451) | ⚪ |
| `gen_ai.request.stop_sequences/seed/penalties/choice.count` | ⚪ | ⚪ | ⚪ | ⚪ (seed solo image-gen) | ⚪ |
| `gen_ai.request.reasoning.level` (Rec, 2026) | 🟡 `effort` top-level (high/xhigh/max, 12k record) | 🟡 `turn_context.effort` (sempre) | ⚪ | 🟡 `thinking_effort` (standard/extended) | ⚪ |
| `gen_ai.request.stream` (CondReq, 2026) | ⚪ *(pl)* | ⚪ *(pl)* | ⚪ *(pl)* | ⚪ *(pl)* | ⚪ *(pl)* |

**Implicazione**: la conformità `gen_ai.request.*` (sampling) è irraggiungibile dai log
di questi client. Va dichiarato nel doc di ingestion OTel (LAV-50): per le sorgenti
"parsed" questi attributi saranno sempre assenti; arriveranno solo da sorgenti OTLP native.

## 4. Esito, errori, retry

| Attributo OTel (livello) | LAV DB | claude_code | codex | cowork | chatgpt | claude_ai |
|---|---|---|---|---|---|---|
| `gen_ai.response.finish_reasons` (Rec) | — | 🟡 `message.stop_reason` sempre presente (tool_use 139.574 / end_turn 20.336 / stop_sequence 657 / max_tokens 22 / refusal 5) + `stop_details` (categoria refusal, raro) | ⚪ per-risposta (solo `turn_aborted.reason`, `status` item) | 🟡 `stop_reason` (message + result-level) + `terminal_reason` | 🟡 `finish_details.type` (stop 38.392 / **interrupted 1.583** / max_tokens 451 / content_filter 8) + `stop_tokens` + `metadata.stop_reason` | ⚪ |
| `error.type` (CondReq, **stable**) | — | 🟡 ricchissimo: `error` su assistant (rate_limit 93, invalid_request 47, auth 28…), `system/api_error` con HTTP status (529 ×603, 429 ×18), `errorDetails`, `isApiErrorMessage` | 🟡 `event_msg/error` (`codex_error_info`), `turn_aborted`, `result.Err`/`isError` MCP, `exit_code` exec | 🟡 `is_error`+`api_error_status` su result, `errors[]`, `api_retry.error_status` | 🟡 `is_error`, `system_error` content (535), `app_pairing.errors[]` strutturati, `model_switcher_deny` | ◐ `tool_result.is_error` (357 true) — solo tool-level; LLM-level ⚪ |
| Retry | — | 🟡 `retryAttempt/retryInMs/maxRetries` su api_error (telemetria completa) | ⚪ (nessun marker) | 🟡 `api_retry` (attempt, backoff ms) | 🟡 `message_type=variant` + `weight=0` + DAG branches; `app_pairing.attempts[]` | 🟡 `parent_message_uuid` con 202 branch point (regen/edit) |
| Rate limit / quota | — | 🟡 `error.rateLimits` (null) + `mcpMeta._meta.rateLimit` | 🟡 `rate_limits` completi su ogni token_count (used_percent, resets_at, plan_type, credits) | 🟡 `rate_limit_event` ×193 (status, resetsAt, overage) | ⚪ | ⚪ |
| Interruzioni/refusal | — | 🟡 `isAbortedMidStream`, `interruptedByShutdown`, `model_refusal_fallback` (categoria + modello fallback) | 🟡 `turn_aborted` (reason=interrupted) | 🟡 `result/error_during_execution` | 🟡 `finish=interrupted`, `stop_reason=user_interrupted` | ⚪ |

## 5. Timing (span / durate / TTFT)

| Segnale OTel | LAV DB | claude_code | codex | cowork | chatgpt | claude_ai |
|---|---|---|---|---|---|---|
| Span start/end per chiamata modello | — (1 solo `timestamp`/riga) | ⚪ (derivabile per differenza record) | ⚪ | ⚪ (derivabile: `status=requesting` + `request_id`) | ⚪ | 🟡 **coppie `start_timestamp`/`stop_timestamp` su 17.700 content block** (thinking avg 3,2s; text avg 4,7s) |
| `gen_ai.client.operation.duration` — turno/run | — | 🟡 `turn_duration.durationMs` (1.579 record/373 file) | 🟡 `task_complete.duration_ms` + `started_at`/`completed_at` | 🟡 `result.duration_ms` + **`duration_api_ms`** (sempre, 304) | ◐ solo `finished_duration_sec` (reasoning) | ◐ derivabile da created_at |
| TTFT (`time_to_first_chunk`) | — | ⚪ | 🟡 **`time_to_first_token_ms`** (542/643 task_complete) | 🟡 **`ttft_ms`** (54) + `ttft_stream_ms` + `time_to_request_ms` | ◐ `app_pairing.first_token_time` (2 record) | ⚪ |
| Durata esecuzione tool | — | 🟡 `toolUseResult.durationMs` (spesso) + `totalDurationMs` per Task | 🟡 `exec_command_end`/`mcp_tool_call_end` `duration.{secs,nanos}` | 🟡 `durationSeconds` (136)/`durationMs` (23) + Task | 🟡 `aggregate_result.start/end_time` (code interpreter, 5.797) | 🟡 tool_use block start/stop (6.375 coppie, avg 9,3s) |

## 6. Tool / function calling

| Attributo OTel (livello) | LAV DB | claude_code | codex | cowork | chatgpt | claude_ai |
|---|---|---|---|---|---|---|
| `gen_ai.tool.name` (Req su execute_tool) | tabelle tipizzate + `mcp_tool_calls.tool_name` ➕ | ✅ | ✅ | ✅ | ✅ `author.name`/`recipient` | ✅ |
| `gen_ai.tool.call.id` (Rec) | — (solo nel raw JSON di `messages.content`) | 🟡 `tool_use.id` letto (jsonl.py:1140) e **scartato** | 🟡 `call_id` su ogni function_call/output | 🟡 come claude_code | ⚪ **genuinamente assente** (solo linkage DAG) | 🟡 `tool_use.id`+`tool_result.tool_use_id` |
| `gen_ai.tool.call.arguments` (OptIn) | ◐ raw in `messages.content`; tipizzati parziali | ✅ (raw completo nel content) | 🟡 parsati ma persistiti solo command/patch | ✅ (raw completo) | ◐ `content_type=code` + `metadata.command/args` | ◐ troncati a 2000 char nel testo |
| `gen_ai.tool.call.result` (OptIn) | ◐ | ✅ blocchi `tool_result` nel raw + 🟡 `toolUseResult` top-level ricco (mai letto) | 🟡 **`function_call_output`/`custom_tool_call_output` MAI salvati** (il transcript Codex è monco dei risultati) | ✅ raw + 🟡 `tool_use_result` nativo | 🟡 `execution_output` e content tool non persistiti | ◐ inline cap 5000 + 🟡 `structured_content` |
| `gen_ai.tool.definitions` (OptIn, 2026) | — | 🟡 attachment `skill_listing`/`agent_listing` | 🟡 `dynamic_tools[]` con JSON Schema completo | 🟡 `system.init.tools/skills/agents` + `mcp_servers[].status` | 🟡 `plugin_ids`/`disabled_tool_ids` | 🟡 `context.tools[]` (server_uuid+tool_name) |
| MCP server identity (`mcp.*`) | `mcp_tool_calls.server_name` | ✅ nome + 🟡 `mcpMeta` | ✅ namespace + 🟡 `invocation.server` | ✅ | ◐ connector markers | ✅ integration_name + 🟡 **`mcp_server_url`** (endpoint reali) |

## 7. Contenuti

| Attributo OTel (livello) | LAV DB | claude_code | codex | cowork | chatgpt | claude_ai |
|---|---|---|---|---|---|---|
| `gen_ai.input/output.messages` (OptIn) | `messages.content` + FTS5 ➕ | ✅ | ✅ (via response_item/message + event_msg) | ✅ | ✅ | ✅ |
| `gen_ai.system_instructions` (OptIn) | — (chatgpt.py salta role=system) | ⚪ mai su disco | 🟡 **`base_instructions.text` COMPLETO** + user/developer_instructions | ⚪ | ◐ `user_editable_context` (custom instructions + preambolo piattaforma); system vero ⚪ | ◐ `projects.json prompt_template` + memories.json (non linkati alle conv) |
| Thinking/reasoning content | in `messages.content` | ✅ | 🟡 `reasoning` items (summary + encrypted) saltati | ✅ | ✅ `thoughts` | ✅ + 🟡 `summaries[]` |
| Multimodale | nel raw content | ✅ image/document base64 | 🟡 image_url, image gen | ✅ | 🟡 attachments (+`fileSizeTokens`) | 🟡 attachments con `extracted_content` |

## 8. ➕ Dove LAV va OLTRE OTel (punti di forza da preservare)

Questi non hanno equivalente nelle semconv `gen_ai.*` — sono il valore aggiunto di LAV:

| Capacità LAV | Dove | Note vs OTel |
|---|---|---|
| ➕ **Costo query-time con validità temporale dei prezzi** | `model_pricing(from_date,to_date)` + LEFT JOIN (queries.py:82-104) | OTel non modella il costo. Cowork espone pure `costUSD` del client → usabile come **validazione incrociata** del calcolo LAV |
| ➕ **Cache token breakdown** (write+read, prezzi cache dedicati) | `token_usage.cache_creation/read_tokens` | LAV lo faceva **prima** che OTel 2026 li standardizzasse; ora mappa 1:1 sui nuovi attributi |
| ➕ **Classificazione AI post-hoc** (sensitivity, topics, people, clients, tags) | `interaction_metadata` | Nessun equivalente semconv; affine agli eval events ma più ricco |
| ➕ **FTS5 full-text sul contenuto** | `messages_fts` + trigger | OTel content capture è opt-in e non indicizzato |
| ➕ **4 dimensioni di filtro** (project/user/host/source) su ogni riga | `projects/users/hosts/session_sources` | OTel ha solo resource attrs; LAV le rende query-abili ovunque |
| ➕ **Tabelle operative tipizzate** (file/bash/search/skill/subagent/mcp) | 6 tabelle dedicate | Più interrogabili di span `execute_tool` generici |
| ➕ **Roll-up master/subagent con chiusura transitiva** | `parent_session_id` + BFS (queries.py:891-934) | Equivalente pratico di `invoke_agent` span tree, già funzionante |
| ➕ **cwd + git_branch per singola riga operativa** | colonne su tutte le tabelle operative | OTel ha `vcs.*` ma non legato a gen_ai |
| ➕ **Dedup per api_message_id** (anti double-count) | indice UNIQUE parziale (jsonl.py:391-394) | Problema che OTel non affronta (i log client duplicano usage per content-block) |
| ➕ **Source attribution multi-surface** (LAV-74) | `map_codex_source` da originator | Più fine di `gen_ai.provider.name` |

## 9. Segnali extra-OTel presenti nei file e oggi non usati (opportunità)

Non richiesti da OTel ma di valore per la missione LAV (cost/usage observability):

- **Cowork**: `total_cost_usd` + `modelUsage.<model>.costUSD` dal client; audit permessi (`permission_request/response`, `permission_denials`); `_audit_hmac` (integrità)
- **Claude Code**: `usage.iterations[]` (breakdown per iterazione API interna — il per-request che mancava), `diagnostics.cache_miss_reason` (perché la cache ha missato), `service_tier`/`inference_geo`/`speed`, hook telemetry, `toolDenialKind`, `entrypoint` (cli/vscode/desktop), `promptSource` (sdk/typed — distingue traffico SDK da umano)
- **Codex**: `rate_limits` (used_percent, resets_at, plan_type, credits), `guardian_assessment` (safety gate), git telemetry (`commit_hash`, `repository_url`), `world_state`, `ghost_commit`
- **ChatGPT**: `citations`/`content_references` (grounding), `channel` Harmony, `sonic_classification_result.latency_ms`, file fratelli (`user.json`, `model_comparisons.json` con timing RLHF, `message_feedback.json`)
- **claude.ai**: `citations[]`, `approval_options/approval_key` (human-in-the-loop), `mcp_server_url`, `voice_note`

## Caveat di verifica

> ⚠️ **Correzioni successive**: questa matrice è stata scritta prima del censimento
> integrale. Vedi la tabella in [05 §5](05-censimento.md) e soprattutto
> **[07 §1](07-numeri-e-insidie.md)**. In particolare, su questa pagina:
> - `stop_reason` claude_code (§4) è presente al 100% ma **null nel 50,1%** dei record assistant
> - `error.type` codex (§4) è marcato 🟡 ricco: in realtà sono **3 record su 205 file (0,03%)**,
>   senza HTTP status → observability degli errori Codex **praticamente assente**
> - `gen_ai.agent.id` (§1) mappato su `agent_id`: **non conforme**: la spec richiede un id
>   *provider-assigned* e vieta gli id di istanza in-memory. L'`agent_id` di LAV corrisponde
>   alla variante `invoke_agent` **INTERNAL** (che non ha `agent.id`) — vedi [07 §9.3](07-numeri-e-insidie.md)
> - mancano ~25 attributi della spec, incluso l'intero namespace `gen_ai.evaluation.*`
>   (l'aggancio naturale di `interaction_metadata`) — vedi [07 §9.4](07-numeri-e-insidie.md)

- Le assenze marcate *(pl)* (plausible) derivavano dal walk strutturale campionario
  (claude_code ~5% dei file per l'inventario chiavi). **Caveat superato per claude_code**:
  il censimento ha poi scansionato **12.933 file su 12.933** (799.367 record). Le altre
  assenze sono confermate a livello full-corpus dal refuter.
- Semantica `input_tokens` **divergente da OTel in 2 sorgenti**: OTel richiede
  input_tokens *inclusivo* dei cached; Anthropic espone il solo non-cache e LAV
  salva così; per Codex LAV salva il *netto* (input−cached, jsonl.py:1518).
  Decisione richiesta → [02-db-evolution-plan.md](02-db-evolution-plan.md) §Decisioni.
- I record `progress` con timing (`elapsedTimeMs`) esistono solo in file Claude
  Code legacy (~v2.1.49): il formato attuale non li scrive più — non pianificarci sopra.
