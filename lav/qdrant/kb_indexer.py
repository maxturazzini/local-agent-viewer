#!/usr/bin/env python3
"""
kb_indexer.py - Auto-indexer for LAV knowledge base (Qdrant).

Reads interactions from the canonical SQLite DB and indexes them into Qdrant.
Designed to run on the collector machine where the canonical DB lives.

Usage:
    python3 kb_indexer.py                          # incremental (only new/unindexed)
    python3 kb_indexer.py --full                   # reindex everything
    python3 kb_indexer.py --session-id <ID>        # index one specific interaction
    python3 kb_indexer.py --project miniMe          # filter by project
    python3 kb_indexer.py --source claude_code      # filter by source tool
    python3 kb_indexer.py --host myserver            # filter by hostname
    python3 kb_indexer.py --username john             # filter by user
    python3 kb_indexer.py --min-messages 5          # skip short interactions
    python3 kb_indexer.py --since 2025-01-01        # only from this date onward
    python3 kb_indexer.py --limit 100               # process at most N interactions
    python3 kb_indexer.py --dry-run                 # preview without writing

Payload stored in Qdrant per interaction:
    session_id      str         Identifier (UUID)
    project         str         Workspace name (e.g. miniMe)
    user            str         Username
    host            str         Hostname where interaction happened
    source          str         Tool: claude_code | chatgpt | codex_cli | cowork_desktop
    timestamp       str (ISO)   First message timestamp
    indexed_at      str (ISO)   When indexed
    summary         str         1-2 sentence summary (Haiku)
    abstract        str         2-3 sentence detail (Haiku)
    classification  str         development|meeting|analysis|brainstorm|support|learning
    topics          list[str]   Up to 5 keywords (Haiku)
    people          list[str]   Third-party people mentioned (Haiku)
    clients         list[str]   Companies/clients mentioned (Haiku)
    tags            list[str]   Manual tags
    message_count   int         Number of messages
    tools_used      list[str]   Tools invoked in the interaction

Searchable via MCP semantic_search filters:
    classification, tags, project
"""

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import lav  # noqa: F401 — triggers .env loading
from lav.config import UNIFIED_DB_PATH, QDRANT_DATA_DIR, QDRANT_COLLECTION, QDRANT_URL


# ── Store factory ────────────────────────────────────────────────────────────

def _get_store():
    from lav.qdrant.store import InteractionVectorStore
    if QDRANT_URL:
        store = InteractionVectorStore(url=QDRANT_URL, collection=QDRANT_COLLECTION)
    else:
        QDRANT_DATA_DIR.mkdir(parents=True, exist_ok=True)
        store = InteractionVectorStore(data_path=QDRANT_DATA_DIR, collection=QDRANT_COLLECTION)
    store.ensure_collection()
    return store


