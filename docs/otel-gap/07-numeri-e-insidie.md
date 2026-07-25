# Numeri misurati e insidie — il documento che rende cancellabile `internal_docs/`

> Fatti quantitativi e vincoli emersi dalle scansioni integrali, che **non compaiono
> altrove** in questa cartella. Chi riprende il progetto a freddo deve leggere questo
> prima di implementare qualunque cosa proposta in [02](02-db-evolution-plan.md) e
> [03](03-adapter-changes.md): **quattro proposte di quei documenti sono, alla luce di
> questi numeri, non implementabili.**

---

## 1. ⛔ Proposte già scritte che i numeri smentiscono

### 1.1 Codex: il formato **corrente** è il più povero sui tool — regressione di schema

Disponibilità di `exit_code` e durata per le tool call, per annata:

| Annata | V1 legacy | V2 env 2025 | V3 env 2026H1 | **V4 corrente (giu-lug 2026)** |
|---|---|---|---|---|
| Copertura | 88,7% | 58,8% | 49,0% | **2,0%** |

Causa misurata: `exec_command_end` — il record che porta `exit_code` + `duration.secs` —
**esiste solo in V3** (708 record; **0 in V2 e 0 in V4**). E `function_call_output.output`
è degradato da JSON con `metadata.{exit_code,duration_seconds}` (V2: 394/768) a stringa
piatta o lista (V4: 913+271, **0 con metadata**). In V4 la durata compare **solo come testo
libero** dentro l'output (*"Wall time 0.2 seconds"*).

> **Invalida** [03 §2](03-adapter-changes.md) [F1] (`exit_code, status | exec_command_end → bash_commands.is_error`)
> e [F2] (`duration.{secs,nanos} → duration_ms`): quei record **non esistono più** nel
> formato che Codex scrive oggi.

### 1.2 Codex: `context_compacted` è vuoto, e gli errori sono quasi inesistenti

- `event_msg/context_compacted` ha **solo la chiave `type`**: zero payload informativo (9 record).
  → **Invalida** [03 §2](03-adapter-changes.md) [F3] (`context_compacted → session_events` con i token).
- Errori strutturati: **3 record su 205 file (0,03%)**, tutti `usage_limit_exceeded`,
  **nessun HTTP status**. → **Contraddice** [01 §4](01-matrix.md), che presenta l'`error.type`
  Codex come 🟡 ricco. L'observability degli errori in Codex è **praticamente assente**.
- `turn_id` su `token_count`: **0 su 8.638** → nessun aggancio al turno se non l'ordine temporale.
- `service_tier` Codex: **0 record su 205 file**. → **Invalida** [02 §B1](02-db-evolution-plan.md)
  e [03 §2 B](03-adapter-changes.md), che lo attribuiscono a `thread_settings`.

### 1.3 claude.ai: `tool_result` non ha **mai** timestamp

Copertura di `start_timestamp`/`stop_timestamp` per tipo di blocco:
thinking **100%** · tool_use 93,6% · text 90,2% · **tool_result 0/6.921** · token_budget 0/1.180.

> **Invalida** [03 §5](03-adapter-changes.md), che dichiara start/stop *"su tutti i block type"*.
> Conseguenza pratica: la latenza di un tool si misura **solo dentro** il blocco `tool_use`,
> mai come `tool_use.stop → tool_result.start`.

Sempre NULL al 100% e da non modellare: `blocco.flags` (34.983/34.983),
`thinking.alternative_display_type` (5.006/5.006).

### 1.4 Cowork: `is_error` è emesso **solo in caso di errore**

`content[tool_result].is_error` presente su **636/2.412 (26,4%)**: l'**assenza significa
successo**, non "sconosciuto".

> **Contraddice** la semantica dichiarata in [02 §A4](02-db-evolution-plan.md)
> (`is_error INTEGER; -- NULL sconosciuto, 0 ok, 1 errore`). Per questa sorgente il NULL
> non esiste: va scritto `0`.

