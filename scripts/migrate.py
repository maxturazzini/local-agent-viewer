#!/usr/bin/env python3
"""
Migrate existing claude-parser databases (one per project) into the
unified LocalAgentViewer database.

Steps:
1. Create unified DB with new schema
2. Iterate over existing .db files in claude-parser/data/
3. For each DB: ATTACH, derive project + user, INSERT...SELECT with IDs
4. Rebuild FTS
5. ANALYZE + PRAGMA optimize
6. Verify counts
7. Original DBs are NOT modified or deleted
"""

import argparse
import sqlite3
from pathlib import Path

from lav import PROJECT_ROOT
from lav.config import UNIFIED_DB_PATH
from lav.parsers.jsonl import (
    init_db,
    get_or_create_project,
    get_or_create_user,
    get_or_create_host,
    detect_user_from_path,
    detect_host,
)

# Path to existing claude-parser data
CLAUDE_PARSER_DATA = PROJECT_ROOT.parent / "claude-parser" / "data"


def migrate_single_db(unified_conn: sqlite3.Connection, old_db_path: Path, dry_run: bool = False) -> dict:
    """Migrate one old per-project DB into the unified DB.

    Uses ATTACH/DETACH to avoid loading everything into memory.

    Args:
        unified_conn: Connection to the unified DB (write mode)
        old_db_path: Path to the old per-project .db file
        dry_run: If True, only count rows without inserting

    Returns:
        Dict with migration stats
    """
    project_name = old_db_path.stem
    print(f"\n  Migrating: {project_name} ({old_db_path})")

    # Derive user + host from project name / current environment
    # For existing DBs, we use the current user/host as best guess
    hostname, os_type, home_dir = detect_host()

    # Try to find the source path from parse_state or conversations.cwd
    try:
        temp_conn = sqlite3.connect(str(old_db_path))
        row = temp_conn.execute("SELECT cwd FROM conversations WHERE cwd IS NOT NULL AND cwd != '' LIMIT 1").fetchone()
        source_path = row[0] if row else ""
        if source_path:
            username = detect_user_from_path(Path(source_path))
        else:
            import getpass
            username = getpass.getuser()
        temp_conn.close()
    except Exception:
        import getpass
        username = getpass.getuser()
        source_path = ""

    project_id = get_or_create_project(unified_conn, project_name, source_path)
    user_id = get_or_create_user(unified_conn, username)
    host_id = get_or_create_host(unified_conn, hostname, os_type, home_dir)

    print(f"    Project ID: {project_id}, User: {username} (id={user_id}), Host: {hostname} (id={host_id})")

    if dry_run:
        temp_conn = sqlite3.connect(str(old_db_path))
        counts = {}
        for table in ["conversations", "messages", "file_operations", "bash_commands",
                       "search_operations", "skill_invocations", "subagent_invocations",
                       "mcp_tool_calls", "token_usage", "session_sources"]:
            try:
                row = temp_conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()
                counts[table] = row[0]
            except:
                counts[table] = 0
        temp_conn.close()
        print(f"    [DRY RUN] Rows: {counts}")
        return {"project": project_name, "dry_run": True, "counts": counts}

    # ATTACH the old database
    alias = "old_db"
    unified_conn.execute(f"ATTACH DATABASE ? AS {alias}", (str(old_db_path),))

    stats = {}

    try:
        # Migrate conversations
        try:
            unified_conn.execute(f"""
                INSERT OR IGNORE INTO conversations
                (session_id, project_id, user_id, host_id, timestamp, display, summary, project, model,
                 total_tokens, message_count, tools_used, cwd, git_branch, parent_session_id, agent_id)
                SELECT
                    session_id, ?, ?, ?, timestamp, display, summary, project, model,
                    total_tokens, message_count, tools_used, cwd, git_branch, parent_session_id, agent_id
                FROM {alias}.conversations
            """, (project_id, user_id, host_id))
            stats["conversations"] = unified_conn.execute(
                f"SELECT COUNT(*) FROM {alias}.conversations"
            ).fetchone()[0]
        except sqlite3.Error as e:
            print(f"    Warning: conversations migration: {e}")
            stats["conversations"] = 0

        # Migrate messages
        try:
            unified_conn.execute(f"""
                INSERT OR IGNORE INTO messages
                (session_id, project_id, user_id, host_id, uuid, type, content, timestamp, tokens_in, tokens_out, model)
                SELECT
                    session_id, ?, ?, ?, uuid, type, content, timestamp, tokens_in, tokens_out, model
                FROM {alias}.messages
            """, (project_id, user_id, host_id))
            stats["messages"] = unified_conn.execute(
                f"SELECT COUNT(*) FROM {alias}.messages"
            ).fetchone()[0]
        except sqlite3.Error as e:
            print(f"    Warning: messages migration: {e}")
            stats["messages"] = 0

        # Migrate file_operations
        try:
            unified_conn.execute(f"""
                INSERT OR IGNORE INTO file_operations
                (timestamp, session_id, project_id, user_id, host_id, tool, file_path, cwd, git_branch)
                SELECT
                    timestamp, session_id, ?, ?, ?, tool, file_path, cwd, git_branch
                FROM {alias}.file_operations
            """, (project_id, user_id, host_id))
            stats["file_operations"] = unified_conn.execute(
                f"SELECT COUNT(*) FROM {alias}.file_operations"
            ).fetchone()[0]
        except sqlite3.Error as e:
            print(f"    Warning: file_operations migration: {e}")
            stats["file_operations"] = 0

        # Migrate bash_commands
        try:
            unified_conn.execute(f"""
                INSERT OR IGNORE INTO bash_commands
                (timestamp, session_id, project_id, user_id, host_id, command, description, target_file, cwd, git_branch)
                SELECT
                    timestamp, session_id, ?, ?, ?, command, description, target_file, cwd, git_branch
                FROM {alias}.bash_commands
            """, (project_id, user_id, host_id))
            stats["bash_commands"] = unified_conn.execute(
                f"SELECT COUNT(*) FROM {alias}.bash_commands"
            ).fetchone()[0]
        except sqlite3.Error as e:
            print(f"    Warning: bash_commands migration: {e}")
            stats["bash_commands"] = 0

        # Migrate search_operations
        try:
            unified_conn.execute(f"""
                INSERT OR IGNORE INTO search_operations
                (timestamp, session_id, project_id, user_id, host_id, tool, pattern, path, output_mode, cwd)
                SELECT
                    timestamp, session_id, ?, ?, ?, tool, pattern, path, output_mode, cwd
                FROM {alias}.search_operations
            """, (project_id, user_id, host_id))
            stats["search_operations"] = unified_conn.execute(
                f"SELECT COUNT(*) FROM {alias}.search_operations"
            ).fetchone()[0]
        except sqlite3.Error as e:
            print(f"    Warning: search_operations migration: {e}")
            stats["search_operations"] = 0

        # Migrate skill_invocations
        try:
            unified_conn.execute(f"""
                INSERT OR IGNORE INTO skill_invocations
                (timestamp, session_id, project_id, user_id, host_id, skill_name, args, cwd, git_branch)
                SELECT
                    timestamp, session_id, ?, ?, ?, skill_name, args, cwd, git_branch
                FROM {alias}.skill_invocations
            """, (project_id, user_id, host_id))
            stats["skill_invocations"] = unified_conn.execute(
                f"SELECT COUNT(*) FROM {alias}.skill_invocations"
            ).fetchone()[0]
        except sqlite3.Error as e:
            print(f"    Warning: skill_invocations migration: {e}")
            stats["skill_invocations"] = 0

        # Migrate subagent_invocations
        try:
            unified_conn.execute(f"""
                INSERT OR IGNORE INTO subagent_invocations
                (timestamp, session_id, project_id, user_id, host_id, subagent_type, description, prompt, model, run_in_background, cwd, git_branch)
                SELECT
                    timestamp, session_id, ?, ?, ?, subagent_type, description, prompt, model, run_in_background, cwd, git_branch
                FROM {alias}.subagent_invocations
            """, (project_id, user_id, host_id))
            stats["subagent_invocations"] = unified_conn.execute(
                f"SELECT COUNT(*) FROM {alias}.subagent_invocations"
            ).fetchone()[0]
        except sqlite3.Error as e:
            print(f"    Warning: subagent_invocations migration: {e}")
            stats["subagent_invocations"] = 0

        # Migrate mcp_tool_calls
        try:
            unified_conn.execute(f"""
                INSERT OR IGNORE INTO mcp_tool_calls
                (timestamp, session_id, project_id, user_id, host_id, tool_name, server_name, cwd, git_branch)
                SELECT
                    timestamp, session_id, ?, ?, ?, tool_name, server_name, cwd, git_branch
                FROM {alias}.mcp_tool_calls
            """, (project_id, user_id, host_id))
            stats["mcp_tool_calls"] = unified_conn.execute(
                f"SELECT COUNT(*) FROM {alias}.mcp_tool_calls"
            ).fetchone()[0]
        except sqlite3.Error as e:
            print(f"    Warning: mcp_tool_calls migration: {e}")
            stats["mcp_tool_calls"] = 0

        # Migrate token_usage
        try:
            unified_conn.execute(f"""
                INSERT OR IGNORE INTO token_usage
                (timestamp, session_id, project_id, user_id, host_id, model, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, cwd)
                SELECT
                    timestamp, session_id, ?, ?, ?, model, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, cwd
                FROM {alias}.token_usage
            """, (project_id, user_id, host_id))
            stats["token_usage"] = unified_conn.execute(
                f"SELECT COUNT(*) FROM {alias}.token_usage"
            ).fetchone()[0]
        except sqlite3.Error as e:
            print(f"    Warning: token_usage migration: {e}")
            stats["token_usage"] = 0

        # Migrate session_sources
        try:
            unified_conn.execute(f"""
                INSERT OR IGNORE INTO session_sources
                (session_id, project_id, source, client_version, process_name, vm_process_name, meta_json)
                SELECT
                    session_id, ?, source, client_version, process_name, vm_process_name, meta_json
                FROM {alias}.session_sources
            """, (project_id,))
            stats["session_sources"] = unified_conn.execute(
                f"SELECT COUNT(*) FROM {alias}.session_sources"
            ).fetchone()[0]
        except sqlite3.Error as e:
            print(f"    Warning: session_sources migration: {e}")
            stats["session_sources"] = 0

        unified_conn.commit()

    finally:
        unified_conn.execute(f"DETACH DATABASE {alias}")

    print(f"    Migrated: {stats}")
    return {"project": project_name, "project_id": project_id, "stats": stats}


