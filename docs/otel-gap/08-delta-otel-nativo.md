# Delta: OTel nativo di Claude Code × file di Claude Code × LAV

> 2026-10-29. Confronto tra i campi che Claude Code manda in OpenTelemetry (fase 1,
> senza contenuti), quello che c'è oggi nei suoi file `.jsonl` e quello che LAV salva.
>
> **Fonti**: `campi-otel.csv` del playbook Hestro sulla telemetria (29/10/2026, 114
> campi per Cowork e Claude Code); 67 transcript Claude Code degli ultimi 3 giorni
> (versioni 2.1.30x); schema del DB LAV letto il 29/10/2026.
>
> **Attenzione**: molti campi "nei file" esistono solo dalla 2.1.300 (15/10/2026). I file
> più vecchi non li hanno. La matrice di luglio ([01-matrix.md](01-matrix.md)) va letta
> con questo delta accanto.

## Per funzionalità

✅ sì · ◐ in parte · ✗ no · ? non verificato. **File** = cosa c'è nei file sorgente che LAV
legge (per Claude Code, versioni 2.1.30x). In ogni area le righe vanno da quelle supportate
da tutti e tre a quelle supportate da uno solo. Nei totali i ✅ sono in grassetto, i ◐ a parte.

| Funzionalità | File | OTel | LAV |
|---|---|---|---|
| **Cosa copre** (9) | | | |
| Claude Code | ✅ | ✅ | ✅ |
| Cowork sul computer | ✅ | ✅ | ✅ |
| ChatGPT (export) | ✅ | ✗ | ✅ |
| Chat di claude.ai (export) | ✅ | ✗ | ✅ |
| Storico prima dell'attivazione | ✅ | ✗ | ✅ |
| Dati più vecchi di 30 giorni | ✗ | ✅ | ✅ |
| Codex (OTel non verificato) | ✅ | ? | ✅ |
| Cowork in cloud | ✗ | ✅ | ◐ |
| Plugin Office | ✗ | ✗ | ✗ |
| *Totale cosa copre* | **6** | **4** | **7** +1◐ |
| **Contenuti e analisi** (11) | | | |
| Testo di prompt e risposte | ✅ | ◐ | ✅ |
| Argomenti e output dei tool | ✅ | ◐ | ✅ |
| Sotto-agenti legati alla sessione madre | ✅ | ◐ | ✅ |
| Progetto di ogni sessione | ◐ | ◐ | ✅ |
| Ricerca nel testo | ✗ | ✗ | ✅ |
| Ricerca semantica | ✗ | ✗ | ✅ |
| Classificazione: argomento, persone, clienti | ✗ | ✗ | ✅ |
| Dati sensibili | ✗ | ✗ | ✅ |
| Grafo di persone e aziende | ✗ | ✗ | ✅ |
| Vista unica su tutti i fornitori | ✗ | ✗ | ✅ |
| Interfaccia web e MCP | ✗ | ✗ | ✅ |
| *Totale contenuti e analisi* | **3** +1◐ | **0** +4◐ | **11** |
| **Costi e token** (8) | | | |
| Token e cache 5 minuti / 1 ora | ✅ | ✅ | ✅ |
| Costo | ✅ | ✅ | ✅ |
| Costo del fast mode | ✅ | ✅ | ✗ |
| Effort | ✅ | ✅ | ✗ |
| Chiamate di servizio separate: titoli, compattazione | ✅ | ✅ | ✗ |
| Token di ragionamento | ✅ | ✗ | ✗ |
| Max token e tipo di thinking | ✅ | ✗ | ✗ |
| Modello richiesto e modello usato | ✅ | ✗ | ✗ |
| *Totale costi e token* | **8** | **5** | **2** |
| **Tempi** (5) | | | |
| Durata dei tool | ✅ | ✅ | ✅ |
| Durata del turno | ✅ | ◐ | ✗ |
| Durata della singola chiamata | ◐ | ✅ | ✗ |
| Tempo al primo token | ✗ | ✅ | ✗ |
| Tempo attivo della persona | ✗ | ✅ | ✗ |
| *Totale tempi* | **2** +1◐ | **4** +1◐ | **1** |
| **Esiti ed errori** (6) | | | |
| Esito dei tool | ✅ | ✅ | ✅ |
| Errori e tentativi del modello | ✅ | ✅ | ✗ |
| Permessi e classificatore | ✅ | ✅ | ✗ |
| Hook | ✅ | ✅ | ✗ |
| Collegamento ai server MCP | ◐ | ✅ | ✗ |
| Motivo di fine risposta | ✅ | ✗ | ✗ |
| *Totale esiti ed errori* | **5** +1◐ | **5** | **1** |
| **Chi e come** (9) | | | |
| Nomi veri di skill e server MCP | ✅ | ◐ | ✅ |
| Client: CLI, VS Code, Desktop | ✅ | ✅ | ◐ |
| Commit e pull request | ✅ | ✅ | ◐ |
| Righe di codice | ✅ | ✅ | ✗ |
| Compattazioni | ✅ | ✅ | ✗ |
| Email della persona e organizzazione | ◐ | ✅ | ◐ |
| Skill chiesta o partita da sola | ◐ | ✅ | ✗ |
| Plugin caricati e impostazioni gestite | ◐ | ✅ | ✗ |
| Chi ha scritto il prompt: persona, plugin, sistema | ✅ | ✗ | ✗ |
| *Totale chi e come* | **6** +3◐ | **7** +1◐ | **1** +3◐ |
| **TOTALE (48 funzionalità)** | **30** +6◐ | **25** +6◐ | **23** +4◐ |

