# Censimento empirico — presenza dei campi per grain e annata

> Dati grezzi per decidere quali campi possono diventare NOT NULL in uno schema unificato.
> Regola adottata: **la nullability vale per tabella**, sulla popolazione di quella tabella.
> Ogni numero deriva da scansione integrale dei file (nessun grep, nessuna stima):
> parsing con `json.loads` riga per riga, conteggi esatti per grain, **tre stati distinti**
> (chiave assente / chiave presente con `null` / chiave presente con valore vuoto).

## Corpus

| Sorgente | File | Volume | Record | Note |
|---|---|---|---|---|
| claude_code | 12.933 | 4,87 GB | **799.367** | 0 righe malformate |
| codex | 205 | 929 MB | 43.873 | include `archived_sessions` |
| cowork | 106 | 59 MB | 10.962 | dedup per realpath |
| chatgpt | 4 export | 780 MB | 148.214 msg | 4 annate 2023-05 → 2025-12 |
| claude_ai | 8 | 147 MB | 10.702 msg | + `design_chats` (schema alternativo) |

**Grain**: G1 conversazione · G2 messaggio (solo user/assistant, esclusi record di servizio) ·
G3 tool call · G4 evento.

---

## 1. La lezione centrale: "chiave presente" ≠ "valore usabile"

Il rischio maggiore per un vincolo NOT NULL non sono i campi assenti — quelli si vedono.
Sono i campi la cui **chiave è presente al 100%** ma il cui **valore è null o vuoto** in
larga parte dei record. Un NOT NULL su questi passerebbe la review e fallirebbe in produzione.

| Campo | Chiave presente | Valore inutilizzabile | Sorgente |
|---|---|---|---|
| `message.stop_reason` | 100% assistant | **null nel 99,4%** | cowork |
| `message.stop_reason` | 100% assistant | **null nel 50,1%** | claude_code |
| `tool_use.id` | 100% (7.108/7.108) | **NULL nell'82,0%** | claude_ai |
| `tool_result.tool_use_id` | 100% | **NULL nell'81,8%** | claude_ai |
| `message.update_time` | 100% (135.681) | **null nel 98,45%** | chatgpt |
| `parent_tool_use_id` | 100% | null nel 97,5% | cowork |
| `message.stop_details` | 100% righe assistant | **null nel 100%** | cowork |
| `message.stop_sequence` | 100% assistant | valorizzato in **0 casi su 328.148** | claude_code |
| `owner`, `is_starred`, `pinned_time`, `is_read_only` | 100% (2025) | null in 6.396/6.396 | chatgpt |
| `message.id` (formato legacy) | 100% | **null sul 43%** (tutti i `role=user`) | codex |
| `message.diagnostics` | 126.530 record | null nel 96,1% | claude_code |
| `gitBranch` | 100% | stringa **vuota** in 5.444 record; 255 conversazioni interamente non-git | claude_code |
| `parentUuid` | 100% | null nell'1,56% — è il primo record della conversazione (null *strutturale*) | claude_code |
| `message.content` | contenitore mai vuoto | solo il **25,2%** contiene testo (il resto sono soli blocchi tool) | claude_code |

⚠️ **Anche i token non sono garantiti**: in Codex `token_count.info` è assente in 53/8.638
eventi (0,6%). Nessun campo, nemmeno l'usage, è universale al 100% senza verifica.

---

## 2. Annate di formato — il test che rompe i candidati NOT NULL

Un campo può essere al 100% oggi e allo 0% nei file di sei mesi fa. Con `--full` reparse,
un NOT NULL su questi campi fa fallire l'ingest dell'intero storico.

### claude_code — 4 annate (segmentate per `version` del record)

| Annata | Periodo | File | Cosa manca del tutto |
|---|---|---|---|
| **A** 2.0.x | 2025-11-09 → 2026-02-22 | 4.018 | `entrypoint`, `promptId`, `sourceToolAssistantUUID`, `attribution*`, `effort`, `usage.iterations`, `usage.speed`, `usage.inference_geo`, `tool_use.caller` — tutti **0,0%**. `stop_reason` valorizzato solo nel 22,4% |
| **B** 2.1.0-99 | 2026-01-07 → 2026-05-22 | 3.419 | ancora 0,0%: `usage.iterations`, `attribution*`, `effort` |
| **C** 2.1.100-199 | 2026-04-12 → 2026-07-13 | 2.804 | `entrypoint` arriva al 100%; `stop_reason` all'86,9% |
| **D** 2.1.200+ | recente | — | compare `effort` (34,6% degli assistant) |

Presenti fin dalla baseline (candidati solidi): `uuid`, `timestamp`, `sessionId`, `type`,
`cwd`, `version`, `userType`, `isSidechain`, `parentUuid`, `message.{role,content,id,model}`,
`usage.{input,output,cache_creation,cache_read}_tokens`, `requestId` (99,7%),
`tool_use.{id,name,input}`.

