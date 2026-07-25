# Findings operativi — bug, rischi e debiti trovati strada facendo

> Problemi emersi durante la gap analysis OTel, il censimento e l'audit di isolamento,
> **indipendenti dal progetto super-schema**. Ognuno è aggredibile da solo, senza
> aspettare il data model. Ordinati per gravità.
>
> Nessuno di questi è stato corretto: la sessione è stata solo di analisi.

---

## 🔴 A. Sicurezza — endpoint di scrittura senza autenticazione

**`POST /api/sync` e `DELETE /api/kb/index` non hanno alcun controllo di API key.**
Zero occorrenze di validazione chiave in [server.py](../../lav/server.py) — l'handler
`do_POST` non verifica nulla. Il server bind su **`0.0.0.0`** per i ruoli `agent` e `both`.

Conseguenza: chiunque raggiunga la porta 8764 di un nodo `role=both`/`collector` (LAN,
Tailscale) può:
- innescare `sync_data()` → `init_db()` + parse completo + pull dagli agent
- **cancellare punti della collection Qdrant** via `DELETE /api/kb/index`

**La documentazione è errata**: CLAUDE.md afferma che `/api/sync` richiede `LAV_API_KEY`.
È vero solo per la CLI (`cli.py:380`) e per MCP (`mcp_server.py:245`), **non** per
l'endpoint HTTP. `utils/services/lav-parser.sh` invia perfino un header Bearer che il
server ignora.

Su `role=agent` le POST rispondono 404 (`server.py:1086-1089`), quindi il nodo esposto è
quello **collector** (prod).

**Fix minimo**: validare `payload["api_key"]` contro `os.environ["LAV_API_KEY"]` in
`do_POST`/`do_DELETE`; rendere configurabile l'indirizzo di bind (`LAV_BIND_ADDR`),
default `127.0.0.1`.

---

## 🟠 B. Bug di correttezza dei dati

| # | Bug | Dove | Effetto |
|---|---|---|---|
| B1 | **Timestamp naive in ora locale** — `datetime.fromtimestamp(epoch)` senza tz | `chatgpt.py:195-202` | Unico parser non-UTC: i confronti cross-source sfalsano di 1-2 ore |
| B2 | **`conversation_id` non esiste nell'export 2023-05** — il parser fa `conv.get('conversation_id')` ma quella annata ha solo `id` | `chatgpt.py` | Le conversazioni più vecchie non sono identificate correttamente |
| B3 | **`cache_creation_tokens` hardcodato a 0** per Codex, mentre `cache_write_input_tokens` esiste nei rollout recenti | `jsonl.py:1527` | Costi di cache-write sottostimati |
| B4 | **`reasoning_output_tokens` ignorato** — Codex è l'unica sorgente col dato esatto | `process_codex_token_count` | Token di reasoning non contabilizzati |
| B5 | **`function_call_output` mai salvato** — i risultati dei tool esistono sempre nei rollout, accoppiati via `call_id` | parser Codex | Il transcript Codex è **monco**: ha le domande e non le risposte |
| B6 | **Rami di rigenerazione ingeriti come conversazione** — ChatGPT: **21.355 messaggi con `weight=0`** | `chatgpt.py` | Rami scartati dall'utente trattati come dialogo reale |
| B7 | **Docstring errato**: `claude_ai.py` dichiara "flat chronological list (no DAG)" ma l'export **è** un DAG — 9.959/10.702 messaggi hanno `parent_message_uuid`, con 202 punti di branch | `claude_ai.py` | Il replay lineare scarta rami silenziosamente |

---

## 🟠 C. Comandi "read-only" che scrivono

| # | Cosa | Dove |
|---|---|---|
| C1 | **`lav-pricing list` apre una connessione in scrittura**: `executescript(MODEL_PRICING_SCHEMA)` + `ensure_pricing_overlap_guard()` con commit | `pricing.py:237-239` |
| C2 | **Tutte le connessioni "read-only" eseguono `PRAGMA journal_mode=WAL` prima di `query_only=ON`** → toccano header e creano/aggiornano i sidecar `-wal`/`-shm` | `server.py:146`, `cli.py:23`, `mcp_server.py:43`, `qdrant/kb_indexer.py:74`, `classifiers/sql_classifier.py:39` |
| C3 | **`lav-backfill-from-snapshot --dry-run` chiama `init_db()` PRIMA del check dry-run** → scrive schema, seed e migrazioni anche in dry-run | `backfill.py:330` vs check a `:337` |
| C4 | **Comandi KB apparentemente di lettura** (`lav kb search`/`status`) fanno `mkdir`, prendono un lock esclusivo e chiamano `ensure_collection()` → `migrate_collection()`, che può eseguire `delete_collection('conversations')` | `cli.py:37-48` |

