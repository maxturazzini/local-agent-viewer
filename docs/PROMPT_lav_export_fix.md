# Fix: collector pull perde messaggi di sessioni "fiume"

> **STATO: RISOLTO in LAV-46** (commit `0f270f4`, rilasciato). Questo documento è il
> brief d'indagine *pre-fix*, conservato come storico. Il fix implementato coincide
> con quello proposto qui sotto (derived-table `MAX(messages.timestamp)` + `COALESCE`
> + filtro child-table sul tail + avanzamento cursor lato collector). Vedi
> `docs/CHANGELOG.md` alla voce LAV-46 e `lav/queries.py:export_sessions`.

## TL;DR per chi lavora

Il pull collector → agent in LAV usa `interactions.timestamp` (= primo messaggio della sessione) come watermark `since`. Risultato: una sessione iniziata alle 06:00 con messaggi che continuano fino a sera viene pullata UNA SOLA VOLTA al primo passaggio (quando il watermark è < 06:00), poi mai più. I nuovi messaggi della stessa sessione restano sul DB dell'agent e non arrivano mai al collector.

**Sospetto regression**: Max ricorda di aver già fissato qualcosa di simile. Prima di scrivere codice, controllare `git log --all --oneline -- lav/queries.py lav/server.py` per `export`, `since`, `last_pull` ecc. e capire se c'è un precedente parziale.

## Sintomo riproducibile

Setup: due macchine, macchia (agent) e minimacs (collector both/agent), ruoli in `~/.local/share/local-agent-viewer/config.json`.

Per il **12 maggio 2026**, contando i messaggi:

