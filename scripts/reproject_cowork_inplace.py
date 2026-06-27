#!/usr/bin/env python3
"""
LAV-62 in-place fix: re-attribute historical Cowork conversations to real projects
WITHOUT reparsing (for hosts whose audit logs have rotated away).

Why
---
LAV-62 fixes Cowork project inference in the parser, but that only affects newly
parsed conversations. On a host where the audit logs are gone, the historical
Cowork rows keep their junk project names (``outputs``, ``SKILL.md``, bare
filenames). This script re-infers the project for each existing Cowork session
from the file paths ALREADY stored in the DB (file_operations / bash_commands /
search_operations) using the same ``infer_cowork_project()`` logic, then moves the
session — across every table — to the corrected project. Finally it drops projects
that end up empty.

This touches ONLY Cowork sessions. It does not merge conversations (that needs the
audit logs); it only fixes project attribution. Safe to run repeatedly (idempotent).

Usage
-----
    python scripts/reproject_cowork_inplace.py --dry-run            # report only
    python scripts/reproject_cowork_inplace.py --yes                # apply (backs up first)
    python scripts/reproject_cowork_inplace.py --db /path/copy.db --yes
"""

import argparse
import socket
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lav.config import UNIFIED_DB_PATH, SOURCE_COWORK_DESKTOP
from lav.parsers.jsonl import infer_cowork_project, get_or_create_project

COWORK_PREFIX = "cowork:%"


def _tables_with_session_and_project(conn: sqlite3.Connection) -> list[str]:
    out = []
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        cols = [c[1] for c in conn.execute(f'PRAGMA table_info("{name}")').fetchall()]
        if "session_id" in cols and "project_id" in cols:
            out.append(name)
    return out


def _gather_paths(conn: sqlite3.Connection, sid: str, pid: int) -> list[str]:
    paths = []
    queries = [
        "SELECT file_path FROM file_operations WHERE session_id=? AND project_id=? ORDER BY timestamp",
        "SELECT target_file FROM bash_commands WHERE session_id=? AND project_id=? ORDER BY timestamp",
        "SELECT path FROM search_operations WHERE session_id=? AND project_id=? ORDER BY timestamp",
    ]
    for q in queries:
        try:
            for (p,) in conn.execute(q, (sid, pid)).fetchall():
                if p and isinstance(p, str) and p.startswith("/"):
                    paths.append(p)
        except sqlite3.Error:
            pass
    return paths


def _reinfer_project(conn: sqlite3.Connection, sid: str, pid: int) -> str:
    for p in _gather_paths(conn, sid, pid):
        proj = infer_cowork_project(p)
        if proj:
            return proj
    return "cowork_default"


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-attribute historical Cowork sessions to real projects (in-place)")
    ap.add_argument("--db", default=str(UNIFIED_DB_PATH))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    db_path = Path(args.db).expanduser()
    print(f"host    : {socket.gethostname()}")
    print(f"database: {db_path}")
    if not db_path.exists():
        print(f"ERROR: DB not found: {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout=30000")

    cowork = conn.execute(
        """SELECT i.session_id, i.project_id, p.name
           FROM interactions i
           JOIN session_sources ss ON ss.session_id=i.session_id AND ss.project_id=i.project_id
           JOIN projects p ON p.id=i.project_id
           WHERE ss.source=?""",
        (SOURCE_COWORK_DESKTOP,),
    ).fetchall()
    print(f"cowork sessions: {len(cowork)}")

    # Plan the moves.
    moves = []  # (sid, old_pid, old_name, new_name)
    for sid, pid, old_name in cowork:
        new_name = _reinfer_project(conn, sid, pid)
        if new_name != old_name:
            moves.append((sid, pid, old_name, new_name))

    from_counts = Counter(m[2] for m in moves)
    to_counts = Counter(m[3] for m in moves)
    print(f"sessions to move: {len(moves)}")
    print(f"  from (top): {from_counts.most_common(10)}")
    print(f"  to   (top): {to_counts.most_common(10)}")

    if args.dry_run:
        print("\n[dry-run] no changes made.")
        conn.close()
        return 0
    if not args.yes:
        print("\nRefusing to write without --yes (or use --dry-run).", file=sys.stderr)
        conn.close()
        return 1
    if not moves:
        print("Nothing to do.")
        conn.close()
        return 0

    # Backup.
    if not args.no_backup:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = db_path.with_suffix(db_path.suffix + f".pre-reproject.{ts}.bak")
        print(f"backing up -> {backup}")
        with sqlite3.connect(str(db_path)) as src, sqlite3.connect(str(backup)) as dst:
            src.backup(dst)

    tables = _tables_with_session_and_project(conn)
    print(f"updating tables: {', '.join(tables)}")

    conn.execute("BEGIN")
    for sid, old_pid, _old_name, new_name in moves:
        new_pid = get_or_create_project(conn, new_name)
        if new_pid == old_pid:
            continue
        for t in tables:
            # Some Cowork sessions are materialized under MULTIPLE project_ids
            # (cross-project dup from the old per-event inference). Moving one copy to a
            # project where another copy already lives would violate the composite PK /
            # UNIQUE, so move what doesn't collide (UPDATE OR IGNORE) and drop the
            # leftover rows — which are duplicates of the target's existing rows.
            conn.execute(
                f'UPDATE OR IGNORE "{t}" SET project_id=? WHERE session_id=? AND project_id=?',
                (new_pid, sid, old_pid),
            )
            conn.execute(
                f'DELETE FROM "{t}" WHERE session_id=? AND project_id=?',
                (sid, old_pid),
            )
    # Drop projects that are now empty.
    dropped = conn.execute(
        "DELETE FROM projects WHERE id NOT IN (SELECT DISTINCT project_id FROM interactions)"
    ).rowcount
    conn.commit()
    print(f"moved {len(moves)} sessions; dropped {dropped} empty projects")

    # Verify: junk project names with interactions should be gone.
    junk = conn.execute(
        """SELECT p.name, COUNT(i.session_id) n FROM projects p
           JOIN interactions i ON i.project_id=p.id
           WHERE p.name='outputs' OR p.name LIKE '%.md' OR p.name LIKE '%.html'
              OR p.name LIKE '%.csv' OR p.name LIKE '%.pdf' OR p.name LIKE '%.xlsx'
           GROUP BY p.name"""
    ).fetchall()
    conn.close()
    print(f"junk projects with interactions remaining: {junk or 'NONE'}")
    print("\nOK. Restart the server so the dashboard reflects the new attribution.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
