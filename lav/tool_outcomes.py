"""LAV-78: outcome of tool calls (status, error text, duration).

The six "tool tables" (file_operations, bash_commands, search_operations,
skill_invocations, subagent_invocations, mcp_tool_calls) record WHAT an agent
invoked but never HOW IT WENT. This module holds the shared vocabulary used to
correlate a `tool_use` block with its `tool_result` block and to write the
outcome back onto the row that produced it:

  tool_call_id  TEXT     toolu_* / call_* — correlation only, NEVER a key
                         (~3% of tool_use ids are duplicated at the source)
  is_error      INTEGER  NULL = no tool_result ever seen | 0 = ok | 1 = error
  duration_ms   INTEGER  wall clock call->result, NULL when not derivable
  error_text    TEXT     bash_commands + mcp_tool_calls only, capped at 2000 chars
  exit_code     INTEGER  bash_commands only, parsed out of the error text

is_error: the 0-vs-NULL rule (the crux)
---------------------------------------
Write 0 when a tool_result EXISTS but carries no `is_error` key — measured on 80
real transcripts, the absence of the key means success (345 keyless successes).
Write NULL ONLY when no tool_result was ever seen: truncated transcript, or a
source that does not emit results at all (e.g. Codex). NULL is never "assumed
success" — every error-rate query MUST exclude NULL from the DENOMINATOR.

duration_ms: what it measures
-----------------------------
It is the WALL CLOCK delta between the timestamp of the record carrying the
tool_use and the timestamp of the record carrying the tool_result. Native
duration fields exist in only ~1.4% of results, so they are not modellable; this
derived delta is available ~100% of the time. It therefore INCLUDES the time the
call sat waiting for a permission prompt, and background tasks can stretch it to
tens of minutes (measured p50 1.0s, p90 5.8s, p99 157s, max 76min). The tail is
real signal, so it is NOT clamped — only obviously broken values (negative, or
beyond MAX_DURATION_MS) are dropped to NULL.

This module must stay importable on its own: `lav.parsers.jsonl` imports it, so
it must NEVER import `lav.parsers.jsonl` at module level (circular import). The
two constants below come from `lav.config`, which imports neither — the two bash
helpers live in jsonl.py and are imported lazily inside the one function that
needs them (see `_bash_helpers`).
"""

import json
import re
import sqlite3

# Safe at module level: lav.config imports only lav/__init__ (stdlib) and
# lav.taxonomy — it never imports lav.parsers.jsonl, so no cycle.
from lav.config import FILE_OPERATION_TOOLS, SEARCH_TOOLS


# ===========================================================================
# SCHEMA VOCABULARY
# ===========================================================================

# The six tool tables, in the order apply_tool_outcome() sweeps them.
TOOL_TABLES = (
    "file_operations",
    "bash_commands",
    "search_operations",
    "skill_invocations",
    "subagent_invocations",
    "mcp_tool_calls",
)

# Single source of truth for the new columns: table -> [(column, "TYPE [DEFAULT ...]")].
# The SCHEMA literal in jsonl.py AND _migrate_add_tool_outcomes() must both be
# derived from this, otherwise a fresh DB and a migrated DB diverge.
# NO NOT NULL anywhere: SQLite forbids ADD COLUMN NOT NULL without a constant
# default, and the parsers use INSERT OR IGNORE, where a constraint violation
# would SILENTLY DISCARD THE ROW.
OUTCOME_COLUMNS = {
    "file_operations": [
        ("tool_call_id", "TEXT DEFAULT ''"),
        ("is_error", "INTEGER"),
        ("duration_ms", "INTEGER"),
    ],
    "bash_commands": [
        ("tool_call_id", "TEXT DEFAULT ''"),
        ("is_error", "INTEGER"),
        ("duration_ms", "INTEGER"),
        ("error_text", "TEXT DEFAULT ''"),
        ("exit_code", "INTEGER"),
    ],
    "search_operations": [
        ("tool_call_id", "TEXT DEFAULT ''"),
        ("is_error", "INTEGER"),
        ("duration_ms", "INTEGER"),
    ],
    "skill_invocations": [
        ("tool_call_id", "TEXT DEFAULT ''"),
        ("is_error", "INTEGER"),
        ("duration_ms", "INTEGER"),
    ],
    "subagent_invocations": [
        ("tool_call_id", "TEXT DEFAULT ''"),
        ("is_error", "INTEGER"),
        ("duration_ms", "INTEGER"),
    ],
    "mcp_tool_calls": [
        ("tool_call_id", "TEXT DEFAULT ''"),
        ("is_error", "INTEGER"),
        ("duration_ms", "INTEGER"),
        ("error_text", "TEXT DEFAULT ''"),
    ],
}

