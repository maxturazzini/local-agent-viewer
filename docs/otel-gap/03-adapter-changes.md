# Proposta di variazione degli adapter

> ## ⚠️ QUATTRO PROPOSTE DI QUESTO DOCUMENTO SONO INVALIDATE
> Il censimento integrale, successivo alla stesura, ha smentito alcune righe qui sotto.
> **Leggere [07 §1](07-numeri-e-insidie.md) prima di implementare qualunque cosa.**
>
> | Proposta in questo doc | Perché non è implementabile |
> |---|---|
> | §2 [F1] `exec_command_end → is_error` e [F2] `duration.{secs,nanos}` | `exec_command_end` **esiste solo nell'annata V3**: 0 record in V2 e **0 in V4, il formato corrente**. Copertura durata/exit_code crollata all'**2,0%** |
> | §2 [F3] `context_compacted → session_events` | Il record ha **solo la chiave `type`**: zero payload, niente token |
> | §2 [B] `service_tier` da Codex `thread_settings` | **0 record su 205 file** |
> | §5 start/stop timestamp "su **tutti** i block type" | `tool_result`: **0/6.921**. La latenza tool si misura solo dentro `tool_use` |
>
> Correzioni minori: le annate legacy Codex sono **14 file, non 52** (§2); la variante
> snake_case `session_id` di claude_code è **questione chiusa** (1.450 record, 0,18%,
> alias di `sessionId` — §1); `requestId` è al **99,81%** sui 328.148 assistant.

> Campo per campo: cosa ogni parser deve iniziare a leggere, da dove, verso dove.
> Presuppone lo schema di [02-db-evolution-plan.md](02-db-evolution-plan.md).
> Tag priorità: **[F0]** bug fix · **[F1]** esito · **[F2]** tool · **[F3]** eventi/durate · **[B]** nice to have.
> I `field_path` citati sono verificati sui file reali (2026-07-24, vedi
> `internal_docs/otel-gap-raw/`).

## Principi comuni

- **Additivo**: nessun campo oggi letto cambia significato; le colonne nuove hanno
  default vuoto/NULL, i vecchi record restano validi.
- **Normalizzazione `stop_reason`** (vocabolario condiviso cross-source):
  `end_turn | tool_use | max_tokens | stop_sequence | refusal | content_filter |
  interrupted | error` — mapping per sorgente qui sotto.
- **Normalizzazione `error_type`** (stile `error.type` OTel, low-cardinality):
  codice provider quando c'è (`rate_limit`, `invalid_request`, `overloaded`,
  `usage_limit_exceeded`…), altrimenti classe (`http_429`, `network`, `_OTHER`).
- **Timestamp**: ovunque UTC ISO-8601. Chi salva stringhe raw già UTC (`Z`) non
  cambia; chatgpt si corregge (F0).

---

## 1. `jsonl.py` — Claude Code (`parse_project`)

Il parser più ricco di segnali ignorati. Oggi processa solo record
`user`/`assistant` (+ summary): i record `system` sono la miniera persa.

### [F0] Bug/quick win
| Campo sorgente | Record | Destinazione | Note |
|---|---|---|---|
| *(nessun bug — riferimento)* | | | |

### [F1] Esito
| Campo sorgente | Record | Destinazione | Note |
|---|---|---|---|
| `message.stop_reason` | assistant | `messages.stop_reason` | enum già OTel-friendly (tool_use/end_turn/stop_sequence/max_tokens/refusal) |
| `message.stop_details` | assistant (raro, refusal) | `messages.stop_reason='refusal'` + dettaglio in `session_events` (refusal_fallback) | contiene `category` (es. 'bio') |
| `requestId` | assistant | `messages.request_id` | `req_*`, presente su 27.768/27.834 |
| `error` / `apiError` / `apiErrorStatus` / `errorDetails` / `isApiErrorMessage` | assistant (rari) | `messages.error_type` (+ dettaglio `session_events` api_error) | valori visti: unknown, rate_limit, invalid_request, authentication_failed, server_error, model_not_found, max_output_tokens |

### [F2] Tool
| Campo sorgente | Record | Destinazione | Note |
|---|---|---|---|
| `content[tool_use].id` | assistant | `<tabella tipizzata>.tool_call_id` | oggi letto (jsonl.py:1140) e scartato — basta propagarlo a `process_tool_call` |
| `toolUseResult.durationMs` / `durationSeconds` | user (tool result) | `<tabella>.duration_ms` | join per `sourceToolUseID`/`tool_use_id` |
| `content[tool_result].is_error` | user | `<tabella>.is_error` | |
| `toolUseResult.{totalDurationMs,totalTokens,totalToolUseCount,resolvedModel,toolStats}` | user (risultato Task) | `subagent_invocations.duration_ms` + `session_events` (run_result subagent) | metriche `invoke_agent` pronte |

