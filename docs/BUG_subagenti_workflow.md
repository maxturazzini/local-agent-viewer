# LAV — Bug ingestion subagenti / workflow

Sessione di riferimento per tutti gli esempi:
`cb76ec06-a09d-4aa7-b2fc-b1541ea967c3` (progetto Arcipelago, project_id 114).
Contiene un workflow (`wf_6d3f81b6-51d`) con 34 subagenti + 1 subagente top-level = 35 sottoconversazioni.

Percorso dati sorgente:
```
~/.claude/projects/<proj>/<session_id>.jsonl                          ← conversazione principale
~/.claude/projects/<proj>/<session_id>/subagents/agent-*.jsonl        ← subagenti top-level (tool Task)
~/.claude/projects/<proj>/<session_id>/subagents/workflows/wf_*/agent-*.jsonl  ← subagenti da workflow
```

File parser: `lav/parsers/jsonl.py`. Funzione principale `parse_project()` (~riga 1500),
ingestion remota `ingest_remote_sessions()` (~riga 2319), risoluzione parent
`resolve_agent_parents()` (~riga 1421).

---

## Stato dei 4 bug

| # | Bug | Stato |
|---|-----|-------|
| 1 | Subagenti persi negli incrementali | ✅ FIXATO (commit `6a12664`) |
| 2 | `interactions` congelato sul collector (`INSERT OR IGNORE`) | ✅ FIXATO (commit `0523667`) + backfill minimacs |
| 3 | Subagenti-workflow collassati nel padre, non navigabili | ❌ DA FIXARE ← **questo documento** |
| 4 | Identità del padre corrotta (agent_id/parent_session_id errati) | ❌ DA FIXARE (conseguenza del #3) |
| 5 | `display`/`summary` inquinato da tag di sistema | ⚠️ minore, opzionale |

---

## BUG #3 — Subagenti da workflow non navigabili (IL PRINCIPALE)

### Sintomo
I messaggi dei 34 subagenti del workflow sono presenti in `messages` (verificato: 1010/1010
uuid ingeriti) MA:
- non hanno una riga `interactions` propria → non compaiono come sottoconversazioni;
- `subagent_invocations` per la sessione = 0;
- collassano tutti dentro `session_id = cb76ec06` → `message_count` gonfiato a ~1905,
  `total_tokens` a ~95M, che è la somma padre + tutti i subagenti.

### Causa radice
Tutti i file `agent-*.jsonl` del workflow hanno **lo stesso `sessionId` del padre**:

```
subagents/agent-afd13ef6...jsonl                → sessionId = cb76ec06, agentId = afd13ef6...
subagents/workflows/wf_.../agent-a3e9aad2...jsonl → sessionId = cb76ec06, agentId = a3e9aad2...
```

(verificato: 35/35 file agente hanno `sessionId = cb76ec06`, ognuno con un `agentId` distinto)

Nel parser (`parse_project`, riga ~1642):
```python
session_id = message.get("sessionId", "")   # ← prende cb76ec06 per TUTTI i subagenti
...
process_message_content(message, conn, ...)  # inserisce in messages con quel session_id
```

La chiave usata per `messages` e `interactions` è `session_id`. Siccome i subagenti-workflow
riusano il `sessionId` del padre, finiscono tutti nella stessa riga. L'`agentId` — che li
distinguerebbe — **viene letto** (riga ~1651, salvato in `session_agent_info`) ma poi **buttato
via**: la tabella `messages` NON ha una colonna `agent_id` (schema: id, session_id, project_id,
user_id, host_id, uuid, type, content, timestamp, tokens_in, tokens_out, model, api_message_id).

### Contrasto: i subagenti-Task funzionano
I subagenti lanciati dal tool `Task` (non da workflow) hanno un `sessionId` PROPRIO nel loro file,
quindi ottengono una riga `interactions` separata con `parent_session_id` + `agent_id` (è così che
`cb76ec06` stessa risulta figlia di un'altra sessione). Il meccanismo di gerarchia ESISTE già —
semplicemente non si applica ai workflow perché condividono il sessionId.

### Fix proposto
Distinguere i subagenti-workflow dando loro un'identità propria. Due opzioni (non esclusive):

**Opzione A — session_id sintetico per file agente (consigliata, riusa la pipeline gerarchia)**
Quando `is_agent_file` è vero, derivare un session_id sintetico dall'`agentId` invece di usare il
`sessionId` del messaggio:
```python
if is_agent_file:
    agent_id = message.get("agentId", agent_id_from_filename)
    effective_session_id = f"{parent_session_id}::agent-{agent_id}"   # o solo agent-<id>
else:
    effective_session_id = message.get("sessionId", "")
```
- `parent_session_id` = il `sessionId` del messaggio (che punta al padre `cb76ec06`) →
  già usato per `resolve_agent_parents`.
- Così ogni subagente-workflow ottiene la sua riga `interactions` con `agent_id` e
  `parent_session_id` corretti, esattamente come i subagenti-Task.
- ATTENZIONE: `upsert_session_source`, `update_interaction`, e il pull `ingest_remote_sessions`
  usano `session_id` come chiave → il session_id sintetico deve essere consistente in tutti i punti.
- Verificare che la UI (interactions.html) sappia mostrare le figlie via `parent_session_id`
  (già lo fa per i Task).

**Opzione B — aggiungere colonna `agent_id` a `messages`**
Aggiungere `agent_id TEXT` a `messages`, popolarla da `message.get("agentId")`. Più semplice ma
lascia i subagenti dentro la stessa interaction (non navigabili come conversazioni separate,
solo filtrabili). Utile come complemento all'opzione A per query fini.

### Verifica del fix
Dopo il fix, una reparse `--full` del progetto deve produrre:
- 35 righe `interactions` con `parent_session_id = cb76ec06` (o l'id sintetico scelto);
- `message_count`/`total_tokens` del PADRE ridotti a solo i suoi messaggi (non più 1905/95M);
- ogni subagente con i suoi conteggi (li abbiamo già mappati: agent-afd13ef6 = 59 msg,
  agent-ab99bb9b = 55, agent-ac6e3304 = 51, ... vedi tabella completa più sotto).

---

## BUG #4 — Identità del padre corrotta (conseguenza del #3)

### Sintomo
La sessione `cb76ec06`, che è una sessione NORMALE (non un subagente), ha in `interactions`:
- `agent_id = ae2fb46e426fd6c5b`
- `parent_session_id = 3e533b85-...`

Entrambi ERRATI. `cb76ec06` non ha nessun file `agent-cb76ec06.jsonl` → non è un subagente.
Invece `ae2fb46e` è un subagente-workflow che sta DENTRO cb76ec06
(`subagents/workflows/wf_.../agent-ae2fb46e...jsonl`).

### Causa radice
Diretta conseguenza del #3. Poiché i messaggi del subagente `ae2fb46e` sono ingeriti sotto
`session_id = cb76ec06`, il blocco (riga ~1650):
```python
if is_agent_file and session_id:
    agent_id = message.get("agentId", agent_id_from_filename)
    if agent_id:
        session_agent_info[session_id] = (parent_from_path, agent_id)  # session_id = cb76ec06 !
```
associa l'`agentId` del subagente al `session_id` del PADRE. Poi `update_interaction` scrive
quell'`agent_id` sulla riga di `cb76ec06`, e `resolve_agent_parents` le assegna un
`parent_session_id` sbagliato (matcha per prossimità temporale ±120s con una Task call qualsiasi).

### Fix
Si risolve automaticamente col fix del #3 (opzione A): se ogni subagente-workflow ha il suo
session_id sintetico, `session_agent_info` viene indicizzato per l'id giusto e la riga del padre
resta pulita (agent_id NULL, parent_session_id NULL). Aggiungere comunque un test di regressione:
una sessione senza file `agent-<suo_id>.jsonl` deve avere `agent_id IS NULL`.

---

## BUG #5 — display/summary inquinato da tag di sistema (minore)

### Sintomo
`interactions.display` di cb76ec06 e ae5ff941 inizia con
`<local-command-caveat>Caveat: The messages below...` invece del primo vero messaggio utente.
Altre sessioni iniziano con `<ide_opened_file>` / `<ide_selection>`.

### Causa
`update_interaction` (riga ~1364) prende i primi messaggi `type='user'` per costruire il display,
ma non filtra i wrapper di sistema (`<local-command-caveat>`, `<ide_opened_file>`,
`<ide_selection>`, `<system-reminder>`, ecc.).

### Fix
Nel calcolo del display, saltare/strippare i blocchi che sono interamente tag di sistema e
prendere il primo testo utente reale. Basso rischio, puramente estetico.

---

## Appendice — le 35 sottoconversazioni (verificate presenti in DB, msg per agente)

agent-afd13ef67fc66c8e6: 59 · agent-ab99bb9bc993d5538: 55 · agent-ac6e3304165f5bc73: 51
agent-a01633a045fcc874a: 47 · agent-a9f63019aeaafde9d: 45 · agent-a3e9aad2086b7cc9d: 42
agent-a20ab749c460b1c41: 36 · agent-ab04f7207c3e49e5c: 36 · agent-aa45420cdc96eb4c0: 35
agent-a28c4a4e3642135c6: 34 · agent-a3b629da45ccb6ead: 33 · agent-a825ed41a52102379: 33
agent-a48b83005d2729f43: 32 · agent-ae2fb46e426fd6c5b: 32 · agent-a096f1f1fd5dc473b: 31
agent-ab85650f3c51e1808: 31 · agent-abdb42d0458f108e1: 30 · agent-a6db2e9becf88615a: 27
agent-a81cd1a8d21bd28a1: 27 · agent-a66600d97444dec0f: 23 · agent-aa96dc25f61b94ecd: 23
agent-ab13c905eaad3c15e: 23 · agent-acc9593afbdd5817d: 23 · agent-ad73fe68cd9c24f2f: 22
agent-a22763bbd60e1e3b1: 21 · agent-a5196650aaa3aeeb5: 21 · agent-a8cfe7097d74c0e04: 21
agent-ac51f9187fb3508da: 21 · agent-a3df0fc48e933cca1: 20 · agent-a6c0d75b630e1de13: 20
agent-a94f5b3a68b8dd37b: 20 · agent-a0d45deb27f975f4d: 19 · agent-a6034ea5fb54f10f4: 19
agent-a3f2187015ce69509: 17 · agent-a6db377cfe8a323f6: 15

Totale: 35 sottoconversazioni, 1010 messaggi user/assistant (1044 righe incl. attachment).
```sql
-- come verificarle (richiede mappa uuid->agent dai file sorgente, vedi cronologia)
SELECT session_id, agent_id, message_count, total_tokens
FROM interactions WHERE parent_session_id = 'cb76ec06-a09d-4aa7-b2fc-b1541ea967c3';
-- oggi restituisce 0 righe (bug #3); dopo il fix deve restituirne 35
```
