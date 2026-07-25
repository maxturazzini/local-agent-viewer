# Stato dei lavori e decisioni — sessione 2026-07-24/25

> Il "libro dei sogni": tutto ciò che è emerso, cosa è deciso, cosa è aperto.
> Nessuna riga di codice è stata scritta né committata. Nessun ticket Jira aperto.

> ## 🅿️ PROGETTO PARCHEGGIATO (2026-07-25)
> Valutato **troppo grande** per essere avviato ora: LAV continua a fare quello che fa.
> Questa cartella è l'archivio completo per riprenderlo più avanti.
> **Per ripartire**: leggere [00](00-stato-e-decisioni.md) → [07](07-numeri-e-insidie.md) →
> sciogliere le decisioni aperte §4 → Fase 0 (§6).
> **Aggredibili subito e indipendenti dal progetto**: i bug e la vulnerabilità in
> [06-findings-operativi.md](06-findings-operativi.md).

## Indice dei documenti

**Ordine di lettura consigliato a freddo**: 00 → 07 → 01 → 04 → 02 → 03

| Doc | Contenuto |
|---|---|
| **[00-stato-e-decisioni.md](00-stato-e-decisioni.md)** | ⬅️ questo file: stato, decisioni prese e aperte, tema doppi, sequenza |
| [README.md](README.md) | Sintesi gap analysis + metodo |
| [01-matrix.md](01-matrix.md) | Matrice `gen_ai.*` × schema LAV × 5 sorgenti (✅ / 🟡 / ⚪) + punti di forza LAV |
| [02-db-evolution-plan.md](02-db-evolution-plan.md) | Piano evoluzione DB: must have / nice to have, DDL, decisioni |
| [03-adapter-changes.md](03-adapter-changes.md) | Variazioni proposte ai 5 adapter, campo per campo ⚠️ *4 proposte invalidate da [07 §1](07-numeri-e-insidie.md)* |
| [04-schema-current.md](04-schema-current.md) | Schema attuale: mappa, cosa regge, cosa no |
| [05-censimento.md](05-censimento.md) | Censimento empirico: presenza campi per grain e annata, candidati NOT NULL |
| [06-findings-operativi.md](06-findings-operativi.md) | **Bug, vulnerabilità e debiti aggredibili da soli**, senza il progetto |
| **[07-numeri-e-insidie.md](07-numeri-e-insidie.md)** | ⚠️ **Leggere prima di implementare**: numeri misurati, proposte smentite, parti di spec OTel mancanti |
| [schema-current.html](schema-current.html) | Diagramma ER navigabile |
| [schema-before-after.html](schema-before-after.html) | Prima/dopo colorato, spina dorsale vs periferia (*proposta, non decisa*) |
| `internal_docs/otel-gap-raw/` | Output grezzi dei 30 agenti (**gitignored**, non sopravvive a un clone) |
| `internal_docs/isolation-checklist.md` | Procedura di isolamento con comandi reali (**gitignored**) |

---

## 1. Cosa è stato accertato

Due workflow multi-agente, **19 agenti**, ~1,8M token, tutto su **file reali** — nessuna
affermazione a memoria:

| Sorgente | Corpus analizzato |
|---|---|
| claude_code | 12.933 file, **4,87 GB, 799.367 record** parsati integralmente |
| codex | 205 rollout, 929 MB, 43.873 righe, 0 non parsabili |
| cowork | 106 audit.jsonl, 59 MB, 10.962 righe |
| chatgpt | 4 export (2023-05 → 2025-12), 780 MB, 148.214 messaggi |
| claude_ai | export completo, 152 MB, 1.515 conv, 34.983 content block |

Più: spec OTel GenAI da fonte ufficiale (repo `semantic-conventions-genai` @ main,
2026-07-24), audit integrale dello schema LAV e delle sue derivazioni, passata
adversariale che ha ribaltato ~20% delle assenze dichiarate al primo giro.

---

## 2. Le scoperte che vincolano il disegno

**Sulle sorgenti**

1. **I parametri di sampling non esistono in NESSUN file** — temperature, top_p/k,
   max_tokens, seed, penalties, stop_sequences: zero occorrenze strutturali su ~6,7 GB.
   È un limite dei provider: la conformità `gen_ai.request.*` è irraggiungibile dai log parsed.
2. **Esiti, errori e durate esistono e LAV li butta** — `stop_reason` (160k+ occorrenze),
   `finish_details` ChatGPT (con `interrupted` ×1.583), TTFT reale in Codex e Cowork,
   durate turno/tool ovunque, telemetria retry/rate-limit completa.
