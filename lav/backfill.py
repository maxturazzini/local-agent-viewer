"""Backfill from a remote agent's SQLite snapshot.

SQL-direct alternative to the HTTP /api/export pull. Useful for wide historical
windows where the HTTP pipeline is fragile (timeouts, multiple round-trips,
Tailscale flakiness). See LAV-48.

Pipeline:
1. Fetch a consistent snapshot of the agent's DB via ssh+sqlite3 .backup + scp.
2. ATTACH the snapshot on the local DB.
3. Translate cross-DB IDs (projects/users/hosts) via name lookups.
4. INSERT OR IGNORE every row whose activity timestamp > --since.
5. Advance parse_state.last_pull:<agent> so subsequent regular pulls don't
   re-fetch this range.
"""
from __future__ import annotations

import argparse
import os
import shlex
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

from lav.config import UNIFIED_DB_PATH
from lav.parsers.jsonl import (
    init_db,
    get_or_create_host,
    get_or_create_project,
    get_or_create_user,
)


# Tables shipped from the snapshot. Order matters only for clarity.
# Each entry: (table_name, columns, has_timestamp_filter).
# `has_timestamp_filter=True` means we add `AND s.timestamp > ?` to the SELECT
# so we don't ship rows older than --since.
CHILD_TABLES: List[Tuple[str, List[str], bool]] = [
    ("messages", [
        "session_id", "project_id", "user_id", "host_id", "uuid", "type",
        "content", "timestamp", "tokens_in", "tokens_out", "model", "api_message_id",
    ], True),
    ("token_usage", [
        "timestamp", "session_id", "project_id", "user_id", "host_id", "model",
        "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens",
        "cwd", "api_message_id",
    ], True),
    ("file_operations", [
        "timestamp", "session_id", "project_id", "user_id", "host_id",
        "tool", "file_path", "cwd", "git_branch",
    ], True),
    ("bash_commands", [
        "timestamp", "session_id", "project_id", "user_id", "host_id",
        "command", "description", "target_file", "cwd", "git_branch",
    ], True),
    ("search_operations", [
        "timestamp", "session_id", "project_id", "user_id", "host_id",
        "tool", "pattern", "path", "output_mode", "cwd",
    ], True),
    ("skill_invocations", [
        "timestamp", "session_id", "project_id", "user_id", "host_id",
        "skill_name", "args", "cwd", "git_branch",
    ], True),
    ("subagent_invocations", [
        "timestamp", "session_id", "project_id", "user_id", "host_id",
        "subagent_type", "description", "prompt", "model", "run_in_background",
        "cwd", "git_branch",
    ], True),
    ("mcp_tool_calls", [
        "timestamp", "session_id", "project_id", "user_id", "host_id",
        "tool_name", "server_name", "cwd", "git_branch",
    ], True),
]

INTERACTION_COLUMNS = [
    "session_id", "project_id", "user_id", "host_id", "timestamp", "display",
    "summary", "project", "model", "total_tokens", "message_count", "tools_used",
    "cwd", "git_branch", "parent_session_id", "agent_id",
]


def fetch_snapshot(ssh_host: str, remote_db: str, local_path: Path,
                   ssh_user: str = None) -> None:
    """Ssh into agent, take a consistent SQLite snapshot, scp it back."""
    ssh_target = f"{ssh_user}@{ssh_host}" if ssh_user else ssh_host
    remote_snapshot = f"/tmp/lav_snapshot_{os.getpid()}.db"
    print(f"[backfill] taking consistent snapshot on {ssh_target}:{remote_db} -> {remote_snapshot}")
    t0 = time.time()
    subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", ssh_target,
         f"sqlite3 {shlex.quote(remote_db)} \".backup '{remote_snapshot}'\""],
        check=True,
    )
    print(f"[backfill] snapshot taken in {time.time() - t0:.1f}s")

    print(f"[backfill] scp {ssh_target}:{remote_snapshot} -> {local_path}")
    t0 = time.time()
    subprocess.run(
        ["scp", "-o", "ConnectTimeout=10",
         f"{ssh_target}:{remote_snapshot}", str(local_path)],
        check=True,
    )
    elapsed = time.time() - t0
    size_mb = local_path.stat().st_size / (1024 * 1024)
    print(f"[backfill] scp done: {size_mb:.0f} MB in {elapsed:.1f}s "
          f"({size_mb / max(elapsed, 0.1):.1f} MB/s)")

    # Clean up the remote snapshot
    subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", ssh_target, f"rm -f {remote_snapshot}"],
        check=False,
    )