| Sorgente | Messaggi del 12/05 |
|---|---|
| JSONL su macchia (`~/.claude/projects/**/*.jsonl`, truth source) | 3477 record |
| DB macchia (`~/.local/share/local-agent-viewer/local_agent_viewer.db`) | 2566 messaggi |
| DB minimacs (stesso DB, sull'altra macchina) | **1025 messaggi** |

Il DB di macchia è ok (parser locale incrementale fa il suo). Il DB di minimacs ha solo il 40% perché il pull ha smesso di ricevere nuove righe per le sessioni che esistevano già.

I log del collector lo mostrano chiaramente: in `~/.local/logs/lav-parser.log` su minimacs si vede `since` congelato per ore allo stesso valore con `messages: 0` di risposta:

```
[pull] Agent 'macchia' — since=2026-05-12T18:05:12.421Z
[pull] Agent 'macchia' OK — {'messages': 0, ...}
[pull] Agent 'macchia' — since=2026-05-12T18:05:12.421Z   <-- stesso since
[pull] Agent 'macchia' OK — {'messages': 0, ...}          <-- ma in macchia DB ce ne sono 111 dopo le 18:05
```

Conferma diretta interrogando l'endpoint sull'agent:

```bash
curl -s "http://localhost:8764/api/export?since=2026-05-12T18:05:12.421Z" | jq '.sessions | length'
# 4  (e sono solo sessioni NUOVE iniziate dopo le 18:05)
# Mentre in DB ci sono 111 messaggi DOPO le 18:05 distribuiti su molte sessioni
```

## Root cause

`lav/queries.py:1814-1832` — funzione `export_sessions`:

```python
interactions = run_query(conn, """
    SELECT c.*, p.name as project_name, ...
    FROM interactions c
    LEFT JOIN ...
    WHERE c.timestamp > ?         <-- qui
    ORDER BY c.timestamp ASC
    LIMIT ?
""", [since_ts, limit])
```

`interactions.timestamp` viene scritto dal parser al primo append della sessione e NON viene aggiornato quando arrivano nuovi messaggi. Quindi `c.timestamp` è il tempo di nascita della sessione, non l'attività più recente.

Conseguenza nel collector (`lav/server.py:220+`, funzione di pull): il `since` resta correttamente uguale al MAX timestamp ricevuto, ma `/api/export` filtra le interactions per `interactions.timestamp > since`, e le sessioni lunghe nate prima di `since` non rientrano mai più nel risultato — quindi i loro nuovi messaggi non vengono mai pullati.

## Fix proposto

Cambiare il filtro di `export_sessions` per usare il timestamp **dell'ultimo messaggio** della sessione, non il primo:

```sql
SELECT c.*, p.name as project_name, ..., m.last_msg_ts
FROM interactions c
LEFT JOIN ... 
LEFT JOIN (
    SELECT session_id, project_id, MAX(timestamp) AS last_msg_ts
    FROM messages
    GROUP BY session_id, project_id
) m ON m.session_id = c.session_id AND m.project_id = c.project_id
WHERE COALESCE(m.last_msg_ts, c.timestamp) > ?
ORDER BY COALESCE(m.last_msg_ts, c.timestamp) ASC
LIMIT ?
```

Lato collector (`lav/server.py` pull function): il `last_pull` deve avanzare al MAX timestamp dei MESSAGGI ricevuti (non delle interactions). Verificare la logica attorno a riga 235 (`last_pull` calcolo) e dove viene persistito.

Considerare anche: child tables (`token_usage`, `file_operations`, `bash_commands`, `search_operations`, `skill_invocations`, `subagent_invocations`, `mcp_tool_calls`) — quando una sessione "vecchia" viene ri-pullata, il batch upsert deve gestire correttamente l'idempotenza. Verificare che gli UPSERT a destinazione non duplichino.

## Edge case da verificare prima di chiudere

1. **Idempotenza**: pullare due volte la stessa sessione (perché il MAX(messages.ts) supera il since ad ogni nuovo append) NON deve duplicare nessun record nelle child tables. Controllare i vincoli UNIQUE su `messages(session_id, project_id, uuid)` e simili.
2. **Sessioni senza messaggi**: improbabile ma possibile (ghost session). La COALESCE con `c.timestamp` copre.
3. **Performance**: il subquery con `MAX(timestamp)` su `messages` può essere costoso se la tabella ha milioni di righe. L'indice `idx_msg_session` aiuta. Verificare EXPLAIN QUERY PLAN.
4. **Schema agent → collector**: oggi il payload `/api/export` espone `interaction` + `messages` per ogni sessione. Tutti i timestamp servono al collector per ricalcolare il watermark. Niente cambia nello schema, solo nel filtro.
5. **Test esistenti**: aggiornare i test in `tests/` che coprono `export_sessions`. Se esistono test di pull end-to-end, riprodurre lo scenario "sessione fiume" e verificare che dopo due round di pull il collector abbia tutti i messaggi.

## Validation plan

Dopo il fix, riprodurre il caso reale del 12 maggio 2026:

```bash
# Sul collector (minimacs)
ssh minimacs.local "sqlite3 ~/.local/share/local-agent-viewer/local_agent_viewer.db \
  'SELECT COUNT(*) FROM messages WHERE timestamp >= \"2026-05-12T00:00:00\" AND timestamp < \"2026-05-13T00:00:00\";'"
# Prima del fix:  1025
# Dopo il fix + reparse/full pull: deve avvicinarsi a 2566 (= count su agent)
```

Una volta validato, considerare se serve esporre un comando `lav sync --full --from-agents` o equivalente per fare il backfill una tantum sui dati storici già "buchi" nel collector.

## Note

- Le SQL per il pannello "daily concurrency / agent worktime" sono già pronte: `/Users/maxturazzini/Library/CloudStorage/OneDrive-Personale/miniMe/projects/lav-gantt/lav_day_queries.sql`. Funzionano contro lo schema attuale ma sotto-stimano del ~50% finché il collector non riceve i dati persi.
- Sospetto regression: cercare in `git log` riferimenti a "since", "watermark", "incremental pull", "stuck", "full pull" o issue/PR già aperti.
