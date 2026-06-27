# LAV-63 — Piano di implementazione (Quiet Canvas + componente dock-ready)

> Generato dal workflow di discovery read-only (4 auditor + sintesi). 56 comportamenti catalogati, 10 ad alto rischio.

## Component API & split component/shell

COMPONENT (.tx — reusable, container-agnostic, dock-ready):
- Owns: .tx-head (title + neutral meta chips + close button calling opts.onClose), an empty .tx-aux slot for shell chrome, and .tx-body (the turn stream).
- Renders: turns (renderTurn keyed on msg.type), substeps (renderSubsteps), tool cards with bound results (renderToolCard + indexResults), thinking (inline line / collapsible), breadcrumb + derived-sessions (navigating via opts.onOpenInteraction), empty/orphan fallbacks.
- Carries as self-contained code: SVG ICONS + toolIcon, parseMessageContent, callPreview/callIsLong/inputDetail/lineCount, indexResults, and event-delegated wiring for expand/collapse + result 'show full'. No fetching, no API_BASE, no state.*, no overlay/backdrop/ESC logic.
- Uses shared utils (imported, not duplicated): escapeHtml, escapeAttr, formatNumber, formatTimestamp, shortenModel, shortenSessionId, copySessionId.

SHELL (popup modal — stays outside .tx):
- #modalOverlay/.modal chrome, backdrop-click + global ESC + close wiring, deep-link ?session= auto-open.
- openInteraction orchestration: the 3 parallel fetches (/api/interaction{?project}, /api/kb/status, /api/interaction/{id}/metadata{?project}) and error/loading handling.
- KB enrichment band (renderKbEnrichment) and the Transcript/Cost tab strip, injected into the component's .tx-aux slot; Cost panel (#costProfileBody + renderCostProfile + _costProfileChart) appended after .tx-body and toggled by the tabs.
- Provides the injected callbacks to the component.

MOUNT SIGNATURE:
mountTranscript(host, data, opts) -> { root, headEl, auxEl, bodyEl, destroy() }
  host: HTMLElement (popup: the modal container; later dock: .shell-dock)
  data: resolved /api/interaction response { interaction, messages, cost_profile }
  opts: {
    onClose(): void,                                  // shell-owned close (popup removes .active)
    onOpenInteraction(sessionId, projectId): void,    // breadcrumb + derived navigation
    copySessionId(el): void,                           // shared util
    container: 'popup' | 'dock'                         // default 'popup'; minor affordances only
  }
Returns refs so the shell can fill auxEl (KB band + tabs), append the Cost panel after bodyEl, and call destroy() to detach component listeners on close.

## Piano

## Goal
One focused write pass on `/Users/maxturazzini/claude_projects/local-agent-viewer/lav/static/interactions.html`: restyle the transcript to "Quiet Canvas" AND extract it into a container-agnostic `.tx` component mounted by the existing popup modal shell. Ship the Popup shell only; keep the component dock-ready (no modal chrome inside it). KB enrichment band and Cost tab must keep working. Single file, vanilla JS, no build, no new deps.

## Step 0 — Pre-flight (do first)
1. `hostname` to confirm we are on macChia (dev). Never touch the prod agent config.
2. `grep -n "toggleCollapsible\|getToolIcon\|renderThinking\|renderToolUse\|renderToolResult\|renderDerivedSessions\|message-text\|tool-call\|thinking-block" interactions.html` to map every consumer before deleting old transcript code/CSS (some helpers are shared with the list table — do NOT remove those).
3. Confirm `.kb-sensitivity` and `.badge-classification` are reused by the LIST table (rows ~1535/1539). These stay untouched in v1 (cross-surface).

## Step 1 — Add Quiet Canvas design tokens (additive, non-breaking)
Add the mockup's token set to `:root` as NEW vars (do not rename existing `--dodger-blue`, `--white`, etc.): `--surface #F7F8FA`, `--surface-2 #F1F3F5`, `--border #E6E8EB`, `--text #1A1D21`, `--muted #6B7280`, `--accent #004BFF`, `--error #DC2626`, `--error-bg #FCEFEF`, `--mono`, `--sans`, spacing `--s1..--s6`, `--radius`, `--radius-sm`. Existing styles are unaffected because these names are new.