def build_id_maps(conn: sqlite3.Connection) -> Tuple[Dict[int, int], Dict[int, int], Dict[int, int]]:
    """Walk src.projects/users/hosts and map each src.id to a local id.

    Missing rows on the local side are created via the existing get_or_create_*
    helpers, so the maps are always complete after this call.
    """
    proj_map: Dict[int, int] = {}
    for src_id, name, source_path in conn.execute(
        "SELECT id, name, source_path FROM src.projects"
    ).fetchall():
        proj_map[src_id] = get_or_create_project(conn, name, source_path or "")

    user_map: Dict[int, int] = {}
    for src_id, username in conn.execute(
        "SELECT id, username FROM src.users"
    ).fetchall():
        user_map[src_id] = get_or_create_user(conn, username)

    host_map: Dict[int, int] = {}
    for src_id, hostname, os_type, home_dir in conn.execute(
        "SELECT id, hostname, os_type, home_dir FROM src.hosts"
    ).fetchall():
        host_map[src_id] = get_or_create_host(conn, hostname, os_type or "", home_dir or "")

    print(f"[backfill] id maps built: projects={len(proj_map)} users={len(user_map)} hosts={len(host_map)}")
    return proj_map, user_map, host_map


def select_active_sessions(conn: sqlite3.Connection, since: str) -> List[Tuple[str, int]]:
    """Return (session_id, src_project_id) for sessions active after --since.

    "Active" = COALESCE(MAX(messages.timestamp), c.timestamp) > since. Same
    logic as LAV-46 export_sessions, applied to the attached snapshot.
    """
    rows = conn.execute("""
        SELECT c.session_id, c.project_id
        FROM src.interactions c
        LEFT JOIN (
            SELECT session_id, project_id, MAX(timestamp) AS last_msg_ts
            FROM src.messages
            GROUP BY session_id, project_id
        ) m ON m.session_id = c.session_id AND m.project_id = c.project_id
        WHERE COALESCE(m.last_msg_ts, c.timestamp) > ?
    """, (since,)).fetchall()
    return [(r[0], r[1]) for r in rows]


def copy_interactions(conn: sqlite3.Connection,
                      sessions: List[Tuple[str, int]],
                      proj_map: Dict[int, int],
                      user_map: Dict[int, int],
                      host_map: Dict[int, int]) -> int:
    """INSERT OR IGNORE interactions for the selected sessions, ID-translated."""
    cols = INTERACTION_COLUMNS
    placeholders = ",".join(["?"] * len(cols))
    insert_sql = f"INSERT OR IGNORE INTO interactions ({','.join(cols)}) VALUES ({placeholders})"

    inserted = 0
    batch: List[tuple] = []
    BATCH_SIZE = 500

    # Build a set for O(1) lookup
    session_keys = {(sid, pid) for sid, pid in sessions}

    # Fetch all interaction rows for these sessions
    src_rows = conn.execute(f"SELECT {','.join(cols)} FROM src.interactions").fetchall()
    for row in src_rows:
        sid, src_pid = row[0], row[1]
        if (sid, src_pid) not in session_keys:
            continue
        translated = list(row)
        translated[1] = proj_map[row[1]]
        translated[2] = user_map[row[2]]
        translated[3] = host_map[row[3]]
        batch.append(tuple(translated))
        if len(batch) >= BATCH_SIZE:
            cur = conn.executemany(insert_sql, batch)
            inserted += cur.rowcount if cur.rowcount > 0 else 0
            batch.clear()
    if batch:
        cur = conn.executemany(insert_sql, batch)
        inserted += cur.rowcount if cur.rowcount > 0 else 0

    # Also copy session_sources (composite PK on session_id+project_id+source)
    session_keys_translated = [(sid, proj_map[src_pid]) for sid, src_pid in sessions]
    # Build SELECT with IN clause for src.session_sources
    if session_keys_translated:
        for sid, src_pid in sessions:
            for source, client_version, process_name in conn.execute(
                "SELECT source, client_version, process_name FROM src.session_sources WHERE session_id = ? AND project_id = ?",
                (sid, src_pid)
            ).fetchall():
                conn.execute(
                    "INSERT OR IGNORE INTO session_sources (session_id, project_id, source, client_version, process_name) VALUES (?, ?, ?, ?, ?)",
                    (sid, proj_map[src_pid], source, client_version, process_name),
                )

    print(f"[backfill] interactions: {inserted} new (of {len(sessions)} active sessions)")
    return inserted


