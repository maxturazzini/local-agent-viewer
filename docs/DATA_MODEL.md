# LAV — Modello dati delle conversazioni

> Documento di riferimento. Spiega come LocalAgentViewer (LAV) rappresenta le
> conversazioni dei vari agenti AI in un'unica base dati, perché esistono **due
> modelli diversi** di conversazione, e cosa restituiscono le query principali.
>
> Tutti i numeri di esempio in questo documento sono **reali**, estratti dal DB
> di test usato durante lo sviluppo di questa feature.

---

## 1. Le 5 sorgenti e le 4 dimensioni di filtro

LAV legge i log di agenti diversi e li normalizza in **un solo database SQLite**.
Ogni conversazione viene etichettata con la sua sorgente (`session_sources.source`).

### Le 5 sorgenti

| Sorgente (`source`) | Da dove | Prefisso del `session_id` |
|---|---|---|
| `claude_code` | Claude Code CLI (JSONL locali) | UUID nudo — es. `3e8d411d-4f4e-...` |
| `codex_cli` | Codex CLI | `codex:` + uuid |
| `cowork_desktop` | Cowork (Claude Desktop, app) | `cowork:` + uuid |
| `chatgpt` | export ChatGPT | `chatgpt:` + id |
| `claude_ai` | export account claude.ai | `claudeai:` + id |

> Nota sul DB di test: contiene 4 delle 5 sorgenti (manca `chatgpt`). Conteggi
> reali delle interazioni nel DB di test:

| Sorgente | Interazioni |
|---|---|
| `claude_code` | 4144 |
| `claude_ai` | 1515 |
| `cowork_desktop` | 96 |
| `codex_cli` | 34 |
| **Totale** | **5789** |

Il prefisso del `session_id` è il modo più rapido per riconoscere la sorgente a
colpo d'occhio: `claude_code` usa l'UUID nudo, tutte le altre sono prefissate.

### Le 4 dimensioni di filtro

Ogni query (dashboard, API, CLI) può filtrare su 4 assi **indipendenti**:

| Dimensione | Tabella | Significato | Esempi nel DB di test |
|---|---|---|---|
| **Project** | `projects` | quale codebase / cartella | `miniMe`, `viewer`, `outputs`, ... (106 progetti) |
| **User** | `users` | quale persona | `maxturazzini` |
| **Host** | `hosts` | quale macchina | `macChia`, `Mac`, `cloud` |
| **Source** | `session_sources` | quale agente | `claude_code`, `cowork_desktop`, ... |

I filtri si combinano liberamente (AND). La chiave primaria di una interazione è
**composta**: `interactions(session_id, project_id)`. Lo stesso `session_id` può
quindi comparire sotto più progetti (vedi §2.2).

---

## 2. I DUE modelli di conversazione (e perché differiscono)

Agenti diversi loggano la stessa "conversazione" in modi strutturalmente diversi.
LAV li riconcilia con **due strategie**:

| Modello | Sorgente tipica | Relazione | `parent_session_id` |
|---|---|---|---|
| **MERGED** (fusione) | `cowork_desktop` | una conversazione = una riga | sempre `NULL` |
| **MASTER / DERIVED** | `claude_code` | un master + N subagent | i figli puntano al master |

### 2.1 Modello MERGED — Cowork

**Il problema.** Cowork logga **una sola conversazione su due piani**:

