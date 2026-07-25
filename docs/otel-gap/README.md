# Gap Analysis — LAV ↔ OpenTelemetry GenAI Semantic Conventions

> Analisi del divario tra ciò che i 5 importer di LAV catturano oggi, ciò che i
> **file sorgente contengono davvero**, e ciò che le semantic conventions
> `gen_ai.*` di OpenTelemetry richiedono. Prodotta il 2026-07-24.
> Bozza in review — non ancora legata a un ticket Jira (epica correlata: LAV-50).

## 🅿️ Stato: progetto parcheggiato

Valutato troppo grande per essere avviato ora. **Punto d'ingresso per riprenderlo:
[00-stato-e-decisioni.md](00-stato-e-decisioni.md)**, che contiene l'indice completo,
le decisioni prese e quelle aperte.

## File

| File | Contenuto |
|---|---|
| **[00-stato-e-decisioni.md](00-stato-e-decisioni.md)** | **Punto d'ingresso**: stato, decisioni, indice completo, sequenza per ripartire |
| [01-matrix.md](01-matrix.md) | Matrice completa: attributo OTel × (schema LAV, claude_code, codex, cowork, chatgpt, claude_ai) con 3 stati per cella + punti di forza LAV oltre OTel |
| [02-db-evolution-plan.md](02-db-evolution-plan.md) | Piano di evoluzione del DB: must have / nice to have, DDL proposto, migrazioni, decisioni aperte |
| [03-adapter-changes.md](03-adapter-changes.md) | Proposta di variazione per ciascuno dei 5 adapter, campo per campo ⚠️ *4 proposte invalidate da [07 §1](07-numeri-e-insidie.md)* |
| [04-schema-current.md](04-schema-current.md) | Schema attuale: mappa grafica, cosa regge, cosa no |
| [05-censimento.md](05-censimento.md) | Censimento empirico: presenza campi per grain e annata |
| [06-findings-operativi.md](06-findings-operativi.md) | Bug, vulnerabilità e debiti **aggredibili da soli** |
| **[07-numeri-e-insidie.md](07-numeri-e-insidie.md)** | ⚠️ Numeri misurati e proposte smentite — **leggere prima di implementare** |
| [schema-current.html](schema-current.html) · [schema-before-after.html](schema-before-after.html) | Diagrammi navigabili in browser |

## La domanda a cui risponde

Non solo "*cosa LAV non importa*", ma la distinzione a tre vie che cambia le
decisioni:

- ✅ **nel file E letto da LAV** — già coperto
- 🟡 **nel file MA LAV cieco** — *debito di parsing*: recuperabile domani con una
  modifica agli adapter, senza toccare le sorgenti
- ⚪ **assente nel file sorgente** — *limite del provider*: nessun parser potrà
  mai recuperarlo; definisce il tetto di conformità OTel onestamente promettibile

## Metodo (verificato, non a memoria)

**Tre workflow multi-agente, 30 agenti in totale** (2026-07-24/25):

1. **Gap analysis** (13 agenti) — per ogni sorgente un ispettore ha fatto l'inventario
   strutturale dei file grezzi, e un **refuter adversariale** ha tentato di confutare ogni
   assenza dichiarata su un campione più ampio (ne ha ribaltate ~20%)
2. **Censimento nullability** (6 agenti) — scansione **integrale** con conteggi esatti per
   grain e per annata, tre stati distinti (assente / null / vuoto) → [05](05-censimento.md)
3. **Audit di isolamento** (5 agenti) — vie di scrittura, servizi, topologia di installazione,
   rete → [06 §E](06-findings-operativi.md)
4. Più una verifica finale di completezza documentale → [07](07-numeri-e-insidie.md)

Corpus **del censimento** (i numeri autorevoli; la gap analysis iniziale girava su un
campione leggermente più ristretto):