def copy_child_table(conn: sqlite3.Connection,
                     table: str,
                     columns: List[str],
                     sessions: List[Tuple[str, int]],
                     since: str,
                     proj_map: Dict[int, int],
                     user_map: Dict[int, int],
                     host_map: Dict[int, int]) -> int:
    """INSERT OR IGNORE rows from src.<table> filtered to timestamp > since."""
    cols = columns
    placeholders = ",".join(["?"] * len(cols))
    insert_sql = f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({placeholders})"

    inserted = 0
    batch: List[tuple] = []
    BATCH_SIZE = 1000

    session_keys = {(sid, pid) for sid, pid in sessions}

    # We need to know the column indices for ID translation.
    # session_id, project_id, user_id, host_id are present in every child table.
    sid_idx = cols.index("session_id")
    pid_idx = cols.index("project_id")
    uid_idx = cols.index("user_id")
    hid_idx = cols.index("host_id")
    ts_idx = cols.index("timestamp")

    # Stream rows from src in a single query, filtered by timestamp on the SQL side.
    src_rows = conn.execute(
        f"SELECT {','.join(cols)} FROM src.{table} WHERE timestamp > ?",
        (since,),
    )
    seen = 0
    for row in src_rows:
        seen += 1
        sid, src_pid = row[sid_idx], row[pid_idx]
        if (sid, src_pid) not in session_keys:
            continue
        translated = list(row)
        translated[pid_idx] = proj_map.get(src_pid)
        translated[uid_idx] = user_map.get(row[uid_idx])
        translated[hid_idx] = host_map.get(row[hid_idx])
        if translated[pid_idx] is None or translated[uid_idx] is None or translated[hid_idx] is None:
            # Should not happen: id maps cover everything we saw in src
            continue
        batch.append(tuple(translated))
        if len(batch) >= BATCH_SIZE:
            cur = conn.executemany(insert_sql, batch)
            inserted += cur.rowcount if cur.rowcount > 0 else 0
            batch.clear()
    if batch:
        cur = conn.executemany(insert_sql, batch)
        inserted += cur.rowcount if cur.rowcount > 0 else 0

    print(f"[backfill] {table}: {inserted} new (of {seen} rows >since in snapshot)")
    return inserted


def advance_cursor(conn: sqlite3.Connection, agent: str, since_used: str) -> str:
    """Set parse_state.last_pull:<agent> to MAX(snapshot.message.timestamp) — but
    never regress below the current value. Historical backfills run against an
    older snapshot than what the live cursor may have already reached via
    regular incremental pulls, so we take MAX(current, new).

    Falls back to `since_used` if no messages > since exist in the snapshot.
    """
    row = conn.execute("""
        SELECT MAX(s.timestamp)
        FROM src.messages s
        WHERE s.timestamp > ?
    """, (since_used,)).fetchone()
    snapshot_max = (row[0] if row and row[0] else since_used)

    key = f"last_pull:{agent}"
    cur = conn.execute(
        "SELECT value FROM parse_state WHERE key = ? AND project_id = -1 AND source = 'remote' AND host_id = -1",
        (key,)
    ).fetchone()
    current = cur[0] if cur else ""
    new_cursor = max(current, snapshot_max)  # lexicographic compare on ISO-8601

    conn.execute("""
        INSERT INTO parse_state (key, project_id, source, host_id, value)
        VALUES (?, -1, 'remote', -1, ?)
        ON CONFLICT(key, project_id, source, host_id) DO UPDATE SET value = excluded.value
    """, (key, new_cursor))
    if new_cursor != snapshot_max:
        print(f"[backfill] last_pull:{agent} kept at {new_cursor} (snapshot MAX={snapshot_max} did not regress)")
    else:
        print(f"[backfill] last_pull:{agent} -> {new_cursor}")
    return new_cursor