def rebuild_fts(conn: sqlite3.Connection):
    """Rebuild FTS index after migration."""
    print("\n  Rebuilding FTS index...")
    try:
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        conn.commit()
        print("    FTS rebuild complete")
    except sqlite3.Error as e:
        print(f"    Warning: FTS rebuild failed: {e}")


def optimize_db(conn: sqlite3.Connection):
    """Run ANALYZE and optimize after migration."""
    print("\n  Optimizing database...")
    conn.execute("ANALYZE")
    conn.execute("PRAGMA optimize")
    conn.commit()
    print("    Optimization complete")


def verify_counts(conn: sqlite3.Connection):
    """Print verification counts for the unified DB."""
    print("\n  Verification counts:")
    tables = [
        "projects", "users", "hosts", "conversations", "messages",
        "file_operations", "bash_commands", "search_operations",
        "skill_invocations", "subagent_invocations", "mcp_tool_calls",
        "token_usage", "session_sources",
    ]
    for table in tables:
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()
            print(f"    {table}: {row[0]}")
        except:
            print(f"    {table}: (error)")


def main():
    parser = argparse.ArgumentParser(
        description="Migrate claude-parser databases to unified LocalAgentViewer DB"
    )
    parser.add_argument(
        "--source-dir", type=str, default=str(CLAUDE_PARSER_DATA),
        help=f"Source directory with per-project .db files (default: {CLAUDE_PARSER_DATA})"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be migrated without actually doing it"
    )
    parser.add_argument(
        "--project", type=str,
        help="Migrate only a specific project"
    )

    args = parser.parse_args()
    source_dir = Path(args.source_dir)

    if not source_dir.exists():
        print(f"Source directory not found: {source_dir}")
        return

    db_files = sorted(source_dir.glob("*.db"))
    if not db_files:
        print(f"No .db files found in {source_dir}")
        return

    if args.project:
        db_files = [f for f in db_files if f.stem == args.project]
        if not db_files:
            print(f"Project '{args.project}' not found in {source_dir}")
            return

    print(f"LocalAgentViewer Migration")
    print(f"  Source: {source_dir}")
    print(f"  Target: {UNIFIED_DB_PATH}")
    print(f"  Projects to migrate: {len(db_files)}")
    if args.dry_run:
        print(f"  *** DRY RUN - no data will be written ***")

    # Initialize unified DB
    conn = init_db(UNIFIED_DB_PATH)

    all_results = []
    for db_file in db_files:
        result = migrate_single_db(conn, db_file, dry_run=args.dry_run)
        all_results.append(result)

    if not args.dry_run:
        rebuild_fts(conn)
        optimize_db(conn)
        verify_counts(conn)

    conn.close()

    print(f"\n{'='*60}")
    print("Migration Complete")
    print(f"{'='*60}")
    for r in all_results:
        if args.dry_run:
            total = sum(r.get("counts", {}).values())
            print(f"  {r['project']}: ~{total} rows")
        else:
            total = sum(r.get("stats", {}).values())
            print(f"  {r['project']} (id={r.get('project_id')}): {total} rows migrated")


if __name__ == "__main__":
    main()
