# Restyle del popup chat (transcript) — analisi e mockup

> Stato: **sola analisi + mockup di esempio**. Nessuna modifica a `lav/static/interactions.html`.
> Esplorazione prodotta da un workflow multi-agente (3 designer indipendenti → panel di giudici a 3 lenti → sintesi).

## 1. Dove vive oggi (e perché conta per il dock futuro)

Non è un componente separato: markup, CSS e JS sono **tutti inline** in `lav/static/interactions.html` (2108 righe).

| Pezzo | Posizione |
|---|---|
| Markup del modal (overlay, header, tabs, body) | `interactions.html:937-957` |
| `openInteraction()` — apre il modal, fetch, chiama i render | `interactions.html:1639` |
| `renderInteractionDetail()` — loop sui messaggi/turn | `interactions.html:1787` |
| `renderThinking()` | `interactions.html:1607` |
| `renderToolUse()` / `renderToolResult()` | `interactions.html:1623` / `1617` |
| CSS modal + messaggi + thinking + tool | `interactions.html:409-647` |

**Nodo per il dock:** `openInteraction` e i render fanno `getElementById('modalOverlay')`/`modalBody` a mano — il *renderer del transcript* (data→HTML) è **fuso** con la *chrome del modal*. Per dockarlo basta scollegare i due: un componente `.tx` che produce l'HTML del transcript da un `mount node`, e due "shell" (Popup / Dock) che lo ospitano. I mockup qui sotto lo dimostrano col toggle **Popup ⇄ Docked**.

## 2. I tre problemi, confermati nel codice

**A. Troppo colore.** Nel modal competono ≥6 accenti:
- assistant = gradiente blu + bordo `--dodger-blue #004BFF` (`interactions.html:530-533`)
- thinking = viola `#9b59b6` (`556-572`)
- tool call = **un secondo blu** `#3498db` (`596-610`) — diverso dal brand
- tool result = verde `#27ae60` (`634`); errori = rosso `#e74c3c`
- + rainbow di badge/tag classification/sensitivity (`491-515`)

Due blu diversi e quattro tinte semantiche sulla stessa schermata.

**B. Drilldown inutile.** `renderThinking()` incapsula *sempre* in un collassabile, anche per la riga singola "(encrypted — no readable thinking content)" (`1607-1614`): un click per rivelare una riga. Idem tool call/result, tutti `collapsed` di default (`1620`, `1635`) anche per output di 2 righe.

**C. Sotto-turn piatti.** Un turn assistant può avere text + thinking + N tool_use, ma vengono buttati piatti in `.message-content` (`1819-1847`). La coppia tool_use→tool_result è agganciata pescando nel messaggio successivo (`1626-1631`), ma i tool_result standalone si renderizzano comunque a sé: manca il raggruppamento "l'agente ha fatto X → risultato Y".

## 3. Le tre direzioni esplorate

### Quiet Canvas

A near-monochrome transcript that reads like a clean git log rather than a chat app. All hierarchy is carried by whitespace, indentation and type weight — the only colors are the single brand accent #004BFF (used sparingly: the "You" label, the active toggle, the thin user rail) and red #DC2626, reserved exclusively for the one error result. User and assistant turns share the same transparent canvas, distinguished only by a small uppercase role label and a 1px left rail. Assistant sub-steps (thinking, tool calls) are indented under the turn as a quiet vertical sequence, with each tool_use bound to its tool_result inside one neutral card. The whole thing is a container-agnostic .tx component mounted into either a Popup overlay or a right-side Dock via a working header toggle.