# ── DB helpers ───────────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    if not UNIFIED_DB_PATH.exists():
        print(f"ERROR: DB not found at {UNIFIED_DB_PATH}")
        sys.exit(1)
    conn = sqlite3.connect(str(UNIFIED_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _build_filter_query(
    project: str = "",
    source: str = "",
    host: str = "",
    username: str = "",
    min_messages: int = 0,
    since: str = "",
    session_id: str = "",
    limit: int = 0,
) -> tuple:
    """Build the SELECT query with optional filters. Returns (sql, params)."""
    sql = """
        SELECT
            c.session_id,
            p.name AS project_name,
            u.username,
            h.hostname,
            ss.source,
            c.timestamp,
            c.message_count
        FROM interactions c
        JOIN projects p ON c.project_id = p.id
        LEFT JOIN users u ON c.user_id = u.id
        LEFT JOIN hosts h ON c.host_id = h.id
        LEFT JOIN session_sources ss ON ss.session_id = c.session_id
                                     AND ss.project_id = c.project_id
        WHERE 1=1
    """
    params = []

    if session_id:
        sql += " AND c.session_id = ?"
        params.append(session_id)
    if project:
        sql += " AND p.name = ?"
        params.append(project)
    if source:
        sql += " AND ss.source = ?"
        params.append(source)
    if host:
        sql += " AND h.hostname LIKE ?"
        params.append(f"%{host}%")
    if username:
        sql += " AND u.username = ?"
        params.append(username)
    if min_messages > 0:
        sql += " AND c.message_count >= ?"
        params.append(min_messages)
    if since:
        sql += " AND c.timestamp >= ?"
        params.append(since)

    sql += " ORDER BY c.timestamp DESC"

    if limit > 0:
        sql += f" LIMIT {limit}"

    return sql, params


def _fetch_interactions(conn: sqlite3.Connection, **kwargs) -> list:
    sql, params = _build_filter_query(**kwargs)
    return conn.execute(sql, params).fetchall()


def _fetch_messages(conn: sqlite3.Connection, session_id: str, project_id: int) -> list:
    """Fetch messages for an interaction in indexer-compatible format."""
    rows = conn.execute(
        """
        SELECT type, content
        FROM messages
        WHERE session_id = ? AND project_id = ?
        ORDER BY id
        """,
        (session_id, project_id),
    ).fetchall()
    return [{"type": r["type"], "content": r["content"]} for r in rows]


def _get_project_id(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT project_id FROM interactions WHERE session_id = ?", (session_id,)
    ).fetchone()
    return row["project_id"] if row else -1


def _get_sql_metadata(conn: sqlite3.Connection, session_id: str, project_id: int) -> dict:
    """Fetch pre-existing SQL classification metadata if available.

    Returns dict compatible with indexer pre_metadata, or None.
    """
    import json as _json
    row = conn.execute(
        """SELECT summary, abstract, process, classification, data_sensitivity,
                  sensitive_data_types, topics, people, clients
           FROM interaction_metadata
           WHERE session_id = ? AND project_id = ?""",
        (session_id, project_id),
    ).fetchone()
    if not row:
        return None
    meta = dict(row)
    for field in ("sensitive_data_types", "topics", "people", "clients"):
        val = meta.get(field)
        if isinstance(val, str):
            try:
                meta[field] = _json.loads(val)
            except (ValueError, TypeError):
                meta[field] = []
    return meta


# ── Main indexing loop ───────────────────────────────────────────────────────

def run(
    full: bool = False,
    limit: int = 0,
    dry_run: bool = False,
    session_id: str = "",
    project: str = "",
    source: str = "",
    host: str = "",
    username: str = "",
    min_messages: int = 0,
    since: str = "",
):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] kb_indexer START")
    print(f"  DB: {UNIFIED_DB_PATH}")
    mode = f"HTTP ({QDRANT_URL})" if QDRANT_URL else f"file ({QDRANT_DATA_DIR})"
    print(f"  Qdrant: {mode}")
    print(f"  Mode: {'full reindex' if full else ('single: ' + session_id[:8] if session_id else 'incremental')}")

    filters = {k: v for k, v in {
        "project": project, "source": source, "host": host,
        "username": username, "min_messages": min_messages, "since": since,
        "session_id": session_id, "limit": limit,
    }.items() if v}
    if filters:
        print(f"  Filters: {filters}")
    if dry_run:
        print("  DRY RUN — no writes")

    store = _get_store()
    conn = _get_db()

    try:
        interactions = _fetch_interactions(
            conn,
            session_id=session_id,
            project=project,
            source=source,
            host=host,
            username=username,
            min_messages=min_messages,
            since=since,
            limit=limit,
        )
        total = len(interactions)
        print(f"  Interactions matched: {total}")

        if total == 0:
            print("  Nothing to do.")
            return

        from lav.qdrant.indexer import InteractionIndexer
        indexer = InteractionIndexer(store)

        indexed = 0
        skipped = 0
        errors = 0

        for i, conv in enumerate(interactions, 1):
            sid = conv["session_id"]
            proj = conv["project_name"] or ""
            user = conv["username"] or ""
            hostname = conv["hostname"] or ""
            src = conv["source"] or ""
            ts = conv["timestamp"] or ""
            msg_count = conv["message_count"] or 0

            # Skip already indexed (unless full reindex or specific interaction)
            if not full and not session_id and store.is_indexed(sid):
                skipped += 1
                continue

            project_id = _get_project_id(conn, sid)
            messages = _fetch_messages(conn, sid, project_id)
            if not messages:
                skipped += 1
                continue

            print(
                f"  [{i}/{total}] {sid[:8]}  "
                f"proj={proj}  src={src}  host={hostname}  msgs={msg_count}",
                end="",
                flush=True,
            )

            # Check for existing SQL classification metadata
            sql_meta = _get_sql_metadata(conn, sid, project_id)

            if dry_run:
                src_label = "sql" if sql_meta else "llm"
                print(f" [dry-run, {src_label}]")
                indexed += 1
                continue

            try:
                t0 = time.time()
                payload = indexer.index(
                    session_id=sid,
                    messages=messages,
                    project=proj,
                    timestamp=ts,
                    user=user,
                    pre_metadata=sql_meta,
                )
                # Enrich payload with host/source (patch after index)
                if hostname or src:
                    store.client.set_payload(
                        collection_name=QDRANT_COLLECTION,
                        payload={
                            k: v for k, v in {"host": hostname, "source": src}.items() if v
                        },
                        points=[store._session_to_id(sid)],
                        wait=True,
                    )
                elapsed = time.time() - t0
                src_label = "sql" if sql_meta else "llm"
                print(f"  OK ({elapsed:.1f}s)  [{src_label}] [{payload.get('classification','')}]  {payload.get('summary','')[:60]}")
                indexed += 1
            except Exception as e:
                print(f"  ERROR: {e}")
                errors += 1

    finally:
        conn.close()

    print(
        f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] DONE — "
        f"indexed={indexed}  skipped={skipped}  errors={errors}"
    )


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="LAV Knowledge Base auto-indexer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 kb_indexer.py                                # incremental, all
  python3 kb_indexer.py --project miniMe --since 2025-01-01
  python3 kb_indexer.py --source claude_code --min-messages 5
  python3 kb_indexer.py --session-id abc123def456...  # single interaction
  python3 kb_indexer.py --dry-run --limit 20          # preview first 20
        """,
    )
    parser.add_argument("--full", action="store_true",
        help="Reindex all interactions (default: only new/unindexed)")
    parser.add_argument("--session-id", metavar="UUID",
        help="Index one specific interaction by session_id")
    parser.add_argument("--project", metavar="NAME",
        help="Filter by project name (e.g. miniMe)")
    parser.add_argument("--source", metavar="SRC",
        choices=["claude_code", "chatgpt", "codex_cli", "cowork_desktop"],
        help="Filter by source tool")
    parser.add_argument("--host", metavar="HOSTNAME",
        help="Filter by hostname (partial match)")
    parser.add_argument("--username", metavar="USER",
        help="Filter by username")
    parser.add_argument("--min-messages", type=int, default=0, metavar="N",
        help="Only index interactions with at least N messages")
    parser.add_argument("--since", metavar="YYYY-MM-DD",
        help="Only index interactions from this date onward")
    parser.add_argument("--limit", type=int, default=0, metavar="N",
        help="Process at most N interactions (0 = no limit)")
    parser.add_argument("--dry-run", action="store_true",
        help="Show what would be indexed without writing to Qdrant")

    args = parser.parse_args()

    run(
        full=args.full,
        limit=args.limit,
        dry_run=args.dry_run,
        session_id=args.session_id or "",
        project=args.project or "",
        source=args.source or "",
        host=args.host or "",
        username=args.username or "",
        min_messages=args.min_messages,
        since=args.since or "",
    )
