---
document: Feasibility study — Day View / Gantt + worktime metrics in LAV
context: Valutare se promuovere il tool standalone artifacts/lav-gantt/ a feature di prima classe di LocalAgentViewer (LAV), come endpoint API + tab dashboard + subcommand CLI. Scritto dall'interno del repo LAV (lettura CLAUDE.md, CHANGELOG, schema, queries.py).
date: 2026-05-17
author: miniMe
status: draft
tags: [lav, claude-code, analytics, gantt, worktime, feasibility]
related:
  - artifacts/lav-gantt/CLAUDE.md
  - /Users/maxturazzini/claude_projects/local-agent-viewer/CLAUDE.md
  - /Users/maxturazzini/claude_projects/local-agent-viewer/docs/CHANGELOG.md
---

# Feasibility — Day View dentro LAV (v2)

## TL;DR

**Fattibile e raccomandato.** Stima realistica **10-13h** spalmate in 1 ticket epic + 4 task. La gran parte dell'ossatura serve già:
- Il pattern `MAX(messages.timestamp)` per `end_ts` è stato introdotto 3 giorni fa da **LAV-46** (river session fix) ed è già riusabile via `export_sessions()`
- Il pattern "nuova tab analytics" è stato consolidato 6 settimane fa da **Cost Intelligence (v0.1.4)** — copy/paste dal layout esistente
- I subagent NON sono "fuori" come pensavo: `subagent_invocations` ha timestamp dedicato, e `interactions.parent_session_id + agent_id` referenziano i subagent come sessioni figlie

**Scope MVP**: 2 metriche worktime (active + assistant wall-clock), Gantt giornaliero, no anonimizzazione. **Out of scope v1**: span/latency sum (le 2 metriche "gonfie"), toggle anon, subagent come righe Gantt separate.

**Rischio principale**: overlap con `get_work_pattern_stats` (Cost Intelligence) — verificare se ha senso fondere o tenere separati. Decisione di design da prendere PRIMA del coding.

---

## 1. Cosa è lav-gantt oggi

Vedi `artifacts/lav-gantt/CLAUDE.md` per dettagli. Sintesi tecnica:

| Aspetto | Implementazione attuale |
|---|---|
| Truth source | JSONL diretti in `~/.claude/projects/**/*.jsonl` |
| Server | `serve.py` HTTP stdlib su `:8770` (no deps) |
| Endpoint | `GET /api/day?date=YYYY-MM-DD` → bundle JSON {rows, projects, worktime, concurrency} |
| Frontend | `dynamic_gantt.html` vanilla JS + SVG Gantt + canvas concorrenza |
| CLI text-only | `agent_worktime.py YYYY-MM-DD` → stampa le 4 metriche |
| Hardcoded | CEST (+2h), `PROJECT_LABELS` dict, SSH a minimacs per summary |
| Limiti noti | Solo Claude Code (no codex/chatgpt), solo macchina locale, subagent esclusi |

Le 4 metriche worktime:
- **Span sum** — gonfia, ~13× rispetto al wall-clock reale (esempio 12/05: 36h vs 2.7h)
- **Latency sum** — somma cappata `Δ(user→assistant)` con cap 10min
- **Active wall-clock** — unione finestre con gap < 5min, deduplicata
- **Assistant wall-clock** — solo intervalli in cui `assistant` rispondeva, deduplicato

Le ultime due sono "le oneste". Le prime due sono utili come overlay per capire il discostamento "consumo apparente vs reale".

---

## 2. Architettura LAV dal di dentro

### 2.1 Schema DB (rilevante per il Gantt)