3. **Volatilità per annata**: `effort` 0,0% nei file 2.0.x → 34,6% in 2.1.200+;
   `entrypoint` 0% → 100%; `usage.iterations`, `attribution*`, `speed`, `inference_geo` idem.
   Nessuno di questi può essere NOT NULL, e una colonna per ognuno significa schema
   che cambia a ogni release del client.
4. **Trappole "presente ma vuoto"**: `stop_reason` è null nel **50,1%** dei record assistant;
   `stop_sequence` valorizzato in **0 casi su 328.148**; `message.content` mai vuoto come
   contenitore ma solo il **25,2%** contiene testo.

**Sullo schema e sui parser**

5. **`INSERT OR IGNORE` + `NOT NULL` = perdita silenziosa** (verificato su SQLite 3.45.3):
   la violazione non solleva, non applica il DEFAULT, **scarta la riga**. Tutti i parser
   usano OR IGNORE. Ogni NOT NULL nuovo va accompagnato da validazione o da `ON CONFLICT DO UPDATE`.
6. **NOT NULL oggi non garantisce nulla di semantico**: la stringa vuota lo soddisfa e i
   parser scrivono `''` quando il dato manca (`chatgpt.py:314`, `claude_ai.py:335`).
   Servono `CHECK (col <> '')`.
7. **Le FOREIGN KEY dichiarate non sono applicate** — `PRAGMA foreign_keys` non è mai attivato.
8. **`tool_call_id` non è univoco**: 167.743 blocchi `tool_use` → 162.643 id distinti (**3,0%
   di duplicati**). Colonna di correlazione, mai chiave.
9. **Doppia definizione di `model_pricing`** (`jsonl.py:332` e `pricing.py:15`): su un DB
   nuovo la CLI standalone ricrea la versione vecchia.
10. **Generated column VIRTUAL promuovibili a posteriori** (verificato): si può estrarre un
    campo da JSON a colonna indicizzata su tabella già popolata, **zero migrazione dati,
    zero modifiche ai parser**. È la via per la retrocompatibilità.

**Tre bug reali, indipendenti da OTel**

11. `chatgpt.py:200` salva timestamp **naive in ora locale** — unico parser non-UTC
12. Codex: `cache_creation_tokens` **hardcodato 0** mentre `cache_write_input_tokens` esiste
13. Codex: `reasoning_output_tokens` presente e **ignorato**; `function_call_output`
    (i risultati dei tool) **mai salvati** → transcript Codex monco

---

## 3. Decisioni prese

| # | Decisione | Chi |
|---|---|---|
| D1 | **La regola NOT NULL vale per tabella**, sulla popolazione di quella tabella — non su tutte le sorgenti. Chi non ha token non scrive righe in `token_usage`, quindi lì `input_tokens` può essere NOT NULL | Max |
| D2 | **OTel mappabile in uscita con viste, in ingresso con adapter di import** (viste o API da decidere). Conseguenza: **lo schema resta nativo LAV**, non assume forma OTel | Max |
| D3 | Prima il data model, poi gli adapter. Niente si muove senza schema a posto | Max |
| D4 | **Modello ibrido colonne/JSON** con criterio "cosa interroghi vs cosa conservi"; promozione JSON→colonna via generated column quando un campo si rivela caldo | condiviso |
| D5 | Distinzione **spina dorsale** (dimensioni, PK composita, pricing, FTS, parse_state, metadata — invariate) vs **periferia** (dove si concentra il lavoro) | condiviso |
| D6 | **Grain dei token**: `token_usage` resta tabella autonoma — in Codex i token vivono in eventi `token_count` non attaccati ai messaggi, quindi l'attribuzione per-messaggio non è affidabile cross-source | sciolta dai dati |

---

## 4. Decisioni APERTE

| # | Domanda | Opzioni | Stato |
|---|---|---|---|
| **A1** | Le 6 tabelle tool | (A) 18 ALTER TABLE, schema invariato · **(B)** `tool_calls` unica + 6 view omonime | Max sta valutando |
| **A2** | Rifare il data model da zero? | (A) evoluzione incrementale · **(B)** greenfield + ri-parse + view di compatibilità | in sospeso, gate Fase 0 |
| **A3** | Il tema dei doppi (§5) | quali facce affrontare, quali lasciare | **nuovo, da istruire** |
| **A4** | Tier T1/T2 campo per campo | quali colonne, quali JSON | dopo A1/A2 |

---

## 5. Il tema dei doppi — 8 facce distinte

Non è un problema solo: sono otto, di natura diversa. Vanno separati prima di decidere.

