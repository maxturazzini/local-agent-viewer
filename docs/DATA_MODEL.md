# LAV — Conversation Data Model

> Reference document. Explains how LocalAgentViewer (LAV) represents
> conversations from various AI agents in a single database, why there are
> **two different conversation models**, and what the main queries return.
>
> All example numbers in this document are **real**, extracted from the test
> DB used during development of this feature.

---

## 1. The 5 sources and the 4 filter dimensions

LAV reads logs from different agents and normalizes them into **a single
SQLite database**. Every conversation is tagged with its source
(`session_sources.source`).

### The 5 sources

| Source (`source`) | Where from | `session_id` prefix |
|---|---|---|
| `claude_code` | Claude Code CLI (local JSONL) | bare UUID — e.g. `3e8d411d-4f4e-...` |
| `codex_cli` | Codex CLI | `codex:` + uuid |
| `cowork_desktop` | Cowork (Claude Desktop app) | `cowork:` + uuid |
| `chatgpt` | ChatGPT export | `chatgpt:` + id |
| `claude_ai` | claude.ai account export | `claudeai:` + id |

> Note on the test DB: it contains 4 of the 5 sources (missing `chatgpt`).
> Real interaction counts in the test DB:

| Source | Interactions |
|---|---|
| `claude_code` | 4144 |
| `claude_ai` | 1515 |
| `cowork_desktop` | 96 |
| `codex_cli` | 34 |
| **Total** | **5789** |

The `session_id` prefix is the fastest way to recognize the source at a
glance: `claude_code` uses the bare UUID, all others are prefixed.

### The 4 filter dimensions

Every query (dashboard, API, CLI) can filter on 4 **independent** axes:

| Dimension | Table | Meaning | Examples in the test DB |
|---|---|---|---|
| **Project** | `projects` | which codebase / folder | `miniMe`, `viewer`, `outputs`, ... (106 projects) |
| **User** | `users` | which person | `alice` |
| **Host** | `hosts` | which machine | `dev-host`, `laptop`, `cloud` |
| **Source** | `session_sources` | which agent | `claude_code`, `cowork_desktop`, ... |

Filters combine freely (AND). An interaction's primary key is **composite**:
`interactions(session_id, project_id)`. The same `session_id` can therefore
appear under multiple projects (see §2.2).

---

## 2. The TWO conversation models (and why they differ)

Different agents log the same "conversation" in structurally different ways.
LAV reconciles them with **two strategies**:

| Model | Typical source | Relationship | `parent_session_id` |
|---|---|---|---|
| **MERGED** | `cowork_desktop` | one conversation = one row | always `NULL` |
| **MASTER / DERIVED** | `claude_code` | one master + N subagents | children point to the master |

### 2.1 MERGED model — Cowork

**The problem.** Cowork logs **a single conversation across two layers**:

1. a "shell" identified by the folder-uuid, which contains the **human
   turns** (the dialogue visible to the user);
2. an **internal agent session** that contains the full execution and the
   **real tokens** (tool calls, thinking, responses).

They are the **same** dialogue, split across two files. Keeping them separate
would produce two incomplete rows: one with human prompts but no costs, one
with costs but no human context.

**The solution.** `parse_cowork_sessions` **merges** the two layers under the
folder-uuid (id prefixed `cowork:`):

- relabels every event to the master folder-uuid;
- **discards** the duplicate turns from the human shell;
- titles the row with the **first human prompt**;
- tokens and cost are the **real** ones from the internal session.

Result: **no parent/child relationship** for Cowork. One conversation = one
row, `parent_session_id = NULL`, the transcript contains the entire human +
assistant dialogue.

**Real example — `cowork:dc3b5fe9-d23d-4f6b-bb02-d417e1d8128f`**

| Field | Value |
|---|---|
| `session_id` | `cowork:dc3b5fe9-d23d-4f6b-bb02-d417e1d8128f` |
| `parent_session_id` | `NULL` |
| Title (`display`) | *"voglio usare pgvis"* (first human prompt) |
| Messages in transcript | **210** (78 user + 132 assistant) |
| `total_tokens` | **7,520,969** |
| Cost | **$5.1158** |
| `children` (in detail) | **0** |

Everything in **a single row**: the human dialogue and the agent's execution
are already merged.

### 2.2 MASTER / DERIVED model — Claude Code

**The case.** Claude Code launches **real subagents**: each lives in its own
`agent-*.jsonl` file. These are NOT duplicates to discard — they are distinct
units of work with their own tokens. They are already linked to the master via
`parent_session_id`.

**The solution.** LAV keeps them as separate rows but **groups** them under
the master:

- the **grouped list** shows **only masters** (`parent_session_id IS NULL`,
  or the self-parent case where the master points to itself);
