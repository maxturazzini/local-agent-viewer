# Piano di evoluzione del DB LAV

> Proposta, non decisione. Deriva dalla matrice in [01-matrix.md](01-matrix.md):
> qui si scelgono i segnali 🟡 (nel file, LAV cieco) da promuovere a colonne/tabelle,
> classificati **must have** / **nice to have**. La modifica agli adapter che li
> popola è in [03-adapter-changes.md](03-adapter-changes.md).

## Criteri di classificazione

**Must have** se soddisfa almeno due tra: (a) mappa un attributo OTel
Required/CondReq/Rec di uso reale; (b) presente in ≥2 sorgenti; (c) abilita
domande che oggi LAV non sa rispondere (errori, interruzioni, latenza, retry);
(d) corregge un dato oggi *sbagliato* (non solo mancante).
**Nice to have**: valore reale ma nicchia, una sola sorgente, o solo diagnostica.

---

## A. MUST HAVE

### A1. Correzioni di dati sbagliati (prima di tutto — sono bug, non feature)

| # | Cosa | Dove oggi | Fix |
|---|---|---|---|
| A1.1 | **Timezone ChatGPT**: `epoch_to_iso` usa `datetime.fromtimestamp()` naive locale — unico parser non-UTC; i confronti cross-source sfalsano di 1-2h | chatgpt.py:195-202 | `datetime.fromtimestamp(epoch, tz=timezone.utc)` + backfill una-tantum delle righe chatgpt esistenti |
| A1.2 | **Codex cache_write azzerato**: `cache_creation_tokens` hardcodato 0 ma `cache_write_input_tokens` esiste nei rollout recenti → costi cache-write sottostimati | jsonl.py:1527 | leggere il campo quando presente |
| A1.3 | **Semantica `input_tokens` divergente** (vedi Decisione D1) | jsonl.py:1518 | documentare + colonna/vista di normalizzazione |

### A2. Esito e errori — `stop_reason`, `error_type`

Il gap OTel più economico da chiudere: `error.type` è l'unico attributo **stable**
delle convenzioni, e i dati ci sono in 4 sorgenti su 5.

```sql
-- messages: esito della risposta (per record assistant)
ALTER TABLE messages ADD COLUMN stop_reason TEXT DEFAULT '';   -- end_turn|tool_use|max_tokens|refusal|interrupted|content_filter|...
ALTER TABLE messages ADD COLUMN error_type TEXT DEFAULT '';    -- rate_limit|invalid_request|server_error|... ('' = ok)
ALTER TABLE messages ADD COLUMN request_id TEXT DEFAULT '';    -- req_* / Cloudflare Ray / request UUID
```

Valori normalizzati cross-source (mapping in 03): `stop_reason` usa il
vocabolario Anthropic esteso; `finish_details.type` ChatGPT vi confluisce.
Domande nuove: *quante conversazioni muoiono per max_tokens? quanti refusal?
quante interruzioni utente? tasso errori 529 per fascia oraria?*

### A3. Durate e TTFT — livello turno/run + tool (NON per-chiamata-modello)

Decisione di realismo: la latenza della singola chiamata modello **non esiste
nei file** (⚪ ovunque tranne derivazioni claude.ai); quella di turno/run/tool
sì (🟡 in 4 sorgenti). Si modella ciò che esiste.

```sql
-- interactions: durate aggregate del run/sessione (da result.success, task_complete, turn_duration)
ALTER TABLE interactions ADD COLUMN duration_ms INTEGER;       -- wall clock del run/turno se la sorgente lo fornisce
ALTER TABLE interactions ADD COLUMN api_duration_ms INTEGER;   -- tempo in API (cowork duration_api_ms)
ALTER TABLE interactions ADD COLUMN ttft_ms INTEGER;           -- time-to-first-token (codex task_complete, cowork ttft_ms)
```

Nota: per Claude Code la granularità è il turno (`turn_duration`, più turni per
sessione) → i turni finiscono in `session_events` (A5) e `interactions.duration_ms`
tiene l'aggregato; per claude.ai le durate per-block derivate (thinking/tool/text)
si aggregano allo stesso modo.

### A4. Tool: correlazione, durata, esito

Chiude `gen_ai.tool.call.id` (Rec) + l'equivalente di `gen_ai.execute_tool.duration`
e rende i tool error interrogabili (357 errori tool nel solo export claude.ai,
oggi invisibili).