| # | Faccia | Natura | Stato |
|---|---|---|---|
| 1 | **Stessa `session_id` sotto più `project_id`** | **By design** — PK composita. Documentato in DATA_MODEL.md §2.2 (42 figli diretti vs 40 nel roll-up dedupato) | corretto, ma genera confusione: due numeri entrambi giusti che rispondono a domande diverse |
| 2 | **`sessionId` non univoco alla sorgente** | I file subagent riusano il sessionId del padre: 10.922 file → **4.402 sessionId distinti**; 8.939 file condividono l'id | risolto da LAV-66 (id sintetici `::agent-`); la chiave vera è `(sessionId, agentId)` |
| 3 | **Tabelle senza UNIQUE** | `bash_commands` e `mcp_tool_calls` → duplicati a ogni reparse | **aperto** — LAV-74 ha dovuto fare wipe manuale |
| 4 | **UNIQUE che non deduplica** | `messages UNIQUE(session_id, project_id, uuid)` con uuid NULL; `subagent_invocations UNIQUE(…, description)` con description NULL. In SQLite NULL ≠ NULL | **aperto** |
| 5 | **UNIQUE fragile = perdita, non duplicato** | `token_usage UNIQUE(timestamp, session_id, project_id)`: due chiamate nello stesso ms collidono, una sparisce | **aperto** (toppa parziale in LAV-39) |
| 6 | **Duplicati alla sorgente** | tool_use id duplicati al 3,0%; righe assistant Cowork ripetute per content-block (usage ripetuto) | parzialmente gestito (LAV-39 dedup per `api_message_id`); l'UI fa già "first occurrence wins" |
| 7 | **Duplicati cross-host** | Stesso computer come host diversi → 152 session_id duplicati su un nodo, 128 sull'altro | risolto da LAV-68 (hostname canonico) |
| 8 | **Branch/regen non deduplicati** | ChatGPT `weight=0` (**21.355 messaggi** di rami scartati inclusi nella linearizzazione); claude.ai 202 branch point via `parent_message_uuid` — il docstring "flat chronological" è errato | **aperto** — oggi si ingeriscono rami abbandonati come se fossero conversazione |

Le facce **3, 4, 5, 8** sono quelle davvero aperte. Le prime tre si chiudono con lo schema;
la 8 richiede una decisione di prodotto: *un ramo di rigenerazione scartato fa parte della
memoria o no?*

---

## 6. La sequenza proposta

```
   A1 (tool_calls)  ─┐
                     ├─→  SUPER-SCHEMA  ─→  retrocompatibilità  ─→  migrazione  ─→  ingestion  ─→  UI
   A2 (greenfield?) ─┘     + dizionario         (view / golden)      (ri-parse)      (adapter)    (opzionale)
                            campi
```

**Fase 0 — il cancello** (prima di qualunque impegno, poche ore, utile in ogni scenario):

1. **Golden file dei 24 endpoint API** + output CLI e MCP dal server attuale.
   È la **prima rete di regressione che LAV abbia mai avuto** (oggi: nessuna test suite) —
   vale identica se si sceglie l'incrementale.
2. **Inventario delle forme SQL** in `queries.py` (2.353 righe): è lì che si concentra il
   costo reale, non nell'UI.
3. **Conteggio orfani**: quante righe del DB hanno una sorgente non più ri-parsabile.
   Se sono tante, il ri-parse non basta → si torna all'incrementale.

**Superficie misurata**: 24 endpoint API, 3 file HTML (nessun JS esterno),
`queries.py` 2.353 righe, `server.py` 1.244, `cli.py` 577, `mcp_server.py` 518.

**L'accoppiamento profondo dell'UI** (`interactions.html:2039-2144`, pairing
`tool_use`→`tool_result` fatto client-side sul JSON raw) è verso il **formato sorgente**,
non verso lo schema LAV — quindi sopravvive a qualsiasi ristrutturazione delle tabelle,
perché `messages.content` resta passthrough verbatim.

---

## 7. Cosa NON è stato deciso e resta fuori

- Nessun ticket Jira aperto, nessun commit, nessuna modifica al codice
- I tre bug (§2.11-13) sono sbloccati e indipendenti dallo schema, ma congelati su
  richiesta finché il data model non è definito
- Modello a span completo e metriche OTel materializzate: fuori scope (§C di
  [02-db-evolution-plan.md](02-db-evolution-plan.md))
- `logs_2.sqlite` e `history.jsonl` di Codex: sorgenti potenzialmente ricche, mai ispezionate