def run(agent: str, ssh_host: str, since: str,
        remote_db: str = "~/.local/share/local-agent-viewer/local_agent_viewer.db",
        snapshot_path: Path = None,
        keep_snapshot: bool = False,
        ssh_user: str = None,
        dry_run: bool = False) -> dict:
    """Main entry point. Returns a stats dict."""
    if snapshot_path is None:
        snapshot_path = Path(f"/tmp/lav_snapshot_{agent}_{os.getpid()}.db")

    if not snapshot_path.exists():
        if not ssh_host:
            raise SystemExit(
                f"[backfill] snapshot {snapshot_path} does not exist and --ssh-host was not provided"
            )
        fetch_snapshot(ssh_host, remote_db, snapshot_path, ssh_user)
    else:
        print(f"[backfill] reusing existing snapshot at {snapshot_path}")

    conn = init_db()
    conn.execute(f"ATTACH DATABASE ? AS src", (str(snapshot_path),))
    try:
        proj_map, user_map, host_map = build_id_maps(conn)
        sessions = select_active_sessions(conn, since)
        print(f"[backfill] {len(sessions)} sessions active since {since}")

        if dry_run:
            print("[backfill] --dry-run set, not writing")
            return {"sessions_active": len(sessions), "dry_run": True}

        stats = {"sessions": len(sessions)}
        with conn:
            stats["interactions"] = copy_interactions(conn, sessions, proj_map, user_map, host_map)
            for table, cols, _ in CHILD_TABLES:
                stats[table] = copy_child_table(
                    conn, table, cols, sessions, since, proj_map, user_map, host_map
                )
            new_cursor = advance_cursor(conn, agent, since)
            stats["last_pull"] = new_cursor
        return stats
    finally:
        conn.execute("DETACH DATABASE src")
        conn.close()
        if not keep_snapshot and snapshot_path.exists():
            snapshot_path.unlink()
            print(f"[backfill] cleaned up snapshot at {snapshot_path}")


def main():
    p = argparse.ArgumentParser(
        description="Backfill from a remote agent's SQLite snapshot (LAV-48). "
                    "SQL-direct alternative to lav sync for wide historical windows."
    )
    p.add_argument("--agent", required=True, help="Agent name (matches config.json agents[].name)")
    p.add_argument("--ssh-host", default=None,
                   help="SSH host of the agent (IP or hostname). Optional if --snapshot-path "
                        "is provided and the file already exists (pre-staged snapshot).")
    p.add_argument("--ssh-user", default=None, help="Optional SSH username (defaults to ssh config)")
    p.add_argument("--since", required=True, help="ISO timestamp, e.g. 2025-11-14T00:00:00")
    p.add_argument("--remote-db", default="~/.local/share/local-agent-viewer/local_agent_viewer.db",
                   help="Path to LAV DB on the agent host")
    p.add_argument("--snapshot-path", type=Path, default=None,
                   help="Where to write the snapshot locally (default: /tmp/lav_snapshot_<agent>_<pid>.db)")
    p.add_argument("--keep-snapshot", action="store_true",
                   help="Don't delete the local snapshot after backfill (useful for debugging)")
    p.add_argument("--dry-run", action="store_true", help="Only print stats, don't write")
    args = p.parse_args()

    t0 = time.time()
    stats = run(
        agent=args.agent,
        ssh_host=args.ssh_host,
        ssh_user=args.ssh_user,
        since=args.since,
        remote_db=args.remote_db,
        snapshot_path=args.snapshot_path,
        keep_snapshot=args.keep_snapshot,
        dry_run=args.dry_run,
    )
    print(f"[backfill] DONE in {time.time() - t0:.1f}s — stats: {stats}")


if __name__ == "__main__":
    main()