| Sorgente | Corpus |
|---|---|
| claude_code | **12.933 file / 4,87 GB / 799.367 record**, 0 righe malformate |
| codex | 205 file rollout / 929 MB — **tutte** le 43.873 righe |
| cowork | 106 file audit.jsonl / 59 MB — **tutte** le 10.962 righe |
| chatgpt | 4 export (2023-05 → 2025-12) / 780 MB / 148.214 messaggi + file fratelli |
| claude_ai | 8 file / 147 MB / 1.515 conv / 34.983 content block |

La passata scettica ha ribaltato ~20% delle assenze dichiarate al primo giro
(es. TTFT in Cowork, endpoint URL negli api_error Claude Code, token count in
app_pairing ChatGPT) — le assenze rimaste in matrice sono quindi verificate a
livello full-corpus, salvo dove marcate *(plausible)*.

Copertura parser LAV verificata con grep + lettura codice (file:line in
[01-matrix.md](01-matrix.md)). Report grezzi completi dei 13 agenti:
`internal_docs/otel-gap-raw/` (gitignored).

## Riferimento spec

- Repo: **open-telemetry/semantic-conventions-genai** @ `main`, consultato
  **2026-07-24** (nessuna release taggata: le convenzioni GenAI sono tutte
  status *Development*; stabili solo gli attributi core presi in prestito:
  `error.type`, `server.address`, `server.port`, `exception.*`)
- Core semconv collegate: v1.43.0 (2026-07-03)
- Rinomine confermate: `gen_ai.system` → `gen_ai.provider.name`;
  `prompt/completion_tokens` → `input/output_tokens`; eventi
  prompt/completion → attributi `gen_ai.input.messages` / `gen_ai.output.messages`
- **Novità 2026 rilevanti per LAV**: `gen_ai.usage.cache_creation.input_tokens`,
  `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.reasoning.output_tokens`,
  `gen_ai.request.reasoning.level`, `gen_ai.conversation.compacted`,
  `gen_ai.request.stream`, `gen_ai.response.time_to_first_chunk`

## I 5 titoli dell'analisi

1. **I parametri di sampling non esistono in NESSUNA sorgente.** temperature,
   top_p/k, max_tokens, penalties, seed, stop_sequences: 0 occorrenze
   strutturali su ~6,7 GB di log complessivi. È un limite dei provider, non di
   LAV — la conformità OTel su `gen_ai.request.*` (sampling) è irraggiungibile
   da questi log e va dichiarata tale.
2. **I segnali di esito e di errore esistono quasi ovunque e LAV li butta via.**
   `stop_reason` (Claude Code: 160k+ occorrenze), `finish_details` (ChatGPT,
   con `interrupted` ×1.583), telemetria errori/retry/rate-limit completa in
   Claude Code, Codex e Cowork. Tutto 🟡: puro debito di parsing.
3. **Le durate per-call esistono più di quanto credessimo.** Cowork ha
   `ttft_ms`/`duration_api_ms` per run; Codex ha `duration_ms` +
   `time_to_first_token_ms` per turno e durate reali per tool; claude.ai ha
   coppie start/stop per 17.700 content block; Claude Code ha durate turno,
   tool e hook. Solo la latenza della *singola chiamata modello* resta assente
   quasi ovunque.
4. **LAV era avanti sullo standard sui cache token** — e ora OTel 2026 li ha
   standardizzati: le colonne `cache_creation_tokens`/`cache_read_tokens`
   mappano 1:1 sui nuovi attributi. Restano oltre-OTel: costo query-time con
   validità temporale prezzi, classificazione AI, FTS5, 4 dimensioni di filtro,
   roll-up subagent.
5. **Due bug/incoerenze indipendenti da OTel** emersi dalla verifica:
   (a) `chatgpt.py` salva timestamp naive in ora locale
   (`datetime.fromtimestamp`, chatgpt.py:200) — unico parser non-UTC;
   (b) per Codex `cache_creation_tokens` è hardcodato 0 (jsonl.py:1527) ma
   `cache_write_input_tokens` esiste nei rollout recenti; inoltre
   `token_usage.input_tokens` ha semantica diversa per sorgente (Codex: netto
   dei cached; OTel lo vuole inclusivo) — va deciso e documentato.