**Letture veloci**: 6 funzionalità le hanno tutti e tre · 8 solo LAV · 8 solo OTel · 6 solo
i file · **16 sono nei file e LAV non le ha** (il debito di parsing).

### Messa in opera (fuori dai totali)

| Funzionalità | File | OTel | LAV |
|---|---|---|---|
| Cosa installare sui computer | | niente, si accende dalla console | l'agente LAV su ogni macchina |
| Tempo reale | | ✅ | ◐ a ogni sync |
| Collegamento a Grafana, Azure, Datadog | | ✅ formato standard | ✗ viste OTel in uscita da fare (D2) |
| Dove stanno i dati | sul computer, 30 giorni | server centrale | sul computer o nel collector LAV |
| Privacy in azienda | | fase 1 senza testi, fase 2 da autorizzare | testi sempre: serve l'autorizzazione come per la fase 2 |

## Legenda della tabella per campo

| Segno | Vuol dire |
|---|---|
| ✅ | LAV lo ha già |
| 🟡 | è nei file di Claude Code, LAV non lo legge: lavoro di parsing |
| 🔵 | solo OTel lo dà |
| ◐ | parziale o derivabile |

## Per campo (dettaglio)

### Agganci tra OTel e LAV

| Dato | OTel | File Claude Code | LAV oggi | Delta |
|---|---|---|---|---|
| Sessione | `session.id` | `sessionId` | `session_id` | ✅ |
| Richiesta della persona | `prompt.id` | `promptId` (97% dei file) | no | 🟡 |
| Messaggio | `message.uuid` | `uuid` | `messages.uuid` | ✅ |
| Chiamata al modello | `request_id` | `requestId` (`req_…`) | no, LAV tiene `message.id` | 🟡 |
| Chiamata a un tool | `tool_use_id` | `tool_use.id` | `tool_call_id` (LAV-78) | ✅ |

### Chi e dove