### [F3] Eventi e durate
| Campo sorgente | Record | Destinazione |
|---|---|---|
| `durationMs`+`messageCount` | system subtype=`turn_duration` (1.579 nel corpus) | `session_events` (turn_complete) + aggregato → `interactions.duration_ms` |
| `error.{status,requestId,formatted}`, `cause.{code,path}`, `retryAttempt/retryInMs/maxRetries` | system subtype=`api_error` (1.270) | `session_events` (api_error/retry; `http_status`, `error_type`; endpoint URL in detail_json) |
| `compactMetadata.{trigger,preTokens,postTokens,durationMs,cumulativeDroppedTokens}` | system subtype=`compact_boundary` (437) | `session_events` (compaction; tokens_before/after) |
| `microcompactMetadata` | system (12) | idem |
| `subtype=model_refusal_fallback` (originalModel, fallbackModel, apiRefusalCategory) | system (2) | `session_events` (refusal_fallback) |
| `isAbortedMidStream` / `interruptedByShutdown` | assistant/user | `session_events` (abort) |

### [B] Nice to have
`message.usage.service_tier/inference_geo/speed` → `token_usage` (B1); `effort` →
`messages.effort` (B2); `usage.cache_creation.ephemeral_5m/1h` + `diagnostics.cache_miss_reason`
→ `session_events` cache_diag (B4); `entrypoint`/`promptSource`/`slug` →
`session_sources.meta_json` (B10); `toolDenialKind` → `session_events` permission (B9);
`isSidechain` → conferma flag subagent; `agentName`/`attribution*` → arricchimento agent.

**Attenzione**: 719 record con variante `session_id` snake_case accanto a
`sessionId` (corpus scan) — verificare che il parser non li perda.

---

## 2. `jsonl.py` — Codex (`parse_codex_sessions`)

Oggi legge solo `session_meta`, `turn_context`, `response_item/message`,
`function_call` (limitato a shell/patch/MCP-resource) e `token_count`. Tutto il
resto del nuovo formato envelope viene saltato.

### [F0] Bug fix
| Campo sorgente | Record | Destinazione | Note |
|---|---|---|---|
| `info.last_token_usage.cache_write_input_tokens` | event_msg/token_count | `token_usage.cache_creation_tokens` | **oggi hardcodato 0** (jsonl.py:1527); il campo esiste nei rollout recenti |
| `info.last_token_usage.reasoning_output_tokens` | event_msg/token_count | `token_usage.reasoning_tokens` | unica sorgente col dato esatto; oggi ignorato |

### [F1] Esito
| Campo sorgente | Record | Destinazione |
|---|---|---|
| `payload.{message,codex_error_info}` | event_msg/error | `session_events` (api_error; error_type=codex_error_info) |
| `payload.{reason,duration_ms}` | event_msg/turn_aborted | `session_events` (abort) + `messages.stop_reason='interrupted'` sull'ultimo assistant del turno |
| `result.Ok.isError` / `result.Err` | event_msg/mcp_tool_call_end | `mcp_tool_calls.is_error` |
| `exit_code`, `status` | event_msg/exec_command_end | `bash_commands.is_error` (exit_code≠0) |