### codex — 4 annate, **due formati incompatibili**

| Annata | Periodo | File | Note |
|---|---|---|---|
| **V1** legacy bare | 2025-08-14 → 2025-09-16 | **14** | Nessun envelope: item al livello root, meta-line `{id,timestamp,instructions,git?}`. `git` solo in 7/14; `instructions` null in 12/14 |
| **V2** envelope 2025 | 2025-09-17 → 2026-01-31 | 18 | Nasce `{timestamp,type,payload}`. `session_meta` minimale |
| **V3** envelope 2026H1 | 2026-02-01 → 2026-05-31 | 48 | Compaiono `base_instructions`, `task_started`/`task_complete` (con `duration_ms`, `time_to_first_token_ms`), `turn_aborted`, `patch_apply_end` |
| **V4** corrente | 2026-06-01 → 2026-07-25 | 125 | Multi-agent: `parent_thread_id` (32%), `forked_from_id` (13,6%), `agent_nickname`, `dynamic_tools` |

> **Correzione**: le annate legacy sono **14 file, non ~52** come stimato nella prima ricognizione.

### cowork — 3 annate (per `claude_code_version`)

| Annata | Periodo | File | Note |
|---|---|---|---|
| **V1** pre-hmac (2.1.5→2.1.78) | 2026-01-12 → 2026-04-06 | 54 | `_audit_hmac` **assente al 100%**; `request_id` assente sugli assistant |
| **V2** hmac (2.1.87→2.1.187) | 2026-04-07 → 2026-07-02 | 40 | `_audit_hmac` 100%; `request_id` sul 48% degli assistant |
| **V3** hmac+ts (2.1.197→2.1.217) | 2026-07-03 → 2026-07-25 | 12 | `request_id` 100%; `timestamp` top-level 99,3% |

### chatgpt — 4 annate, con una rottura di schema

| Annata | Conversazioni | Note |
|---|---|---|
| **V1** 2023-05 | 154 | Solo 8 chiavi a G1. **`conversation_id` NON esiste** (solo `id`) |
| **V2** 2023-11 | 663 | Nascono `conversation_id`, `gizmo_id`, `message.status`, `role='tool'` |
| **V3** 2024-05 | 1.848 | Compare `metadata.request_id` (27,7%), `default_model_slug` |
| **V4** 2025-12 | 6.396 | G1 passa da 14 a 30 chiavi: `gizmo_type`, `voice`, `async_status`, `memory_scope`… |

🐞 **Bug del parser**: `chatgpt.py` legge `conv.get('conversation_id')`, che **nell'export
2023-05 non esiste**. Le conversazioni più vecchie non sono identificabili correttamente.

### claude_ai — 5 varianti, di cui una è un secondo formato

| Variante | Periodo | Conv | Note |
|---|---|---|---|
| **V1** pre-DAG | 2023-07-11 → 2024-05-15 | 127 | `parent_message_uuid` NULL nel **100%**; nessun blocco tool/thinking |
| **V2** DAG | 2024-05-16 → 2024-11-03 | 287 | Rollout del DAG il 2024-05-16 |
| **V3** tool **senza id** | 2024-11-04 → 2026-01-15 | 864 | Compaiono `tool_use`/`tool_result`, `thinking` (2025-02), `token_budget` (2025-10). **`tool_use.id` = NULL** |
| **V4** tool **con id** | 2026-01-16 → 2026-04-27 | 237 | Dal 2026-01-16 compaiono `tool_use.id` e `tool_result.tool_use_id` |
| **V5** `design_chats` | 2026-04-18 | 4 file | **Schema diverso**, non variante temporale: `sender`→`role`, `human`→`user`, `chat_messages`→`messages` |

---

## 3. Candidati NOT NULL sopravvissuti — per grain e sorgente

Solo i campi al **100% esatto** su tutti i record del grain **e** su tutte le annate.

**G1 conversazione** — universali in tutte e 5 le sorgenti:
`conversation_id` · `start_timestamp`
*(più, dove disponibili: `ended_at`, `cwd`, `client_version`)*

**G2 messaggio** — universali in tutte e 5 **con una riserva**:
`role` · `content` (come **struttura**, mai come testo) · `message_id`/`uuid` ⚠️ · `timestamp` ⚠️

> ⚠️ **I due campi segnati non sono universali se si includono i 14 file legacy di Codex
> (V1, ago-set 2025)**: quegli item non hanno envelope, quindi **761/761 sono senza
> timestamp**, e `message.id` è NULL su **46/107** messaggi (tutti i `role=user`).
> Sono NOT NULL-abili **solo** escludendo l'annata V1 o assegnando un valore derivato in
> ingest. Vedi [07 §8](07-numeri-e-insidie.md).