| Dato | OTel | File Claude Code | LAV oggi | Delta |
|---|---|---|---|---|
| Organizzazione e account | `organization.id`, `user.account_uuid` | `credential_org.organizationUuid`, `bridge-session.owner*Uuid` | no | 🟡 |
| Email della persona | `user.email` | no | no, l'utente viene dal path | 🔵 |
| Client: CLI, VS Code, Desktop | `service.name`, `terminal.type` | `entrypoint` (cli, claude-vscode, claude-desktop, sdk-ts) | solo in `cowork_remote` | 🟡 |
| Versione del client | `service.version` | `version` | `session_sources.client_version` | ✅ |
| Cartelle e sistema | `workspace.host_paths`, `os.*` | `cwd` | `cwd`, `hosts.os_type` | ✅ |

### Prompt e risposte

| Dato | OTel | File Claude Code | LAV oggi | Delta |
|---|---|---|---|---|
| Testo di prompt e risposte | `prompt`, `response`, solo in fase 2 | completo | `messages.content` | ✅ LAV meglio |
| Chi ha scritto il prompt: persona, plugin, sistema, altro agente | no | `origin`, `turnOrigin`, `promptSource` | no | 🟡 solo file |
| Comando con la barra | `command_name` | `<command-name>` nel testo | ◐ | 🟡 |
| Chiamate di servizio: titolo, compattazione, ricerca web | `query_source` | `api-request.querySource` | no | 🟡 |

### Chiamata al modello

| Dato | OTel | File Claude Code | LAV oggi | Delta |
|---|---|---|---|---|
| Token in ingresso, in uscita, in cache | `input/output/cache_*_tokens` | `usage.*` | `token_usage` | ✅ |
| Cache a 5 minuti e a un'ora | `cache_creation_5m/1h_tokens` | `usage.cache_creation.*` | `cache_creation_1h_tokens` (LAV-96) | ✅ |
| Token di ragionamento | no | `usage.output_tokens_details.thinking_tokens` | no | 🟡 solo file |
| Modello richiesto | no, solo quello che risponde | `requestedModel`, `api-request.params.model` | no | 🟡 solo file |
| Max token, tipo di thinking | no | `api-request.params` | no | 🟡 solo file |
| Effort | `effort` | `effort`, `perTurnEffort` | no | 🟡 |
| Velocità (fast mode) | `speed` | `usage.speed` | no, e `pricing.py` non ha i prezzi fast | 🟡 |
| Motivo di fine risposta | no, solo nel corpo in fase 2 | `message.stop_reason` | no | 🟡 solo file |
| Costo calcolato da Anthropic | `cost_usd` per chiamata | `cost-state.totalCostUSD` e `modelUsage.*.costUSD` per sessione | solo in `cowork_remote` | 🟡 per sessione, 🔵 per chiamata |
| Durata della chiamata | `duration_ms` | ◐ da `api-request.timestamp` all'ultimo blocco con lo stesso `requestRef`; `cost-state.totalAPIDuration` per sessione | no | 🟡 stima, 🔵 esatta |
| Tempo al primo token | `ttft_ms` | no | no | 🔵 |
| A chi attribuire il costo: skill, server MCP, agente | sì, ma in fase 1 i nomi arrivano come `custom` | `attributionSkill`, `attributionMcpServer`, `attributionMcpTool`, `attributionAgent`, con i nomi veri | no | 🟡 file meglio |

### Errori

| Dato | OTel | File Claude Code | LAV oggi | Delta |
|---|---|---|---|---|
| Errori e tentativi del modello | `api_error`: `status_code`, `attempt`, `error` | `system/api_error`: codice, `retryAttempt`, `retryInMs`, `maxRetries` | no | 🟡 |

### Tool

| Dato | OTel | File Claude Code | LAV oggi | Delta |
|---|---|---|---|---|
| Esito, errore, durata | `tool_result`: `success`, `error_type`, `duration_ms` | `toolUseResult`, `is_error` | sei tabelle tool: `is_error`, `duration_ms`, `error_text`, `exit_code` | ✅ |
| Argomenti del tool | `tool_parameters`, solo in fase 2 | completi | sì | ✅ LAV meglio |
| Permesso accettato o rifiutato, e da chi | `tool_decision`: `decision`, `source`, `permission_mode`, `classifier_verdict` | `permissionDecision` (decision, source, reasonType classifier, rule o mode) e record `permission-mode` | no | 🟡 |
| Ambito del server MCP | `mcp_server_scope` | no | no | 🔵 |

