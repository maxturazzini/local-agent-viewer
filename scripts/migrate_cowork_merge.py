#!/usr/bin/env python3
"""
LAV-61 / LAV-62 data migration: re-materialize Cowork conversations with the
MERGE model + cleaned project inference.

Why this is needed
-------------------
LAV-61 changes how Cowork events are keyed: every event in one ``audit.jsonl`` is
now relabeled under the folder-uuid master (``cowork:<uuid>``) and the human-shell
duplicate turns are dropped. LAV-62 changes Cowork project inference. Both change
values that are *materialized* in the DB (``interactions.session_id`` /
``project_id`` / ``token_usage`` rows), so a plain ``lav-parse --full`` is NOT
enough: the OLD per-event Cowork rows keep their old session_ids and would survive
as orphans (append-only ``INSERT OR REPLACE`` only overwrites the same id),
producing duplicate list rows and double-counted cost.

What this script does (idempotent)
----------------------------------
1. Back up the DB (unless --no-backup).
2. Purge every Cowork row (``session_id LIKE 'cowork:%'``) from all tables that
   have a ``session_id`` column, plus the Cowork ``parse_state`` rows.
3. Reparse Cowork from the local audit logs with the new merge + project code.
4. Delete orphaned empty projects (no interactions) unless --keep-empty-projects.
5. Verify: Cowork slaves must be 0; report the new project distribution.

Only Cowork data is touched. claude_code / codex / chatgpt / claude_ai rows are
left exactly as they are.

Usage
-----
    # dry run (no writes), shows what would happen
    python scripts/migrate_cowork_merge.py --dry-run

    # real run against the default unified DB (asks for the --yes gate)
    python scripts/migrate_cowork_merge.py --yes

    # against a specific DB (e.g. a test copy)
    python scripts/migrate_cowork_merge.py --db /path/to/copy.db --yes
"""

import argparse
import socket
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running as `python3 scripts/migrate_cowork_merge.py` (repo not installed) as well
# as via the venv python where `lav` is installed.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lav.config import UNIFIED_DB_PATH, SOURCE_COWORK_DESKTOP
from lav.parsers.jsonl import init_db, parse_cowork_sessions

COWORK_PREFIX = "cowork:%"


def _cowork_counts(conn: sqlite3.Connection) -> dict:
    cur = conn.cursor()
    masters = cur.execute(
        "SELECT COUNT(*) FROM interactions WHERE session_id LIKE ? AND parent_session_id IS NULL",
        (COWORK_PREFIX,),
    ).fetchone()[0]
    slaves = cur.execute(
        "SELECT COUNT(*) FROM interactions WHERE session_id LIKE ? AND parent_session_id IS NOT NULL",
        (COWORK_PREFIX,),
    ).fetchone()[0]
    tu = cur.execute(
        "SELECT COUNT(*) FROM token_usage WHERE session_id LIKE ?", (COWORK_PREFIX,)
    ).fetchone()[0]
    return {"masters": masters, "slaves": slaves, "token_usage_rows": tu}


def _tables_with_session_id(conn: sqlite3.Connection) -> list[str]:
    out = []
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall():
        cols = [c[1] for c in conn.execute(f'PRAGMA table_info("{name}")').fetchall()]
        if "session_id" in cols:
            out.append(name)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="LAV-61/62 Cowork merge data migration")
    ap.add_argument("--db", default=str(UNIFIED_DB_PATH), help="DB path (default: unified DB)")
    ap.add_argument("--dry-run", action="store_true", help="show plan, write nothing")
    ap.add_argument("--yes", action="store_true", help="confirm a real (non-dry-run) write")
    ap.add_argument("--no-backup", action="store_true", help="skip the pre-migration backup")
    ap.add_argument("--keep-empty-projects", action="store_true",
                    help="do not delete projects that end up with 0 interactions")
    args = ap.parse_args()

    db_path = Path(args.db).expanduser()
    print(f"host         : {socket.gethostname()}")
    print(f"database     : {db_path}")
    if not db_path.exists():
        print(f"ERROR: DB not found: {db_path}", file=sys.stderr)
        return 2

    # Read-only peek at current state.
    with sqlite3.connect(str(db_path)) as peek:
        before = _cowork_counts(peek)
        tables = _tables_with_session_id(peek)
    print(f"cowork before: {before}")
    print(f"tables w/ session_id: {', '.join(tables)}")

    if args.dry_run:
        print("\n[dry-run] would: backup -> purge cowork rows -> reparse --full -> "
              "drop empty projects -> verify. No changes made.")
        return 0

    if not args.yes:
        print("\nRefusing to modify the DB without --yes (or use --dry-run).", file=sys.stderr)
        return 1

    # 1. Backup.
    if not args.no_backup:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = db_path.with_suffix(db_path.suffix + f".pre-lav61.{ts}.bak")
        print(f"\nbacking up -> {backup}")
        # sqlite .backup gives a consistent snapshot even if a reader is attached.
        with sqlite3.connect(str(db_path)) as src, sqlite3.connect(str(backup)) as dst:
            src.backup(dst)
    else:
        print("\n[--no-backup] skipping backup")

    conn = init_db(db_path)  # CREATE TABLE IF NOT EXISTS + migrations; safe on existing DB
    conn.execute("PRAGMA busy_timeout=30000")

    # 2. Purge Cowork rows.
    print("purging cowork rows...")
    purged = {}
    conn.execute("BEGIN")
    for t in tables:
        n = conn.execute(f'DELETE FROM "{t}" WHERE session_id LIKE ?', (COWORK_PREFIX,)).rowcount
        if n:
            purged[t] = n
    ps = conn.execute("DELETE FROM parse_state WHERE source = ?", (SOURCE_COWORK_DESKTOP,)).rowcount
    conn.commit()
    print(f"  purged: {purged}  parse_state: {ps}")

    # 3. Reparse Cowork with the new merge + project inference.
    print("reparsing cowork (full)...")
    parse_cowork_sessions(conn, full_reparse=True)
    conn.commit()

    # 4. Drop orphaned empty projects.
    if not args.keep_empty_projects:
        dropped = conn.execute(
            "DELETE FROM projects WHERE id NOT IN (SELECT DISTINCT project_id FROM interactions)"
        ).rowcount
        conn.commit()
        print(f"dropped empty projects: {dropped}")

    # 5. Verify.
    after = _cowork_counts(conn)
    print(f"\ncowork after : {after}")
    junk = conn.execute(
        "SELECT COUNT(*) FROM projects WHERE name='outputs' OR name LIKE '%.md' "
        "OR name LIKE '%.html' OR name LIKE '%.csv' OR name LIKE '%.pdf' OR name LIKE '%.xlsx'"
    ).fetchone()[0]
    proj_rows = conn.execute(
        """SELECT p.name, COUNT(*) n FROM interactions i
           JOIN projects p ON p.id = i.project_id
           JOIN session_sources ss ON ss.session_id = i.session_id AND ss.project_id = i.project_id
           WHERE ss.source = ? GROUP BY p.name ORDER BY n DESC LIMIT 10""",
        (SOURCE_COWORK_DESKTOP,),
    ).fetchall()
    conn.close()

    print(f"junk project names remaining: {junk}")
    print("top cowork projects:", proj_rows)

    if after["slaves"] != 0:
        print("\nFAIL: cowork slaves != 0 after merge — investigate before serving.", file=sys.stderr)
        return 3
    print("\nOK: cowork merged (0 slaves). Restart the server so it serves the new data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