**G2 sotto-grain assistant** (solo claude_code/cowork/codex — chi non ha token non scrive righe):
`model` · `api_message_id` · `input_tokens` · `output_tokens` · `cache_creation_input_tokens` ·
`cache_read_input_tokens` — **100% sui 328.148 record assistant di claude_code**

**G3 tool call**: `tool_name` · `timestamp` universali.
`tool_call_id` **NO** — null all'82% in claude.ai, assente in chatgpt, e non univoco (§4).

**G4 evento** (claude_code, codex, cowork): `event_type` · `timestamp`.

---

## 4. Unicità delle chiavi — impatto sulle PK

- **`sessionId` non è univoco per file**: 10.922 file portano solo **4.402 sessionId distinti**;
  8.939 file condividono l'id con un altro, perché i subagent riusano il sessionId del padre.
  La chiave affidabile è `(sessionId, agentId)` o l'id sintetico LAV-66 `<parent>::agent-<agentId>`.
  `agentId` è al 100% sui file `agent-*` e allo 0% sui top-level: discrimina perfettamente.
- **`tool_use.id` non è univoco nemmeno dentro lo stesso file**: 167.743 blocchi per
  162.643 id distinti = **3,0% di duplicati** (riscritture e resume). Colonna di
  correlazione, **mai** chiave o UNIQUE.
- Sui file `<uuid>.jsonl` il nome file coincide col `sessionId` nel 99,8% dei casi.

---

## 5. Correzioni alla matrice ([01-matrix.md](01-matrix.md))

Il censimento, misurando i tre stati, corregge alcune celle della prima ricognizione:

| Cella | Prima | Dopo il censimento |
|---|---|---|
| claude_ai · `gen_ai.tool.call.id` | 🟡 presente (7.108/7.108) | 🟡 ma **NULL nell'82%** — la correlazione tool funziona solo dal 2026-01-16 |
| cowork · `finish_reasons` | 🟡 `stop_reason` presente | 🟡 ma **null nel 99,4%** — utile solo a livello `result` |
| codex · annate legacy | ~52 file | **14 file** |
| codex · token | ✅ sempre | ✅ ma `token_count.info` assente nello 0,6% |
| chatgpt · `conversation.id` | ✅ | ✅ dal 2023-11 — **assente nell'export 2023-05** (bug parser) |

---

## 6. Audit dei vincoli attuali — cosa impedisce oggi i NOT NULL

1. **`INSERT OR IGNORE` + `NOT NULL` = perdita silenziosa** (verificato su SQLite 3.45.3):
   la violazione non solleva eccezione, non applica il DEFAULT, **scarta la riga**.
   Tutte le scritture dei parser usano `OR IGNORE`. Un NOT NULL aggiunto senza toccare i
   parser diventa un meccanismo di perdita dati muto.
2. **NOT NULL non garantisce nulla di semantico**: `interactions.timestamp` è NOT NULL ma
   la stringa vuota lo soddisfa, e i parser scrivono `''` quando il dato manca
   (`chatgpt.py:314`, `claude_ai.py:335`). Servono `CHECK (col <> '')`.
3. **Le FOREIGN KEY dichiarate non sono applicate**: `PRAGMA foreign_keys` non è mai attivato.
4. **UNIQUE con colonne nullable non deduplica**: `messages UNIQUE(session_id, project_id, uuid)`
   con `uuid` NULL, `subagent_invocations UNIQUE(…, description)` con `description` NULL.
   In SQLite NULL ≠ NULL.
5. **Le derivazioni LAV sono totali ma euristiche**: `host_id`, `user_id`, `project_id` non
   falliscono mai (creano sempre una riga) ma ricadono su bucket sentinella
   (`codex_default`, `cowork_default`) quando l'inferenza non riesce.
6. **`source` può mancare del tutto** (riga `session_sources` assente, non NULL): per Codex
   l'upsert è agganciato al solo evento `session_meta`.
7. **Export/sync tollerante ma con un buco**: `/api/export` usa `SELECT *`, quindi le colonne
   nuove finiscono automaticamente nel payload e un collector vecchio le ignora senza errori.
   Ma il cursore `last_pull` avanza comunque e l'export riseleziona solo le sessioni con
   messaggi più recenti di `since`: **una sessione che smette di ricevere messaggi non viene
   mai riesportata**, quindi i campi nuovi restano persi finché non si fa un pull `full`.

---

## 7. Dati grezzi

Output completi dei 30 agenti (gap analysis, censimento, audit isolamento) in
`internal_docs/otel-gap-raw/` — **gitignored**, quindi locali alla macchina.
Rigenerabili: i workflow sono descritti in [README.md](README.md#metodo-verificato-non-a-memoria).