### 1.5 Codex: `source` non è scalare, e `session_meta` è ri-emesso

`session_meta.source` è una **stringa** in 421/468 righe ma un **oggetto**
`{"subagent":{...}}` in **42/468 (9,0%)**. Inoltre `session_meta` viene **ri-emesso a ogni
resume**: 482 righe per 205 file, fino a **19 volte nello stesso file** (53 file con >1).

> Colpisce la modifica raccomandata in [04 §6.2](04-schema-current.md) e nel diagramma
> prima/dopo: **`source` denormalizzato NOT NULL su `interactions`** non è banale, e
> l'upsert di `session_sources` va reso idempotente rispetto ai resume.

---

## 2. Perdite di dati odierne, quantificate

| Sorgente | Perdita | Numeri |
|---|---|---|
| **chatgpt** | La linearizzazione segue solo il ramo di `current_node`: **13.439 nodi-messaggio mai visti**. Copertura 2025: **90,6%** (9.608 messaggi user/assistant persi su 102.248); copertura 2024: **85,2%** | ⚠️ è la faccia opposta di [06 B6](06-findings-operativi.md) (rami `weight=0` *ingeriti*): il parser **contemporaneamente** ingerisce rami scartati e perde rami attivi |
| **chatgpt** | L'export 2023-05 è scartato **integralmente**: `chatgpt.py:282` fa `conv.get('conversation_id','')` + `if not conversation_id: continue`, e quell'annata ha solo `id` → **154/154 conversazioni saltate in silenzio**. Fix sicuro: `COALESCE(conversation_id, id)` — dove esistono entrambi **coincidono sempre (8.907/8.907)** | |
| **chatgpt** | `extract_chatgpt_tools` usa solo `author.name`: vede i **37.947 risultati** ma non le **24.855 chiamate** (il nome sta in `recipient`). Col coalesce si copre il 100% di G3 | |
| **claude_ai** | Il parser **scarta 264 messaggi** (testo renderizzato vuoto) e **non emette nulla per 39 conversazioni** | |
| **claude_ai** | I **`design_chats`** (4 file, 21 messaggi, 57 tool call) sono **ignorati**. ⚠️ Includerli **cambia il contratto NOT NULL**: escludendoli, `account.uuid` e `updated_at` di messaggio salgono a **100,0%**; includendoli, no. È una decisione di modellazione da prendere esplicitamente | |

---

## 3. Sync agent→collector: tre buchi, non uno

[06 D8](06-findings-operativi.md) ne documenta uno solo (il cursore `last_pull`). Gli altri due
sono la ragione per cui il piano di migrazione di [02 §E](02-db-evolution-plan.md)
("le colonne nuove si aggiungono al payload con fallback") **non basta**:

1. **`INSERT OR IGNORE` su `interactions` non aggiorna mai.** Solo l'UPDATE di refresh a
   `jsonl.py:3042-3056` rinfresca un elenco **fisso di 7 campi**. → **Ogni colonna nuova di
   `interactions` non si popolerà MAI sul collector per le sessioni già ingerite, nemmeno con
   un full re-pull**, se non viene aggiunta a quell'UPDATE.
2. **`session_sources` è a senso unico**: l'export porta solo `source` (`queries.py:2045`),
   l'ingest scrive solo `(session_id, project_id, source)` (`jsonl.py:2841-2845`).
   `client_version`, `process_name`, `meta_json` **non viaggiano già oggi**.
3. **`interaction_metadata` non è nell'export**: la classificazione AI **non raggiunge mai
   il collector**.

Altri vincoli dell'export: filtra le figlie con `timestamp > since` → righe con timestamp
NULL o `''` **non saranno mai sincronizzate**; serializza con `json.dumps` → **una colonna
BLOB romperebbe l'intero endpoint**; il pull è **limitato a 1.000 sessioni** per chiamata.

