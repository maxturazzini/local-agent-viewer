#!/usr/bin/env python3
"""LAV unified CLI — query and manage the LocalAgentViewer knowledge base."""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

import lav  # noqa: F401 — triggers .env loading
from lav.config import UNIFIED_DB_PATH, QDRANT_DATA_DIR, QDRANT_COLLECTION, QDRANT_URL

_kb_store = None

# LAV-78 — sources that CANNOT be backfilled by `lav backfill tool-outcomes`.
# The backfill reconstructs tool outcomes from the raw JSON content blocks kept
# verbatim in messages.content. These two sources never store that:
#   claude_ai — claude_ai.py writes RENDERED TEXT, the tool_use/tool_result
#               blocks are already flattened away and unrecoverable.
#   chatgpt   — the ChatGPT export has no tool_result blocks at all.
# They are excluded from the session sweep and reported as an explicit skip
# line with their row counts, never silently ignored.
BACKFILL_UNSUPPORTED_SOURCES = ("claude_ai", "chatgpt")


# ── Connections ─────────────────────────────────────────────

def _get_read_connection() -> Optional[sqlite3.Connection]:
    """Read-only connection to the unified DB."""
    if not UNIFIED_DB_PATH.exists():
        return None
    conn = sqlite3.connect(str(UNIFIED_DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _get_write_connection() -> sqlite3.Connection:
    """Read-write connection to the unified DB."""
    conn = sqlite3.connect(str(UNIFIED_DB_PATH))
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _get_kb_store():
    """Lazy-init Qdrant vector store."""
    global _kb_store
    if _kb_store is None:
        from lav.qdrant.store import InteractionVectorStore
        if QDRANT_URL:
            _kb_store = InteractionVectorStore(url=QDRANT_URL, collection=QDRANT_COLLECTION)
        else:
            QDRANT_DATA_DIR.mkdir(parents=True, exist_ok=True)
            _kb_store = InteractionVectorStore(data_path=QDRANT_DATA_DIR, collection=QDRANT_COLLECTION)
        _kb_store.ensure_collection()
    return _kb_store


# ── Auth ────────────────────────────────────────────────────

def _check_write_auth():
    """Check LAV_API_KEY is set for write operations."""
    key = os.environ.get("LAV_API_KEY", "")
    if not key:
        _die("LAV_API_KEY not set. Required for write operations.")


def _check_read_auth():
    """Check LAV_READ_API_KEY if configured."""
    expected = os.environ.get("LAV_READ_API_KEY", "")
    if not expected:
        return  # open access
    # For CLI, the key is in the env — nothing to pass
    # This check is a guard for when READ key is set but env is misconfigured


# ── Output ──────────────────────────────────────────────────

def _output(data, fmt="json"):
    """Format output to stdout."""
    if fmt == "json":
        json.dump(data, sys.stdout, indent=2, ensure_ascii=False, default=str)
        sys.stdout.write("\n")
    elif fmt == "table":
        _print_table(data)
    elif fmt == "brief":
        _print_brief(data)


def _print_table(data):
    """Print data as ASCII table."""
    if isinstance(data, dict):
        # Single record — print key: value
        for k, v in data.items():
            if isinstance(v, (list, dict)):
                print(f"{k}:")
                _print_table(v)
            else:
                print(f"  {k}: {v}")
        return

    if not data:
        print("(no results)")
        return

    if not isinstance(data, list):
        print(data)
        return

    if not isinstance(data[0], dict):
        for item in data:
            print(item)
        return

    # List of dicts — tabular
    cols = list(data[0].keys())
    widths = {c: len(c) for c in cols}
    rows = []
    for row in data:
        formatted = {}
        for c in cols:
            v = row.get(c)
            s = "" if v is None else str(v)
            if len(s) > 60:
                s = s[:57] + "..."
            formatted[c] = s
            widths[c] = max(widths[c], len(s))
        rows.append(formatted)

    header = "  ".join(c.upper().ljust(widths[c]) for c in cols)
    print(header)
    print("-" * len(header))
    for row in rows:
        print("  ".join(row[c].ljust(widths[c]) for c in cols))


def _print_brief(data):
    """One line per result."""
    if isinstance(data, dict):
        # Extract list from known wrapper keys
        for key in ("interactions", "results"):
            if key in data and isinstance(data[key], list):
                data = data[key]
                break
        else:
            # Interaction detail: show messages as brief transcript
            if "messages" in data and isinstance(data["messages"], list):
                interaction = data.get("interaction", {})
                sid = interaction.get("session_id", "")
                proj = interaction.get("project_name", "")
                ts = interaction.get("timestamp", "")[:16]
                print(f"# {sid}  {proj}  {ts}")
                for m in data["messages"]:
                    role = m.get("type", "?")
                    content = (m.get("content") or "")[:120]
                    content = content.replace("\n", " ")
                    print(f"  [{role}] {content}")
                return
            data = [data]
    if not isinstance(data, list):
        print(data)
        return
    for item in data:
        if isinstance(item, dict):
            sid = item.get("session_id", "")
            ts = item.get("timestamp", "")[:16]
            proj = item.get("project_name", item.get("project", ""))
            summary = item.get("meta_summary") or item.get("summary") or item.get("display", "")
            if summary and len(summary) > 80:
                summary = summary[:77] + "..."
            print(f"{ts}  {sid[:12]}  {proj or '-':<20}  {summary}")
        else:
            print(item)


def _die(msg, code=1):
    """Print error to stderr and exit."""
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


# ── Helpers ─────────────────────────────────────────────────

def _nonneg_int(value):
    """argparse type for counts where 0 is meaningful and negatives are not.

    Guards the LAV-78 backfill args: a bare `int` would let `--limit -1` through,
    and SQLite reads a negative LIMIT as "no limit" — i.e. the exact opposite of
    what the user asked for, in write mode. Fail loudly instead.

    LAV-79 wires it to the read path too (`lav search --limit`, `lav kb search
    --limit`): same hazard, milder blast radius — `lav search --limit -1` used to
    dump the whole table instead of erroring.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"'{value}' is not an integer")
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {n}")
    return n


def _resolve_name_to_id(conn, table, column, value):
    """Resolve a name (project, user) to its integer ID."""
    from lav.queries import run_query
    rows = run_query(conn, f"SELECT id FROM {table} WHERE {column} = ?", [value])
    return rows[0]["id"] if rows else None


# ── Commands ────────────────────────────────────────────────

def cmd_search(args):
    """Search interactions (FTS5)."""
    conn = _get_read_connection()
    if not conn:
        _die("No database. Run lav-parse first.")

    from lav.queries import get_interactions_list

    try:
        project_id = _resolve_name_to_id(conn, "projects", "name", args.project) if args.project else None
        user_id = _resolve_name_to_id(conn, "users", "username", args.user) if args.user else None

        data = get_interactions_list(
            conn,
            project_id=project_id,
            user_id=user_id,
            search=args.query,
            start_date=args.start,
            end_date=args.end,
            limit=args.limit,
            grouped=False,  # search must surface every session (incl. slaves), not collapse to masters
        )
        _output(data, args.format)
    finally:
        conn.close()


def cmd_show(args):
    """Show full interaction transcript."""
    conn = _get_read_connection()
    if not conn:
        _die("No database. Run lav-parse first.")

    from lav.queries import get_interaction_detail

    try:
        data = get_interaction_detail(conn, args.session_id)
        if not data:
            _die(f"Interaction '{args.session_id}' not found")
        _output(data, args.format)
    finally:
        conn.close()


def cmd_day(args):
    """Day View bundle: per-session Gantt + worktime metrics for a single day."""
    import re
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", args.date or ""):
        _die("date must be YYYY-MM-DD")

    conn = _get_read_connection()
    if not conn:
        _die("No database. Run lav-parse first.")

    from lav.queries import get_day_bundle

    try:
        project_id = _resolve_name_to_id(conn, "projects", "name", args.project) if args.project else None
        user_id = _resolve_name_to_id(conn, "users", "username", args.user) if args.user else None
        bundle = get_day_bundle(
            conn, args.date,
            project_id=project_id,
            user_id=user_id,
            client_source=args.source,
        )
        if args.format == "brief":
            # One line per session — easy diff vs golden
            for r in bundle["rows"]:
                print(f"{r['start'][11:19]}  {r['end'][11:19]}  "
                      f"{r['project_name']:<20}  msg={r['msg_count']:<4}  "
                      f"dur={r['duration_min']:.1f}m  {r['session_id'][:8]}")
            w = bundle["worktime"]
            m = bundle["meta"]
            print(f"\n{m['total_sessions']} sessions · {m['total_projects']} projects · "
                  f"{m['total_messages']} msgs · peak {bundle['peak_concurrency']}")
            print(f"active_wallclock = {w['active_wallclock_sec']/3600:.2f}h · "
                  f"assistant_wallclock = {w['assistant_wallclock_sec']/3600:.2f}h")
        else:
            _output(bundle, args.format)
    finally:
        conn.close()


def cmd_kb_search(args):
    """Semantic search in Qdrant KB."""
    try:
        store = _get_kb_store()
    except Exception as e:
        _die(f"KB not available: {e}")

    filters = {}
    if args.classification:
        filters["classification"] = args.classification
    if args.tags:
        filters["tags"] = [t.strip() for t in args.tags.split(",")]
    if args.project:
        filters["project"] = args.project

    results = store.search(args.query, limit=args.limit, filters=filters if filters else None)
    out = [
        {"session_id": r.session_id, "score": r.score, "payload": r.payload}
        for r in results
    ]
    _output(out, args.format)


def cmd_kb_status(args):
    """Check if interaction is indexed in KB."""
    try:
        store = _get_kb_store()
    except Exception as e:
        _die(f"KB not available: {e}")

    indexed = store.is_indexed(args.session_id)
    payload = store.get(args.session_id) if indexed else None
    _output({"session_id": args.session_id, "indexed": indexed, "payload": payload}, args.format)


def cmd_kb_index(args):
    """Index an interaction into Qdrant KB."""
    _check_write_auth()

    try:
        store = _get_kb_store()
    except Exception as e:
        _die(f"KB not available: {e}")

    conn = _get_read_connection()
    if not conn:
        _die("No database. Run lav-parse first.")

    from lav.queries import get_interaction_detail
    try:
        data = get_interaction_detail(conn, args.session_id)
    finally:
        conn.close()

    if not data:
        _die(f"Interaction '{args.session_id}' not found in SQLite")

    conv = data["interaction"]
    messages = data["messages"]

    tag_list = [t.strip() for t in args.tags.split(",")] if args.tags else []
    metadata = json.loads(args.pre_metadata) if args.pre_metadata else None

    from lav.qdrant.indexer import InteractionIndexer
    indexer = InteractionIndexer(store)

    try:
        payload = indexer.index(
            session_id=args.session_id,
            messages=messages,
            project=conv.get("project_name", ""),
            timestamp=conv.get("timestamp", ""),
            user=conv.get("username", ""),
            custom_tags=tag_list if tag_list else None,
            pre_metadata=metadata,
        )
    except Exception as e:
        _die(f"Indexing failed: {e}")

    _output({"status": "indexed", "session_id": args.session_id, "payload": payload}, args.format)


def cmd_kb_remove(args):
    """Remove interaction from Qdrant KB."""
    _check_write_auth()

    try:
        store = _get_kb_store()
    except Exception as e:
        _die(f"KB not available: {e}")

    store.delete(args.session_id)
    _output({"status": "removed", "session_id": args.session_id}, args.format)


def cmd_kb_tags(args):
    """Update tags on an indexed interaction."""
    _check_write_auth()

    try:
        store = _get_kb_store()
    except Exception as e:
        _die(f"KB not available: {e}")

    if not store.is_indexed(args.session_id):
        _die(f"Interaction '{args.session_id}' not indexed. Use 'lav kb index' first.")

    tag_list = [t.strip() for t in args.tags.split(",")]
    store.update_tags(args.session_id, tag_list)
    _output({"status": "updated", "session_id": args.session_id, "tags": tag_list}, args.format)


def cmd_sync(args):
    """Trigger data sync/reparse."""
    _check_write_auth()

    from lav.server import sync_data

    result = sync_data(
        scope=args.scope,
        project=args.project,
        source=args.source,
        full=args.full,
    )
    _output(result, args.format)


def cmd_pricing(args):
    """Manage model pricing."""
    if args.action == "list":
        conn = _get_read_connection()
        if not conn:
            _die("No database. Run lav-parse first.")
        try:
            from lav.pricing import get_pricing
            rows = get_pricing(conn, model=args.model)
            _output(rows, args.format)
        finally:
            conn.close()

    elif args.action == "add":
        _check_write_auth()
        if not args.model or args.input_price is None or args.output_price is None or not args.from_date:
            _die("--model, --input, --output, and --from-date are required for 'add'")
        conn = _get_write_connection()
        try:
            from lav.pricing import upsert_pricing
            upsert_pricing(
                conn, model=args.model, input_price=args.input_price,
                output_price=args.output_price, from_date=args.from_date,
                provider=args.provider,
                cache_write=args.cache_write or 0,
                cache_read=args.cache_read or 0,
                to_date=args.to_date, notes=args.notes,
            )
            _output({"status": "added", "model": args.model, "from_date": args.from_date}, args.format)
        finally:
            conn.close()

    else:
        _die(f"Unknown pricing action '{args.action}'. Use 'list' or 'add'.")


# ── backfill: tool outcomes (LAV-78) ────────────────────────
#
# Why this exists: on the collector, rows arrive through the sync ingest guarded
# by NOT EXISTS, so an EXISTING row is never updated. A reparse on the agent
# therefore cannot heal the collector. This command rebuilds the outcome purely
# from data already sitting in the LOCAL DB (messages.content is a verbatim JSON
# passthrough of the source), so it runs identically and independently on both
# nodes — no reparse, no re-sync, no watermark touched.
#
# Auth posture: same as lav-classify — it writes to the LOCAL SQLite DB directly
# rather than calling the API, so no LAV_API_KEY is required.

# Only messages that actually carry the block we're after are fetched. instr()
# is an exact substring test (unlike LIKE, where the `_` in "tool_use" would act
# as a single-char wildcard). Note `"tool_use"` — quotes included — cannot match
# inside `"tool_use_id"`, so the two passes never overlap.
_BF_TOOL_USE_SQL = (
    "SELECT timestamp, content FROM messages "
    "WHERE session_id = ? AND project_id = ? AND instr(content, '\"tool_use\"') > 0 "
    "ORDER BY id"
)
_BF_TOOL_RESULT_SQL = (
    "SELECT timestamp, content FROM messages "
    "WHERE session_id = ? AND project_id = ? AND instr(content, '\"tool_use_id\"') > 0 "
    "ORDER BY id"
)


def _bf_table_columns(conn, table):
    """Column names of `table`, empty set when the table does not exist."""
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _bf_missing_columns(conn):
    """List of '<table>.<column>' outcome columns not present in this DB."""
    from lav.tool_outcomes import OUTCOME_COLUMNS
    missing = []
    for table, cols in OUTCOME_COLUMNS.items():
        present = _bf_table_columns(conn, table)
        if not present:
            missing.append(f"{table} (table absent)")
            continue
        missing.extend(f"{table}.{col}" for col, _type in cols if col not in present)
    return missing


def _bf_session_query(since, project, source, limit):
    """Build (sql, params) for the session sweep.

    Sessions come from `interactions` (one row per session_id+project_id) rather
    than from a DISTINCT over 595k messages. Ordered most-recent-first so
    --limit N means "the N most recent sessions".
    """
    placeholders = ",".join("?" * len(BACKFILL_UNSUPPORTED_SOURCES))
    where = [f"COALESCE(ss.source, '') NOT IN ({placeholders})"]
    params = list(BACKFILL_UNSUPPORTED_SOURCES)

    # The MAX(message timestamp) join is only paid for when --since is used.
    # Same "active since" definition as lav/backfill.py select_active_sessions.
    msg_join = ""
    if since:
        msg_join = (
            "LEFT JOIN (SELECT session_id, project_id, MAX(timestamp) AS last_msg_ts "
            "           FROM messages GROUP BY session_id, project_id) m "
            "  ON m.session_id = i.session_id AND m.project_id = i.project_id "
        )
        where.append("COALESCE(m.last_msg_ts, i.timestamp) >= ?")
        params.append(since)
    if project:
        where.append("p.name = ?")
        params.append(project)
    if source:
        where.append("COALESCE(ss.source, '') = ?")
        params.append(source)

    sql = (
        "SELECT i.session_id, i.project_id "
        "FROM interactions i "
        "JOIN projects p ON p.id = i.project_id "
        "LEFT JOIN session_sources ss "
        "  ON ss.session_id = i.session_id AND ss.project_id = i.project_id "
        + msg_join +
        "WHERE " + " AND ".join(where) + " "
        "ORDER BY i.timestamp DESC"
    )
    # `is not None`, NOT truthiness: --limit 0 must mean literally zero sessions
    # (SQLite honours LIMIT 0), while an omitted --limit stays unbounded. The
    # truthy form made `--limit 0` a full write-mode sweep. Negative values are
    # rejected at parse time (_nonneg_int) because SQLite reads LIMIT -1 as
    # "no limit".
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return sql, params


def _bf_unsupported_counts(conn):
    """Sessions + messages held by the sources that cannot be backfilled."""
    marks = ",".join("?" * len(BACKFILL_UNSUPPORTED_SOURCES))
    params = list(BACKFILL_UNSUPPORTED_SOURCES)
    out = {s: {"source": s, "sessions": 0, "messages": 0} for s in BACKFILL_UNSUPPORTED_SOURCES}
    try:
        for src, n in conn.execute(
            "SELECT ss.source, COUNT(*) FROM interactions i "
            "JOIN session_sources ss ON ss.session_id = i.session_id "
            "                       AND ss.project_id = i.project_id "
            f"WHERE ss.source IN ({marks}) GROUP BY ss.source", params
        ):
            out.setdefault(src, {"source": src, "sessions": 0, "messages": 0})["sessions"] = n
        for src, n in conn.execute(
            "SELECT ss.source, COUNT(*) FROM messages m "
            "JOIN session_sources ss ON ss.session_id = m.session_id "
            "                       AND ss.project_id = m.project_id "
            f"WHERE ss.source IN ({marks}) GROUP BY ss.source", params
        ):
            out.setdefault(src, {"source": src, "sessions": 0, "messages": 0})["messages"] = n
    except sqlite3.Error as e:
        print(f"[backfill] could not count unsupported sources: {e}", file=sys.stderr)
    for row in out.values():
        row["reason"] = ("messages.content holds rendered text, not raw JSON blocks"
                         if row["source"] == "claude_ai"
                         else "export carries no tool_result blocks")
    return [row for row in out.values() if row["sessions"] or row["messages"]]


def _bf_breakdown(conn):
    """Per-table is_error breakdown: 1 / 0 / NULL, plus correlation coverage."""
    from lav.tool_outcomes import TOOL_TABLES
    out = {}
    for table in TOOL_TABLES:
        try:
            row = conn.execute(
                "SELECT COUNT(*), "
                "       SUM(CASE WHEN is_error = 1 THEN 1 ELSE 0 END), "
                "       SUM(CASE WHEN is_error = 0 THEN 1 ELSE 0 END), "
                "       SUM(CASE WHEN is_error IS NULL THEN 1 ELSE 0 END), "
                "       SUM(CASE WHEN tool_call_id IS NOT NULL AND tool_call_id != '' "
                "                THEN 1 ELSE 0 END) "
                f"FROM {table}"
            ).fetchone()
        except sqlite3.Error as e:
            out[table] = {"error": str(e)}
            continue
        total, err, ok, unknown, correlated = (row[0] or 0, row[1] or 0, row[2] or 0,
                                               row[3] or 0, row[4] or 0)
        known = err + ok
        out[table] = {
            "rows": total,
            "is_error_1": err,
            "is_error_0": ok,
            "is_error_null": unknown,
            "with_tool_call_id": correlated,
            # NULL is never "assumed success": it is excluded from the denominator.
            "error_rate_pct": round(100.0 * err / known, 2) if known else None,
        }
    return out


def cmd_backfill(args):
    """Dispatch `lav backfill <subcommand>`."""
    if args.backfill_command == "tool-outcomes":
        return cmd_backfill_tool_outcomes(args)
    if args.backfill_command == "tool-kind":
        return cmd_backfill_tool_kind(args)
    if args.backfill_command == "claude-ai-outcomes":
        return cmd_backfill_claude_ai_outcomes(args)
    _die(f"Unknown backfill subcommand '{args.backfill_command}'.")


# LAV-83. Contract with lav/parsers/claude_ai.py:_render_content_block, which
# writes exactly these two lines into messages.content:
#     \n--- tool_use: {name} ---\n{payload}\n
#     \n--- tool_result: {name} ({ok|error}) ---\n{body}\n
# If that renderer's format ever changes, this regex silently matches nothing and
# coverage drops to zero with no error — hence the `markers_seen` counter in the
# output, which is 0 exactly when the contract has broken. Anchored whole-line
# (MULTILINE) so a transcript pasted INTO a conversation cannot match mid-line.
_CLAUDE_AI_MARKER_RE = re.compile(
    r"^--- tool_(use|result): (.+?)(?: \((ok|error)\))? ---$", re.MULTILINE)


def _claude_ai_normalize_tool(name):
    """`<integration>:<tool>` -> `<tool>`.

    The claude.ai export renders result names qualified by their integration
    ('Control your Mac:osascript', 'playwright:playwright_console_logs',
    'wix:CallWixSiteAPI') while the tool_use side and the mcp_tool_calls row hold
    the bare name. Measured: without this, pairing drops from 6.921/6.921 to
    6.206/6.921.
    """
    return (name or "").split(":")[-1].strip()


def _claude_ai_pair_outcomes(rows):
    """Pair rendered tool_use/tool_result markers -> {(tool, rank): is_error}.

    `rows` is the session's messages.content in insertion order — which IS
    conversation order, because claude_ai.py walks chat_messages in order.

    A FIFO of open tool_use ranks PER NORMALISED NAME, popped oldest-first by
    each result. Counting results per name instead (the obvious approach) scores
    6.206/6.921; this scores 6.921/6.921 with zero orphans, because it survives
    interleaved calls of different tools.

    `rank` is the 0-based ordinal of that tool_use among calls of the same tool
    in this session, which is exactly how the rows were inserted.
    """
    open_calls = {}      # normalised tool -> [rank, ...] still awaiting a result
    next_rank = {}       # normalised tool -> next ordinal to hand out
    outcomes = {}
    markers = 0
    orphans = 0
    for content in rows:
        if not content:
            continue
        for kind, raw_name, status in _CLAUDE_AI_MARKER_RE.findall(content):
            name = _claude_ai_normalize_tool(raw_name)
            if not name:
                continue
            markers += 1
            if kind == "use":
                rank = next_rank.get(name, 0)
                next_rank[name] = rank + 1
                open_calls.setdefault(name, []).append(rank)
            else:
                queue = open_calls.get(name)
                if not queue:
                    orphans += 1
                    continue
                rank = queue.pop(0)
                # The renderer emits "(ok)" when the key is absent, which is the
                # LAV-78 rule: absence of is_error means success. So `error` ->
                # 1 and everything else -> 0; NEVER NULL here, a result WAS seen.
                outcomes[(name, rank)] = 1 if status == "error" else 0
    return outcomes, markers, orphans


def cmd_backfill_claude_ai_outcomes(args):
    """LAV-83: recover claude.ai is_error from the markers already in messages.

    lav/parsers/claude_ai.py reads `is_error` off every tool_result block and
    uses it ONLY to render "(ok)"/"(error)" into the message text; the
    mcp_tool_calls INSERT never stores it. So the outcome of every claude.ai tool
    call is sitting in the database as prose, in a column no query can aggregate.

    This reads it back out. No new export is needed — which matters, because the
    original data-*-batch-0000 folder no longer exists on either machine.

    Purely local, no watermark, no sync: run it on EACH node. Not suitable for
    utils/services/lav-parser.sh — it is a one-shot over a static export, not a
    per-cycle heal. Safe (and necessary) to re-run after
    `lav-parse-claude-ai --full`, which deletes and re-inserts the rows.
    """
    db_path = Path(args.db) if getattr(args, "db", None) else UNIFIED_DB_PATH
    if not db_path.exists():
        _die(f"No database at {db_path}. Run lav-parse first.")

    dry_run = bool(getattr(args, "dry_run", False))
    limit = getattr(args, "limit", None)

    conn = sqlite3.connect(str(db_path))
    try:
        if "is_error" not in _bf_table_columns(conn, "mcp_tool_calls"):
            _die("mcp_tool_calls.is_error does not exist. Run lav-parse (or "
                 "lav-server) once so init_db() applies the LAV-78 migration.")

        sql = ("SELECT DISTINCT session_id, project_id FROM mcp_tool_calls "
               "WHERE session_id LIKE 'claudeai:%' ORDER BY session_id")
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        sessions = conn.execute(sql).fetchall()

        stats = {
            "sessions_scanned": 0,
            "markers_seen": 0,
            "results_orphaned": 0,
            "rows_stamped": 0,
            "errors_recovered": 0,
            "unmatched_rank": 0,
            "sessions_failed": 0,
        }
        mode = "DRY RUN (all changes rolled back)" if dry_run else "WRITING"
        print(f"[backfill] {mode} — {len(sessions)} claude.ai sessions on {db_path}",
              file=sys.stderr)

        for session_id, project_id in sessions:
            stats["sessions_scanned"] += 1
            try:
                contents = [r[0] for r in conn.execute(
                    "SELECT content FROM messages WHERE session_id = ? AND project_id = ? "
                    "ORDER BY id", (session_id, project_id))]
                outcomes, markers, orphans = _claude_ai_pair_outcomes(contents)
                stats["markers_seen"] += markers
                stats["results_orphaned"] += orphans
                if not outcomes:
                    continue

                # Row ids per tool, in insertion order — the same order the
                # renderer walked the content blocks, so ordinal N here is the
                # call the Nth marker described.
                ids_by_tool = {}
                for tool_name, row_id in conn.execute(
                        "SELECT tool_name, id FROM mcp_tool_calls "
                        "WHERE session_id = ? AND project_id = ? ORDER BY timestamp, id",
                        (session_id, project_id)):
                    ids_by_tool.setdefault(_claude_ai_normalize_tool(tool_name), []).append(row_id)

                for (tool, rank), is_error in outcomes.items():
                    ids = ids_by_tool.get(tool)
                    if not ids or rank >= len(ids):
                        # More markers than rows: a transcript quoted inside the
                        # conversation, or a call whose row was never written.
                        # Counted, never guessed at.
                        stats["unmatched_rank"] += 1
                        continue
                    n = conn.execute(
                        "UPDATE mcp_tool_calls SET is_error = ? "
                        "WHERE id = ? AND is_error IS NULL",
                        (is_error, ids[rank])).rowcount
                    stats["rows_stamped"] += n
                    if n and is_error:
                        stats["errors_recovered"] += n
            except Exception as e:
                stats["sessions_failed"] += 1
                print(f"[backfill] session {session_id} failed: {e}", file=sys.stderr)
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                continue

            if dry_run:
                conn.rollback()
            else:
                conn.commit()

        if stats["markers_seen"] == 0 and sessions:
            # The renderer contract broke (or these sessions predate it). Say so
            # loudly: silently stamping nothing looks identical to success.
            print("[backfill] WARNING: 0 markers found. The '--- tool_result: ...' "
                  "format in lav/parsers/claude_ai.py may have changed.", file=sys.stderr)

        remaining = conn.execute(
            "SELECT COUNT(*) FROM mcp_tool_calls "
            "WHERE session_id LIKE 'claudeai:%' AND is_error IS NULL").fetchone()[0]
    finally:
        conn.close()

    stats["still_unmeasured"] = remaining
    stats["dry_run"] = dry_run
    _output(stats, args.format)


def cmd_backfill_tool_kind(args):
    """LAV-85: (re)derive mcp_tool_calls.kind on the local DB.

    Normally redundant — _migrate_add_tool_kind() already derives `kind` for every
    row on every init_db(), i.e. on every parse and every server start, on every
    node. This command exists for the one case the migration deliberately cannot
    handle: the migration only ever FILLS BLANKS, so editing
    tool_outcomes.CLAUDE_AI_BUILTIN_TOOLS has no effect on rows already
    classified. `--reclassify` blanks the column first, which is the only way to
    apply an edited whitelist to history.
    """
    from lav.parsers.jsonl import init_db, _migrate_add_tool_kind
    from lav.tool_outcomes import TOOL_KIND_BUILTIN_HOST, TOOL_KIND_MCP

    db_path = Path(args.db) if getattr(args, "db", None) else UNIFIED_DB_PATH
    if not db_path.exists():
        _die(f"No database at {db_path}. Run lav-parse first.")

    conn = sqlite3.connect(str(db_path))
    try:
        if "kind" not in _bf_table_columns(conn, "mcp_tool_calls"):
            # init_db() would add it, but silently doing schema work under a
            # command the user asked to *reclassify* would hide a half-migrated DB.
            _die("mcp_tool_calls.kind does not exist. Run lav-parse (or lav-server) "
                 "once so init_db() applies the LAV-85 migration, then re-run this.")

        before = dict(conn.execute(
            "SELECT COALESCE(kind, ''), COUNT(*) FROM mcp_tool_calls GROUP BY 1").fetchall())

        if getattr(args, "reclassify", False):
            if getattr(args, "dry_run", False):
                # No cheap way to preview without writing: the classification is a
                # Python function over 3 columns. Say so instead of pretending.
                _output({"dry_run": True, "reclassify": True,
                         "note": "--dry-run with --reclassify reports the current "
                                 "distribution only; re-run without --dry-run to apply.",
                         "before": before}, args.format)
                return
            conn.execute("UPDATE mcp_tool_calls SET kind = ''")
            conn.commit()

        _migrate_add_tool_kind(conn)

        after = dict(conn.execute(
            "SELECT COALESCE(kind, ''), COUNT(*) FROM mcp_tool_calls GROUP BY 1").fetchall())
    finally:
        conn.close()

    _output({
        "db": str(db_path),
        "reclassify": bool(getattr(args, "reclassify", False)),
        "before": before,
        "after": after,
        # tool_kind() is total, so anything left at '' means the derivation did not
        # finish — a real failure, not an "unknown" bucket.
        "unclassified": after.get("", 0),
        "kinds": [TOOL_KIND_MCP, TOOL_KIND_BUILTIN_HOST],
    }, args.format)


def cmd_backfill_tool_outcomes(args):
    """Reconstruct tool_call_id / is_error / duration_ms / error_text / exit_code
    from messages.content, in place, on the local DB."""
    from lav.tool_outcomes import (
        apply_tool_outcome,
        apply_workflow_id,
        iter_content_blocks,
        outcome_from_tool_result,
        stamp_tool_call_id,
        workflow_id_from_tool_result,
    )

    db_path = Path(args.db).expanduser() if args.db else UNIFIED_DB_PATH
    dry_run = bool(args.dry_run)

    if not db_path.exists():
        _die(f"No database at {db_path}. Run lav-parse first.")

    if dry_run:
        # Strictly no writes: skip init_db (which would apply DDL) and open a
        # plain connection whose transactions are always rolled back.
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA busy_timeout=5000")
    else:
        # init_db is idempotent and carries the LAV-78 migration; honouring --db
        # here is what makes the command runnable against a copy.
        from lav.parsers.jsonl import init_db
        conn = init_db(db_path)

    try:
        missing = _bf_missing_columns(conn)
        if missing:
            hint = ("run without --dry-run (init_db applies the migration) "
                    if dry_run else "the LAV-78 migration did not apply ")
            _die(f"outcome columns missing on {db_path}: {', '.join(missing)} — {hint}")

        skipped = _bf_unsupported_counts(conn)
        for row in skipped:
            print(f"[backfill] SKIP source={row['source']}: {row['sessions']} sessions / "
                  f"{row['messages']} messages cannot be backfilled "
                  f"({row['reason']})", file=sys.stderr)

        if args.source and args.source in BACKFILL_UNSUPPORTED_SOURCES:
            result = {
                "db": str(db_path), "dry_run": dry_run, "status": "nothing to do",
                "filters": {"since": args.since, "project": args.project,
                            "source": args.source, "limit": args.limit},
                "skipped_sources": skipped,
                "sessions_scanned": 0,
            }
            _output(result, args.format)
            return

        sql, params = _bf_session_query(args.since, args.project, args.source, args.limit)
        sessions = conn.execute(sql, params).fetchall()

        total_sessions = len(sessions)
        # Same `is not None` rule as --limit: --progress-every 0 means "no
        # progress lines" (the only sane reading of zero), not "fall back to
        # 200". Negatives are rejected at parse time by _nonneg_int.
        every = 200 if args.progress_every is None else args.progress_every
        mode = "DRY RUN (all changes rolled back)" if dry_run else "WRITING"
        print(f"[backfill] {mode} — {total_sessions} sessions on {db_path}", file=sys.stderr)

        stats = {
            "sessions_scanned": 0,
            "sessions_with_tool_calls": 0,
            "tool_use_blocks": 0,
            "rows_stamped": 0,
            "tool_result_blocks": 0,
            "outcome_rows_updated": 0,
            "tool_results_unmatched": 0,
            "workflow_ids_stamped": 0,   # LAV-82
            "sessions_failed": 0,
        }
        t0 = time.time()

        for session_id, project_id in sessions:
            stats["sessions_scanned"] += 1
            touched = 0
            try:
                # ── pass 1: tool_use -> stamp the correlation id ────────────
                # MUST fully precede pass 2 for this session, otherwise the
                # UPDATE in pass 2 has no tool_call_id to match on.
                for ts, content in conn.execute(_BF_TOOL_USE_SQL, (session_id, project_id)):
                    for block in iter_content_blocks(content):
                        if block.get("type") != "tool_use":
                            continue
                        # messages.content is a verbatim passthrough of the
                        # source: never assume a field has the expected type.
                        call_id = block.get("id")
                        name = block.get("name")
                        if not isinstance(call_id, str) or not call_id:
                            continue
                        if not isinstance(name, str) or not name:
                            continue
                        stats["tool_use_blocks"] += 1
                        n = stamp_tool_call_id(
                            conn, session_id, project_id, ts,
                            name, block.get("input") or {}, call_id,
                        )
                        stats["rows_stamped"] += n
                        touched += n

                # ── pass 2: tool_result -> write the outcome ────────────────
                for ts, content in conn.execute(_BF_TOOL_RESULT_SQL, (session_id, project_id)):
                    for block in iter_content_blocks(content):
                        if block.get("type") != "tool_result":
                            continue
                        call_id = block.get("tool_use_id")
                        if not isinstance(call_id, str) or not call_id:
                            continue
                        stats["tool_result_blocks"] += 1
                        n = apply_tool_outcome(
                            conn, session_id, project_id, call_id,
                            outcome_from_tool_result(block), ts,
                        )
                        stats["outcome_rows_updated"] += n
                        touched += n
                        # LAV-82: recover the wf_ cohort id from the Workflow
                        # tool_result. This is what makes history reconstructable
                        # on BOTH nodes with no reparse and no sync — the parent's
                        # tool_use never knew the id, but its result announces it
                        # ("Transcript dir: .../subagents/workflows/wf_<id>") and
                        # tool_call_id carries it home. Counted separately: it is
                        # not an outcome, and folding it into n would make an
                        # unmatched result look matched.
                        wf = workflow_id_from_tool_result(block)
                        if wf:
                            w = apply_workflow_id(conn, session_id, project_id, call_id, wf)
                            stats["workflow_ids_stamped"] += w
                            touched += w
                        if n == 0:
                            stats["tool_results_unmatched"] += 1
            except Exception as e:
                # One malformed session must never abort the sweep. Roll it back
                # whole, so pass-1 stamps are not left without their pass-2
                # outcomes. KeyboardInterrupt is not an Exception — it still exits.
                stats["sessions_failed"] += 1
                print(f"[backfill] session {session_id} failed: {e}", file=sys.stderr)
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                continue

            if touched:
                stats["sessions_with_tool_calls"] += 1
            # Commit per session — crash resilience, same convention as the
            # per-project commits in the parsers.
            if dry_run:
                conn.rollback()
            else:
                conn.commit()

            if every and stats["sessions_scanned"] % every == 0:
                print(f"[backfill] {stats['sessions_scanned']}/{total_sessions} sessions · "
                      f"{stats['rows_stamped']} ids stamped · "
                      f"{stats['outcome_rows_updated']} outcomes applied · "
                      f"{time.time() - t0:.1f}s", file=sys.stderr)

        if dry_run:
            conn.rollback()

        result = {
            "db": str(db_path),
            "dry_run": dry_run,
            "status": "dry-run: nothing written" if dry_run else "applied",
            "filters": {"since": args.since, "project": args.project,
                        "source": args.source, "limit": args.limit},
            "skipped_sources": skipped,
            "elapsed_sec": round(time.time() - t0, 1),
        }
        result.update(stats)
        result["is_error_breakdown"] = _bf_breakdown(conn)
        result["is_error_breakdown_scope"] = (
            "whole table, state BEFORE this dry run (every change was rolled back)"
            if dry_run else "whole table, state AFTER this run"
        )

        if args.format == "brief":
            print(f"{'DRY RUN' if dry_run else 'APPLIED'}  "
                  f"sessions={stats['sessions_scanned']}  "
                  f"stamped={stats['rows_stamped']}  "
                  f"outcomes={stats['outcome_rows_updated']}  "
                  f"unmatched={stats['tool_results_unmatched']}  "
                  f"{result['elapsed_sec']}s")
            for table, b in result["is_error_breakdown"].items():
                if "error" in b:
                    continue
                print(f"  {table:<22} err={b['is_error_1']:<7} ok={b['is_error_0']:<7} "
                      f"null={b['is_error_null']:<7} rate={b['error_rate_pct']}")
        else:
            _output(result, args.format)
    finally:
        conn.close()


# ── Parser setup ────────────────────────────────────────────

def _add_common_args(parser):
    """Add common filter args to a subparser."""
    parser.add_argument("--project", help="Filter by project name")
    parser.add_argument("--user", help="Filter by username")
    parser.add_argument("--start", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", help="End date (YYYY-MM-DD)")
    parser.add_argument("--limit", type=_nonneg_int, default=20, help="Max results (default: 20)")
    parser.add_argument("--format", choices=["json", "table", "brief"], default="json",
                        help="Output format (default: json)")


def _add_format_arg(parser):
    """Add just the --format arg."""
    parser.add_argument("--format", choices=["json", "table", "brief"], default="json",
                        help="Output format (default: json)")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="lav",
        description="LAV — query and manage the LocalAgentViewer knowledge base",
    )
    sub = parser.add_subparsers(dest="command")

    # ── search ──
    p_search = sub.add_parser("search", help="Full-text search in interactions")
    p_search.add_argument("query", help="Search query")
    _add_common_args(p_search)
    p_search.set_defaults(func=cmd_search)

    # ── show ──
    p_show = sub.add_parser("show", help="Show full interaction transcript")
    p_show.add_argument("session_id", help="Session UUID")
    _add_format_arg(p_show)
    p_show.set_defaults(func=cmd_show)

    # ── day ──
    p_day = sub.add_parser("day", help="Day View bundle: Gantt rows + worktime metrics for a day")
    p_day.add_argument("date", help="YYYY-MM-DD")
    p_day.add_argument("--project", help="Filter by project name")
    p_day.add_argument("--user", help="Filter by username")
    p_day.add_argument("--source", help="Filter by source (claude_code, codex_cli, ...)")
    p_day.add_argument("--format", choices=["json", "table", "brief"], default="json",
                       help="Output format (default: json)")
    p_day.set_defaults(func=cmd_day)

    # ── kb ── (sub-subcommands)
    p_kb = sub.add_parser("kb", help="Knowledge base operations (Qdrant)")
    kb_sub = p_kb.add_subparsers(dest="kb_command")

    # kb search
    p_kb_search = kb_sub.add_parser("search", help="Semantic search in KB")
    p_kb_search.add_argument("query", help="Natural language query")
    p_kb_search.add_argument("--classification", help="Filter by classification type")
    p_kb_search.add_argument("--tags", help="Comma-separated tags to filter by")
    p_kb_search.add_argument("--project", help="Filter by project name")
    p_kb_search.add_argument("--limit", type=_nonneg_int, default=10, help="Max results (default: 10)")
    _add_format_arg(p_kb_search)
    p_kb_search.set_defaults(func=cmd_kb_search)

    # kb status
    p_kb_status = kb_sub.add_parser("status", help="Check if interaction is indexed")
    p_kb_status.add_argument("session_id", help="Session UUID")
    _add_format_arg(p_kb_status)
    p_kb_status.set_defaults(func=cmd_kb_status)

    # kb index
    p_kb_index = kb_sub.add_parser("index", help="Index interaction into KB")
    p_kb_index.add_argument("session_id", help="Session UUID")
    p_kb_index.add_argument("--tags", help="Comma-separated tags")
    p_kb_index.add_argument("--pre-metadata", dest="pre_metadata",
                            help="JSON string with pre-computed metadata")
    _add_format_arg(p_kb_index)
    p_kb_index.set_defaults(func=cmd_kb_index)

    # kb remove
    p_kb_remove = kb_sub.add_parser("remove", help="Remove interaction from KB")
    p_kb_remove.add_argument("session_id", help="Session UUID")
    _add_format_arg(p_kb_remove)
    p_kb_remove.set_defaults(func=cmd_kb_remove)

    # kb tags
    p_kb_tags = kb_sub.add_parser("tags", help="Update tags on indexed interaction")
    p_kb_tags.add_argument("session_id", help="Session UUID")
    p_kb_tags.add_argument("--set", dest="tags", required=True, help="Comma-separated tags (replaces existing)")
    _add_format_arg(p_kb_tags)
    p_kb_tags.set_defaults(func=cmd_kb_tags)

    # ── sync ──
    p_sync = sub.add_parser("sync", help="Trigger data sync/reparse")
    p_sync.add_argument("--scope", choices=["all", "project", "source"], default="all",
                        help="Sync scope (default: all)")
    p_sync.add_argument("--project", help="Project name (when scope=project)")
    p_sync.add_argument("--source", help="Source type (when scope=source)")
    p_sync.add_argument("--full", action="store_true", help="Full reparse")
    _add_format_arg(p_sync)
    p_sync.set_defaults(func=cmd_sync)

    # ── pricing ──
    p_pricing = sub.add_parser("pricing", help="Manage model pricing")
    p_pricing.add_argument("action", choices=["list", "add"], help="Action to perform")
    p_pricing.add_argument("--model", help="Model name")
    p_pricing.add_argument("--provider", help="Provider name")
    p_pricing.add_argument("--input", type=float, dest="input_price",
                           help="Input price per 1M tokens")
    p_pricing.add_argument("--output", type=float, dest="output_price",
                           help="Output price per 1M tokens")
    p_pricing.add_argument("--from-date", dest="from_date", help="Start date (YYYY-MM-DD)")
    p_pricing.add_argument("--to-date", dest="to_date", help="End date (YYYY-MM-DD)")
    p_pricing.add_argument("--cache-write", type=float, dest="cache_write", help="Cache write price/Mtok")
    p_pricing.add_argument("--cache-read", type=float, dest="cache_read", help="Cache read price/Mtok")
    p_pricing.add_argument("--notes", help="Optional notes")
    _add_format_arg(p_pricing)
    p_pricing.set_defaults(func=cmd_pricing)

    # ── backfill ── (sub-subcommands)
    p_bf = sub.add_parser("backfill", help="One-shot repairs on the local DB")
    bf_sub = p_bf.add_subparsers(dest="backfill_command")

    # backfill tool-outcomes
    p_bf_to = bf_sub.add_parser(
        "tool-outcomes",
        help="LAV-78: rebuild tool_call_id / is_error / duration_ms from messages.content",
        description="Reconstruct the outcome of past tool calls from the raw JSON blocks "
                    "already stored in messages.content, and write it onto the six tool "
                    "tables. Runs entirely on the LOCAL DB — no reparse, no re-sync, no "
                    "parse_state watermark is touched — so it must be run on each node "
                    "(the collector's sync ingest is NOT EXISTS-guarded and never updates "
                    "an existing row). Sources claude_ai and chatgpt cannot be backfilled.",
    )
    p_bf_to.add_argument("--db", help=f"SQLite DB path (default: {UNIFIED_DB_PATH})")
    p_bf_to.add_argument("--dry-run", action="store_true",
                         help="Count what would change, roll everything back, write nothing")
    p_bf_to.add_argument("--since", help="Only sessions active since this ISO timestamp")
    p_bf_to.add_argument("--project", help="Only this project name")
    p_bf_to.add_argument("--source", help="Only this session_sources.source (claude_code, "
                                          "codex_cli, cowork_desktop, ...)")
    p_bf_to.add_argument("--limit", type=_nonneg_int,
                         help="Process at most N sessions (most recent first). "
                              "0 = no sessions at all; omit for no limit")
    p_bf_to.add_argument("--progress-every", type=_nonneg_int, default=200, dest="progress_every",
                         help="Print a progress line every N sessions "
                              "(default: 200; 0 = no progress lines)")
    _add_format_arg(p_bf_to)
    p_bf_to.set_defaults(func=cmd_backfill)

    # backfill tool-kind
    p_bf_tk = bf_sub.add_parser(
        "tool-kind",
        help="LAV-85: (re)derive mcp_tool_calls.kind (mcp vs builtin_host)",
        description="mcp_tool_calls is the catch-all for tool calls with no dedicated "
                    "table, so it also holds ChatGPT's and claude.ai's own built-in "
                    "tools. `kind` separates the two. init_db() already derives it for "
                    "every row on every parse and every server start, so this command is "
                    "normally redundant — its reason to exist is --reclassify, the only "
                    "way to apply an edited CLAUDE_AI_BUILTIN_TOOLS whitelist to rows "
                    "that were already classified. Local DB only: run it on each node.",
    )
    p_bf_tk.add_argument("--db", help=f"SQLite DB path (default: {UNIFIED_DB_PATH})")
    p_bf_tk.add_argument("--reclassify", action="store_true",
                         help="Blank kind on every row first, then re-derive. Needed after "
                              "editing the built-in tool whitelist; without it, only rows "
                              "with no kind yet are touched")
    p_bf_tk.add_argument("--dry-run", action="store_true",
                         help="Report the current distribution without writing")
    _add_format_arg(p_bf_tk)
    p_bf_tk.set_defaults(func=cmd_backfill)

    # backfill claude-ai-outcomes
    p_bf_ca = bf_sub.add_parser(
        "claude-ai-outcomes",
        help="LAV-83: recover claude.ai is_error from the markers already in messages",
        description="The claude.ai parser reads is_error off every tool_result block and "
                    "uses it only to render '(ok)'/'(error)' into the message text — it "
                    "never stores it, so all claude.ai rows sit at is_error IS NULL. This "
                    "reads it back out of messages.content. No new export needed. Local "
                    "DB only: run it on each node. Re-run after lav-parse-claude-ai --full.",
    )
    p_bf_ca.add_argument("--db", help=f"SQLite DB path (default: {UNIFIED_DB_PATH})")
    p_bf_ca.add_argument("--dry-run", action="store_true",
                         help="Count what would change, roll everything back, write nothing")
    p_bf_ca.add_argument("--limit", type=_nonneg_int,
                         help="Process at most N sessions; 0 = none, omit for no limit")
    _add_format_arg(p_bf_ca)
    p_bf_ca.set_defaults(func=cmd_backfill)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(2)

    # Handle kb with no subcommand
    if args.command == "kb" and not getattr(args, "kb_command", None):
        parser.parse_args(["kb", "--help"])
        sys.exit(2)

    # Handle backfill with no subcommand
    if args.command == "backfill" and not getattr(args, "backfill_command", None):
        parser.parse_args(["backfill", "--help"])
        sys.exit(2)

    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(2)

    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        _die(str(e))


if __name__ == "__main__":
    main()