Dati timing già presenti, granulari per ogni messaggio. Da [`lav/parsers/jsonl.py:85-120`](file:///Users/maxturazzini/claude_projects/local-agent-viewer/lav/parsers/jsonl.py#L85):

```sql
-- interactions: 1 riga per sessione
CREATE TABLE interactions (
    session_id TEXT NOT NULL,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id),
    host_id INTEGER NOT NULL DEFAULT 1 REFERENCES hosts(id),
    timestamp TEXT NOT NULL,           -- start (primo messaggio)
    display TEXT, summary TEXT,
    message_count INTEGER DEFAULT 0,
    parent_session_id TEXT,            -- ← per subagent linking
    agent_id TEXT,                     -- ← per subagent linking
    PRIMARY KEY (session_id, project_id)
);

-- messages: granulare, indicizzato per timestamp
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT, project_id INTEGER,
    user_id INTEGER, host_id INTEGER,
    type TEXT NOT NULL,                -- 'user' | 'assistant'
    content TEXT,
    timestamp TEXT,                    -- ISO8601, indicizzato (idx_msg_timestamp)
    tokens_in/out INTEGER DEFAULT 0,
    model TEXT
);

-- subagent_invocations: timestamp di invocazione
CREATE TABLE subagent_invocations (
    timestamp TEXT NOT NULL,
    session_id TEXT, project_id INTEGER,
    subagent_type TEXT NOT NULL,
    description TEXT, prompt TEXT, model TEXT,
    run_in_background INTEGER
);
```

**Conseguenza**: tutti i dati necessari per il Gantt ci sono. Per `end_ts` di sessione → derivare via `MAX(messages.timestamp)`. Per subagent → `subagent_invocations` ha già il timestamp (puntiforme) e in più le sessioni figlie sono in `interactions` con `parent_session_id != NULL`.

### 2.2 Pattern già esistenti riusabili

| Cosa | Dove | Note |
|---|---|---|
| 4D filtering (project/user/host/source) | `build_filters()` [queries.py:23](file:///Users/maxturazzini/claude_projects/local-agent-viewer/lav/queries.py#L23) | Riuso identico |
| Date range query | Pattern `start → >=`, `end → <= + T23:59:59` ovunque | Riuso identico |
| **`MAX(messages.timestamp)` per end di sessione** | `export_sessions()` [queries.py:1814](file:///Users/maxturazzini/claude_projects/local-agent-viewer/lav/queries.py#L1814) | **Pattern LAV-46**, già blindato per river sessions cross-midnight |
| `LEFT JOIN session_sources` (filtro per source) | `_join_session_sources()` [queries.py:64](file:///Users/maxturazzini/claude_projects/local-agent-viewer/lav/queries.py#L64) | Riuso identico |
| Aggregazione per `DATE(timestamp)` | `get_timeline_stats()` [queries.py:604](file:///Users/maxturazzini/claude_projects/local-agent-viewer/lav/queries.py#L604) | Già aggrega skills/subagents/mcp per giorno |
| Aggregazione per ora (`strftime('%H')`) | `get_files_stats()` [queries.py:232](file:///Users/maxturazzini/claude_projects/local-agent-viewer/lav/queries.py#L232) | Pattern già consolidato per heatmap orarie |
| Nuova tab dashboard | Cost Intelligence (v0.1.4), [dashboard.html:1307+](file:///Users/maxturazzini/claude_projects/local-agent-viewer/lav/static/dashboard.html#L1307) | `data-tab="X"` + `<div id="X" class="tab-content">` + click listener |
| Nuovo endpoint `GET /api/...` | `server.py` handler (vedi `_runtime_config` role gating) | Per role `both` only |
| Nuovo subcommand CLI | `cli.py:415+` add_parser + cmd_X + `_output(data, fmt)` | ~25 righe |

### 2.3 Cose già esistenti che POTREBBERO sovrapporsi

| Funzione | Cosa fa | Overlap con Day View? |
|---|---|---|
| `get_work_pattern_stats` [queries.py:1370](file:///Users/maxturazzini/claude_projects/local-agent-viewer/lav/queries.py#L1370) | Pattern hour/day/complexity per Cost Intelligence | **Parziale**: lavora su `messages.timestamp` aggregato per hour/dayofweek. Day View invece zooma su singolo giorno con dedup wall-clock. Compatibili, non sostitutivi |
| `get_efficiency_metrics` [queries.py:1603](file:///Users/maxturazzini/claude_projects/local-agent-viewer/lav/queries.py#L1603) | Cache trend, cost-per-call, efficienza | **Nessuno**: focus su cost/cache, non su tempo |
| `get_timeline_stats` [queries.py:604](file:///Users/maxturazzini/claude_projects/local-agent-viewer/lav/queries.py#L604) | Counter giornaliero di skills/subagents/mcp | **Parziale**: stesso asse temporale, ma counter aggregati per data, non timeline intra-giorno. Compatibili |

**Decisione di design**: Day View è feature ortogonale a Cost Intelligence. Non rifondere, non sostituire. Lasciare le altre tab come sono.

### 2.4 Workflow mandatory

Da `CLAUDE.md` LAV (LAV-45 ha formalizzato):

1. Jira ticket → In Progress
2. Plan → approval
3. Dev → test e2e manuale (no test suite)
4. Update CLAUDE.md + README + .env.example (se nuove vars) + **`docs/CHANGELOG.md` (sempre, prefisso ticket)**
5. Commit con ticket ref (`LAV-XX: ...`)
6. Push `origin/main`
7. Deploy minimacs con decision tree (vedi §6)
8. Jira comment + Done

**Two-environment awareness**:
- **macChia** (dev): role `agent`, `:8764` serve solo `/api/health|info|export`. **No dashboard.** Per testare UI: spawn temp `lav-server` su `:8765` con role monkey-patch `lav.server._runtime_config`
- **minimacs.local** (prod): role `both`, dashboard completo su `:8764`

---

## 3. Gap analysis dettagliata

| # | Componente | Esiste? | Effort se manca |
|---|---|---|---|
| 1 | Timestamp granulare per messaggio | ✅ `messages.timestamp` ISO8601 + idx | 0 |
| 2 | Tipo messaggio user/assistant | ✅ `messages.type` | 0 |
| 3 | Aggregato sessione | ✅ `interactions` | 0 |
| 4 | `end_ts` derivato (pattern LAV-46) | ✅ in `export_sessions()` | 0 — riusare il LEFT JOIN |
| 5 | 4D filter helper | ✅ `build_filters()` | 0 |
| 6 | SQL per fetch timestamp grezzi del giorno | ❌ | ~30min — query semplice |
| 7 | Math worktime (active + assistant wall-clock dedup) | ❌ | ~2h — port da `agent_worktime.py` |
| 8 | Math span / latency sum (opzionale v1) | ❌ | ~1h se incluso |
| 9 | Calcolo curva concorrenza | ❌ | ~1h — algoritmo sweep line |
| 10 | Endpoint `GET /api/day` | ❌ | ~1h — pattern handler esistente |
| 11 | Tab Day View | ❌ | ~3h — copy pattern Cost Intelligence + port SVG Gantt |
| 12 | CLI `lav day` | ❌ | ~1h — pattern `add_parser` consolidato |
| 13 | Subagent come righe Gantt separate | ❌ | ~2h se incluso (linkare via `parent_session_id`) |
| 14 | Timezone-aware (no CEST hardcoded) | ❌ | ~30min — TZ del server + `Intl` browser |
| 15 | Anonimizzazione progetti | ❌ | ~1h se incluso |
| 16 | Test e2e manuale | ❌ | ~2h — golden test su 12/05 vs JSON in `artifacts/lav-gantt/` |
| 17 | Docs (CLAUDE.md ✗ / README ✓ / CHANGELOG ✓) | parziale | ~1h |

**MVP** = items 6, 7, 9, 10, 11, 12, 14, 16, 17 → **stima 11-13h**.

**v1.5** (subagent + anon + span/latency) = +4-5h.

---

## 4. Proposta di design

### 4.1 Backend

**Nuova funzione in `lav/queries.py`** (in sezione nuova `# DAY VIEW / WORKTIME`):

```python
def get_day_bundle(conn, date_str: str,
                   project_id=None, user_id=None, host_id=None,
                   client_source=None) -> dict:
    """
    Returns full bundle for the Day View tab.

    - sessions: 1 row per session active on this day (Gantt rows)
    - worktime: {active_wallclock_min, assistant_wallclock_min}
    - concurrency: sweep-line of active sessions over the day (1pt/min)
    - meta: total sessions, total projects, peak concurrency
    """
    day_start = f"{date_str}T00:00:00"
    day_end = f"{date_str}T23:59:59"

    # Step 1: select sessions active on this day (river-safe, LAV-46 pattern)
    where, params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        client=client_source, table_alias='c'
    )
    sessions = run_query(conn, f"""
        SELECT
            c.session_id, c.project_id,
            c.timestamp AS start_ts,
            m.last_msg_ts AS end_ts,
            c.message_count,
            c.summary, c.display,
            p.name AS project_name,
            COALESCE(ss.source, 'unknown') AS source
        FROM interactions c
        LEFT JOIN (
            SELECT session_id, project_id, MAX(timestamp) AS last_msg_ts
            FROM messages
            WHERE timestamp >= ? AND timestamp <= ?
            GROUP BY session_id, project_id
        ) m ON m.session_id = c.session_id AND m.project_id = c.project_id
        LEFT JOIN session_sources ss ON ss.session_id = c.session_id AND ss.project_id = c.project_id
        LEFT JOIN projects p ON p.id = c.project_id
        WHERE m.last_msg_ts IS NOT NULL
          {where.replace('WHERE', 'AND') if where else ''}
        ORDER BY c.timestamp
    """, [day_start, day_end] + params)

    # Step 2: fetch granular timestamps per session for worktime math
    # Step 3: compute active/assistant wall-clock (port from agent_worktime.py)
    # Step 4: sweep line for concurrency curve
    ...
```

**Nuovo endpoint in `lav/server.py`** (sezione handler, pattern uguale a `/api/cost-intelligence`):

```python
elif path == '/api/day':
    if _runtime_config.get('role') == 'agent':
        return _send_404()  # dashboard endpoint, role both only
    date = params.get('date', [None])[0]
    if not date or not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return _send_400("date required as YYYY-MM-DD")
    with sqlite3.connect(UNIFIED_DB_PATH) as conn:
        conn.execute("PRAGMA query_only=ON")
        bundle = get_day_bundle(conn, date, **filters_from_query(params))
    self._send_json(bundle)
```

### 4.2 Frontend

**Nuovo tab in `lav/static/dashboard.html`** seguendo pattern Cost Intelligence:

```html
<!-- dentro #dashboardTabs -->
<div class="tab" data-tab="dayview">Day View</div>

<!-- nuovo blocco -->
<div id="dayview" class="tab-content">
  <div class="dayview-controls">
    <button id="day-prev">←</button>
    <input type="date" id="day-picker">
    <button id="day-next">→</button>
    <span id="day-meta"></span>
  </div>
  <div id="day-metrics" class="metric-cards"></div>
  <div id="day-gantt"></div>
  <div id="day-concurrency"></div>
</div>
```

**Nuovo file `lav/static/day.js`** — port di `artifacts/lav-gantt/dynamic_gantt.html`:
- Rimuovere chiamata a `serve.py` custom → fetch `/api/day?date=...`
- Rimuovere timezone hardcoded → usare `Intl.DateTimeFormat`
- Rimuovere `PROJECT_LABELS` → usare `project_name` dal bundle
- Mantenere: SVG Gantt rows, curva concorrenza sotto, tooltip

Vanilla JS, niente nuove deps. Coerente con il dashboard esistente.

### 4.3 CLI

**Nuovo subcommand in `lav/cli.py`** (pattern consolidato, ~25 righe):

```python
# in main() dopo gli altri add_parser
p_day = sub.add_parser("day", help="Worktime metrics + sessions for a given day")
p_day.add_argument("date", help="YYYY-MM-DD")
_add_common_args(p_day)
_add_format_arg(p_day)
p_day.set_defaults(func=cmd_day)

def cmd_day(args, conn):
    from lav.queries import get_day_bundle
    bundle = get_day_bundle(conn, args.date,
                            project_id=args.project_id, user_id=args.user_id,
                            host_id=args.host_id, client_source=args.source)
    _output(bundle, args.format)
```

### 4.4 Dove vive il codice originale

`artifacts/lav-gantt/` rimane come **archivio storico** dopo il merge (con README che punta a LAV). I 3 file Python diventano "fonte di logica da copiare/adattare", NON di import. LAV mantiene il vincolo zero-deps core.

---

## 5. Cose da NON portare (scope decisions)

| Feature lav-gantt | Verso LAV | Perché no/sì |
|---|---|---|
| Hardcoded CEST | ❌ | LAV è multi-tenant in prospettiva (anche cloud). TZ-aware obbligatorio |
| `PROJECT_LABELS` dict | ❌ | LAV ha `projects` table normalizzata |
| SSH a minimacs per summary | ❌ | In LAV tutto è locale al server (DB già locale) |
| Anonimizzazione toggle | ❌ v1 | Eventuale v2; per ora non MVP |
| Span sum / latency sum | ❌ v1 | Le "gonfie" sono utili come overlay, ma confondono se mostrate sempre. v1 solo le 2 oneste, v1.5 le altre 2 come tab "advanced" |
| Subagent come righe Gantt separate | ❌ v1 | Disegnabili in v1.5 via `parent_session_id` linking. v1: contati ma non visualizzati |
| Curva concorrenza separata | ✅ | Mantenere — utile insight ortogonale al Gantt |
| Date picker + nav back/forward | ✅ | Essenziale per "scrolling temporale" |

---

## 6. Workflow di esecuzione

### 6.1 Jira (epic + 4 task)

| Ticket | Titolo | Stima |
|---|---|---|
| LAV-XX | **EPIC**: Day View — Gantt giornaliero + worktime metrics | — |
| LAV-Xa | Backend: `get_day_bundle()` + `GET /api/day` + math active/assistant wall-clock | 4-6h |
| LAV-Xb | Frontend: tab Day View + `day.js` + SVG Gantt + curva concorrenza | 3-4h |
| LAV-Xc | CLI: `lav day YYYY-MM-DD` | 1h |
| LAV-Xd | Docs: CLAUDE.md (se architettura cambia), README (user-facing), CHANGELOG (sempre) | 1h |

**Ticket separati post-MVP** (non parte di v1):
- LAV-Yy: Subagent rows in Day View (linking via `parent_session_id`)
- LAV-Yz: Span / latency sum overlay metrics

### 6.2 Dev workflow

1. Plan approval su LAV-Xa → coding backend
2. Test backend: query SQLite locale (macchia), confronto con bundle JSON in `artifacts/lav-gantt/2026-05-12_lav_bursts*.json`
3. Plan approval LAV-Xb → coding frontend
4. Test UI: spawn temp `lav-server` su `:8765` con role override → browser locale
5. Plan approval LAV-Xc → coding CLI
6. Test CLI: `lav day 2026-05-12 --format json | jq` confronto con golden JSON
7. Plan approval LAV-Xd → docs + CHANGELOG
8. Commit unico o per ticket (LAV permette multi-ticket commit se coupled)
9. Push `origin/main`

### 6.3 Deploy minimacs (da decision tree CLAUDE.md)

Cambi previsti in v1:
- `lav/queries.py` (nuove funzioni) → restart `python.*-m lav.server`
- `lav/server.py` (nuovo endpoint) → restart
- `lav/static/dashboard.html` + nuovo `lav/static/day.js` → solo `git pull` + hard refresh
- `lav/cli.py` (subcommand) → no restart, pyproject.toml NON cambia

Sequenza:
```bash
ssh minimacs.local 'cd ~/claude_projects/local-agent-viewer && git pull'
ssh minimacs.local 'kill $(pgrep -f "python.*-m lav.server")'
# KeepAlive auto-restart
ssh minimacs.local 'curl -s http://localhost:8764/api/day?date=2026-05-12 | jq .meta'
```

Verifica: aprire `http://minimacs.local:8764` → tab Day View → testare 3 date (oggi, 12/05, una random storica).

---

## 7. Rischi tecnici

| # | Rischio | Probabilità | Impatto | Mitigazione |
|---|---|---|---|---|
| 1 | Drift fra logica worktime lav-gantt vs port LAV | Media | Basso | Golden test: bundle LAV per 12/05 deve matchare `artifacts/lav-gantt/2026-05-12_lav_bursts_v2.json` entro ±1% |
| 2 | Performance query su giornata pesante (100+ sessioni, 5k+ messaggi) | Bassa | Basso | Index `idx_msg_timestamp` esistente. Test su 12/05 (~2566 messaggi macchia DB) come benchmark |
| 3 | Cross-machine: macchia non visibile su minimacs finché collector pull | Media | Medio | Comportamento già noto da LAV-46. Documentare nel banner UI. Se Max guarda Day View su minimacs poco dopo sessione su macchia, mostra "ultimo sync: HH:MM" |
| 4 | Timezone errato per chi non è UTC+2 | Bassa | Basso | TZ server backend + `Intl` browser |
| 5 | Overlap implicito con `get_work_pattern_stats` (Cost Intelligence) | Media | Basso | Decisione di design upfront: tab Day View è zoom intra-giorno, Cost Intelligence è pattern aggregati. Coesistono |
| 6 | Subagent invocations contati ma non disegnati → user si chiede "perché picco 6 ma vedo solo 3 row?" | Media | Basso | Footer Gantt: "X sessioni principali + Y subagent invocations". Trasparenza esplicita |
| 7 | River session cross-midnight (sessione iniziata ieri, ancora attiva oggi) | Media | Medio | Pattern LAV-46 già gestisce. Il bundle di "oggi" include sessione con `start_ts < oggi` se ha messaggi su oggi |

---

## 8. Bozza CHANGELOG entry (richiesta dal workflow)

```markdown
## Unreleased

LAV-XX: Day View — daily Gantt + honest worktime metrics.
- New dashboard tab "Day View" with date picker (← / →) showing a per-session Gantt grouped by project, with a concurrency curve below.
- Two honest wall-clock metrics computed per day: `active_wallclock_min` (union of activity windows with gap < 5min, deduplicated across parallel sessions) and `assistant_wallclock_min` (only intervals where assistant was responding, deduplicated). Avoids the ~13× inflation of naïve span-sum.
- New backend function `get_day_bundle()` in `lav/queries.py` reuses the LAV-46 river-session pattern (`COALESCE(MAX(messages.timestamp), c.timestamp)`) so cross-midnight sessions surface correctly on both days.
- New endpoint `GET /api/day?date=YYYY-MM-DD` with standard 4D filtering. Dashboard-only (role `both`); returns 404 for role `agent`.
- New CLI subcommand `lav day YYYY-MM-DD [--format json|table|brief]` exposing the same bundle for terminal use.
- Frontend port from `artifacts/lav-gantt/dynamic_gantt.html` (standalone tool, parked since 2026-05-13). Original timezone hardcoded (CEST) replaced with `Intl.DateTimeFormat`; original project labels dict replaced with normalized `projects.name`.
- Subagent invocations are counted but not yet drawn as separate Gantt rows (footer shows the count). Tracked in LAV-Yy for v1.5.
- No schema changes, no new dependencies.
```

---

## 9. Manual test plan

| # | Test | Aspettativa |
|---|---|---|
| 1 | `GET /api/day?date=2026-05-12` su macchia (role agent) | 404 |
| 2 | `GET /api/day?date=2026-05-12` su minimacs (role both) | 200 + bundle JSON |
| 3 | Bundle 12/05 vs `artifacts/lav-gantt/2026-05-12_lav_bursts_v2.json` | Numero sessioni identico, worktime entro ±1% |
| 4 | Tab UI 12/05 | 25 row sessioni, curva concorrenza con picco 6 ~14:36, 4 metric cards |
| 5 | Tab UI giorno random storico (>30gg fa) | Funziona o "no data" gracefully |
| 6 | Tab UI giorno futuro | "no data" |
| 7 | Filtro project=miniMe sul tab | Solo sessioni miniMe |
| 8 | `lav day 2026-05-12 --format brief` | Una riga per sessione |
| 9 | `lav day 2026-05-12 --format json \| jq .worktime` | Solo blocco worktime |
| 10 | River session: sessione iniziata 2026-05-11 con messaggi anche su 2026-05-12 | Compare su entrambi i giorni |
| 11 | Performance: `time lav day 2026-05-12` | < 1s |
| 12 | Cross-machine: aprire Day View su minimacs entro 5min da sessione macchia | Mostra ultima sessione (post-sync) o banner "ultimo sync" |

---

## 10. Alternative scartate

### A. Tenere lav-gantt come tool personale separato
Rifiutata: l'utente (Max) ha già 5 strumenti che fanno overlap parziale. Day View dentro LAV consolida.

### B. Skill condivisibile `/day-view`
Rifiutata: lavora solo sui JSONL locali di chi lancia la skill. Day View dentro LAV beneficia di sync cross-machine, cross-source (codex/chatgpt anche), filtri 4D.

### C. Solo endpoint API in LAV, UI in `artifacts/lav-gantt/`
**Considerare se Max vuole MVP più leggero** (~3-4h):
- Solo LAV-Xa (backend) + LAV-Xd (CHANGELOG)
- `artifacts/lav-gantt/dynamic_gantt.html` modificato per fetchare da LAV invece che dal proprio `serve.py`
- UI resta "artifact personale", logica vive in LAV
- Trade-off: non integrato nel dashboard, ma sblocca subito il valore (storia + cross-source)

### D. Fusione con Cost Intelligence (sub-tab dentro Cost Intelligence)
Rifiutata: focus diverso (tempo vs cost), confonderebbe la narrativa di entrambe le tab.

---

## 11. Decisioni richieste a Max

1. **GO / NO-GO** sull'integrazione completa (§4) vs alternativa C (§10) vs NO-GO totale
2. **Metriche worktime in MVP**:
   - Solo 2 oneste (active + assistant wall-clock) ← raccomandato
   - Tutte 4 (incluse span/latency come overlay)
3. **Subagent in MVP**:
   - Contati ma non disegnati ← raccomandato
   - Disegnati come righe figlie (+2h)
4. **Naming tab UI**:
   - "Day View"
   - "Daily"
   - "Gantt"
   - Altro
5. **Quando partire**: subito / dopo che chiude un altro epic LAV / parking per ora

---

## 12. Open questions per la review

- Esiste un test bench LAV con golden snapshot a cui ancorare il test #3? (CHANGELOG cita `tests/test_title_parsing.py` come pattern manuale)
- La tab "Cost Intelligence" ha già un metric-card style che possiamo riusare 1:1?
- L'utente Max preferisce vedere worktime in formato `Xh Ym` o in minuti decimali?
- Vale la pena aggiungere `worktime_today` come metrica realtime nel `/api/info` per il banner del dashboard?