## Step 2 — Add `.tx` component CSS (new namespace, no collisions)
Port the mockup's `.tx`, `.tx-head`, `.tx-title`, `.tx-close`, `.tx-meta`, `.tx-aux` (new slot — see Step 4), `.tx-body`, `.turn/.turn--user/.turn--assistant`, `.turn-label/.role-sub`, `.turn-text`, `.substeps/.substep`, `.think-line`, `.step-card/.is-error/.open`, `.call-row/.call-icon/.call-name/.call-preview/.call-size/.call-caret`, `.call-detail/.detail-label/.code`, `.result-row/.result-icon/.result-body/.result-meta/.result-text/.clamp/.result-expand`, error result rules. All class names are new and will not collide with `.message`/`.tool-call`/`.thinking-block`.

## Step 3 — Build the `.tx` component (pure render + self-contained wiring)
Port the mockup's helpers/renderers, adapting them to REAL API data and preserving audited behavior:
- **Carry pure utils into the component:** SVG `ICONS` map + `toolIcon()`. Extend ICONS to cover EVERY current tool (Read, Bash, Edit, Grep, Write, Glob, Task, Skill, WebSearch, WebFetch, TodoWrite) plus `_default` wrench and an `mcp__*` fallback to wrench — otherwise 7 known tools regress to the default icon.
- **Keep `parseMessageContent` (lines 1729-1740) verbatim** and use it everywhere the mockup used `m.content||[]`. The real API returns content as string / JSON-string / non-array; the mockup's naive `content||[]` would break those. This is non-negotiable.
- **`indexResults`:** run `parseMessageContent` on each message before scanning for `tool_result`. Build a global `tool_use_id -> result` map AND a `consumed` set. This widens pairing from next-message-only (current) to global (mockup) — confirm during test that ids do not collide within a session.
- **`renderToolCard(item, results)`:** keep mockup structure (icon + escaped name + 1-line `callPreview` + size hint + caret only when `callIsLong`). Bind the paired result inside the same card. FIX two current bugs while porting: (a) escape `item.name` (already done in mockup); (b) stringify non-string result content via `typeof c==='string'?c:JSON.stringify(c)` so object results don't render `[object Object]`. Use `callPreview` (slice-then-display) rather than the current escape-then-slice(60) that can cut an HTML entity.
- **Thinking via `renderSubsteps`:** encrypted/empty thinking (`(it.thinking||'').trim()` falsy, incl. `redacted_thinking`) → ONE muted italic `.think-line` "Extended thinking — encrypted, no readable content", NO control. Never dump the signature blob. Thinking WITH content → inline preview when ≤4 lines, collapsible `.step-card` with "+N lines" when longer.
- **`renderTurn`:** key off `msg.type` (real field) NOT `msg.role`. Map `type==='user'`→"You", else "Agent". Show the `tokens_in/out` sub-label. DECISION (preserve current metric visibility): show the token sub-label on ANY turn that has tokens (user or agent), not agent-only as the mockup does. Text items render as escaped `.turn-text` (pre-wrap, NO markdown).
- **Orphan / empty preservation (regression guards the mockup drops):**
  - A user turn that is ONLY `tool_result`s is a continuation → skip its empty shell (mockup behavior) — but FIRST mark those results consumed.
  - After rendering all turns, any `tool_result` NOT in the `consumed` set (orphan/unpaired) renders as a standalone `.result-row` so it does not VANISH (current code always shows results).
  - Empty-message fallback: if a non-continuation message produced no turn content, show raw escaped content, or muted "(empty message)" when raw is empty/`[]`/`""` (port lines 2010-2017).
