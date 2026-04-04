#!/usr/bin/env python3
"""LAV unified CLI — query and manage the LocalAgentViewer knowledge base."""

import argparse
import json
import os
import sqlite3
import sys
from typing import Optional

import lav  # noqa: F401 — triggers .env loading
from lav.config import UNIFIED_DB_PATH, QDRANT_DATA_DIR, QDRANT_COLLECTION, QDRANT_URL

_kb_store = None


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


# ── Parser setup ────────────────────────────────────────────

def _add_common_args(parser):
    """Add common filter args to a subparser."""
    parser.add_argument("--project", help="Filter by project name")
    parser.add_argument("--user", help="Filter by username")
    parser.add_argument("--start", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", help="End date (YYYY-MM-DD)")
    parser.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")
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

    # ── kb ── (sub-subcommands)
    p_kb = sub.add_parser("kb", help="Knowledge base operations (Qdrant)")
    kb_sub = p_kb.add_subparsers(dest="kb_command")

    # kb search
    p_kb_search = kb_sub.add_parser("search", help="Semantic search in KB")
    p_kb_search.add_argument("query", help="Natural language query")
    p_kb_search.add_argument("--classification", help="Filter by classification type")
    p_kb_search.add_argument("--tags", help="Comma-separated tags to filter by")
    p_kb_search.add_argument("--project", help="Filter by project name")
    p_kb_search.add_argument("--limit", type=int, default=10, help="Max results (default: 10)")
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