# Partial indexes on the correlation key — one per tool table. Kept here so the
# SCHEMA literal and the migration create byte-identical DDL.
OUTCOME_INDEX_SQL = tuple(
    "CREATE INDEX IF NOT EXISTS idx_{t}_tool_call ON {t}(session_id, project_id, tool_call_id) "
    "WHERE tool_call_id != ''".format(t=_t)
    for _t in TOOL_TABLES
)

# error_text is truncated to this many characters before it hits the DB.
ERROR_TEXT_CAP = 2000

# Sanity ceiling for the derived duration: 7 days. This is NOT a clamp of long
# legitimate waits (the 76min tail is kept) — it only drops garbage produced by
# clock skew or by a mis-paired tool_call_id.
MAX_DURATION_MS = 7 * 24 * 60 * 60 * 1000

# ^\s*Exit code (\d+) on the tool_result text. Anchored at the start of the
# string (NOT multiline): verified on real transcripts, MULTILINE matches
# exactly the same rows and only adds false-positive risk from command output.
EXIT_CODE_RE = re.compile(r"^\s*Exit code (\d+)")

# Tables that also carry error_text / exit_code (derived from OUTCOME_COLUMNS
# so there is only one place to edit).
_ERROR_TEXT_TABLES = {t for t, cols in OUTCOME_COLUMNS.items()
                      if any(c == "error_text" for c, _ in cols)}
_EXIT_CODE_TABLES = {t for t, cols in OUTCOME_COLUMNS.items()
                     if any(c == "exit_code" for c, _ in cols)}

# duration_ms is computed IN SQL, from the row's own timestamp, never in Python:
# the tool_use may have been parsed in run N and its tool_result arrive in run
# N+1, so the delta must be evaluated against whatever is already stored.
# julianday() returns NULL for '' and for unparseable strings, which is exactly
# the "not derivable" case. Placeholders (in order): result_ts, result_ts,
# MAX_DURATION_MS, result_ts.
_DURATION_SQL = (
    "CASE WHEN timestamp IS NULL OR timestamp = '' OR julianday(timestamp) IS NULL "
    "          OR julianday(?) IS NULL "
    "     THEN NULL "
    "     WHEN CAST((julianday(?) - julianday(timestamp)) * 86400000 AS INTEGER) "
    "          BETWEEN 0 AND ? "
    "     THEN CAST((julianday(?) - julianday(timestamp)) * 86400000 AS INTEGER) "
    "     ELSE NULL END"
)


# ===========================================================================
# LAZY HELPERS (no circular import)
# ===========================================================================

_BASH_HELPERS = None


def _bash_helpers():
    """Return (get_bash_category, extract_target_file).

    Imported lazily from lav.parsers.jsonl: jsonl imports THIS module at module
    level, so importing it back at module level would be a cycle. Deliberately
    NOT duplicated here — a re-implementation that drifts would make
    tool_row_matches() claim a WRONG file_path, which silently stamps the
    outcome of one call onto another row.

    LAV-79 dropped `is_file_related_bash` from this tuple: the parser no longer
    gates the Bash branch on it, so mirroring it here would make
    tool_row_matches() return [] for ~75% of shell calls.
    """
    global _BASH_HELPERS
    if _BASH_HELPERS is None:
        from lav.parsers.jsonl import (  # noqa: PLC0415 (lazy on purpose)
            extract_target_file,
            get_bash_category,
        )
        _BASH_HELPERS = (get_bash_category, extract_target_file)
    return _BASH_HELPERS


# ===========================================================================
# PARSING HELPERS
# ===========================================================================

def split_mcp_tool_name(tool_name):
    """'mcp__server__the__tool' -> ('the__tool', 'server'); non-mcp -> (tool_name, '').

    Reproduces exactly the split already done in process_tool_call (jsonl.py),
    so the (tool_name, server_name) pair here matches what was inserted into
    mcp_tool_calls.
    """
    if not tool_name or not tool_name.startswith("mcp__"):
        return (tool_name or "", "")
    parts = tool_name.split("__")
    if len(parts) >= 3:
        return ("__".join(parts[2:]), parts[1])
    return (tool_name, "")


def iter_content_blocks(content):
    """Tolerant reader for the raw `messages.content` column.

    Accepts a JSON string, an already-decoded list/dict, or a plain non-JSON
    string (Claude Code stores user text verbatim). Returns the list of dict
    blocks, [] on anything else. Never raises.
    """
    if content is None:
        return []
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8", "replace")
        except Exception:
            return []
    if isinstance(content, str):
        text = content.strip()
        if not text or text[0] not in "[{":
            return []
        try:
            content = json.loads(text)
        except (ValueError, TypeError):
            return []
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]