- **Breadcrumb + derived sessions live INSIDE the component body** but their navigation calls `opts.onOpenInteraction(sid, projectId)` (injected) — the component must not own `openInteraction`. Restyle derived sessions into a Quiet-Canvas list (neutral rows, no green cost) and fix the pre-existing half-broken collapse by using the new `.step-card.open` class-toggle model instead of the old `maxHeight`/`.collapsed` engine. Route the parent `session_id`/`project_id` through `escapeAttr` (current breadcrumb injects raw — fix it).
- **Component header (`.tx-head`):** title (`summary||display||'Interaction'`, ellipsis), meta chips (timestamp, N messages = `messages.length`, tokens, `$cost`, model via `shortenModel`, copyable session-id span). Drop the inline cost-green `#00D35D`; cost chip is neutral per Quiet Canvas. The session-id span calls the injected `opts.copySessionId`. Close button calls `opts.onClose`.
- **Self-contained wiring (`wireComponent(root)`):** use event delegation scoped to the component root for: `.call-row` expand (toggle `.open` + `aria-expanded`), `.result-expand` "show full", close, breadcrumb/derived nav, session-id copy. Do NOT use inline `onclick` and do NOT reuse the old `toggleCollapsible` maxHeight engine (the new caret/`.open` CSS replaces it).

## Step 4 — Mount API + popup shell integration (keep KB + Cost working)
Define the mount entrypoint:
```
mountTranscript(host, data, opts) -> { root, headEl, auxEl, bodyEl, destroy }
```
- `host`: the modal container element (popup now; `.shell-dock` later).
- `data`: the resolved `/api/interaction` response `{ interaction, messages, cost_profile }`.
- `opts`: `{ onClose, onOpenInteraction, copySessionId, container:'popup'|'dock' }`.
- The component renders `.tx-head`, then an EMPTY `.tx-aux` slot (`auxEl`), then `.tx-body`. The shell fills `auxEl` with the KB band + the Transcript/Cost tab strip, and appends the Cost panel after `.tx-body`. This keeps the component container-agnostic (in dock the slot can stay empty / host a compact summary) while KB + Cost keep working in the popup.

Rework `openInteraction(sessionId, projectId)` (currently lines 1779-1822) to:
1. Show overlay + a designed loading state in the body region.
2. Keep the SAME 3 parallel fetches (`/api/interaction{?project}`, `/api/kb/status`, `/api/interaction/{id}/metadata{?project}`), same `.catch->null` tolerance, same single error trigger on the conv fetch.
3. Call `mountTranscript(modalHost, data, { onClose: closeModal, onOpenInteraction: openInteraction, copySessionId, container:'popup' })`.
4. Into the returned `auxEl`: render `renderKbEnrichment(kbData, sqlMeta)` (unchanged logic, still `textContent`-safe; KB badge colors stay as-is for v1) and the tab strip. Append `#costProfileBody` and call `renderCostProfile(data.cost_profile)`.
5. Tab switch toggles `.tx-body` vs `#costProfileBody`; `.tx-head` + KB band stay visible across tabs (preserve current persist-across-tabs behavior). Move the duplicated inline tab hex (`#004BFF/#000000/#666666` in 3 places) onto a CSS `.active` class using `--accent`.
6. Preserve `_costProfileChart` destroy-before-recreate (line ~2096) to avoid Chart.js leaks.

Close paths stay SHELL-owned and popup-specific: close button (via `opts.onClose`), backdrop click (`e.target===overlay`), global ESC. These are injected so the future dock variant supplies its own (no backdrop). Preserve deep-link `?session=` auto-open (no projectId) and `copySessionId` http-LAN `execCommand` fallback (needed on minimacs over http).

## Step 5 — Retire old transcript code/CSS (only after Step 3-4 replace it)
Remove `renderThinking`, `renderToolUse`, `renderToolResult`, the old item-dispatch loop, old `getToolIcon` (emoji), and the old transcript CSS (`.message`, `.tool-call`, `.thinking-block`, `.tool-output`, `.tool-result-standalone`, `.toggle-icon` collapse, `.subagents*`). Remove `toggleCollapsible` ONLY if the Step-0 grep confirms no remaining consumer. Keep shared utils (`escapeHtml`, `escapeAttr`, `formatNumber`, `formatTimestamp`, `shortenModel`, `shortenSessionId`, `copySessionId`) — they are used by both the component and the list table.