**Mosse chiave:**
- Killed the rainbow: replaced two blues + purple + green + per-type tints with neutral surfaces (#FFFFFF/#F7F8FA/#F1F3F5) and small monochrome line-icons. Color now means only two things — accent #004BFF for active/identity and red #DC2626 for the single error result.
- Encrypted/empty thinking renders inline as ONE muted italic line ('Extended thinking — encrypted, no readable content') with NO disclosure control, eliminating the one-click-to-reveal-one-line drilldown.
- Drilldown restraint by content length: short tool inputs and short results show inline; only genuinely long content collapses, and the collapsed control always shows a 1-line preview plus a size hint chip ('+N lines'). Tool calls default to collapsed-with-preview, never blank.
- Sub-turns are an ordered, visually nested sequence: assistant text first, then thinking and each tool_use indented under the turn rail with a small connector tick — no more flat dump.
- Every tool_use is bound to its tool_result inside the SAME card (results indexed from the next user message by tool_use_id, tu_01..tu_03), so call and output are physically connected rather than scattered.
- Dockability proven: the transcript is a self-contained .tx component re-rendered identically into a Popup overlay (~860px centered) or a Dock (~420px, full viewport height) via a header segmented toggle; the page shifts its right padding to make room for the dock.
- User vs assistant differentiated with minimal means only — an uppercase role label ('You' in accent / 'Agent' in text) plus rail color, no gradients or heavy borders. Agent label carries quiet token counts (in/out) as muted metadata.
- Error treatment is singular and minimal: tu_03's error gets a soft red background, red icon and red mono text — the only red on screen — instead of competing with success-green checkmarks elsewhere.

### Threaded Rail (IDE timeline)

The transcript reads like a CI/agent run log: one vertical rail down the left, each turn a node, and an assistant turn's sub-steps (thinking, every tool_use, every paired tool_result) branching as child nodes on a nested rail. Color is stripped to neutrals carrying hierarchy with a single brand accent (#004BFF) for the You/Agent labels and rail nodes, and red (#DC2626) reserved exclusively for errors — no purple, teal, green or second blue. Each tool_use is bound to its tool_result on the SAME card (Call / Result), so tu_01..tu_03 are visibly connected instead of dumped flat. The whole thing is a container-agnostic component (.lav-transcript) injected unchanged into either a centered Popup overlay or a narrow right-side Docked panel via a working header toggle.

**Mosse chiave:**
- Killed the rainbow: every type (assistant/thinking/tool-call/tool-result) now shares neutral surfaces; #004BFF appears only on labels, rail nodes and focus rings, red only on the tu_03 error. No gradients, no per-type accent hues.
- Bound tool_use to tool_result on one card. The two user messages that only carry tool_results are detected as 'carriers' and folded into the preceding assistant's tool cards, so the result sits under its own Call — never a free-floating green block.
- Drilldown restraint by content size: the encrypted thinking placeholder is a single muted inline line with NO disclosure; single-line tool results (incl. the tu_03 error) render inline; only multi-line output (tu_01, tu_02) collapses, always showing a preview line + a '+N lines' size hint.
- Tool calls default to collapsed-with-preview (Call label + first JSON line + line count), never blank-collapsed — fixing the 'one click to reveal nothing' problem.
- Sub-turns are an ordered nested rail: thinking and each tool render as child nodes with a status dot (neutral = ok, red = error), monospace tool names, so an assistant turn's text + thinking + multiple tools read as a structured sequence.
- Turn-level collapse: noisy multi-step turns get a 'collapse' pill that folds to a one-line summary ('thinking · 2 tool calls (Read Bash)'); a collapse-all / expand-all pair lives in the component header.
- Dockability proven: the identical buildTranscript() markup mounts into a ~860px centered Popup (wide layout) or a 420px full-height right Dock (is-narrow layout that hides tool descriptions), toggled live from the header with Esc-to-dismiss.

### Calm Bubbles

A muted messaging layout that fixes the rainbow problem by letting neutrals carry all hierarchy and reserving the single brand blue (#004BFF) for just the user bubble and active controls, with red (#DC2626) only for errors. Thinking and tool activity become compact neutral "attachment" cards stacked on a hairline rail beneath the assistant bubble, so an assistant turn and its ordered sub-steps read as one grouped sequence, with every tool_use bound to its tool_result inside the same card. The transcript is a container-agnostic component: the exact same DOM node is moved between a Popup overlay (~860px) and a Docked right-side panel (~420px, full height), proving reuse in both shells.

**Mosse chiave:**
- Killed the color-per-type scheme: assistant blue gradient, purple thinking, second-blue tool calls, and green results all collapse into neutral surfaces (white/#F7F8FA) + small line icons. Brand blue survives only on the user bubble and the active toggle; red is the sole semantic hue, used only on the error card.
- Encrypted/empty thinking renders as an inline pill with the muted placeholder text shown directly ('Thinking — encrypted, no readable content') and NO disclosure control, removing the one-click-to-reveal-one-line drilldown.
- Sub-turns are grouped on a left hairline rail under the assistant bubble in original order (thinking, then each tool_use), instead of being dumped flat — the nested sequence is visually obvious.
- Each tool_use is bound to its tool_result inside the SAME card: a result row shows a one-line preview plus an ok-tick or red error mark, so tu_01/tu_02/tu_03 are never orphaned. The Edit error (tu_03) tints exactly one card red.
- Drilldown restraint: short results (the grep hit, the error line) render inline; only genuinely long content is collapsible, and the collapsed head always shows a 1-line preview plus a size hint ('+12 lines') — never blank-collapsed.
- Tool_result-only user messages are folded into the cards above rather than rendered as empty user bubbles, so the conversation reads as 3 user/assistant exchanges instead of 6 noisy rows.
- Dockability proven concretely: a working header segmented toggle re-parents the identical transcript node between the overlay shell and the dock shell, syncs the pressed state, and shifts the page content left when docked — no duplicated markup.

## 4. Scoreboard dei giudici (panel a 3 lenti, 0–10)

| Direzione | Calma colore | Chiarezza sotto-turn | Restraint drilldown | Dockability | **Overall** |
|---|---|---|---|---|---|
| Quiet Canvas | 9 | 8 | 8.3 | 8.7 | **8.2** |
| Threaded Rail (IDE timeline) | 8.7 | 8 | 7.7 | 7.7 | **7.7** |
| Calm Bubbles | 8 | 8 | 7.7 | 7.3 | **7.3** |

**Vincitore: Quiet Canvas (8.2).** È la base della versione consigliata.

## 5. Raccomandazione (lead designer)

Chosen base: "Quiet Canvas" — it scored highest on every dimension (colorCalm 9, subturnClarity 8, drilldownRestraint 8.3, dockability 8.7, overall 8.2) and most directly answers the four reported problems: it kills the rainbow by carrying hierarchy with whitespace + a 6-step gray ramp, it already binds tool_use to tool_result in one card via an indexResults() id-map, it renders encrypted thinking as one inline muted line, and its .tx component is genuinely container-agnostic (re-rendered identically into a popup or a right dock with a header toggle and a .page.docked padding shift).

Grafts from the runner-ups:
- From "Threaded Rail (IDE timeline)": (1) the content-size drilldown threshold refined to >2 lines (so 2-line results stay inline) instead of Quiet Canvas's >1-line cut, applied via a single isLong() helper used for both results and thinking; (2) the carrier-turn concept made explicit (onlyResults user messages folded into the preceding assistant's cards); (3) turn-level collapse for genuinely noisy turns (>=2 sub-steps) with a one-line folded summary ("thinking · 2 tool calls (Read Bash)") and a header "Collapse/Expand steps" action; (4) the size hint always carried on collapsed regions ("+N lines").
- From "Calm Bubbles": the colorless success state — the result tick now uses currentColor/--muted instead of green, so RED (#DC2626) is the genuinely SOLE chromatic semantic; plus the hasText ? inline : collapse branch logic for thinking, and the model shown as a neutral mono pill.

Bugs fixed vs. the critiques: (a) removed Quiet Canvas's decorative radial-gradient body wash (the canvas is now truly flat #F7F8FA); (b) fixed the short-multi-line thinking truncation bug with a correct THREE-WAY branch — empty/encrypted -> one muted inline line (no control); short readable (<=2 lines) -> inline muted block (no control, never ellipsis-truncated); long readable -> collapse-with-preview; (c) success is colorless, killing the green that both Calm Bubbles and Quiet Canvas leaked. CSS audit confirms 14 hex values total: the neutral ramp + #004BFF (identity/active only) + the #DC2626 error family; no #3498db / #9b59b6 / #27ae60 / teal anywhere.

## 6. Spec di implementazione (per quando si passa al codice)

## LAV Transcript restyle — implementation spec ("Quiet Canvas+")

### (a) Token palette (CSS variables)
```css
:root{
  /* neutral ramp — carries ALL hierarchy */
  --white:#FFFFFF; --surface:#F7F8FA; --surface-2:#F1F3F5;
  --border:#E6E8EB; --border-strong:#D6D9DD;
  --text:#1A1D21; --muted:#6B7280; --muted-2:#9AA0A8;
  /* the ONLY two semantic colors */
  --accent:#004BFF;            /* identity / active ONLY: You label, user rail, active toggle, focus rings */
  --error:#DC2626;             /* errors ONLY */
  --error-bg:#FCEFEF; --error-border:#F0C9C9;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --fs-xs:11px; --fs-sm:12px; --fs-base:13px; --fs-md:14.5px; --fs-lg:15px; --fs-xl:20px;
  --s1:4px; --s2:8px; --s3:12px; --s4:16px; --s5:24px; --s6:32px;
}
```
Discipline rule: NO per-type accent hues. Thinking and tool calls use neutral surfaces + monochrome line icons (currentColor). The success tick is `--muted`, not green — red is the sole chromatic semantic.

### (b) Turn / sub-turn DOM structure
```
.tx                              (container-agnostic component root; .is-narrow when docked)
  .tx-head                       (title, tx-meta, seg toggle [Popup|Docked], Collapse-steps action, close)
  .tx-body  (scroll)
    .turn.turn--user | .turn--assistant     (::before = 1px left rail; accent@.5 for user, --border for agent)
      .turn-head  ( .turn-label "You"/"Agent" + .turn-sub tokens + .turn-collapse pill[only if >=2 substeps] )
      .turn-content
        .turn-text*                          (assistant prose / user prompt; pre-wrap)
        .substeps                            (ordered nested sequence)
          .substep   (::before = horizontal tick connecting to the turn rail)
            -> .think-line          (empty/encrypted thinking: ONE muted italic line, no control)
            -> .think-inline        (short readable thinking: inline block, no control)
            -> .step-card           (long thinking OR a tool_use)
                 .call-row (button if long)  icon + mono name + 1-line preview + "+N lines" + caret
                 .call-detail (collapsed)    input blob / Replace+With for Edit
                 .result-row                 BOUND tool_result: icon(muted ok / red err) + meta + .result-text(.clamp)
      .turn-folded                           (one-line summary shown when turn collapsed)
```
Carrier-turn folding: a user message whose content is ONLY `tool_result` items returns `''` from `renderTurn` (`onlyResults` guard); its results are reached through the `indexResults()` id->result map and rendered inside the originating tool card. This collapses the 6 raw messages into 3 logical exchanges and guarantees no orphan result block.

### (c) Drilldown rules
- `isLong(s)` = `lineCount(s) > 2 || s.length > 200`. Single/2-line content (incl. the encrypted-thinking placeholder, the grep hit, the 1-line error) renders INLINE with NO disclosure control.
- Only `isLong` content collapses, and a collapsed control ALWAYS shows a 1-line preview (`firstLine`) plus a size hint chip (`+N lines`). Never blank-collapsed.
- Tool-call INPUT: collapsed-with-preview by default when `callIsLong(item)` (>2 input lines, or any Edit with old/new_string); short inputs render the preview line with no caret.
- Thinking is a strict 3-way branch (empty -> inline line; short -> inline block; long -> collapse-with-preview) — this fixes the prior silent-truncation of 2–4 line thoughts.
- RESULT text uses a mask-clamp + a muted "Show full result (+N lines)" link only when `isLong`.

### (d) Container-agnostic renderer (dockability)
`renderTranscript(host)` writes the full `.tx` markup into ANY host element and then calls `wireComponent(host)` to scope all listeners to that host — it never references the modal/overlay. Two shells (`#shellPopup` inside an overlay, `#shellDock` as a fixed right `<aside>`) are just hosts; the segmented header toggle calls `setMode('popup'|'dock')` which re-renders into the other host and toggles `.page.docked` (right-padding shift) and `.tx.is-narrow` (tighter spacing, smaller preview type). Esc + overlay-click dismiss.

### Minimal refactor of today's inline code in `lav/static/interactions.html`
Today the renderer is fused with modal chrome:
- `openInteraction(sessionId, projectId)` (~L1639) reaches into `#modalOverlay` / `#modalBody` directly and calls `renderInteractionDetail(data)`.
- `renderInteractionDetail(data)` (~L1787) loops messages, calling `renderThinking` (~L1607), `renderToolUse(item, nextMessage)` (~L1623, already does the tool_use_id pairing), `renderToolResult` (~L1617, renders standalone — source of orphan green blocks), with `parseMessageContent` (~L1589) and `getToolIcon` (~L1602).

Refactor steps (no framework):
1. Extract a pure `renderTranscript(host, data)` that emits the `.tx` markup into a passed-in `host` (drop all `getElementById('modalBody')` coupling). Move the new CSS into the page `<style>` replacing `.tool-call`/`.thinking-block`/`.tool-result-standalone` color rules.
2. Replace the three per-item renderers with: `renderThinking(it)` (3-way), `renderToolCard(item, results)` (binds the result inside the SAME card), and DELETE `renderToolResult` standalone — instead build a `results = indexResults(messages)` map once and add the `onlyResults` carrier guard in the turn loop so result-only user messages don't emit empty turns.
3. Keep `openInteraction` only as the data-fetch + shell-open path; have it call `renderTranscript(modalBody, data)`. The same function can later be called with a dock host, proving reuse — no second renderer.
4. Move per-card expand + result "show full" handlers into a `wireComponent(host)` that scopes `querySelectorAll` to `host`, replacing the global `toggleCollapsible(this)` inline onclicks.

## 7. Come provarli

Aprili nel browser (doppio click o `open`):

```
docs/restyle/mockup-A-quiet-canvas.html      # base consigliata
docs/restyle/mockup-B-threaded-rail.html     # timeline da dev-tool
docs/restyle/mockup-C-calm-bubbles.html      # messaging muto
docs/restyle/mockup-RECOMMENDED.html         # sintesi
```

Ognuno renderizza **lo stesso** transcript di esempio (6 turn, con thinking cifrato a riga singola, tool call multipli e un risultato di Edit in errore) e ha il toggle **Popup ⇄ Docked** in alto, così confronti le rese 1:1 e vedi come lo stesso componente si comporta da popup e da pannello laterale.

> Nessun file di produzione è stato toccato. Prossimo step (separato): scegliere la direzione, poi rifattorizzare `renderInteractionDetail` & co. in un componente `.tx` container-agnostico.