```sql
-- su TUTTE le 6 tabelle operative (file_operations, bash_commands, search_operations,
-- skill_invocations, subagent_invocations, mcp_tool_calls):
ALTER TABLE <t> ADD COLUMN tool_call_id TEXT DEFAULT '';   -- toolu_* / call_* / tool_use_id
ALTER TABLE <t> ADD COLUMN duration_ms INTEGER;            -- NULL quando la sorgente non la dà
ALTER TABLE <t> ADD COLUMN is_error INTEGER;               -- 0 = ok, 1 = errore, NULL = nessun tool_result visto (vedi nota)
```

> ⚠️ **Semantica di `is_error` — NON è "NULL = sconosciuto, 0 = ok, 1 = errore".**
> **Regola definitiva, fissata da LAV-78**: `0` = un `tool_result` **esiste** e la chiave
> `is_error` è **assente** — l'assenza significa **successo** (misurati 345 successi senza
> chiave su 80 transcript claude_code; in Cowork il campo è emesso solo in caso di errore,
> presente su 636/2.412 = 26,4% — [07 §1.4](07-numeri-e-insidie.md), che già contraddiceva
> la formulazione originale di questa sezione). `1` = errore dichiarato. `NULL` = **nessun
> `tool_result` mai visto** (transcript troncato, o parser che non ne legge). Il NULL non è
> mai "successo presunto": ogni query di error-rate deve escluderlo dal **denominatore**.

Il pairing tool_use↔tool_result (oggi ricostruito dalla UI leggendo il raw JSON,
interactions.html:2044) diventa query-abile.