## Step 6 — Manual e2e (macChia dev; minimacs is prod)
Spin a temp `lav-server` with role overridden to `both` on a spare port (e.g. 8765) via a one-off Python launcher that monkey-patches `lav.server._runtime_config` (never edit the prod agent config). Then in the browser against `http://localhost:8765/interactions.html`:
1. Open an interaction → transcript renders; user vs agent turns distinguishable by label + accent rail only (no purple/teal/green).
2. A `tool_use` with its `tool_result` shows ONE bound card (no duplication); short input = no caret, long input (old/new_string, >4 lines) = caret + "+N lines" expands to detail.
3. Error result (`is_error:true`) is the ONLY red; the matching card shows `.is-error`.
4. Encrypted/empty thinking shows the single muted italic line — NOT a blob.
5. A known tool (Write/Glob/Task/TodoWrite) shows a real icon, not the wrench; an `mcp__*` tool falls back to wrench.
6. An interaction whose API content arrives as a JSON string still renders (parseMessageContent path).
7. Orphan `tool_result` (no matching tool_use) still appears; an empty message shows "(empty message)".
8. Breadcrumb (child interaction) and derived-sessions rows navigate via `onOpenInteraction`; project context preserved.
9. KB band renders above tabs and persists when switching to Cost; Qdrant vs SQL source still chosen correctly.
10. Cost tab: chart + totals + message-level table render; reopening a different interaction does not duplicate/leak the chart.
11. Copy session-id works (and over plain http via fallback); close via X, backdrop, and ESC; deep-link `?session=` auto-opens.
12. Long result clamps with mask + "Show full result (+N lines)" that expands.

## Step 7 — Docs + commit (after approval, per project workflow)
Update `docs/CHANGELOG.md` under `## Unreleased` with `LAV-XX:` entry. CLAUDE.md only if architecture notes change. Static-only change → deploy on minimacs is `git pull` + hard refresh (no restart). Ask before commit; reference the ticket.

## Checklist di parità (acceptance)

- [ ] parseMessageContent normalization preserved verbatim: null->[{text:''}]; string-that-parses-to-array->array; string-that-parses-to-non-array->[{text: ORIGINAL string}]; non-JSON string->[{text:content}]; array->as-is; other object->[{text:String(content)}]
- [ ] Turn rendering keys off msg.type (not msg.role); type 'user'->You label, otherwise Agent label
- [ ] tool_use is bound to its tool_result inline on ONE card (no double-render of the result)
- [ ] Pairing still finds the immediate-next-user-message result AND now any same-session result (global map) without mis-binding on duplicate ids
- [ ] Orphan/unpaired tool_result still renders (standalone) and does not vanish
- [ ] A user turn that is ONLY tool_results is skipped as a continuation (no empty shell)
- [ ] Empty-message fallback preserved: raw escaped content, or muted '(empty message)' for empty/'[]'/'""'
- [ ] Encrypted/empty thinking (incl. redacted_thinking) -> single muted italic line, never the signature blob
- [ ] Thinking with readable content is shown (inline when short, collapsible with +N lines when long)
- [ ] Text rendered as plain escaped text with pre-wrap; NO markdown introduced (XSS surface unchanged)
- [ ] escapeHtml is the body-context XSS boundary; tool name is now escaped; breadcrumb parent ids routed through escapeAttr
- [ ] getToolIcon covers all current tools (Read,Bash,Edit,Grep,Write,Glob,Task,Skill,WebSearch,WebFetch,TodoWrite) + default wrench + mcp__* fallback
- [ ] Non-string tool result/output content is JSON.stringified (no [object Object])
- [ ] Tool input preview does not cut HTML entities (slice happens before escaping)
- [ ] Token sub-label shown on any turn that has tokens_in/out (metric visibility not reduced)
- [ ] Modal header shows title (summary||display||'Interaction', truncated), timestamp, messages.length, total_tokens, $cost, shortened model, copyable session-id
- [ ] Cost no longer rendered in green; single-accent + red-only-errors enforced in the component
- [ ] Breadcrumb-to-parent renders for child interactions and navigates via injected onOpenInteraction with project context
- [ ] Derived/child sessions block renders, navigates via onOpenInteraction, and its collapse no longer leaves stray padding
- [ ] copySessionId works incl. the http-LAN execCommand fallback; transient Copied/Copy-failed feedback
- [ ] KB enrichment band renders above the tabs, persists across Transcript/Cost tab switches, chooses Qdrant over SQL fallback, and stays display:none when neither source
- [ ] Cost tab renders summary cards, cumulative Chart.js chart, totals table, and message-level table; _costProfileChart destroyed before recreate (no leak on reopen)
- [ ] Three parallel fetches preserved with kb/metadata .catch->null tolerance; only the conv fetch failure triggers the error state
- [ ] Loading and error states are present (designed) in the transcript region
- [ ] Close via X button, backdrop click (overlay target only), and global ESC all work; tab state resets to Transcript on each open
- [ ] Deep-link ?session= still auto-opens (no projectId); projectId omitted -> request without ?project
- [ ] Cross-surface CSS classes used by the list table (.kb-sensitivity, .badge-classification) are left intact
- [ ] Single file, vanilla JS, no build step, no new dependencies