**Workaround immediato** per C1: usare `lav pricing list` (CLI unificata, `query_only`)
invece di `lav-pricing list` (CLI standalone).

---

## 🟡 D. Debiti dello schema

| # | Debito | Effetto |
|---|---|---|
| D1 | **`INSERT OR IGNORE` + `NOT NULL` = perdita silenziosa** (verificato su SQLite 3.45.3): la violazione non solleva, non applica il DEFAULT, scarta la riga | Qualsiasi NOT NULL aggiunto senza toccare i parser diventa perdita dati muta |
| D2 | **NOT NULL non garantisce nulla di semantico**: la stringa vuota lo soddisfa e i parser scrivono `''` (`chatgpt.py:314`, `claude_ai.py:335`) | Servono `CHECK (col <> '')` |
| D3 | **Le FOREIGN KEY dichiarate non sono applicate** — `PRAGMA foreign_keys` mai attivato | I vincoli referenziali sono decorativi |
| D4 | **`bash_commands` e `mcp_tool_calls` senza UNIQUE** | Duplicati a ogni reparse — LAV-74 ha dovuto fare wipe manuale |
| D5 | **UNIQUE con colonne nullable non deduplica**: `messages(session_id, project_id, uuid)` con `uuid` NULL; `subagent_invocations(…, description)` con `description` NULL. In SQLite NULL ≠ NULL | Il vincolo non scatta mai su quelle righe |
| D6 | **`token_usage UNIQUE(timestamp, session_id, project_id)` fragile**: due chiamate API nello stesso millisecondo collidono e una si perde | Perdita silenziosa (toppa parziale in LAV-39) |
| D7 | **Doppia definizione di `model_pricing`** (`jsonl.py:332` e `pricing.py:15`) | Su un DB nuovo la CLI standalone ricrea la versione vecchia |
| D8 | **Buco di sincronizzazione**: `/api/export` usa `SELECT *` (le colonne nuove passano da sole), ma il cursore `last_pull` avanza comunque e l'export riseleziona solo le sessioni con messaggi più recenti di `since` | Una sessione che smette di ricevere messaggi **non viene mai riesportata**: i campi nuovi restano persi fino a un pull `full` |

---

## 💣 E. Mine da conoscere prima di lavorare sul data model