- the master's cost and tokens are **rolled up** across the entire subagent
  tree (recursive, arbitrary depth);
- `derived_count` = number of descendants is added.

**Real example — master `3e8d411d-4f4e-417f-afff-4e03654b975a`** (project `id=1`)

| View | `total_tokens` | Cost | `derived_count` |
|---|---|---|---|
| **Flat** (master only, per-session) | 7,870,243 | $9.0065 | 0 |
| **Grouped** (master + entire tree) | **14,680,355** | **$21.102** | **40** |

In grouped mode the master "absorbs" the cost of its subagents: from $9.00
(alone) to $21.10 (itself + the 40 derived). The title stays the master's:
*"Claude CLI Update Lock Files Bug Fix"*.

> **Note on 40 vs 42.** `get_interaction_children` (see §4) is
> *project-agnostic* and returns **42** direct children, because the same
> subagent `session_id` can be materialized under **multiple projects**
> (composite primary key: here the subagent appears under `project_id=1` and
> `project_id=65`). The grouped list's roll-up, on the other hand, dedupes
> cross-project duplicates with a visited-set on `(session_id, project_id)`
> and counts each node **once**: hence `derived_count=40`. Both figures are
> correct — they answer different questions ("how many child rows exist" vs
> "how many unique subagents am I summing into the cost").

---

## 3. Grouped list vs flat list

`get_interactions_list(..., grouped=True|False)` has two modes:

| | `grouped=True` (default, dashboard) | `grouped=False` (flat) |
|---|---|---|
| Rows shown | only top-level masters | **every** session |
| Cost / tokens | rolled up across master + descendants | per-session |
| `derived_count` | number of descendants | always `0` |
| Parent filter | `parent_session_id IS NULL` or self-parent or promoted orphan | none |

**What counts as "top-level" in grouped mode:** a row is top-level if
`parent_session_id IS NULL`, **or** if it points to itself (self-parent),
**or** if its parent doesn't exist anywhere (*orphan promotion* — so no row
disappears). The parent lookup is **project-agnostic**: it matches only on
`session_id` (globally unique UUID), because a Cowork master lives in one
project while its events may end up under different inferred projects.

For Cowork, the grouped/flat distinction is nearly irrelevant: having no
children, a Cowork conversation is already a single top-level row in both
modes.

**Important — search / CLI / MCP remain FLAT.** Text search (`lav search`),
the CLI, and the MCP tools always work in flat mode: every session is its own
result, with its own per-session cost. Grouping is a **presentation** feature
of the dashboard list, not a way of indexing the data.

---

## 4. What the main queries return

### `get_interactions_list(...)`

Returns `{ interactions, total, limit, offset }`. Each element of
`interactions` includes, besides the base fields (`session_id`, `project_id`,
`model`, `message_count`, `display`, `summary`, ...):

- `client_source` — the source (`claude_code`, `cowork_desktop`, ...);
- `cost_usd` — cost calculated at query-time via LEFT JOIN on
  `model_pricing` (never materialized);
- `total_tokens` — in grouped mode, already rolled up across the tree;
- **`derived_count`** — how many subagents/descendants were summed
  (`0` in flat mode, or when the master has no children).

### `get_interaction_children(conn, session_id, project_id=None)`

Returns the **direct children** (one level) of a master, ordered
oldest-first. Each child has: `session_id`, `project_id`, `summary`,
`display`, `agent_id`, `timestamp`, `total_tokens`, `message_count`,
`cost_usd` (per-session).

It is **project-agnostic**: it matches only on `parent_session_id`, so it can
return children that live in different projects than the master (see the 40
vs 42 note in §2.2). The `project_id` parameter is accepted for compatibility
but **not** used as a filter.

### `get_interaction_detail(conn, session_id, project_id)`

Returns the full transcript (`messages`), plus:

- `interaction.children` = output of `get_interaction_children` (the
  derived subagents);
- `interaction.parent_interaction` if the row has a parent;
- `interaction.cost_usd` per-session.

For Cowork: `children` is empty and the transcript **already** contains the
entire human + assistant dialogue (210 messages in the example). For a Claude
Code master: `children` lists the derived subagents to expand in the detail
view.

---

## 5. One-sentence summary

- **Cowork = merged**: one `audit.jsonl` = one conversation = one row
  (`parent=NULL`), real tokens and cost, title from the first human prompt.
- **Claude Code = master/derived**: subagents remain separate rows but the
  grouped list shows only the master with cost/tokens rolled up across the
  entire tree and a `derived_count`; the detail view expands the derived
  subagents.
- **search / CLI / MCP** always remain flat (one session = one result).