## Rischi

- Pairing scope widens from next-message-only to a global tool_use_id map: if a tool_use_id ever repeats within a session, a result could bind to the wrong call. Mitigate by testing real sessions and keeping the consumed-set + orphan fallback.
- The .tx-aux slot approach interleaves shell DOM (KB band + tabs) between component-owned head and body; if the dock shell later wants a different layout this slot contract must hold. Documented as the seam.
- Removing the old toggleCollapsible/maxHeight engine and old transcript CSS could break any non-transcript consumer; Step-0 grep must confirm none remain before deletion.
- Persist-KB-across-tabs + component-owned head requires .tx-head and KB band to sit above the tab-switched panels; getting the DOM order wrong would hide KB on the Cost tab or duplicate the header.
- No in-flight fetch abort on close today; reworking openInteraction could surface stale-render-into-closed-modal if mount runs after close. Low risk but worth a guard (check overlay still active before mounting).
- KB classification/sensitivity/tag colors and the Cost tab green/multi-color palette remain non-monochrome in v1 (kept working, not re-tokenized); this is an intentional scope cut that leaves a visual inconsistency between the restyled transcript and the still-colorful KB/Cost chrome.
- Chart.js _costProfileChart is module-global; if the rework changes init timing the destroy-before-recreate guard must stay or charts leak/duplicate on reopen.
- Extending the SVG icon set is hand-work; a missed tool name silently falls back to the wrench (visual-only regression, not functional).

## Domande aperte

- Token sub-label: preserve current behavior (show on ANY turn with tokens, incl. user) or follow the mockup (agent-turn only)? Plan assumes preserve-current; confirm.
- KB band + Cost tab colors: leave as-is for v1 (transcript-only restyle) or also neutralize to Quiet Canvas now? Plan defers them; confirm scope.
- Dock/Popup segmented toggle from the mockup: omit entirely in v1, or render it disabled/'coming soon' to advertise the future dock? Plan omits it (popup-only) unless you want the affordance shown.
- Should v1 add the accessibility wins the mockup implies (role=dialog/aria-modal, focus trap, body scroll-lock) or keep current behavior to stay minimal? Plan keeps current to limit blast radius.
- Provenance label ('Qdrant KB' vs 'SQL Classification') and host/source plain spans in the KB meta row: keep exactly as today, or restyle within v1?
- Where should the close button live visually given the component owns .tx-head but the shell owns backdrop/tabs — top-right of the component head (plan's choice) acceptable?