> ✅ **Stato: PARZIALMENTE FATTO da LAV-78** — nota aggiunta a valle dell'implementazione
> (dettaglio in `docs/CHANGELOG.md`; vocabolario condiviso in `lav/tool_outcomes.py`).
>
> - **Atterrato**: le 3 colonne uniformi (`tool_call_id`, `is_error`, `duration_ms`) su tutte
>   e 6 le tabelle tool, più `error_text TEXT DEFAULT ''` (cap 2000 char, scritto solo in caso
>   di errore) su `bash_commands` e `mcp_tool_calls`, più `exit_code INTEGER` su
>   `bash_commands` — che **non è un campo di nessuna sorgente**: si estrae con
>   `^\s*Exit code (\d+)` dal testo del `tool_result` e copre il 73% degli errori Bash (il
>   resto è permission-denied/blocked, dove un exit code non esiste). Più 6 indici parziali
>   `idx_<t>_tool_call ON <t>(session_id, project_id, tool_call_id) WHERE tool_call_id != ''`.
>   `OUTCOME_COLUMNS` in `lav/tool_outcomes.py` è l'unica fonte di verità, iterata **sia** dal
>   literal `SCHEMA` **sia** dalla migrazione (altrimenti DB nuovo e DB migrato divergono).
> - **Chi popola davvero**: i parser che vedono i blocchi `tool_result` (claude_code, cowork)
>   e l'ingest di sync del collector; lo storico con `lav backfill tool-outcomes`.
>   `claude_ai`, `chatgpt` e **codex** restano NULL — per Codex i `function_call_output`
>   esistono nei rollout ma non sono ancora letti (vedi A7).
> - **`tool_call_id` è solo correlazione, mai chiave**: il 3,0% degli id è duplicato alla
>   sorgente. **`duration_ms` è derivata**, non presa dalla sorgente (i campi nativi coprono
>   l'1,4% dei risultati): è il wall clock fra il record del `tool_use` e quello del
>   `tool_result`, quindi **include l'attesa del prompt di permission** e i task in background
>   (p50 1,0s, p90 5,8s, p99 157s, max 76 min). Non è latenza del tool e non è clampata.
> - **NON deciso, deliberatamente**: la decisione aperta **A1** in
>   [00-stato-e-decisioni.md](00-stato-e-decisioni.md) (tabella `tool_calls` unica + 6 view
>   omonime *vs* 18 `ALTER TABLE`) resta **aperta**. LAV-78 ha preso la via additiva senza
>   chiuderla: se in futuro si sceglie l'opzione (B), queste colonne migrano nella tabella
>   unica come le altre.
> - **Resta da fare in A4**: esito/durata per le sorgenti che oggi non espongono un
>   `tool_result` leggibile (Codex → A7) e il collegamento al messaggio che ha invocato il
>   tool (`message_uuid`, vedi [04 §4.4](04-schema-current.md)).

### A5. Nuova tabella `session_events` — la spina dorsale observability

Un'unica tabella flessibile per gli eventi che oggi vengono buttati e che non
meritano 6 tabelle dedicate. Copre: api_error/retry (Claude Code, Cowork),
rate_limit (Cowork, Codex), compaction (tutte), turn_duration (Claude Code),
task_complete (Codex), result.success (Cowork), permission audit, refusal/fallback.

```sql
CREATE TABLE IF NOT EXISTS session_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    project_id INTEGER NOT NULL,
    user_id INTEGER DEFAULT 1,
    host_id INTEGER DEFAULT 1,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,       -- 'api_error'|'retry'|'rate_limit'|'compaction'|'turn_complete'|'run_result'|'permission'|'refusal_fallback'|'abort'
    -- colonne promosse (query frequenti senza json_extract):
    error_type TEXT DEFAULT '',     -- error.type OTel-style
    http_status INTEGER,            -- 529, 429, ...
    duration_ms INTEGER,            -- durata dell'unità che l'evento chiude (turno, run, compaction)
    ttft_ms INTEGER,
    tokens_before INTEGER,          -- compaction: preTokens
    tokens_after INTEGER,           -- compaction: postTokens
    detail_json TEXT DEFAULT ''     -- payload integrale dell'evento (retryAttempt, resets_at, plan_type, ...)
);
CREATE INDEX IF NOT EXISTS idx_session_events_session ON session_events(session_id, project_id);
CREATE INDEX IF NOT EXISTS idx_session_events_type_ts ON session_events(event_type, timestamp);
```

Perché una tabella sola: gli event type condividono la stessa forma
(chi/quando/tipo/misura) e le query tipiche sono trasversali ("tutti gli errori
di ieri", "compaction per progetto"). `detail_json` evita l'esplosione di colonne;
le 6 colonne promosse coprono i filtri caldi. Stesso pattern additivo di
`parse_state` — nessun impatto sulle tabelle esistenti.

### A6. Token estesi

```sql
ALTER TABLE token_usage ADD COLUMN reasoning_tokens INTEGER DEFAULT 0;  -- Codex reasoning_output_tokens (unica sorgente col dato esatto)
```

(Cowork ha solo la stima `estimated_tokens` → in `session_events.detail_json`, non qui.)

### A7. Transcript Codex completo — tool result

Non una colonna: oggi il parser Codex **non salva i risultati dei tool**
(`function_call_output`, `custom_tool_call_output`) benché siano sempre presenti
accoppiati via `call_id`. Il transcript ha le domande e non le risposte — gap di
completezza contenuti, non solo di observability. Destinazione: `messages.content`
(stesso pattern raw-JSON di Claude Code). Dettaglio in 03.

## B. NICE TO HAVE

| # | Segnale | Sorgenti | Destinazione proposta |
|---|---|---|---|
| B1 | `service_tier`, `inference_geo`, `speed` | claude_code, cowork (tier anche codex thread_settings) | colonne su `token_usage` (3 TEXT) — utile per analisi costi/piani |
| B2 | Reasoning effort (`gen_ai.request.reasoning.level`) | claude_code `effort`, codex `effort`, chatgpt `thinking_effort` | colonna `messages.effort` o `token_usage.effort` |
| B3 | System instructions (Codex: complete; chatgpt/claude.ai: parziali) | codex, chatgpt, claude_ai | `session_sources.meta_json` (piccole) o tabella `session_instructions` se si vuole FTS |
| B4 | Cache TTL breakdown (`ephemeral_5m/1h`) + `cache_miss_reason` | claude_code, cowork | `session_events` (event_type='cache_diag') |
| B5 | `model_context_window` | codex, cowork | `session_sources.meta_json` |
| B6 | Costo dal client (`total_cost_usd`, `modelUsage.costUSD`) | cowork | `session_events.detail_json` (run_result) → report di riconciliazione col costo calcolato LAV |
| B7 | Provider esplicito (`gen_ai.provider.name`) | derivabile ovunque da source | tabella statica di mapping in queries/export OTel — nessuna colonna |
| B8 | Identità account/org (path Cowork, user.json ChatGPT, account.uuid claude.ai) | 3 sorgenti | `users.meta_json` — attenzione privacy (email/telefono restano fuori) |
| B9 | Permission audit (`permission_request/response`, `toolDenialKind`, `approval_key`) | cowork, claude_code, claude_ai | `session_events` (event_type='permission') |
| B10 | `entrypoint` (cli/vscode/desktop) e `promptSource` (sdk/typed) | claude_code | `session_sources.meta_json` — distingue traffico SDK da umano |
| B11 | Citations/grounding | chatgpt, claude_ai | fuori scope DB core; eventuale uso in classificazione |
| B12 | Branch/regen DAG (weight=0, parent_message_uuid) | chatgpt, claude_ai | per ora solo fix di consapevolezza nel parser (non perdere silenziosamente i branch); modellazione DAG fuori scope |
| B13 | Subagent Codex (`agent_nickname`, `parent_thread_id`, `sub_agent_activity`) | codex | riuso di `parent_session_id`/`agent_id` esistenti (pattern LAV-66) |
| B14 | Git telemetry Codex (`commit_hash`, `repository_url`) | codex | `session_sources.meta_json` |

## C. Esplicitamente FUORI (per ora)

- **Modello a span completo / tabella spans generica**: la granularità
  turno+tool+eventi copre ciò che i file contengono davvero; uno span store
  arriverà semmai col receiver OTLP nativo (LAV-53), non per i log parsed.
- **Metriche OTel materializzate** (`gen_ai.client.token.usage` histogram):
  derivabili a query-time da `token_usage` — coerente con la filosofia LAV
  (mai materializzare ciò che si può calcolare).
- **Record `progress` legacy** Claude Code: formato morto (v2.1.49), non pianificarci.
- **`logs_2.sqlite` / `history.jsonl` di Codex**: sorgenti alternative
  potenzialmente ricche, non ispezionate — eventuale spike separato.

## D. Decisioni aperte (da prendere prima di codare)

| # | Decisione | Opzioni | Raccomandazione |
|---|---|---|---|
| D1 | **Semantica `input_tokens`**: OTel li vuole *inclusivi* dei cached; Anthropic espone il non-cache; LAV-Codex salva il netto | (a) lasciare per-sorgente e documentare; (b) normalizzare a inclusivo (rompe i costi: il prezzo input vale solo sul non-cache); (c) tenere le colonne com'è + esporre `input_tokens_total` calcolato in query/export OTel | **(c)** — le colonne attuali sono corrette per il *costo* (missione LAV); la vista inclusiva serve solo alla conformità OTel in export |
| D2 | `stop_reason`/`request_id` su `messages` o su `token_usage`? | messages = visibile nel transcript; token_usage = allineato al concetto "una riga = una chiamata API" | **messages** (già ha `api_message_id` per il join; token_usage resta puro usage) |
| D3 | Backfill: le colonne nuove valgono solo per i parse futuri o si riparsa? | incrementale-only vs `--full` per sorgente | **`--full` una-tantum per claude_code+codex+cowork** (i file sono ancora tutti su disco); chatgpt/claude_ai al prossimo import di export |
| D4 | `session_events` cresce (stop_hook_summary ×7.153 nel solo corpus attuale): ingerire tutti gli event type subito o partire dal set A5? | tutto vs core | **core A5** (error/retry/rate_limit/compaction/turn/run/permission/refusal); hook telemetry e cache_diag dietro flag |
| D5 | Versionare la conformità OTel: dichiarare il mapping in `docs/` o esporre un endpoint `/api/otel-mapping`? | doc statico vs runtime | doc statico ora; runtime solo con LAV-53 |

## E. Migrazioni

Tutte additive, pattern esistente `_migrate_*` in `init_db()` (come
`_migrate_add_api_message_id`, jsonl.py:383):

1. `ALTER TABLE ... ADD COLUMN` con DEFAULT — sicuro su SQLite, nessun lock
   prolungato, compatibile con DB esistenti su entrambi i nodi
2. `CREATE TABLE IF NOT EXISTS session_events` + 2 indici
3. Nessuna modifica a PK, indici esistenti, FTS
4. `/api/export`: le colonne nuove si aggiungono al payload con fallback ''/NULL
   → collector più vecchi le ignorano (verificare tolleranza import prima del
   deploy incrociato agent/collector — stessa sequenza di LAV-74)
5. Ordine deploy: prima collector (prod), poi agent (dev) — l'export arricchito
   non deve arrivare a un collector che non ha ancora le colonne

## F. Sequenza proposta (per ticketizzazione sotto LAV-50 o epica nuova)

1. **Fase 0 — bug fix** (A1): timezone chatgpt + cache_write codex + doc D1. Piccola, autonoma, valore immediato
2. **Fase 1 — esito** (A2 + mapping stop_reason/error): schema + claude_code + cowork + chatgpt
3. **Fase 2 — tool** (A4 + A7): tool_call_id/duration/is_error + tool result Codex
4. **Fase 3 — eventi e durate** (A3 + A5 + A6): session_events + durate/ttft + reasoning_tokens
5. **Fase 4 — nice to have** a scelta (B1, B2, B9, B10 i più probabili)

Ogni fase: sviluppo → test e2e su dev-host → deploy prod → backfill dove previsto (D3).