---

## 4. Migrazioni: i caveat che rendono rischioso "tutto additivo"

[02 §E](02-db-evolution-plan.md) dice "tutte additive, pattern esistente, sicuro su SQLite".
I fatti misurati:

- SQLite **non permette `ADD COLUMN NOT NULL` senza DEFAULT costante**.
- Ogni colonna va aggiunta **sia allo `SCHEMA` sia alla `_migrate_*`**, altrimenti un DB
  *fresh* e uno *migrated* divergono.
- Le 4 `_migrate_*` sono in **try/except che stampano e proseguono** (`jsonl.py:459-478`):
  un ALTER fallito lascia il DB **mezzo-migrato mentre il parse continua a scrivere** —
  combinato con `INSERT OR IGNORE` significa perdita muta.
- **Non esiste una tabella `schema_version`**: lo `"schema_version": 1` di `/api/export` è
  **hardcoded** (`server.py:898,909`) e **nessuno lo legge** (`pull_from_agents`,
  `server.py:246-251`, non lo controlla).
- Cambiare un vincolo su `messages`/`interactions` richiede **table-rebuild su 5 GB con FTS5
  external-content + 3 trigger** (`jsonl.py:255-273`).
- Attivare `PRAGMA foreign_keys` farebbe **emergere orfani già presenti**: 521 righe
  `session_sources` senza interaction sul DB dev.

---

## 5. Sentinelle: quattro fatti che ribaltano affermazioni dei documenti

- **`source=''` è dichiarata ma non usata da nessun call site**: 0 righe sul DB reale.
  → [04 §3](04-schema-current.md) e `schema-current.html` la elencano tra le "sentinelle
  esplicite che reggono": è una convenzione **solo nominale**.
- Per claude_code, `parse_state.source` **non è un'etichetta di sorgente** ma una chiave
  composita path-scoped: **`claude_code:<project_dir>`** (`jsonl.py:1816`).
- **Doppia sentinella per lo stesso buco**: sessione senza `session_sources` → `'unknown'`
  in export, ma se la chiave `client_source` manca nel payload viene attribuita a
  **`'claude_code'`** (`jsonl.py:2839`) = **misattribuzione silenziosa**.
- Le derivazioni (`host_id`, `user_id`, `project_id`) non falliscono mai ma ricadono su
  bucket sentinella (`codex_default`, `cowork_default`) quando l'inferenza non riesce.

---

## 6. Cowork: il doppio `session_id` — e perché la fusione di LAV è corretta

**212 session_id distinti su 106 file**: 106 sono la sessione *shell* (solo `type=user`,
**300 righe = 4,2%** del dialogo) e 106 le *inner* (95,8% del dialogo + **100%** di
assistant/system/result/tool).

> Conferma importante per il greenfield: **la fusione che fa LAV è corretta e non perde
> nulla.** Se si adottasse il `session_id` grezzo come chiave, **metà delle conversazioni
> sarebbe priva di modello, token, costo e cwd.**

Duplicazione per content-block: 4.053 righe assistant → **2.203 `message.id` distinti**
(1,84 righe/messaggio, max 10). Contare senza dedup **gonfia G2 del 35,6%** (7.084 vs 5.225)
e i token di 1,84×.