**E1 — La tabella legacy `conversations` esiste ancora in produzione.**
La migrazione distruttiva `_migrate_conversations_to_interactions`
([jsonl.py:411-447](../../lav/parsers/jsonl.py#L411-L447)) fa un `shutil.copy2` di ~5 GB
seguito da `DROP TABLE`. È inerte **solo perché `interactions` non è vuota** (13.927 righe).
**Qualsiasi esperimento che svuoti `interactions` su una copia del DB reale fa scattare
quel percorso.**

**E2 — L'editable install è condiviso e dirottabile.**
`~/.local/lav-venv/.../__editable___local_agent_viewer_*_finder.py` contiene
`MAPPING = {'lav': '<checkout principale>/lav'}` **hardcoded**. Un `pip install -e .`
lanciato da un worktree **con quel venv** riscrive il mapping: da quel momento
`lav-server` (KeepAlive), il LaunchAgent parser e i processi `lav-mcp` eseguono il codice
sperimentale, **in silenzio**. Chi lavora su un worktree deve crearsi un venv dedicato.

Corollario: `lav-parse` (console script) esegue **sempre** il checkout principale, mentre
`python -m lav.parsers.jsonl` con cwd nel worktree esegue il worktree. Nella stessa shell,
due codebase diverse a seconda di come invochi.

**E3 — Il path del DB non è parametrizzabile.**
`UNIFIED_DB_PATH` è calcolato a import-time in [config.py:18-20](../../lav/config.py#L18-L20)
da `Path.home()`. Nessuna env var. L'unica leva attuale è ridefinire `$HOME`, che però
trascina anche le directory sorgente e la config.
**Modifica minima abilitante**: `LAV_DB_PATH` come override, default invariato.
Stessa cosa per `QDRANT_DATA_DIR` / `QDRANT_COLLECTION` (costanti) e per `role`/`port`
(solo `config.json` machine-global, nessuna env var).

**E4 — Il LaunchAgent `com.aimax.lav-parser` è immune all'isolamento della shell.**
`StartInterval 900` + `RunAtLoad`, e il plist **forza `HOME=/Users/maxturazzini`**: nessuna
variabile d'ambiente della shell lo tocca. Va fermato con `launchctl bootout` (non `kill`,
non `launchctl stop`, che non bloccano il tick successivo). Scrive sul DB **ogni 15 minuti**.

**E5 — `lav-parse` notifica il collector di produzione.**
Ogni parse locale termina con `POST {collector_url}/api/sync` verso l'altra macchina
([jsonl.py:2719](../../lav/parsers/jsonl.py#L2719), `notify_collector` a `:2726-2765`).
Un comando locale innesca una scrittura su un DB remoto. È guardato da `runtime_config`
(`role == "agent"` e `collector_url` presente).

---

## 🔵 F. Igiene di sistema

- **`com.aimax.qdrant` è in crash-loop**: ~60.000 esecuzioni, **142 MB di log**, e il
  binario `~/.local/bin/qdrant` **non esiste**. Da rimuovere o riparare.

---

---

## G. Come lavorare isolati su questo progetto

La procedura completa con i comandi reali (path, hostname, UID) è in
`internal_docs/isolation-checklist.md` — **gitignored**, quindi non sopravvive a un clone.
Qui resta la parte **generalizzabile**, che è ciò che conta davvero.

### I quattro canali da sigillare

Un worktree isola **solo il codice**. Gli altri tre restano aperti:

| Canale | Perché perde | Sigillo |
|---|---|---|
| **Codice** | — | `git worktree add` + branch dedicato |
| **Installazione Python** | L'editable install ha il mapping al checkout principale **hardcoded**; un `pip install -e .` dal worktree con il venv di produzione **dirotta i servizi vivi** (E2) | **venv dedicato dentro il worktree** — non opzionale |
| **Dati** | `UNIFIED_DB_PATH` calcolato a import-time, nessuna env var (E3) | Ridefinire `$HOME` verso una sandbox — **funziona solo in coppia col venv del worktree** (con HOME sandbox + venv di prod il DB finisce dentro il checkout principale) |
| **Processi in background** | Il LaunchAgent parser scrive ogni 15 min e **forza `HOME`** nel plist, quindi ignora l'ambiente della shell (E4) | `launchctl bootout` (non `kill`, non `launchctl stop`: non fermano il tick successivo) |
| **Rete** | Ogni parse notifica il collector di produzione (E5) | Config sandbox senza `collector_url`, oppure `role != agent` |

### Il pattern del runner sigillato

Un'unica funzione shell che impacchetta tutti i sigilli, così è impossibile invocare per
sbaglio il codice o i dati di produzione:

```bash
lavbox() { env -i HOME="$SANDBOX" TMPDIR=/tmp PATH=/usr/bin:/bin:/usr/sbin:/sbin \
  PYTHONDONTWRITEBYTECODE=1 LAV_HOSTNAME=sandbox QDRANT_URL=http://127.0.0.1:9 "$@"; }

# unica forma ammessa di invocazione:
lavbox "$WORKTREE/.venv/bin/python3" -m lav.<modulo>
```

`env -i` azzera anche le chiavi API ereditate (`OPENAI_API_KEY`, `LAV_FOUNDRY_KEY`, …),
quindi nessuna chiamata a servizi esterni a pagamento parte per errore.
`LAV_HOSTNAME=sandbox` è il **tracciante**: se una riga finisse per sbaglio nel DB reale,
la si trova con `SELECT * FROM hosts WHERE hostname='sandbox'` (atteso: vuoto).

### Il test del canarino — da eseguire **prima** del lavoro vero

Il principio: non basta verificare che la produzione sia intatta, bisogna prima dimostrare
che **una scrittura è realmente avvenuta** nell'ambiente isolato. Altrimenti "prod invariato"
non dimostra nulla — potresti semplicemente non aver eseguito niente.

1. **T0**: snapshot della produzione (dimensione, mtime, conteggi righe delle tabelle chiave,
   md5 della config). Le letture vanno fatte con `sqlite3 "file:<db>?mode=ro"` **diretto**,
   perché le connessioni "read-only" del progetto eseguono `PRAGMA journal_mode=WAL` prima
   di `query_only=ON` (C2) e quindi non sono letture pure.
2. **Scrittura deliberata** dall'ambiente isolato (es. una riga di pricing con un modello
   sentinella riconoscibile).
3. **Test positivo A**: la riga sentinella **esiste** nel DB della sandbox. *Se fallisce,
   stai eseguendo un ambiente diverso da quello che credi — fermati.*
4. **Test positivo B**: la riga sentinella **non** esiste in produzione.
5. **Test positivo C**: lo snapshot T1 è **identico** a T0.

Ripetere il confronto T0/T1 anche a **fine lavoro**, prima di ripristinare.

### Ripristino

Ogni sigillo ha il suo comando simmetrico, da eseguire in ordine inverso. Il ripristino del
LaunchAgent parser lancia subito un parse di recupero del backlog (`RunAtLoad`): è il
comportamento voluto. Va inoltre verificato che il mapping dell'editable install di
produzione punti ancora al checkout principale — se è stato dirottato, si ripara con un
`pip install -e <checkout-principale>` dal venv di produzione e un restart del server.