### Ambiente di lavoro

| Dato | OTel | File Claude Code | LAV oggi | Delta |
|---|---|---|---|---|
| Collegamento ai server MCP: tempi, errori 429, 404, timeout | `mcp_server_connection` | solo i nomi dei server falliti (`deferred_tools_delta.failedMcpServers`) | no | 🔵 (🟡 i nomi) |
| Hook: quanti, quali bloccano, durata | `hook_execution_*` | `hook_success`, `hook_non_blocking_error` con stderr, `system/stop_hook_summary` | no | 🟡 file meglio |
| Skill usata | `skill_activated` (`custom_skill` in fase 1) | tool `Skill` | `skill_invocations` | ✅ |
| Skill chiesta o partita da sola | `invocation_trigger` | ◐ `<command-name>` contro tool `Skill` | no | 🟡 |
| Compattazione | `compaction`: `pre_tokens`, `post_tokens` | `compact_boundary.compactMetadata`: pre, post, durata, trigger | no | 🟡 |
| Sotto-agenti | `subagent_completed` | file del sotto-agente | `subagent_invocations` e roll-up | ✅ |
| Plugin caricati, impostazioni gestite | `plugin_loaded`, `managed_settings_resolved` | ◐ elenchi di skill e agenti | no | 🔵 |
| Pulizia dei transcript | `retention_sweep` (`period_days`, default 30) | no | no | 🔵 |
| Citazioni con @, sondaggio | `at_mention`, `feedback_survey` | no | no | 🔵 |

### Metriche

| Dato | OTel | File Claude Code | LAV oggi | Delta |
|---|---|---|---|---|
| Tempo attivo, persona e CLI | `claude_code.active_time.total` | ◐ `system/turn_duration.durationMs`, solo la CLI | no | 🟡 la CLI, 🔵 la persona |
| Righe di codice | `claude_code.lines_of_code.count` | `cost-state.totalLinesAdded/Removed`, `structuredPatch` | no | 🟡 |
| Pull request e commit | `claude_code.pull_request.count`, `.commit.count` | record `pr-link`; `git commit` nei comandi Bash | ◐ commit in `bash_commands` | 🟡 |
| Sessioni nuove o riprese | `claude_code.session.count` (`start_type`) | ◐ | conteggio sì, tipo no | 🔵 |

## In numeri (tabella per campo)

| | Righe |
|---|---|
| ✅ LAV lo ha già | 12 |
| 🟡 nei file, da leggere | 24 |
| di cui solo nei file, OTel fase 1 non li ha | 6 |
| 🔵 solo OTel | 8, più la parte esatta di costo, durata e tempo attivo per chiamata |

## Cosa dice (tabella per campo)

1. **Il grosso del delta è parsing.** 24 dati su 44 sono già nei file e LAV li scarta.
2. **Su sei dati i file battono OTel**: chi ha scritto il prompt, token di ragionamento,
   modello richiesto, parametri della chiamata, motivo di fine risposta, nomi veri di
   skill e server MCP.
3. **OTel serve davvero per otto cose**, quasi tutte sull'ambiente di lavoro (connessioni
   MCP, plugin, impostazioni, pulizia), più la misura esatta per singola chiamata:
   costo, durata, tempo al primo token.
4. **`retention_sweep` è un allarme per LAV**: Claude Code cancella i transcript dopo
   30 giorni di default. Quello che LAV non legge entro 30 giorni si perde.
5. **Cowork in cloud resta fuori da questa tabella**: lì i file locali non ci sono, e OTel
   è il canale ufficiale (in alternativa LAV-94 e LAV-97).