Altri limiti: 2 file **senza `system/init`** (model/cwd/version al 98,1%) e **11/106 senza
record `result`** (costo/durata/turni all'**89,6%**). Una singola conversazione copre
**3 annate e 6 versioni client nello stesso file** → l'assunzione "un file = un'annata" è falsa.

---

## 7. claude_code: quattro correzioni misurate

- **`session_id` snake_case: questione chiusa.** 1.450 record su 799.367 (**0,18%**),
  concentrati in **5 conversazioni**, solo annata 2.1.200+, ed è **un alias di `sessionId`**
  (stesso valore). Va normalizzato in ingest, non è un campo diverso.
  *(→ [03 §1](03-adapter-changes.md) riporta 719 e lascia la verifica aperta: superato.)*
- **`caller` sui blocchi tool_use è inutile**: un solo valore su tutto il corpus,
  `{"type":"direct"}`, **121.560 occorrenze**. Zero potere informativo — non modellarlo.
- **Il server MCP è estraibile al 100% dal nome del tool**: i nomi hanno **sempre 3 segmenti**
  `mcp__<server>__<tool>` (**12.508/12.508**), mentre `attributionMcpServer` copre solo il **3,9%**.
- **`api_message_id` non è affidabile al 100%**: 3.863 record assistant hanno `message.id`
  che **non inizia con `msg_`** (id sintetici locali) → id API "vero" ~98,8%. I 637 assistant
  senza `requestId` **non sono un problema di annata** (distribuiti su tutti i mesi; 334 sono
  messaggi sintetici di errore API). Nessuno dei due è NOT NULL-abile.

---

## 8. Codex: i numeri che ridimensionano i bug noti

- **`cache_write_input_tokens` esiste solo nel 3,5% dei `token_count` (300/8.638)**:
  il bug "`cache_creation_tokens` hardcodato 0" ([06 B3](06-findings-operativi.md)) ha
  impatto **limitato**, e il campo non è stabile nemmeno a livello di evento.
- **`token_count.info` assente in 53/8.638 (0,6%)** e `rate_limits` assente in 130/8.638:
  **nemmeno i token sono garantiti al 100%**.
- **Formato legacy V1**: **761/761 item senza timestamp** (nessun envelope) e `message.id`
  NULL su **46/107** messaggi (tutti i `role=user`).
  → Questo **corregge [05 §3](05-censimento.md)**, che elenca `message_id` e `timestamp`
  tra gli universali NOT NULL "in tutte e 5": **non lo sono**, se si includono i 14 file legacy.

---

## 9. OTel: le parti della spec mai riportate

### 9.1 Il formato normativo dei messaggi — è il contratto della decisione D2

Le viste di export OTel non sono progettabili senza questo. JSON Schema normativi
(`model/gen-ai/gen-ai-input-messages.json`, `-output-messages.json`, `-system-instructions.json`):

```
array di { role, parts[] }
part types: text | tool_call{id,name,arguments} | tool_call_response{id,response}
output messages: uno per choice, con finish_reason
```

Sugli **eventi** il contenuto **MUST** essere strutturato; sugli **span** SHOULD essere
strutturato ma **MAY** essere una stringa JSON finché non atterra OTEP 4485. Esiste un
pattern "upload hook" per storage esterno. Le convenzioni per gli **streaming chunk** sono
ancora **TODO**.

### 9.2 Le metriche — dove LAV è più vicino allo standard di quanto creda

[02 §C](02-db-evolution-plan.md) liquida il tema citando una sola metrica. L'elenco reale:

- **Client**: `gen_ai.client.token.usage` (attributi Required `operation.name`, `provider.name`,
  `token.type`; **buckets consigliati 1…67108864**; *report **billable** tokens*),
  `gen_ai.client.operation.duration`
- **Novità 2026**: `gen_ai.client.operation.time_to_first_chunk`, `…time_per_output_chunk`
- **Server**: `gen_ai.server.request.duration`, `…time_to_first_token`, `…time_per_output_token`
- **Agent/workflow/tool**: `gen_ai.invoke_agent.duration`, **`gen_ai.invoke_agent.inference_calls`**,
  **`gen_ai.invoke_agent.tool_calls`**, `gen_ai.execute_tool.duration`, `gen_ai.workflow.duration`

> Le due in grassetto mappano **1:1** su `toolUseResult.totalToolUseCount` e sul roll-up
> master/subagent che LAV **ha già**. È il punto in cui LAV è più vicino allo standard, e non
> era scritto da nessuna parte.

### 9.3 `gen_ai.agent.id`: il mapping della matrice non è conforme

La spec richiede un id **provider-assigned e stabile** (`asst_*`, ARN Bedrock, urn GCP) e dice
esplicitamente *"in-memory instance ids **NOT** recommended"*. L'`agent_id` di LAV è un id
sintetico locale → corrisponde alla variante **`invoke_agent` INTERNAL** (che infatti **non ha**
`provider.name` né `agent.id`), non a `gen_ai.agent.id`.
→ [01 §1](01-matrix.md) va corretta o annotata.

I 5 tipi di span agente, con kind e naming: `create_agent` (CLIENT), `invoke_agent` (CLIENT
per agenti remoti / **INTERNAL** per in-process), `invoke_workflow`, `plan` (INTERNAL),
`execute_tool` (INTERNAL).

### 9.4 ~25 attributi mai citati nella matrice

`gen_ai.output.type` · `request.previous_response.id` · `request.encoding_formats` ·
`prompt.name` / `prompt.version` / `prompt.variable.*` · `tool.description` · `tool.type` ·
`agent.description` / `agent.version` · `workflow.name` · `data_source.id` · `retrieval.*` ·
`memory.*` · `embeddings.dimension.count` · e **l'intero namespace `gen_ai.evaluation.*`**
(`evaluation.name` Required, `score.value`, `score.label`, `explanation`, più l'evento
`gen_ai.evaluation.result`).

> `gen_ai.evaluation.*` è **l'aggancio naturale di `interaction_metadata`**, oggi liquidato
> in [01 §8](01-matrix.md) con un generico "affine agli eval events".

---

## 10. Sicurezza: il buco più grave è in **lettura**

[06 §A](06-findings-operativi.md) documenta gli endpoint di scrittura senza auth. Ma:

**`GET /api/export` non ha alcuna autenticazione** e il server bind su `0.0.0.0` per i ruoli
`agent` e `both`. È il **dump completo delle sessioni** — l'intero storico delle conversazioni —
servito in LAN/Tailscale a chiunque.

Nota operativa correlata: il collector può aspirare i dati di una macchina agent **in
qualunque momento**, anche a parser fermo.

---

## 11. Altre mine operative

- **Un `lav-parse` di test *consuma* le sessioni**: sposta i watermark in `parse_state`, e il
  parser schedulato di produzione le **salta al giro successivo**. È una **perdita
  silenziosa**, non un doppione — e il DB è append-only, senza rollback.
- **`progress.data.normalizedMessages[]`** (file legacy claude_code) replica l'usage dei
  subagent, **mai contato**: impatto sui totali token **mai quantificato**.
- **`message.context_management.applied_edits` è sempre `[]`** in claude_code e cowork:
  chiave presente, mai popolata.
- **Miglior mapping per `gen_ai.request.reasoning.level`**: `thinkingMetadata`
  (`{level, disabled, maxThinkingTokens, triggers[]}`) sul record **user** di claude_code è
  request-side, quindi semanticamente più corretto di `effort` (che sta sull'assistant).
- **Sorgenti mai ispezionate**: `~/.codex/logs_2.sqlite` (**211 MB, WAL attivo**) e
  `.codex-global-state.json`; `model_comparisons.json` degli export ChatGPT
  (**415 record RLHF con 6 campi di timing** e il prompt completo, incluso lo slot system).

---

## 12. Nota sulla riproducibilità

La spec OTel è stata consultata su `open-telemetry/semantic-conventions-genai` **@ `main`,
2026-07-24**, senza release taggata: **il commit hash non è stato registrato**. `main` è
verosimilmente già cambiato, quindi gli attributi marcati "novità 2026"
(`usage.cache_creation/cache_read.input_tokens`, `usage.reasoning.output_tokens`,
`request.reasoning.level`, `conversation.compacted`, `request.stream`,
`response.time_to_first_chunk`) **vanno riverificati** prima di implementare.