### [F2] Tool — il gap più grosso di Codex
| Campo sorgente | Record | Destinazione | Note |
|---|---|---|---|
| `payload.{call_id,output}` | response_item/**function_call_output** | `messages.content` (blocco tool_result nel raw, stesso pattern Claude Code) | **oggi il transcript Codex non ha i risultati dei tool** |
| `payload.{name,input,call_id,status}` + `_output` | response_item/custom_tool_call(_output) (1.735+1.735) | idem + tabelle tipizzate (`apply_patch` custom → file_operations) | oggi persi del tutto |
| `payload.call_id` | function_call (già parsato) | `<tabella>.tool_call_id` | |
| `duration.{secs,nanos}` | exec_command_end / mcp_tool_call_end | `<tabella>.duration_ms` | |
| `invocation.{server,tool}` | mcp_tool_call_end | conferma `mcp_tool_calls.server_name/tool_name` | |

### [F3] Eventi e durate
| Campo sorgente | Record | Destinazione |
|---|---|---|
| `payload.{started_at}` / `{completed_at,duration_ms,time_to_first_token_ms}` | task_started / task_complete | `session_events` (turn_complete; duration_ms, ttft_ms) + aggregato → `interactions.duration_ms`/`ttft_ms` |
| `payload.rate_limits.{primary,secondary,plan_type,credits}` | token_count | `session_events` (rate_limit) — campionare 1/turno, non ogni evento |
| record `compacted` / `context_compacted` | | `session_events` (compaction) |

### [B] Nice to have
`base_instructions.text` (+`user_instructions`/`developer_instructions`) →
system instructions (B3); `effort`/`summary` → B2; `model_provider` → provider
mapping (B7); `git.{commit_hash,repository_url}` → meta_json (B14);
subagent topology (`agent_nickname`, `parent_thread_id`, `sub_agent_activity`) →
`parent_session_id`/`agent_id` pattern LAV-66 (B13); `reasoning` items summary →
transcript thinking; `guardian_assessment` → session_events permission;
`model_context_window`, `thread_settings.service_tier` → meta_json.

**Attenzione formato legacy 2025** (52 file: item bare senza envelope,
`record_type='state'`): ogni estensione va guardia-ata con `payload.get(...)` e
testata anche su quei file.

---

## 3. `jsonl.py` — Cowork (`parse_cowork_sessions`)

Oggi legge solo `user`/`assistant`/`system.init`. Gli eventi audit esclusivi
(result, rate_limit, permission, api_retry, thinking_tokens, tool_use_summary)
sono tutti ignorati — ed è la sorgente con la telemetria di run più completa.

### [F1] Esito
| Campo sorgente | Record | Destinazione |
|---|---|---|
| `message.stop_reason` (quando non-null) + `stop_reason` top-level | assistant / result.success | `messages.stop_reason` (per-messaggio; il result-level chiude il run) |
| `is_error`, `api_error_status`, `errors[]`, `terminal_reason` | result | `session_events` (run_result; error_type) |
| `request_id` | assistant (933) | `messages.request_id` |

### [F2] Tool
| Campo sorgente | Record | Destinazione |
|---|---|---|
| `content[tool_use].id` / `content[tool_result].{tool_use_id,is_error}` | assistant/user | `<tabella>.tool_call_id` / `.is_error` (riuso F2 Claude Code — stessi helper) |
| `tool_use_result.{durationMs,durationSeconds}` | user | `<tabella>.duration_ms` |
| `tool_use_result.{totalDurationMs,totalTokens,agentId,agentType}` | user (Task) | `subagent_invocations` + session_events |

### [F3] Eventi e durate — il piatto forte
| Campo sorgente | Record | Destinazione |
|---|---|---|
| `duration_ms`, `duration_api_ms`, `ttft_ms`, `ttft_stream_ms`, `time_to_request_ms`, `num_turns` | result.success (304) | `interactions.duration_ms/api_duration_ms/ttft_ms` + `session_events` (run_result) |
| `total_cost_usd` + `modelUsage.<m>.costUSD` | result.success | `session_events.detail_json` (run_result) → **report riconciliazione costo client vs costo LAV** (B6) |
| `attempt`, `max_retries`, `retry_delay_ms`, `error_status` | system/api_retry (69) | `session_events` (retry) |
| `rate_limit_info.{status,resetsAt,rateLimitType,overage*}` | rate_limit_event (193) | `session_events` (rate_limit) |
| `compact_metadata.{trigger,pre_tokens}` | system/compact_boundary | `session_events` (compaction) |
| `permission_request/response` (tool_name, decision, granted) + `permission_denials[]` | system / result | `session_events` (permission) (B9) |

### [B] Nice to have
`usage.service_tier/inference_geo` → B1; `diagnostics.cache_miss_reason` (con
enum: tools_changed 73, system_changed 12…) → B4; account/org UUID dal path file
→ `users.meta_json` (B8, privacy-aware); `client_platform`, `capabilities` →
meta_json; `estimated_tokens` (stima thinking) → detail_json; `_audit_hmac` →
fuori scope (integrità, non observability).

---

## 4. `chatgpt.py`

### [F0] Bug fix — timezone
`epoch_to_iso` (chatgpt.py:195-202): `datetime.fromtimestamp(epoch)` →
`datetime.fromtimestamp(epoch, tz=timezone.utc)`, output con offset esplicito.
Backfill: una-tantum sulle righe `chatgpt:%` esistenti (conversione determinstica
locale→UTC) o reimport dell'export.

### [F1] Esito
| Campo sorgente | Dove | Destinazione | Note |
|---|---|---|---|
| `metadata.finish_details.type` | message.metadata | `messages.stop_reason` | mapping: stop→end_turn, interrupted→interrupted, max_tokens→max_tokens, content_filter→content_filter |
| `metadata.finish_details.{stop_tokens,stop,reason}` | | detail in `messages` non serve — eventuale meta | `interrupted` ×1.583 è il segnale nuovo di valore |
| `metadata.stop_reason` (agent mode: user_interrupted…) | | `messages.stop_reason` (fallback) | |
| `metadata.request_id` | (104k, Cloudflare Ray) | `messages.request_id` | il suffisso colo (es. `-MXP`) è geografia edge — lasciare intero |
| `metadata.is_error` + `content_type=system_error` | | `messages.error_type` | |

### [F2] Tool
| Campo sorgente | Dove | Destinazione | Note |
|---|---|---|---|
| `metadata.aggregate_result.{start_time,end_time,status,timeout_triggered}` | code interpreter (5.797) | `mcp_tool_calls.duration_ms` (end−start) + `.is_error` | **unica coppia start/end esplicita dell'export** |
| `content_type=code` (args) / `execution_output` (result) | tool messages | includere nel testo `messages.content` (oggi i turni tool non lasciano contenuto) | |
| `tool_call_id` | — | — | **non esiste** nell'export: linkage solo DAG — documentato, niente da fare |

### [F3/B]
`requested_model_slug` vs `model_slug` (routing) → meta; `thinking_effort` → B2;
`weight=0`/`message_type=variant` → **consapevolezza branch**: oggi la
linearizzazione può includere branch scartati — decidere se filtrare `weight=0`
(proposta: sì, con flag `--keep-branches`); `turn_exchange_id` → correlazione in
meta; `user.json` fratello (account id, email, plus) → B8 privacy-aware;
`async_task_*`, `agent_entrypoint` → detail_json se si ingeriscono eventi.

---

## 5. `claude_ai.py`

L'export non ha né modello né token (⚪ confermato full-scan): il valore
recuperabile è timing, tool correlation, errori tool.

### [F1] Esito
| Campo sorgente | Dove | Destinazione |
|---|---|---|
| `tool_result.is_error` | content block (357 true / 6.921) | `mcp_tool_calls.is_error` — oggi solo marcatore testuale "(error)" (claude_ai.py:108) |
| `web_fetch` error payload (`error_type` JSON-in-string: ROBOTS_DISALLOWED…) | tool_result content | `mcp_tool_calls.is_error` + error_type in detail (parse `json.loads` del testo, guardato) |

### [F2] Tool + timing
| Campo sorgente | Dove | Destinazione | Note |
|---|---|---|---|
| `tool_use.id` / `tool_result.tool_use_id` | content blocks | `mcp_tool_calls.tool_call_id` | |
| `content[].start_timestamp/stop_timestamp` | tutti i block type (17.700 coppie differenti) | tool_use → `mcp_tool_calls.duration_ms`; thinking/text → aggregato `interactions.duration_ms` | oggi il parser usa solo start_timestamp come timestamp (claude_ai.py:309) e **butta stop** |
| `mcp_server_url` + `context.tools[].server_uuid` | tool_use (51) | `mcp_tool_calls.server_name` arricchito / meta | endpoint MCP reali (mcp.atlassian.com, mcp.stripe.com…) |

### [B]
`parent_message_uuid` (9.959 non-null, 202 branch point) → il docstring
"flat chronological" è **errato**: l'export è un DAG con branch di
edit/regenerate — replay lineare perde branch silenziosamente; minimo: contare e
loggare i branch scartati. `account.uuid` (già in meta) → B8; `summaries[]` dei
thinking → transcript; `citations[]` → B11; `attachments.extracted_content` →
già nel testo; `voice_note` → marker; `projects.json prompt_template` /
`memories.json` → B3 (non linkabili alle conversazioni: solo storage a livello
account).

---

## Riepilogo impatto per fase

| Fase | Adapter toccati | Colonne/tabelle nuove usate | Valore |
|---|---|---|---|
| F0 | chatgpt, codex | (nessuna — fix su colonne esistenti) | dati corretti: UTC uniforme, cache-write e reasoning tokens Codex |
| F1 | tutti e 5 | `messages.stop_reason/error_type/request_id` | tasso errori, interruzioni, refusal, correlazione API |
| F2 | tutti e 5 | `*.tool_call_id/duration_ms/is_error` + transcript Codex completo | execute_tool observability, pairing query-abile, tool error rate |
| F3 | claude_code, codex, cowork (+claude_ai durate) | `session_events`, `interactions.duration_ms/api_duration_ms/ttft_ms`, `token_usage.reasoning_tokens` | latenza turno/run, TTFT, retry, rate limit, compaction |
| B | a scelta | B1-B14 | tier/geo, effort, permission audit, riconciliazione costi, identità account |