def _flatten_result_content(content):
    """Flatten a tool_result `content` (str, or list of {'type':'text',...}) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        if content is None:
            return ""
        return str(content)
    parts = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            if item.get("type") == "text":
                parts.append(item.get("text", "") or "")
            elif isinstance(item.get("text"), str):
                parts.append(item["text"])
    return "\n".join(p for p in parts if p)


def _is_truthy_error(value):
    """is_error flag -> 0/1. ABSENT KEY -> 0 (measured: absence means success).

    Bare truthiness, except for the string forms some exporters emit
    ('false'/'0'/'no' are non-empty strings and would otherwise read as errors).
    """
    if isinstance(value, str):
        return 1 if value.strip().lower() not in ("", "false", "0", "no", "none", "null") else 0
    return 1 if value else 0


def outcome_from_tool_result(block):
    """Extract the outcome from one `tool_result` content block.

    Returns {"is_error": 0|1, "error_text": str, "exit_code": int|None}.
    An ABSENT is_error key means success (0) — see the module docstring. NULL is
    never produced here: this function is only called when a tool_result exists.
    """
    if not isinstance(block, dict):
        return {"is_error": 0, "error_text": "", "exit_code": None}

    is_error = _is_truthy_error(block.get("is_error"))
    if not is_error:
        # Only errors carry text — success payloads are huge and useless here.
        return {"is_error": 0, "error_text": "", "exit_code": None}

    text = _flatten_result_content(block.get("content"))
    exit_code = None
    m = EXIT_CODE_RE.search(text)
    if m:
        try:
            exit_code = int(m.group(1))
        except (TypeError, ValueError):
            exit_code = None
    return {
        "is_error": 1,
        "error_text": text[:ERROR_TEXT_CAP],
        "exit_code": exit_code,
    }


# ===========================================================================
# ROUTING: which row(s) did this tool_use produce?
# ===========================================================================

def tool_row_matches(tool_name, tool_input):
    """Rows a given tool_use maps to: [(table, extra_where_sql, extra_params)].

    ALWAYS a list: a Bash call whose category AND target file both resolve (e.g.
    `cat foo.py`) writes BOTH a file_operations row and a bash_commands row
    (jsonl.py process_tool_call). `extra_where_sql` is only the discriminating
    tail — the caller adds timestamp/session_id/project_id.

    Mirrors the routing and the NOT EXISTS guard keys of process_tool_call:
        file_operations      tool = ? AND file_path = ?
        bash_commands        command = ?
        search_operations    tool = ? AND pattern = ?
        skill_invocations    skill_name = ?
        subagent_invocations subagent_type = ? AND description IS ?
        mcp_tool_calls       tool_name = ?

    Returns [] for a tool that lands in no table. Claiming a row that was never
    written is harmless (the UPDATE is a no-op); claiming a WRONG file_path is
    not, which is why the Bash branch re-runs the exact same guards as the
    parser instead of guessing.
    """
    if not tool_name:
        return []
    if not isinstance(tool_input, dict):
        tool_input = {}

    matches = []

    if tool_name in FILE_OPERATION_TOOLS:
        file_path = tool_input.get("file_path", "")
        if file_path:
            matches.append(("file_operations", "tool = ? AND file_path = ?",
                            [tool_name, file_path]))

    elif tool_name == "Bash":
        command = tool_input.get("command", "")
        # LAV-79: mirrors process_tool_call EXACTLY — a non-str payload is
        # coerced away and an empty/whitespace-only command writes nothing;
        # everything else writes a bash_commands row. There is no longer an
        # is_file_related_bash() gate on either side. Keep the two in step: if
        # this stayed gated, every bash_commands row with an EMPTY tool_call_id
        # (agents on pre-LAV-78 code, Codex rows without a call_id) would be
        # permanently unbackfillable by stamp_tool_call_id() — is_error would
        # stay NULL forever and the row would drop out of the error-rate
        # denominator unnoticed.
        if isinstance(command, str) and command.strip():
            # file_operations row only when BOTH the category and the target
            # file resolve — otherwise the parser wrote no such row. Note the
            # `tool` column holds the category ('BashRead'/'BashWrite'), never
            # the literal 'Bash'.
            get_bash_category, extract_target_file = _bash_helpers()
            bash_category = get_bash_category(command)
            target_file = extract_target_file(command)
            if bash_category and target_file:
                matches.append(("file_operations", "tool = ? AND file_path = ?",
                                [bash_category, target_file]))
            # ...but the bash_commands row is ALWAYS written for a non-blank
            # command, file-related or not (`git push`, `python3 x.py`, `cd /tmp`).
            matches.append(("bash_commands", "command = ?", [command]))

    elif tool_name in SEARCH_TOOLS:
        pattern = tool_input.get("pattern", "")
        if pattern:
            matches.append(("search_operations", "tool = ? AND pattern = ?",
                            [tool_name, pattern]))

    elif tool_name == "Skill":
        skill_name = tool_input.get("skill", "")
        if skill_name:
            matches.append(("skill_invocations", "skill_name = ?", [skill_name]))

    elif tool_name == "Task":
        subagent_type = tool_input.get("subagent_type", "")
        if subagent_type:
            # `description` may be NULL in the DB (collector sync ships whatever
            # the agent had): "description = ?" never matches NULL in SQL, so
            # use IS, which behaves like = for non-NULL values.
            description = tool_input.get("description", "")
            matches.append(("subagent_invocations",
                            "subagent_type = ? AND description IS ?",
                            [subagent_type, description]))

    elif tool_name.startswith("mcp__"):
        mcp_tool, _server_name = split_mcp_tool_name(tool_name)
        matches.append(("mcp_tool_calls", "tool_name = ?", [mcp_tool]))

    return matches


# ===========================================================================
# WRITES
# ===========================================================================

def apply_tool_outcome(conn, session_id, project_id, tool_call_id, outcome, result_ts):
    """Stamp the outcome of one tool_result onto every row carrying tool_call_id.

    Sweeps ALL SIX tables — it must not stop at the first hit, a file-related
    Bash lives in two of them. Sets is_error / duration_ms everywhere, plus
    error_text where the column exists and exit_code on bash_commands.

    duration_ms is computed in SQL against the row's own timestamp (see
    _DURATION_SQL) so it stays correct across incremental runs. Returns the
    total number of rows updated.
    """
    if not tool_call_id or not session_id or project_id is None:
        return 0
    if not isinstance(outcome, dict):
        return 0

    is_error = outcome.get("is_error")
    error_text = (outcome.get("error_text") or "")[:ERROR_TEXT_CAP]
    exit_code = outcome.get("exit_code")
    ts = result_ts if isinstance(result_ts, str) and result_ts.strip() else None

    total = 0
    for table in TOOL_TABLES:
        set_parts = ["is_error = ?"]
        params = [is_error]

        if table in _ERROR_TEXT_TABLES:
            set_parts.append("error_text = ?")
            params.append(error_text)
        if table in _EXIT_CODE_TABLES:
            set_parts.append("exit_code = ?")
            params.append(exit_code)

        if ts is None:
            # No usable result timestamp: keep is_error, drop the duration.
            set_parts.append("duration_ms = NULL")
        else:
            set_parts.append("duration_ms = " + _DURATION_SQL)
            params.extend([ts, ts, MAX_DURATION_MS, ts])

        params.extend([session_id, project_id, tool_call_id])
        sql = (
            "UPDATE {table} SET {sets} "
            "WHERE session_id = ? AND project_id = ? AND tool_call_id = ? "
            "AND tool_call_id != ''"
        ).format(table=table, sets=", ".join(set_parts))

        try:
            cur = conn.execute(sql, params)
            total += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        except sqlite3.Error as e:
            print(f"DB error (outcome {table}): {e}")

    return total


def stamp_tool_call_id(conn, session_id, project_id, timestamp, tool_name, tool_input,
                       tool_call_id):
    """Backfill only: set tool_call_id on PRE-EXISTING rows that have none.

    Rows are matched with tool_row_matches() plus (timestamp, session_id,
    project_id) — the same key the parser's NOT EXISTS guards use. The
    "(tool_call_id IS NULL OR tool_call_id = '')" clause makes it strictly
    additive: an id already written by the parser is never overwritten.
    Returns the number of rows updated.
    """
    if not tool_call_id or not session_id or project_id is None:
        return 0
    if not timestamp:
        return 0

    total = 0
    for table, extra_where, extra_params in tool_row_matches(tool_name, tool_input):
        sql = (
            "UPDATE {table} SET tool_call_id = ? "
            "WHERE session_id = ? AND project_id = ? AND timestamp = ? "
            "AND (tool_call_id IS NULL OR tool_call_id = '') "
            "AND {extra}"
        ).format(table=table, extra=extra_where)
        params = [tool_call_id, session_id, project_id, timestamp] + list(extra_params)
        try:
            cur = conn.execute(sql, params)
            total += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        except sqlite3.Error as e:
            print(f"DB error (stamp {table}): {e}")

    return total