1. un "guscio" identificato dalla folder-uuid, che contiene i **turni umani**
   (il dialogo visibile all'utente);
2. una **sessione agente interna** che contiene l'esecuzione completa e i **token
   reali** (tool, thinking, risposte).

Sono lo **stesso** dialogo, spezzato in due file. Tenerli separati darebbe due
righe monche: una con i prompt umani ma senza costi, una con i costi ma senza il
contesto umano.

**La soluzione.** `parse_cowork_sessions` **fonde** i due piani sotto la
folder-uuid (id prefissato `cowork:`):

- ri-etichetta ogni evento al master folder-uuid;
- **scarta** i turni duplicati del guscio umano;
- titola la riga con il **primo prompt umano**;
- token e costo sono quelli **reali** della sessione interna.

Risultato: **nessun rapporto padre/figlio** per Cowork. Una conversazione = una
riga, `parent_session_id = NULL`, il transcript contiene l'intero dialogo
umano + assistente.

**Esempio reale — `cowork:dc3b5fe9-d23d-4f6b-bb02-d417e1d8128f`**

| Campo | Valore |
|---|---|
| `session_id` | `cowork:dc3b5fe9-d23d-4f6b-bb02-d417e1d8128f` |
| `parent_session_id` | `NULL` |
| Titolo (`display`) | *"voglio usare pgvis"* (primo prompt umano) |
| Messaggi nel transcript | **210** (78 utente + 132 assistente) |
| `total_tokens` | **7.520.969** |
| Costo | **$5,1158** |
| `children` (in detail) | **0** |

Tutto in **una sola riga**: il dialogo umano e l'esecuzione dell'agente sono già
uniti.

### 2.2 Modello MASTER / DERIVED — Claude Code

**Il caso.** Claude Code lancia **subagent reali**: ognuno vive nel suo file
`agent-*.jsonl`. Questi NON sono duplicati da scartare — sono unità di lavoro
distinte con i loro token. Sono già collegati al master tramite
`parent_session_id`.

**La soluzione.** LAV li tiene come righe separate ma li **raggruppa** sotto il
master:

- la **lista raggruppata** mostra **solo i master** (`parent_session_id IS NULL`,
  oppure il caso self-parent in cui il master punta a se stesso);
- il costo e i token del master vengono **rollati-up** su tutto l'albero dei
  subagent (ricorsivo, profondità arbitraria);
- viene aggiunto `derived_count` = numero di discendenti.

**Esempio reale — master `3e8d411d-4f4e-417f-afff-4e03654b975a`** (progetto `id=1`)

| Vista | `total_tokens` | Costo | `derived_count` |
|---|---|---|---|
| **Flat** (solo il master, per-sessione) | 7.870.243 | $9,0065 | 0 |
| **Grouped** (master + tutto l'albero) | **14.680.355** | **$21,102** | **40** |

In modalità raggruppata il master "assorbe" il costo dei suoi subagent: da
$9,00 (solo lui) a $21,10 (lui + i 40 derivati). Il titolo resta quello del
master: *"Claude CLI Update Lock Files Bug Fix"*.

> **Nota su 40 vs 42.** `get_interaction_children` (vedi §4) è *project-agnostico*
> e restituisce **42** figli diretti, perché lo stesso `session_id` di un
> subagent può essere materializzato sotto **più progetti** (chiave primaria
> composta: qui il subagent compare sotto `project_id=1` e `project_id=65`). Il
> roll-up della lista raggruppata, invece, deduplica i doppioni cross-project con
> un visited-set su `(session_id, project_id)` e conta ogni nodo **una volta**:
> da qui `derived_count=40`. Le due cifre sono entrambe corrette — rispondono a
> domande diverse ("quante righe figlie esistono" vs "quanti subagent unici
> sommo nel costo").

---

## 3. Lista raggruppata (grouped) vs piatta (flat)

`get_interactions_list(..., grouped=True|False)` ha due modalità:

| | `grouped=True` (default, dashboard) | `grouped=False` (flat) |
|---|---|---|
| Righe mostrate | solo i master top-level | **ogni** sessione |
| Costo / token | rollati-up su master + discendenti | per-sessione |
| `derived_count` | numero discendenti | sempre `0` |
| Filtro parent | `parent_session_id IS NULL` o self-parent o orfano promosso | nessuno |

**Cosa conta come "top-level" in grouped:** una riga è top-level se
`parent_session_id IS NULL`, **oppure** se punta a se stessa (self-parent),
**oppure** se il suo parent non esiste da nessuna parte (*orphan promotion* — così
nessuna riga sparisce). Il lookup del parent è **project-agnostico**: si confronta
solo sul `session_id` (UUID globalmente unico), perché un master Cowork vive in un
progetto mentre i suoi eventi possono finire in progetti inferiti diversi.

Per Cowork la distinzione grouped/flat è quasi ininfluente: non avendo figli, una
conversazione Cowork è già una singola riga top-level in entrambe le modalità.

**Importante — search / CLI / MCP restano FLAT.** La ricerca testuale (`lav
search`), la CLI e gli strumenti MCP lavorano **sempre** in modalità piatta: ogni
sessione è un risultato a sé, con il suo costo per-sessione. Il raggruppamento è
una funzione di **presentazione** della lista nella dashboard, non un modo di
indicizzare i dati.

---

## 4. Cosa restituiscono le query principali

### `get_interactions_list(...)`

Restituisce `{ interactions, total, limit, offset }`. Ogni elemento di
`interactions` include, oltre ai campi base (`session_id`, `project_id`, `model`,
`message_count`, `display`, `summary`, ...):

- `client_source` — la sorgente (`claude_code`, `cowork_desktop`, ...);
- `cost_usd` — costo calcolato a query-time via LEFT JOIN su `model_pricing`
  (mai materializzato);
- `total_tokens` — in grouped, già rollato-up sull'albero;
- **`derived_count`** — quanti subagent/discendenti sono stati sommati
  (`0` in flat, o quando il master non ha figli).

### `get_interaction_children(conn, session_id, project_id=None)`

Restituisce i **figli diretti** (un livello) di un master, ordinati dal più
vecchio. Ogni figlio ha: `session_id`, `project_id`, `summary`, `display`,
`agent_id`, `timestamp`, `total_tokens`, `message_count`, `cost_usd` (per-sessione).

È **project-agnostico**: matcha solo su `parent_session_id`, quindi può
restituire figli che vivono in progetti diversi dal master (vedi nota 40 vs 42 in
§2.2). Il parametro `project_id` è accettato per compatibilità ma **non** usato
come filtro.

### `get_interaction_detail(conn, session_id, project_id)`

Restituisce il transcript completo (`messages`), più:

- `interaction.children` = output di `get_interaction_children` (i derivati);
- `interaction.parent_interaction` se la riga ha un parent;
- `interaction.cost_usd` per-sessione.

Per Cowork: `children` è vuoto e il transcript contiene **già** l'intero dialogo
umano + assistente (210 messaggi nell'esempio). Per un master Claude Code:
`children` elenca i subagent derivati da espandere nel dettaglio.

---

## 5. Riepilogo in una frase

- **Cowork = fusione**: una `audit.jsonl` = una conversazione = una riga
  (`parent=NULL`), token e costo reali, titolo dal primo prompt umano.
- **Claude Code = master/derived**: i subagent restano righe separate ma la lista
  raggruppata mostra solo il master con costo/token rollati-up su tutto l'albero e
  un `derived_count`; il dettaglio espande i derivati.
- **search / CLI / MCP** restano sempre piatti (una sessione = un risultato).
