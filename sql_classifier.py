#!/usr/bin/env python3
"""
sql_classifier.py - Batch classifier for conversation metadata (SQLite-based).

Uses gpt-4o-mini via OpenAI Structured Outputs to classify conversations
and store metadata in the conversation_metadata table.

Independent from Qdrant — same schema for 1:1 comparison.

Usage:
    python3 sql_classifier.py                           # incremental (only unclassified)
    python3 sql_classifier.py --full                     # reclassify everything
    python3 sql_classifier.py --limit 50                 # test on 50
    python3 sql_classifier.py --project miniMe           # filter by project
    python3 sql_classifier.py --min-messages 5           # skip short conversations
    python3 sql_classifier.py --dry-run                  # preview without writing
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Bootstrap path & env ────────────────────────────────────────────────────

_PROJECT_DIR = Path(__file__).parent
sys.path.insert(0, str(_PROJECT_DIR))

_env_file = _PROJECT_DIR / ".env"
if _env_file.exists():
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

from config import UNIFIED_DB_PATH


# ── DB helpers ───────────────────────────────────────────────────────────────

def _get_db(readonly: bool = True) -> sqlite3.Connection:
    if not UNIFIED_DB_PATH.exists():
        print(f"ERROR: DB not found at {UNIFIED_DB_PATH}")
        sys.exit(1)
    conn = sqlite3.connect(str(UNIFIED_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    return conn


def _get_write_db() -> sqlite3.Connection:
    from parser import init_db
    return init_db(UNIFIED_DB_PATH)


def _fetch_candidates(
    conn: sqlite3.Connection,
    full: bool = False,
    project: str = "",
    source: str = "",
    host: str = "",
    username: str = "",
    min_messages: int = 0,
    since: str = "",
    limit: int = 0,
) -> list:
    """Fetch conversations to classify."""
    sql = """
        SELECT
            c.session_id,
            c.project_id,
            p.name AS project_name,
            u.username,
            h.hostname,
            ss.source,
            c.timestamp,
            c.message_count
        FROM conversations c
        JOIN projects p ON c.project_id = p.id
        LEFT JOIN users u ON c.user_id = u.id
        LEFT JOIN hosts h ON c.host_id = h.id
        LEFT JOIN session_sources ss ON ss.session_id = c.session_id
                                     AND ss.project_id = c.project_id
    """

    if not full:
        sql += """
        LEFT JOIN conversation_metadata cm
            ON cm.session_id = c.session_id AND cm.project_id = c.project_id
        """

    sql += " WHERE 1=1"

    params = []

    if not full:
        sql += " AND cm.session_id IS NULL"

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

    return conn.execute(sql, params).fetchall()


def _fetch_messages(conn: sqlite3.Connection, session_id: str, project_id: int) -> list:
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


def _upsert_metadata(
    conn: sqlite3.Connection,
    session_id: str,
    project_id: int,
    metadata: dict,
    model_used: str,
):
    now = datetime.now().isoformat()
    conn.execute(
        """
        INSERT INTO conversation_metadata
            (session_id, project_id, summary, abstract, process, classification,
             data_sensitivity, sensitive_data_types, topics, people, clients,
             tags, model_used, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id, project_id) DO UPDATE SET
            summary = excluded.summary,
            abstract = excluded.abstract,
            process = excluded.process,
            classification = excluded.classification,
            data_sensitivity = excluded.data_sensitivity,
            sensitive_data_types = excluded.sensitive_data_types,
            topics = excluded.topics,
            people = excluded.people,
            clients = excluded.clients,
            model_used = excluded.model_used,
            updated_at = excluded.updated_at
        """,
        (
            session_id,
            project_id,
            metadata.get("summary", ""),
            metadata.get("abstract", ""),
            metadata.get("process", ""),
            metadata.get("classification", "development"),
            metadata.get("data_sensitivity", "internal"),
            json.dumps(metadata.get("sensitive_data_types", [])),
            json.dumps(metadata.get("topics", [])),
            json.dumps(metadata.get("people", [])),
            json.dumps(metadata.get("clients", [])),
            "[]",  # tags — manual, not auto-generated
            model_used,
            now,
            now,
        ),
    )


# ── Main ─────────────────────────────────────────────────────────────────────

def run(
    full: bool = False,
    limit: int = 0,
    dry_run: bool = False,
    project: str = "",
    source: str = "",
    host: str = "",
    username: str = "",
    min_messages: int = 0,
    since: str = "",
    model: str = "gpt-4.1-mini",
):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] sql_classifier START")
    print(f"  DB: {UNIFIED_DB_PATH}")
    print(f"  Model: {model}")
    print(f"  Mode: {'full reclassify' if full else 'incremental'}")

    filters = {k: v for k, v in {
        "project": project, "source": source, "host": host,
        "username": username, "min_messages": min_messages, "since": since,
        "limit": limit,
    }.items() if v}
    if filters:
        print(f"  Filters: {filters}")
    if dry_run:
        print("  DRY RUN — no writes")

    # Check OpenAI key
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key and not dry_run:
        print("ERROR: OPENAI_API_KEY not set. Set it in .env or environment.")
        sys.exit(1)

    # Read-only connection for fetching candidates
    read_conn = _get_db(readonly=True)

    try:
        candidates = _fetch_candidates(
            read_conn,
            full=full,
            project=project,
            source=source,
            host=host,
            username=username,
            min_messages=min_messages,
            since=since,
            limit=limit,
        )
    finally:
        read_conn.close()

    total = len(candidates)
    print(f"  Conversations to classify: {total}")

    if total == 0:
        print("  Nothing to do.")
        return

    # Initialize OpenAI client (only if not dry-run)
    openai_client = None
    if not dry_run:
        import openai
        openai_client = openai.OpenAI(api_key=api_key)

    from classifiers.openai_classifier import classify_conversation

    # Write connection for storing results
    write_conn = None
    if not dry_run:
        write_conn = _get_write_db()

    # Read connection for fetching messages
    read_conn = _get_db(readonly=True)

    classified = 0
    errors = 0
    total_input_tokens = 0
    total_output_tokens = 0
    stats = {}

    try:
        for i, conv in enumerate(candidates, 1):
            sid = conv["session_id"]
            pid = conv["project_id"]
            proj = conv["project_name"] or ""
            msg_count = conv["message_count"] or 0

            print(
                f"  [{i}/{total}] {sid[:8]}  "
                f"proj={proj}  msgs={msg_count}",
                end="",
                flush=True,
            )

            messages = _fetch_messages(read_conn, sid, pid)
            if not messages:
                print(" [no messages, skip]")
                continue

            if dry_run:
                print(" [dry-run]")
                classified += 1
                continue

            try:
                t0 = time.time()
                metadata = classify_conversation(messages, openai_client, model=model)
                elapsed = time.time() - t0

                _upsert_metadata(write_conn, sid, pid, metadata, model)
                write_conn.commit()

                cls = metadata.get("classification", "?")
                sens = metadata.get("data_sensitivity", "?")
                summary = metadata.get("summary", "")[:60]
                stats[cls] = stats.get(cls, 0) + 1

                print(f"  OK ({elapsed:.1f}s)  [{cls}/{sens}]  {summary}")
                classified += 1

            except Exception as e:
                print(f"  ERROR: {e}")
                errors += 1

    finally:
        read_conn.close()
        if write_conn:
            write_conn.close()

    print(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] DONE")
    print(f"  Classified: {classified}  Errors: {errors}")
    if stats:
        print(f"  By classification: {stats}")


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SQL-based conversation classifier (gpt-4o-mini)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 sql_classifier.py                            # incremental
  python3 sql_classifier.py --full                     # reclassify all
  python3 sql_classifier.py --limit 50                 # test on 50
  python3 sql_classifier.py --project miniMe           # filter by project
  python3 sql_classifier.py --min-messages 5           # skip short
  python3 sql_classifier.py --dry-run --limit 5        # preview
        """,
    )
    parser.add_argument("--full", action="store_true",
        help="Reclassify all (default: only unclassified)")
    parser.add_argument("--project", metavar="NAME",
        help="Filter by project name")
    parser.add_argument("--source", metavar="SRC",
        choices=["claude_code", "chatgpt", "codex_cli", "cowork_desktop"],
        help="Filter by source tool")
    parser.add_argument("--host", metavar="HOSTNAME",
        help="Filter by hostname (partial match)")
    parser.add_argument("--username", metavar="USER",
        help="Filter by username")
    parser.add_argument("--min-messages", type=int, default=0, metavar="N",
        help="Skip conversations with fewer than N messages")
    parser.add_argument("--since", metavar="YYYY-MM-DD",
        help="Only classify from this date onward")
    parser.add_argument("--limit", type=int, default=0, metavar="N",
        help="Process at most N conversations (0 = no limit)")
    parser.add_argument("--model", default="gpt-4.1-mini",
        help="OpenAI model (default: gpt-4o-mini)")
    parser.add_argument("--dry-run", action="store_true",
        help="Preview without writing to DB")

    args = parser.parse_args()

    run(
        full=args.full,
        limit=args.limit,
        dry_run=args.dry_run,
        project=args.project or "",
        source=args.source or "",
        host=args.host or "",
        username=args.username or "",
        min_messages=args.min_messages,
        since=args.since or "",
        model=args.model,
    )
