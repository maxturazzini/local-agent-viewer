#!/usr/bin/env python3
"""
LocalAgentViewer - Multi-user, multi-host AI agent interaction parser.

Parses Claude Code, Codex CLI, and Claude Desktop interaction files (.jsonl)
into a unified SQLite database with 4 dimensions:
  - project_id: which codebase
  - user_id: which person
  - host_id: which machine
  - source: which tool (claude_code / codex_cli / cowork_desktop)

Based on claude-parser, refactored for unified DB architecture.
"""

import argparse
import getpass
import hashlib
import io
import json
import os
import platform
import re
import socket
import sqlite3
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Generator, Optional

from lav.config import (
    UNIFIED_DB_PATH,
    FILE_OPERATION_TOOLS,
    SEARCH_TOOLS,
    FILE_COMMANDS,
    BASH_READ_COMMANDS,
    BASH_WRITE_COMMANDS,
    SOURCE_CLAUDE_CODE,
    SOURCE_CODEX_CLI,
    SOURCE_COWORK_DESKTOP,
    SOURCE_CHATGPT_WORK_DESKTOP,
    SOURCE_CODEX_DESKTOP,
    SOURCE_CODEX_VSCODE,
    SOURCE_CODEX_LOCAL,
    get_claude_projects_dirs,
    get_codex_sessions_dirs,
    get_cowork_sessions_dirs,
    load_runtime_config,
)
# LAV-78: tool-call outcomes. tool_outcomes imports lav.config only (it pulls the
# three bash helpers back out of THIS module lazily), so this direction is safe.
from lav import tool_outcomes

# ===========================================================================
# SCHEMA - Unified multi-project, multi-user, multi-host
# ===========================================================================

SCHEMA = """
-- Reference tables: 3 dimensions

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    meta_json TEXT
);
INSERT OR IGNORE INTO users (id, username, first_seen, last_seen)
VALUES (1, 'unknown', datetime('now'), datetime('now'));

CREATE TABLE IF NOT EXISTS hosts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hostname TEXT NOT NULL UNIQUE,
    os_type TEXT,
    home_dir TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    meta_json TEXT
);
INSERT OR IGNORE INTO hosts (id, hostname, first_seen, last_seen)
VALUES (1, 'unknown', datetime('now'), datetime('now'));

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    source_path TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    meta_json TEXT
);

-- Data tables: all with project_id + user_id + host_id

CREATE TABLE IF NOT EXISTS interactions (
    session_id TEXT NOT NULL,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id),
    host_id INTEGER NOT NULL DEFAULT 1 REFERENCES hosts(id),
    timestamp TEXT NOT NULL,
    display TEXT,
    summary TEXT,
    project TEXT,
    model TEXT,
    total_tokens INTEGER DEFAULT 0,
    message_count INTEGER DEFAULT 0,
    tools_used TEXT,
    cwd TEXT,
    git_branch TEXT,
    parent_session_id TEXT,
    agent_id TEXT,
    -- LAV-82: wf_<id> when this session is a child agent of a Workflow run, taken
    -- from its transcript path (<parent>/subagents/workflows/wf_<id>/agent-*.jsonl).
    -- '' for a plain Task/Agent subagent and for a top-level session. It is what
    -- makes a 12-agent workflow reconstructable instead of looking like 12
    -- unrelated spawns — and it cannot be inferred from timestamps: 21 of the 43
    -- parent sessions here ran MORE THAN ONE workflow.
    -- WARNING: update_interaction() does INSERT OR REPLACE over the whole row, so
    -- this column MUST stay in its column list or every parse silently blanks it.
    workflow_id TEXT DEFAULT '',
    PRIMARY KEY (session_id, project_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id),
    host_id INTEGER NOT NULL DEFAULT 1 REFERENCES hosts(id),
    uuid TEXT,
    type TEXT NOT NULL,
    content TEXT,
    timestamp TEXT,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    model TEXT,
    api_message_id TEXT DEFAULT '',
    agent_id TEXT,
    UNIQUE(session_id, project_id, uuid)
);

CREATE TABLE IF NOT EXISTS file_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT NOT NULL,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id),
    host_id INTEGER NOT NULL DEFAULT 1 REFERENCES hosts(id),
    tool TEXT NOT NULL,
    file_path TEXT NOT NULL,
    cwd TEXT,
    git_branch TEXT,
    -- LAV-78 outcome columns (see lav/tool_outcomes.py OUTCOME_COLUMNS — the
    -- single source of truth; every column here must also exist in
    -- _migrate_add_tool_outcomes or a fresh DB and a migrated DB diverge):
    --   tool_call_id  toolu_*/call_* — correlation only, NEVER a key (~3% dupes at the source)
    --   is_error      NULL = no tool_result ever seen | 0 = ok | 1 = error
    --   duration_ms   wall clock call->result, NULL when not derivable
    -- NO NOT NULL: ADD COLUMN NOT NULL is illegal without a constant default and
    -- the parsers use INSERT OR IGNORE, where a violation silently drops the row.
    tool_call_id TEXT DEFAULT '',
    is_error INTEGER,
    duration_ms INTEGER,
    UNIQUE(timestamp, session_id, project_id, tool, file_path)
);

CREATE TABLE IF NOT EXISTS bash_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT NOT NULL,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id),
    host_id INTEGER NOT NULL DEFAULT 1 REFERENCES hosts(id),
    command TEXT NOT NULL,
    description TEXT,
    target_file TEXT,
    cwd TEXT,
    git_branch TEXT,
    -- LAV-78 outcome columns (see file_operations above). bash_commands also
    -- carries error_text (capped at tool_outcomes.ERROR_TEXT_CAP) and exit_code,
    -- parsed out of the tool_result text by tool_outcomes.EXIT_CODE_RE — ~73% of
    -- Bash errors; the rest are permission-denied/blocked, which have no exit code.
    tool_call_id TEXT DEFAULT '',
    is_error INTEGER,
    duration_ms INTEGER,
    error_text TEXT DEFAULT '',
    exit_code INTEGER,
    -- LAV-79: cmd_name = basename of the program the command actually runs
    -- (bash_cmd_name() below). Derived, never NULL from the parser: '' means
    -- "derived and undecidable". KEEP IN SYNC with _migrate_add_cmd_name, which
    -- adds the very same column (with the same DEFAULT '') to existing DBs and
    -- backfills it in place — a fresh DB and a migrated DB must not diverge.
    cmd_name TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS search_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT NOT NULL,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id),
    host_id INTEGER NOT NULL DEFAULT 1 REFERENCES hosts(id),
    tool TEXT NOT NULL,
    pattern TEXT NOT NULL,
    path TEXT,
    output_mode TEXT,
    cwd TEXT,
    -- LAV-78 outcome columns (see file_operations above)
    tool_call_id TEXT DEFAULT '',
    is_error INTEGER,
    duration_ms INTEGER
);

CREATE TABLE IF NOT EXISTS skill_invocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT NOT NULL,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id),
    host_id INTEGER NOT NULL DEFAULT 1 REFERENCES hosts(id),
    skill_name TEXT NOT NULL,
    args TEXT,
    cwd TEXT,
    git_branch TEXT,
    -- LAV-78 outcome columns (see file_operations above)
    tool_call_id TEXT DEFAULT '',
    is_error INTEGER,
    duration_ms INTEGER,
    UNIQUE(timestamp, session_id, project_id, skill_name)
);

CREATE TABLE IF NOT EXISTS subagent_invocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT NOT NULL,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id),
    host_id INTEGER NOT NULL DEFAULT 1 REFERENCES hosts(id),
    subagent_type TEXT NOT NULL,
    description TEXT,
    prompt TEXT,
    model TEXT,
    run_in_background INTEGER DEFAULT 0,
    cwd TEXT,
    git_branch TEXT,
    -- LAV-78 outcome columns (see file_operations above)
    tool_call_id TEXT DEFAULT '',
    is_error INTEGER,
    duration_ms INTEGER,
    -- LAV-82: which tool spawned this ('Task' | 'Agent' | 'Workflow') and, for a
    -- Workflow run, the wf_<id> cohort its child agents share. Both are derived
    -- ATTRIBUTES of the call, deliberately NOT part of the UNIQUE key below:
    -- adding them would let the same call be inserted twice by two parser
    -- versions. Same rule LAV-78 applied to tool_call_id.
    spawn_tool TEXT DEFAULT '',
    workflow_id TEXT DEFAULT '',
    UNIQUE(timestamp, session_id, project_id, subagent_type, description)
);

CREATE TABLE IF NOT EXISTS mcp_tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT NOT NULL,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id),
    host_id INTEGER NOT NULL DEFAULT 1 REFERENCES hosts(id),
    tool_name TEXT NOT NULL,
    server_name TEXT,
    cwd TEXT,
    git_branch TEXT,
    -- LAV-78 outcome columns (see file_operations above); mcp_tool_calls also
    -- carries error_text. Codex-sourced rows keep is_error NULL by design:
    -- function_call_output is not parsed, so no tool_result is ever seen.
    tool_call_id TEXT DEFAULT '',
    is_error INTEGER,
    duration_ms INTEGER,
    error_text TEXT DEFAULT '',
    -- LAV-85: 'mcp' | 'builtin_host'. This table is the catch-all for tool calls
    -- with no dedicated table, so it also holds ChatGPT's and claude.ai's OWN
    -- built-in tools (38% of the rows on prod). Derived by tool_outcomes.tool_kind()
    -- and, for existing rows, by _migrate_add_tool_kind — KEEP THE TWO IN SYNC.
    -- The empty default is transient: the migration classifies every row, and the
    -- function is total, so '' must not survive an init_db().
    kind TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT NOT NULL,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    user_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id),
    host_id INTEGER NOT NULL DEFAULT 1 REFERENCES hosts(id),
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cwd TEXT,
    api_message_id TEXT DEFAULT '',
    UNIQUE(timestamp, session_id, project_id)
);
-- LAV-39: idx_token_usage_api_msg partial UNIQUE created in _migrate_add_api_message_id

CREATE TABLE IF NOT EXISTS session_sources (
    session_id TEXT NOT NULL,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    source TEXT NOT NULL,
    client_version TEXT,
    process_name TEXT,
    vm_process_name TEXT,
    meta_json TEXT,
    PRIMARY KEY (session_id, project_id)
);

-- parse_state: NO NULL, sentinel values for composite PK
-- host_id included so each machine tracks its own incremental cursor
CREATE TABLE IF NOT EXISTS parse_state (
    key TEXT NOT NULL,
    project_id INTEGER NOT NULL DEFAULT -1,
    source TEXT NOT NULL DEFAULT '',
    host_id INTEGER NOT NULL DEFAULT -1,
    value TEXT,
    PRIMARY KEY (key, project_id, source, host_id)
);

-- FTS5 for message search
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='id'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

-- Indexes: per dimension
CREATE INDEX IF NOT EXISTS idx_int_project_ts ON interactions(project_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_int_user ON interactions(user_id);
CREATE INDEX IF NOT EXISTS idx_int_host ON interactions(host_id);
CREATE INDEX IF NOT EXISTS idx_int_user_project ON interactions(user_id, project_id);

CREATE INDEX IF NOT EXISTS idx_token_project_ts ON token_usage(project_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_token_user ON token_usage(user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_token_host ON token_usage(host_id, timestamp);

CREATE INDEX IF NOT EXISTS idx_fileops_project_ts ON file_operations(project_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_project ON messages(project_id, session_id);

CREATE INDEX IF NOT EXISTS idx_file_ops_path ON file_operations(file_path);
CREATE INDEX IF NOT EXISTS idx_file_ops_timestamp ON file_operations(timestamp);
CREATE INDEX IF NOT EXISTS idx_file_ops_tool ON file_operations(tool);
CREATE INDEX IF NOT EXISTS idx_bash_timestamp ON bash_commands(timestamp);
CREATE INDEX IF NOT EXISTS idx_search_pattern ON search_operations(pattern);
CREATE INDEX IF NOT EXISTS idx_skill_name ON skill_invocations(skill_name);
CREATE INDEX IF NOT EXISTS idx_skill_timestamp ON skill_invocations(timestamp);
CREATE INDEX IF NOT EXISTS idx_subagent_type ON subagent_invocations(subagent_type);
CREATE INDEX IF NOT EXISTS idx_subagent_timestamp ON subagent_invocations(timestamp);
CREATE INDEX IF NOT EXISTS idx_mcp_tool ON mcp_tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_mcp_timestamp ON mcp_tool_calls(timestamp);
CREATE INDEX IF NOT EXISTS idx_token_timestamp ON token_usage(timestamp);
CREATE INDEX IF NOT EXISTS idx_token_model ON token_usage(model);
CREATE INDEX IF NOT EXISTS idx_int_timestamp ON interactions(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_msg_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_session_sources_source ON session_sources(source);

-- LAV-85: idx_mcp_kind is created in _migrate_add_tool_kind, NOT here — same
-- reason as idx_bash_cmd_name immediately below.

-- LAV-79: idx_bash_cmd_name is created in _migrate_add_cmd_name, NOT here — for
-- the same reason as the LAV-78 indexes below: CREATE TABLE IF NOT EXISTS does
-- not add cmd_name to a pre-LAV-79 bash_commands, so indexing it from SCHEMA
-- would abort this whole executescript on every existing DB.

-- LAV-78: the 6 idx_<tool_table>_tool_call partial indexes are created in
-- _migrate_add_tool_outcomes (from tool_outcomes.OUTCOME_INDEX_SQL), NOT here.
-- "WHERE tool_call_id != ''" cannot be compiled against a pre-LAV-78 table, and
-- CREATE TABLE IF NOT EXISTS does not add the column to an existing one, so
-- putting it in SCHEMA aborts the whole executescript on every existing DB.
-- Same reason as idx_token_usage_api_msg (LAV-39) above.

-- Interaction metadata (SQL-based classification, independent from Qdrant)
CREATE TABLE IF NOT EXISTS interaction_metadata (
    session_id TEXT NOT NULL,
    project_id INTEGER NOT NULL,
    summary TEXT,
    abstract TEXT,
    process TEXT,
    classification TEXT,
    data_sensitivity TEXT,
    sensitive_data_types TEXT,
    topics TEXT,
    people TEXT,
    clients TEXT,
    tags TEXT,
    model_used TEXT,
    created_at TEXT,
    updated_at TEXT,
    PRIMARY KEY (session_id, project_id),
    FOREIGN KEY (session_id, project_id) REFERENCES interactions(session_id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_intmeta_classification ON interaction_metadata(classification);
CREATE INDEX IF NOT EXISTS idx_intmeta_sensitivity ON interaction_metadata(data_sensitivity);
CREATE INDEX IF NOT EXISTS idx_intmeta_project ON interaction_metadata(project_id);

-- Model pricing (costs calculated at query time via JOIN)
CREATE TABLE IF NOT EXISTS model_pricing (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT NOT NULL,
    provider TEXT,
    input_price_per_mtok REAL NOT NULL,
    output_price_per_mtok REAL NOT NULL,
    cache_write_price_per_mtok REAL DEFAULT 0,
    cache_read_price_per_mtok REAL DEFAULT 0,
    from_date TEXT NOT NULL,
    to_date TEXT,
    notes TEXT,
    UNIQUE(model, from_date)
);
CREATE INDEX IF NOT EXISTS idx_pricing_model_date ON model_pricing(model, from_date);
"""

PRAGMAS = """
PRAGMA page_size = 8192;
PRAGMA auto_vacuum = INCREMENTAL;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
"""


# ===========================================================================
# DATABASE INIT
# ===========================================================================

def _migrate_parse_state(conn: sqlite3.Connection):
    """Migrate parse_state table: add host_id column to PK if missing."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(parse_state)").fetchall()}
    if "host_id" in cols:
        return  # already migrated
    print("Migrating parse_state: adding host_id dimension...")
    conn.executescript("""
        ALTER TABLE parse_state RENAME TO _parse_state_old;
        CREATE TABLE parse_state (
            key TEXT NOT NULL,
            project_id INTEGER NOT NULL DEFAULT -1,
            source TEXT NOT NULL DEFAULT '',
            host_id INTEGER NOT NULL DEFAULT -1,
            value TEXT,
            PRIMARY KEY (key, project_id, source, host_id)
        );
        DROP TABLE _parse_state_old;
    """)
    conn.commit()
    print("  Migration complete (old parse_state dropped, full reparse needed)")


def _migrate_add_api_message_id(conn: sqlite3.Connection):
    """LAV-39: add api_message_id column to messages/token_usage + partial unique index."""
    msg_cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "api_message_id" not in msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN api_message_id TEXT DEFAULT ''")
    tu_cols = {row[1] for row in conn.execute("PRAGMA table_info(token_usage)").fetchall()}
    if "api_message_id" not in tu_cols:
        conn.execute("ALTER TABLE token_usage ADD COLUMN api_message_id TEXT DEFAULT ''")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_token_usage_api_msg "
        "ON token_usage(session_id, project_id, api_message_id) "
        "WHERE api_message_id != ''"
    )
    conn.commit()


def _migrate_add_agent_id(conn: sqlite3.Connection):
    """LAV-66: add agent_id column to messages (subagent identity) + partial index."""
    msg_cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "agent_id" not in msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN agent_id TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_msg_agent ON messages(agent_id) "
        "WHERE agent_id IS NOT NULL"
    )
    conn.commit()


def _migrate_add_tool_outcomes(conn: sqlite3.Connection):
    """LAV-78: add the outcome columns to the 6 tool tables + partial indexes.

    Iterates tool_outcomes.OUTCOME_COLUMNS (the single source of truth shared with
    the SCHEMA literal) and checks PRAGMA table_info COLUMN BY COLUMN, so a run
    that died halfway through completes on the next call instead of being skipped
    because "the first column already exists".

    init_db() wraps every migration in try/except that PRINTS AND CONTINUES, so a
    failure here leaves a HALF-MIGRATED DB that the parsers keep writing to with
    INSERT OR IGNORE — i.e. silent row loss. The self-check at the end re-reads the
    schema and shouts about anything still missing.
    """
    added = 0
    failure = None
    try:
        for table, columns in tool_outcomes.OUTCOME_COLUMNS.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if not existing:
                continue  # table not created yet (SCHEMA runs first — odd, not fatal)
            for column, decl in columns:
                if column in existing:
                    continue
                # NO NOT NULL here — SQLite rejects ADD COLUMN NOT NULL without a
                # constant default, and the parsers' INSERT OR IGNORE would then
                # discard offending rows without a word.
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                added += 1
            # Commit per table so a later failure cannot roll back the work already
            # done: the next run then only has to finish what is left.
            conn.commit()

        for ddl in tool_outcomes.OUTCOME_INDEX_SQL:
            conn.execute(ddl)
        conn.commit()
    except Exception as e:  # noqa: BLE001 — re-raised below, after the self-check
        failure = e

    if added:
        print(f"  LAV-78: added {added} tool-outcome column(s)")

    # Self-check — runs even when an ALTER blew up above, which is exactly the
    # case init_db()'s print-and-continue would otherwise bury.
    missing = []
    for table, columns in tool_outcomes.OUTCOME_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not existing:
            missing.append(f"{table} (table missing)")
            continue
        for column, _decl in columns:
            if column not in existing:
                missing.append(f"{table}.{column}")
    if missing:
        print("!" * 72)
        print("!! LAV-78 MIGRATION INCOMPLETE — these columns are STILL MISSING:")
        for item in missing:
            print(f"!!   {item}")
        print("!! Tool outcomes will NOT be recorded and writes may be silently dropped.")
        print("!! Re-run init_db (any lav-parse/lav-server start) to finish the migration.")
        print("!" * 72)

    if failure is not None:
        raise failure


CMD_NAME_BATCH = 5000  # LAV-79: rows per commit in the cmd_name backfill

# LAV-82. Claude Code renamed the subagent-spawning tool Task -> Agent on this
# date; measured in the corpus, the last `Task` tool_use is 2026-02-27 and the
# first `Agent` is 2026-03-01. Used ONLY as the date guard on the one-shot
# spawn_tool seed — never as a routing condition.
SPAWN_TOOL_RENAME_DATE = "2026-03-01"

# parse_state key marking that the seed already ran on this DB. Scoped with the
# repo's sentinels (project_id=-1, source='', host_id=-1) — never NULL.
_SPAWN_TOOL_SEEDED = "schema:spawn_tool_seeded"


def _migrate_add_cmd_name(conn: sqlite3.Connection):
    """LAV-79: add bash_commands.cmd_name, index it, and derive it IN PLACE.

    Three idempotent steps, each safe to re-run and safe to resume after a crash:

    1. ALTER TABLE ... ADD COLUMN cmd_name TEXT DEFAULT '' — column by column via
       PRAGMA table_info, the same guard style as _migrate_add_tool_outcomes.
       (table_info is also why cmd_name is a REAL column and not a GENERATED one:
       PRAGMA table_info does not list generated columns, so queries._table_columns
       and cli._bf_table_columns — the repo's graceful-degradation idiom — would be
       permanently blind to it.)
    2. CREATE INDEX IF NOT EXISTS idx_bash_cmd_name — turns the dashboard GROUP BY
       into a covering-index scan AND makes step 3's own resume probe an index seek.
    3. Backfill. cmd_name is a pure function of bash_commands.command, which is
       already on disk, so history needs an UPDATE, NOT a reparse. Two passes:
         - cmd_name IS NULL  (rows from a DB where the column was added without a
           default, or by an explicit column list): every selected row is written,
           so the candidate set strictly shrinks and the pass terminates.
         - cmd_name = ''     (what ADD COLUMN DEFAULT '' leaves on existing rows):
           paged by id, and only rows whose derived name is non-empty are written.
           Rows that legitimately derive to '' (~0.05%: undecidable commands) stay
           candidates forever — that is intentional, it costs one index seek per
           init_db and keeps '' meaning exactly one thing on disk.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(bash_commands)").fetchall()}
    if not cols:
        return  # table not created yet (SCHEMA runs first — odd, not fatal)
    if "cmd_name" not in cols:
        # DEFAULT '' (a constant) is what makes ADD COLUMN legal here and keeps the
        # column non-NULL for every pre-existing row; step 3 then fills it in.
        conn.execute("ALTER TABLE bash_commands ADD COLUMN cmd_name TEXT DEFAULT ''")
        conn.commit()
        print("  LAV-79: added bash_commands.cmd_name")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_bash_cmd_name ON bash_commands(cmd_name)")
    conn.commit()

    updated = 0

    # Pass 1 — NULL rows. No id paging needed: every row read is written back.
    while True:
        rows = conn.execute(
            "SELECT id, command FROM bash_commands WHERE cmd_name IS NULL LIMIT ?",
            (CMD_NAME_BATCH,),
        ).fetchall()
        if not rows:
            break
        conn.executemany(
            "UPDATE bash_commands SET cmd_name = ? WHERE id = ?",
            [(bash_cmd_name(command), row_id) for row_id, command in rows],
        )
        conn.commit()
        updated += len(rows)

    # Pass 2 — '' rows. Paged by id because non-writable rows (derived '') would
    # otherwise be re-selected forever inside this loop.
    last_id = 0
    while True:
        rows = conn.execute(
            "SELECT id, command FROM bash_commands "
            "WHERE cmd_name = '' AND id > ? ORDER BY id LIMIT ?",
            (last_id, CMD_NAME_BATCH),
        ).fetchall()
        if not rows:
            break
        last_id = rows[-1][0]
        payload = [(name, row_id) for row_id, name in
                   ((r[0], bash_cmd_name(r[1])) for r in rows) if name]
        if payload:
            conn.executemany(
                "UPDATE bash_commands SET cmd_name = ? WHERE id = ?", payload)
            updated += len(payload)
        conn.commit()

    if updated:
        print(f"  LAV-79: derived cmd_name for {updated} bash_commands row(s)")


def _migrate_add_subagent_spawn(conn: sqlite3.Connection):
    """LAV-82: subagent_invocations.spawn_tool / .workflow_id, interactions.workflow_id.

    The two new subagent_invocations columns are derived attributes, so they are
    NOT in the UNIQUE key and adding them cannot duplicate a row.

    spawn_tool gets a ONE-SHOT seed rather than a permanent rule: every row that
    exists before this migration was necessarily written by the old Task-only
    branch, so it is provably 'Task'. Two belts, because a collector ingesting
    from an agent still on old code would keep shipping rows with an empty
    spawn_tool long after this runs, and re-seeding those as 'Task' would be a lie:

      1. a parse_state flag, so the seed runs exactly once per DB;
      2. a date guard — the rename landed 2026-03-01, and the last real Task call
         in the corpus is 2026-02-27.

    interactions.workflow_id needs no backfill here: agent transcript files bypass
    BOTH the mtime skip and the per-message watermark (see the is_agent_file
    guards), so a plain incremental lav-parse re-reads every wf_ agent file on the
    next run and update_interaction() fills the column from the path.
    """
    subagent_cols = {row[1] for row in
                     conn.execute("PRAGMA table_info(subagent_invocations)").fetchall()}
    if subagent_cols:
        for column, decl in (("spawn_tool", "TEXT DEFAULT ''"),
                             ("workflow_id", "TEXT DEFAULT ''")):
            if column not in subagent_cols:
                conn.execute(
                    f"ALTER TABLE subagent_invocations ADD COLUMN {column} {decl}")
                conn.commit()
                print(f"  LAV-82: added subagent_invocations.{column}")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_subagent_spawn "
                     "ON subagent_invocations(spawn_tool)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_subagent_workflow "
                     "ON subagent_invocations(workflow_id)")
        conn.commit()

        if get_parse_state(conn, _SPAWN_TOOL_SEEDED) is None:
            seeded = conn.execute(
                "UPDATE subagent_invocations SET spawn_tool = 'Task' "
                "WHERE COALESCE(spawn_tool, '') = '' AND timestamp < ?",
                (SPAWN_TOOL_RENAME_DATE,),
            ).rowcount
            set_parse_state(conn, _SPAWN_TOOL_SEEDED, SPAWN_TOOL_RENAME_DATE)
            conn.commit()
            if seeded:
                print(f"  LAV-82: seeded spawn_tool='Task' on {seeded} historical row(s)")

    interaction_cols = {row[1] for row in
                        conn.execute("PRAGMA table_info(interactions)").fetchall()}
    if interaction_cols and "workflow_id" not in interaction_cols:
        conn.execute("ALTER TABLE interactions ADD COLUMN workflow_id TEXT DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_int_workflow "
                     "ON interactions(workflow_id)")
        conn.commit()
        print("  LAV-82: added interactions.workflow_id")
    elif interaction_cols:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_int_workflow "
                     "ON interactions(workflow_id)")
        conn.commit()


def _migrate_add_tool_kind(conn: sqlite3.Connection):
    """LAV-85: add mcp_tool_calls.kind, index it, and derive it IN PLACE.

    Same shape as _migrate_add_cmd_name. The derivation runs in three passes that
    together reproduce tool_kind() exactly:

      1. session_id LIKE 'chatgpt:%'  -> builtin_host, in pure SQL. That branch of
         tool_kind() consults nothing else, so there is no logic to duplicate.
      2. session_id LIKE 'claudeai:%' -> tool_kind() IN PYTHON, row by row. This is
         the only branch that needs the whitelist and the `<integration>:<tool>`
         normalisation, and expressing THAT in SQL means an unreadable
         rtrim/replace idiom for split(':')[-1] that nobody can review. Calling the
         real function instead makes drift between migration and parser impossible.
         The set is small enough that the loop is free: 5.379 rows here, 6.564 on
         prod, against 204k in the table.
      3. everything still '' -> mcp. tool_kind() has no third answer.

    Because tool_kind() is TOTAL, pass 3 leaves no unclassified row, so the resume
    probe on every later init_db() is an empty index seek rather than a scan. That
    totality is also the contract the dashboard relies on: `kind = ''` on a DB
    init_db() has touched means the migration FAILED, not "unknown".

    This migration only ever fills blanks. Applying an EDITED whitelist to history
    needs `lav backfill tool-kind --reclassify`, which resets the column first.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(mcp_tool_calls)").fetchall()}
    if not cols:
        return  # table not created yet (SCHEMA runs first — odd, not fatal)
    if "kind" not in cols:
        conn.execute("ALTER TABLE mcp_tool_calls ADD COLUMN kind TEXT DEFAULT ''")
        conn.commit()
        print("  LAV-85: added mcp_tool_calls.kind")

    # (kind, server_name) and not (kind) alone: every dashboard query that splits
    # the two worlds then groups by server inside one of them.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mcp_kind ON mcp_tool_calls(kind, server_name)")
    conn.commit()

    updated = conn.execute(
        "UPDATE mcp_tool_calls SET kind = ? WHERE kind = '' AND session_id LIKE ?",
        (tool_outcomes.TOOL_KIND_BUILTIN_HOST,
         tool_outcomes._CHATGPT_SESSION_PREFIX + "%"),
    ).rowcount
    conn.commit()

    # Paged by id: rows that tool_kind() resolves to 'mcp' are written too, so the
    # candidate set does shrink — but paging keeps the working set bounded on a DB
    # where the claude.ai corpus is much larger than this one.
    last_id = 0
    while True:
        rows = conn.execute(
            "SELECT id, session_id, server_name, tool_name FROM mcp_tool_calls "
            "WHERE kind = '' AND session_id LIKE ? AND id > ? ORDER BY id LIMIT ?",
            (tool_outcomes._CLAUDE_AI_SESSION_PREFIX + "%", last_id, CMD_NAME_BATCH),
        ).fetchall()
        if not rows:
            break
        last_id = rows[-1][0]
        conn.executemany(
            "UPDATE mcp_tool_calls SET kind = ? WHERE id = ?",
            [(tool_outcomes.tool_kind(sid, server, tname), row_id)
             for row_id, sid, server, tname in rows],
        )
        conn.commit()
        updated += len(rows)

    updated += conn.execute(
        "UPDATE mcp_tool_calls SET kind = ? WHERE kind = ''",
        (tool_outcomes.TOOL_KIND_MCP,),
    ).rowcount
    conn.commit()

    if updated:
        print(f"  LAV-85: derived kind for {updated} mcp_tool_calls row(s)")


def _migrate_conversations_to_interactions(conn: sqlite3.Connection, db_path: Path):
    """Migrate old 'conversations'/'conversation_metadata' tables to 'interactions'/'interaction_metadata'."""
    # Check if old 'conversations' table exists
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='conversations'"
    ).fetchone()
    if not row:
        return  # No old table, nothing to migrate

    # Check if new 'interactions' table is empty (freshly created by schema)
    count = conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
    if count > 0:
        return  # Already has data, skip migration

    import shutil
    bak_path = db_path.with_suffix('.db.bak')
    print(f"Migrating conversations -> interactions (backup: {bak_path})")
    shutil.copy2(str(db_path), str(bak_path))

    # Copy conversations -> interactions
    conn.execute("INSERT INTO interactions SELECT * FROM conversations")
    print(f"  Copied conversations -> interactions")

    # Copy conversation_metadata -> interaction_metadata (if old table exists)
    meta_row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='conversation_metadata'"
    ).fetchone()
    if meta_row:
        conn.execute("INSERT INTO interaction_metadata SELECT * FROM conversation_metadata")
        print(f"  Copied conversation_metadata -> interaction_metadata")

    # Drop old tables
    conn.execute("DROP TABLE IF EXISTS conversation_metadata")
    conn.execute("DROP TABLE conversations")
    conn.commit()
    print("  Migration complete: old tables dropped")


def init_db(db_path: Path = UNIFIED_DB_PATH) -> sqlite3.Connection:
    """Initialize the unified SQLite database with schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(PRAGMAS)
    conn.executescript(SCHEMA)
    conn.commit()
    # Seed default model pricing
    from lav.pricing import ensure_pricing_overlap_guard, seed_default_pricing
    seed_default_pricing(conn)
    # LAV-76: one open-ended pricing row per model (warns instead of failing
    # when a pre-existing DB still holds duplicates)
    try:
        ensure_pricing_overlap_guard(conn)
    except Exception as e:
        print(f"  pricing overlap guard skipped: {e}")
    # Migrate existing DBs that lack host_id in parse_state
    try:
        _migrate_parse_state(conn)
    except Exception as e:
        print(f"  Migration check skipped: {e}")
    # LAV-39: add api_message_id column + partial unique index
    try:
        _migrate_add_api_message_id(conn)
    except Exception as e:
        print(f"  api_message_id migration skipped: {e}")
    # LAV-66: add agent_id column to messages
    try:
        _migrate_add_agent_id(conn)
    except Exception as e:
        print(f"  agent_id migration skipped: {e}")
    # LAV-78: add tool-outcome columns to the 6 tool tables + partial indexes
    try:
        _migrate_add_tool_outcomes(conn)
    except Exception as e:
        print(f"  tool_outcomes migration skipped: {e}")
    # LAV-79: add bash_commands.cmd_name + index, and derive it for existing rows
    try:
        _migrate_add_cmd_name(conn)
    except Exception as e:
        print(f"  cmd_name migration skipped: {e}")
    # LAV-85: add mcp_tool_calls.kind + index, and derive it for existing rows
    try:
        _migrate_add_tool_kind(conn)
    except Exception as e:
        print(f"  tool_kind migration skipped: {e}")
    # LAV-82: subagent spawn_tool / workflow_id + interactions.workflow_id
    try:
        _migrate_add_subagent_spawn(conn)
    except Exception as e:
        print(f"  subagent_spawn migration skipped: {e}")
    # Migrate conversations -> interactions
    try:
        _migrate_conversations_to_interactions(conn, db_path)
    except Exception as e:
        print(f"  conversations->interactions migration skipped: {e}")
    return conn


# ===========================================================================
# DETECTION: user, host
# ===========================================================================

def detect_user_from_path(source_path: Path) -> str:
    """Detect username from a filesystem path. Cross-platform."""
    path_str = str(source_path)
    # macOS / Linux
    m = re.match(r'/(Users|home)/([^/]+)/', path_str)
    if m:
        return m.group(2)
    # Windows
    m = re.match(r'[A-Z]:[/\\]Users[/\\]([^/\\]+)', path_str)
    if m:
        return m.group(1)
    return getpass.getuser()


# Generic placeholder names macOS/BSD hand out when the real host name is not
# resolvable (DHCP/Bonjour transients). Never worth a distinct host record.
_GENERIC_HOSTNAMES = {
    "", "mac", "macbook", "macbookpro", "macbook-pro", "macbookair",
    "macbook-air", "imac", "localhost", "unknown",
}


def _normalize_hostname(hostname: str) -> str:
    """Strip .local, .localdomain etc. to avoid duplicate host records."""
    for suffix in ('.localdomain', '.local'):
        if hostname.endswith(suffix):
            hostname = hostname[:-len(suffix)]
    return hostname


def _is_valid_hostname(hostname: str) -> bool:
    """Reject empty, generic-fallback, or corrupted (mojibake/surrogate) names.

    LAV-68: socket.gethostname() on macOS is volatile — it transiently
    returns the generic 'Mac' or an undecodable byte string that surfaces as
    mojibake (U+FFFD) or surrogate-escaped chars (U+DC80..). Such values must
    never become host records, or one machine's sessions get split across hosts.
    """
    if not hostname or not hostname.strip():
        return False
    for ch in hostname:
        o = ord(ch)
        # control chars (C0/DEL/C1) or replacement/surrogate => corrupted encoding
        if o < 0x20 or o == 0x7f or 0x80 <= o <= 0x9f:
            return False
        if ch == "�" or 0xd800 <= o <= 0xdfff:
            return False
    if hostname.strip().lower() in _GENERIC_HOSTNAMES:
        return False
    return True


def _canonical_hostname() -> str:
    """Stable host identity, immune to socket.gethostname() volatility (LAV-68).

    Precedence: LAV_HOSTNAME env > config.json 'hostname' key > validated
    socket.gethostname() > 'unknown' sentinel. A corrupted or generic detected
    name is discarded rather than turned into a spurious host record.
    """
    env = os.environ.get("LAV_HOSTNAME", "").strip()
    if _is_valid_hostname(env):
        return _normalize_hostname(env)
    try:
        cfg_host = str(load_runtime_config().get("hostname", "")).strip()
        if _is_valid_hostname(cfg_host):
            return _normalize_hostname(cfg_host)
    except Exception:
        pass
    sock = _normalize_hostname(socket.gethostname())
    if _is_valid_hostname(sock):
        return sock
    return "unknown"


def detect_host() -> tuple[str, str, str]:
    """Detect current host info. Returns (hostname, os_type, home_dir)."""
    hostname = _canonical_hostname()
    os_type = platform.system().lower()
    home_dir = str(Path.home())
    return (hostname, os_type, home_dir)


def detect_host_from_path(source_path: Path) -> tuple[str, str, str]:
    """Detect host info from path + runtime. Returns (hostname, os_type, home_dir)."""
    hostname = _canonical_hostname()
    path_str = str(source_path)

    if '/Users/' in path_str:
        os_type = 'darwin'
    elif '/home/' in path_str:
        os_type = 'linux'
    elif re.match(r'[A-Z]:[/\\]', path_str):
        os_type = 'windows'
    else:
        os_type = platform.system().lower()

    m = re.match(r'(/(Users|home)/[^/]+)', path_str)
    home_dir = m.group(1) if m else str(Path.home())
    return (hostname, os_type, home_dir)


# ===========================================================================
# GET OR CREATE: project, user, host
# ===========================================================================

def get_or_create_project(conn: sqlite3.Connection, name: str, source_path: str = "") -> int:
    """Get project_id, creating the project row if needed."""
    cursor = conn.execute("SELECT id FROM projects WHERE name = ?", (name,))
    row = cursor.fetchone()
    if row:
        conn.execute(
            "UPDATE projects SET last_seen = datetime('now'), source_path = COALESCE(NULLIF(?, ''), source_path) WHERE id = ?",
            (source_path, row[0])
        )
        return row[0]
    cursor = conn.execute(
        "INSERT INTO projects (name, source_path, first_seen, last_seen) VALUES (?, ?, datetime('now'), datetime('now'))",
        (name, source_path)
    )
    return cursor.lastrowid


def get_or_create_user(conn: sqlite3.Connection, username: str) -> int:
    """Get user_id, creating the user row if needed."""
    cursor = conn.execute("SELECT id FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    if row:
        conn.execute("UPDATE users SET last_seen = datetime('now') WHERE id = ?", (row[0],))
        return row[0]
    cursor = conn.execute(
        "INSERT INTO users (username, first_seen, last_seen) VALUES (?, datetime('now'), datetime('now'))",
        (username,)
    )
    return cursor.lastrowid


def get_or_create_host(conn: sqlite3.Connection, hostname: str, os_type: str = "", home_dir: str = "") -> int:
    """Get host_id, creating the host row if needed."""
    cursor = conn.execute("SELECT id FROM hosts WHERE hostname = ?", (hostname,))
    row = cursor.fetchone()
    if row:
        conn.execute(
            "UPDATE hosts SET last_seen = datetime('now'), os_type = COALESCE(NULLIF(?, ''), os_type), home_dir = COALESCE(NULLIF(?, ''), home_dir) WHERE id = ?",
            (os_type, home_dir, row[0])
        )
        return row[0]
    cursor = conn.execute(
        "INSERT INTO hosts (hostname, os_type, home_dir, first_seen, last_seen) VALUES (?, ?, ?, datetime('now'), datetime('now'))",
        (hostname, os_type, home_dir)
    )
    return cursor.lastrowid


# ===========================================================================
# PARSE STATE (scoped)
# ===========================================================================

def get_parse_state(conn: sqlite3.Connection, key: str, project_id: int = -1, source: str = "", host_id: int = -1) -> Optional[str]:
    """Get scoped parse state value (host-aware)."""
    cursor = conn.execute(
        "SELECT value FROM parse_state WHERE key = ? AND project_id = ? AND source = ? AND host_id = ?",
        (key, project_id, source, host_id)
    )
    row = cursor.fetchone()
    return row[0] if row else None


def set_parse_state(conn: sqlite3.Connection, key: str, value: str, project_id: int = -1, source: str = "", host_id: int = -1):
    """Set scoped parse state value (host-aware)."""
    conn.execute(
        "INSERT OR REPLACE INTO parse_state (key, project_id, source, host_id, value) VALUES (?, ?, ?, ?, ?)",
        (key, project_id, source, host_id, value)
    )


def set_parse_state_monotonic(conn: sqlite3.Connection, key: str, value: str,
                              project_id: int = -1, source: str = "", host_id: int = -1,
                              compare=None, label: str = "", force: bool = False) -> bool:
    """LAV-79: write `value` only if it is strictly GREATER than what is stored.

    ``--since`` lowers the watermark that GATES the parse; it must be
    structurally incapable of lowering the watermark that is PERSISTED. That
    single invariant is what makes a ``--since`` run non-destructive: worst case
    it re-reads files it already holds (every insert is guarded by a UNIQUE
    constraint or a NOT EXISTS), it can never make a LATER incremental run
    re-read them again.

    This is belt AND braces on purpose. Seeding the running max from the STORED
    watermark instead of the lowered one (see parse_project / parse_codex_sessions)
    already makes the write monotone in every case; this helper is what survives a
    future edit to that seeding line. The codex path proves the point: it had a
    ``max_dt > original`` guard and still regressed, because ``original`` was read
    from the dict the interception had already lowered. A guard that compares
    against the effective value is not a guard.

    `compare` maps both sides to a comparable form before the test — the Codex
    path passes ``_parse_codex_watermark_ts`` because 31 production watermarks are
    legacy NAIVE local-time strings that must not be compared lexicographically.
    Without it the test is plain string comparison, exact for the 24-char ISO-Z
    shape claude_code and cowork both write.

    `force=True` is the ``--full`` escape hatch: a full reparse deletes and
    rebuilds, so it stays allowed to reset a stale-high watermark down to the true
    on-disk max (pre-LAV-79 behaviour, preserved verbatim). ``--full --since`` is
    rejected in main(), so force and a lowered gate can never coexist.

    Returns True if the value was written. A refusal that would REGRESS the
    watermark is printed; a refusal of an identical value is the normal warm-run
    no-op and stays quiet.
    """
    if not value:
        return False
    if not force:
        prev = get_parse_state(conn, key, project_id, source, host_id)
        if prev:
            try:
                a, b = (compare(value), compare(prev)) if compare else (value, prev)
            except (ValueError, TypeError):
                a = b = None
            if a is not None and b is not None and a <= b:
                if a < b:
                    where = f" for {label}" if label else ""
                    print(f"  [watermark] held at {prev} — refused {value}{where}")
                return False
    set_parse_state(conn, key, value, project_id, source, host_id)
    return True


# ===========================================================================
# HELPERS
# ===========================================================================

def extract_project_name(project_path: Path) -> str:
    """Extract a clean project name from a Claude projects directory path.

    Claude projects encode the full working directory path by replacing both
    '/' and '_' with '-', e.g. /Users/max/my_code/twin-peaks →
    -Users-max-my-code-twin-peaks.  We try to reconstruct the original path
    via greedy filesystem matching so hyphenated project names are preserved.
    Falls back to the last meaningful segment when the path can't be fully
    resolved (e.g. cloud-storage paths with lossy encoding).
    """
    name = project_path.name

    # Real filesystem path (e.g. cwd from Codex/Cowork) — last component is the name
    if not name.startswith('-'):
        return name

    parts = [p for p in name.split('-') if p]
    if not parts:
        return name

    # Greedily match filesystem directories from left to right.
    # For each candidate span we try the original (hyphens) and an underscore
    # variant, since '_' is also encoded as '-' by Claude Code.
    current = Path('/')
    i = 0
    while i < len(parts):
        matched = False
        for j in range(i + 1, min(i + 7, len(parts) + 1)):
            segment = '-'.join(parts[i:j])
            for variant in (segment, segment.replace('-', '_')):
                candidate = current / variant
                if candidate.is_dir():
                    current = candidate
                    i = j
                    matched = True
                    break
            if matched:
                break
        if not matched:
            break

    if i >= len(parts):
        # All segments resolved — last directory is the project
        return current.name

    # Fallback: filesystem couldn't resolve the full path (cloud storage,
    # unmounted volumes, etc.).  Use the old heuristic — filter known path
    # components and take the last meaningful segment.
    meaningful = [p for p in parts if p and p not in
                  ('Users', getpass.getuser(), 'Library', 'CloudStorage')]
    if meaningful:
        return meaningful[-1]
    return name


def smart_title(display: str) -> str:
    """Generate intelligent title from display text when summary is not available."""
    if not display:
        return "(no title)"

    ide_file_match = re.search(r'<ide_opened_file>.*?file\s+(.+?\.[\w]+)', display)
    if ide_file_match:
        filepath = ide_file_match.group(1)
        filename = filepath.split('/')[-1] if '/' in filepath else filepath
        return f"File: {filename}"

    cleaned = re.sub(r'<ide_selection>.*?</ide_selection>\s*', '', display, flags=re.DOTALL)
    cleaned = re.sub(r'<ide_selection>.*?\n', '', cleaned, flags=re.DOTALL)
    cleaned = re.sub(r'<ide_opened_file>.*?</ide_opened_file>\s*', '', cleaned, flags=re.DOTALL)
    cleaned = re.sub(r'<ide_opened_file>.*', '', cleaned, flags=re.DOTALL)
    cleaned = re.sub(r'^(Warmup|warmup)\s*$', '', cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()

    if not cleaned:
        return "(no title)"

    if '. ' in cleaned[:100]:
        idx = cleaned.index('. ') + 1
        return cleaned[:idx]

    if len(cleaned) > 80:
        return cleaned[:80] + '...'

    return cleaned


# LAV-66: Claude Code wraps injected context in pseudo-XML tags. A user message
# can be entirely made of these wrappers (slash-command invocations, IDE state,
# hook output) — using it as interaction display/summary shows noise like
# "<local-command-caveat>Caveat: The messages below..." instead of real text.
_SYSTEM_TAG_NAMES = (
    "local-command-caveat", "local-command-stdout", "local-command-stderr",
    "command-name", "command-message", "command-args", "command-contents",
    "ide_opened_file", "ide_opened_files", "ide_selection", "ide_diagnostics",
    "system-reminder", "session-start-hook",
)
_SYSTEM_TAG_BLOCK_RE = re.compile(
    r"<(" + "|".join(_SYSTEM_TAG_NAMES) + r")(?:\s[^>]*)?>.*?</\1\s*>",
    re.DOTALL | re.IGNORECASE,
)
_SYSTEM_TAG_LONE_RE = re.compile(
    r"</?(?:" + "|".join(_SYSTEM_TAG_NAMES) + r")(?:\s[^>]*)?/?>",
    re.IGNORECASE,
)


def strip_system_tags(text: str) -> str:
    """Remove system wrapper tag blocks; returns '' if nothing real remains."""
    if not text or "<" not in text:
        return (text or "").strip()
    cleaned = _SYSTEM_TAG_BLOCK_RE.sub("", text)
    cleaned = _SYSTEM_TAG_LONE_RE.sub("", cleaned)
    return cleaned.strip()


def parse_jsonl_file(file_path: Path) -> Generator[dict, None, None]:
    """Parse a JSONL file and yield each message."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
    except Exception as e:
        print(f"Error reading {file_path}: {e}")


def format_codex_session_id(session_id: str) -> str:
    """Prefix Codex session IDs to avoid collisions."""
    if not session_id:
        return ""
    if session_id.startswith("codex:"):
        return session_id
    return f"codex:{session_id}"


# Explicit CLI-family originators that all collapse to codex_cli.
_CODEX_CLI_ORIGINATORS = frozenset({
    "codex_cli_rs", "codex-tui", "codex_tui", "codex_exec", "codex_cli", "codex-cli",
})

# Recognized Codex surfaces (positively identified) — used to upgrade a stale
# generic codex_cli/codex_local row, both in the local parser and when ingesting
# a remote agent's re-attributed sessions (LAV-74).
_CODEX_RECOGNIZED_SOURCES = frozenset({
    SOURCE_CHATGPT_WORK_DESKTOP, SOURCE_CODEX_DESKTOP, SOURCE_CODEX_VSCODE, SOURCE_CODEX_CLI,
})


def map_codex_source(originator: str) -> tuple[str, bool]:
    """Map a Codex ``session_meta.payload.originator`` to a LAV source label (LAV-74).

    Returns ``(source, recognized)``. ``recognized`` is True only when the
    originator positively identifies a surface — the caller uses it to decide
    whether re-attributing a stale ``codex_cli`` row is safe. Unknown non-empty
    originators map to ``codex_local`` (raw value preserved in meta_json); an
    absent originator falls back to ``codex_cli`` (legacy default) without
    claiming recognition, so it never overrides an existing label.

    Note: ``payload.source`` (e.g. "vscode") is the editor host, NOT the
    surface — do not use it for attribution.
    """
    orig = (originator or "").strip()
    if not orig:
        return SOURCE_CODEX_CLI, False
    key = orig.lower()
    if key == "codex_work_desktop":
        return SOURCE_CHATGPT_WORK_DESKTOP, True
    if key in ("codex desktop", "codex_desktop"):
        return SOURCE_CODEX_DESKTOP, True
    if key == "codex_vscode":
        return SOURCE_CODEX_VSCODE, True
    if (key in _CODEX_CLI_ORIGINATORS
            or key.startswith("codex_cli") or key.startswith("codex-cli")
            or "tui" in key or key.endswith("_exec")):
        return SOURCE_CODEX_CLI, True
    return SOURCE_CODEX_LOCAL, False


def _parse_codex_event_ts(ts: str) -> Optional[datetime]:
    """Parse a Codex event timestamp into an aware UTC datetime.

    Codex writes ISO-8601 UTC (trailing ``Z``). A rare naive value is assumed
    to already be UTC (Codex logs UTC), NOT local time.
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_codex_watermark_ts(ts: str) -> Optional[datetime]:
    """Parse a stored Codex watermark into an aware UTC datetime.

    Legacy watermarks were written as ``datetime.now().isoformat()`` — naive
    LOCAL time. A naive value is therefore interpreted as local time and
    converted to UTC; an already-aware value is just normalized to UTC.
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    # naive -> astimezone() presumes system-local tz, then convert to UTC.
    return dt.astimezone(timezone.utc)


def _normalize_since(raw: str) -> tuple:
    """LAV-79: parse ``--since`` once into ``(str_z, dt_utc, epoch)``.

    Accepts ``2026-07-10``, ``2026-07-10T00:00:00Z``, ``2026-07-10T02:00:00+02:00``.
    A naive value is read as UTC (LAV logs UTC), never as local time.

    The 24-char millisecond ISO-Z shape of ``str_z`` is load-bearing, not
    cosmetic: every claude_code / cowork watermark and record timestamp is
    exactly ``YYYY-MM-DDTHH:MM:SS.mmmZ`` and the parser compares them
    LEXICOGRAPHICALLY. A second-precision ``...:00Z`` would compare ``'Z'``
    (0x5A) against ``'.'`` (0x2E) and silently sort AFTER every record inside
    that same second. The three forms are returned together so the string, the
    datetime (codex compares aware UTC datetimes) and the epoch (the file-mtime
    gate) can never drift apart.
    """
    s = (raw or "").strip()
    if len(s) == 10:
        s += "T00:00:00"
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return (dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", dt, dt.timestamp())


def format_cowork_session_id(session_id: str) -> str:
    """Prefix Cowork/Claude Desktop session IDs to avoid collisions."""
    if not session_id:
        return ""
    if session_id.startswith("cowork:"):
        return session_id
    return f"cowork:{session_id}"


def _cowork_user_text(event: dict) -> Optional[str]:
    """First plain-text body of a Cowork user event (used to title a merged conversation)."""
    msg = event.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                return item.get("text")
    return None


# Generic path segments that are NOT a project (output/scratch dirs, OS/cloud folders).
_COWORK_GENERIC_SEGMENTS = frozenset({
    "outputs", "output", "uploads", "upload", "mnt", "home", "cache", "tmp", "temp",
    "downloads", "desktop", "documents", "applications", "movies", "music", "pictures",
    "public", "library", "cloudstorage", "tool-results", "tool_results", "sessions",
    "artifacts", "shared", "var", "folders", "private", "users", "skills", "uploads",
    ".claude", ".skills", ".config", ".local-plugins", ".git", "node_modules",
})
# Segment names after which the NEXT segment is the real project root.
_COWORK_ROOT_MARKERS = frozenset({"mnt", "claude_coworks", "artifacts"})


def _looks_like_filename(seg: str) -> bool:
    """A path segment that is a file (has an extension), not a directory/project."""
    return "." in seg and not seg.startswith(".")


def infer_cowork_project(path_str: str) -> Optional[str]:
    """Best-effort REAL project name from a Cowork path, or None (-> cowork_default).

    Cowork has no project working dir: it runs in an ephemeral sandbox (/sessions/...)
    and writes results into generic folders (outputs/, uploads/) or names files directly.
    The old extract_project_name returned the last path segment, so it produced junk
    project names like 'outputs' or 'SKILL.md'. This resolver instead rejects sandbox /
    scratch / OS locations and extracts a meaningful project root, falling back to None.
    """
    if not path_str or not path_str.startswith("/"):
        return None
    low = path_str.lower()
    # Ephemeral / non-project locations -> no project.
    if ("local-agent-mode-sessions" in low or "claude-code-sessions" in low
            or low.startswith("/tmp") or low.startswith("/private/tmp")
            or "/var/folders/" in low or "claude-hostloop" in low):
        return None

    segments = [s for s in Path(path_str).parts if s and s != "/"]
    if not segments:
        return None

    def _pick(seg: str) -> Optional[str]:
        s = (seg or "").strip()
        if not s or s.startswith("-") or _looks_like_filename(s):
            return None
        if s.lower() in _COWORK_GENERIC_SEGMENTS:
            return None
        return s

    # 1. Segment right after a known root marker (e.g. /mnt/<project>, /Claude_Coworks/<project>).
    for i in range(len(segments) - 1):
        if segments[i].lower() in _COWORK_ROOT_MARKERS:
            cand = _pick(segments[i + 1])
            if cand:
                return cand

    # 2. /Users/<user>/...: first meaningful dir, skipping OS/cloud wrappers.
    if segments[0].lower() == "users" and len(segments) > 2:
        for seg in segments[2:]:
            if seg.lower().startswith("onedrive"):
                continue
            cand = _pick(seg)
            if cand:
                return cand

    return None


# ===========================================================================
# SESSION SOURCE
# ===========================================================================

def upsert_session_source(
    conn: sqlite3.Connection,
    session_id: str,
    project_id: int,
    source: str,
    client_version: str = "",
    process_name: str = "",
    vm_process_name: str = "",
    meta: Optional[dict] = None,
    override_sources: Optional[set] = None,
):
    """Upsert session source with project scope.

    ``override_sources`` (LAV-74): an optional set of stale source labels the
    caller is allowed to re-attribute to ``source``. The normal upsert never
    changes an already-set source (COALESCE keeps it); this lets the Codex
    parser upgrade an old generic ``codex_cli`` row to a newly recognized
    surface. Other parsers pass ``None`` and keep the original behaviour.
    """
    if not session_id or not source:
        return
    meta_json = json.dumps(meta) if isinstance(meta, dict) else None
    try:
        conn.execute(
            """INSERT OR IGNORE INTO session_sources
               (session_id, project_id, source, client_version, process_name, vm_process_name, meta_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (session_id, project_id, source, client_version or None, process_name or None, vm_process_name or None, meta_json),
        )
        conn.execute(
            """UPDATE session_sources
               SET
                 source = COALESCE(NULLIF(source,''), ?),
                 client_version = COALESCE(NULLIF(client_version,''), ?),
                 process_name = COALESCE(NULLIF(process_name,''), ?),
                 vm_process_name = COALESCE(NULLIF(vm_process_name,''), ?),
                 meta_json = COALESCE(meta_json, ?)
               WHERE session_id = ? AND project_id = ?""",
            (source, client_version or None, process_name or None, vm_process_name or None, meta_json, session_id, project_id),
        )
        if override_sources:
            overridable = tuple(s for s in override_sources if s and s != source)
            if overridable:
                placeholders = ",".join("?" * len(overridable))
                conn.execute(
                    f"""UPDATE session_sources
                        SET source = ?, meta_json = COALESCE(?, meta_json)
                        WHERE session_id = ? AND project_id = ? AND source IN ({placeholders})""",
                    (source, meta_json, session_id, project_id, *overridable),
                )
    except sqlite3.Error:
        return


# ===========================================================================
# BASH / FILE CLASSIFICATION
# ===========================================================================

def is_file_related_bash(command: str) -> bool:
    """Check if a bash command is file-related."""
    if not command:
        return False
    first_word = command.split()[0] if command.split() else ""
    cmd_name = Path(first_word).name
    return cmd_name in FILE_COMMANDS


def extract_target_file(command: str) -> Optional[str]:
    """Extract target file from a bash command (best effort)."""
    if not command:
        return None
    parts = command.split()
    if len(parts) < 2:
        return None
    cmd = Path(parts[0]).name

    if cmd in ('cat', 'head', 'tail', 'less', 'more', 'rm', 'touch', 'mkdir'):
        for part in parts[1:]:
            if not part.startswith('-'):
                return part
    elif cmd in ('cp', 'mv'):
        non_flags = [p for p in parts[1:] if not p.startswith('-')]
        if len(non_flags) >= 2:
            return non_flags[1]
        elif non_flags:
            return non_flags[0]
    elif cmd == 'ls':
        for part in parts[1:]:
            if not part.startswith('-'):
                return part
    return None


def get_bash_category(command: str) -> Optional[str]:
    """Classify bash command into Read/Write category."""
    if not command:
        return None
    parts = command.split()
    if not parts:
        return None
    cmd = Path(parts[0]).name

    if cmd in BASH_READ_COMMANDS:
        return 'BashRead'
    elif cmd in BASH_WRITE_COMMANDS:
        return 'BashWrite'
    return None


# ---------------------------------------------------------------------------
# LAV-79: cmd_name — which program a Bash call actually runs
# ---------------------------------------------------------------------------
# bash_commands now holds 100% of Bash calls, so "what was run" can no longer be
# a handful of LIKE arms over the raw command text — 69% of the real corpus lands
# in 'other'. cmd_name is that answer, derived once at parse time and stored.
#
# The naive rule (first whitespace token, basename, lower) is the only thing SQL
# could do on its own, and it is not good enough: measured over 63,982 real Bash
# commands it yields 901 buckets, 'cd' alone takes 17,220 of them (27%) and only
# 69.5% of rows get a usable program name. The rule below gets the same corpus to
# 303 buckets, 'cd' = 0 and 99.86% usable, at 3.4 us/command.
CMD_NAME_MAX_SCAN = 4000  # heredoc bodies are irrelevant; the verb is up front
CMD_NAME_MAX_HOPS = 12    # wrapper/preamble hops before giving up
CMD_NAME_MAX_LEN = 64     # storage cap

# FOO=bar / FOO+="a b" / X=$(cmd) — quote-aware, so `A="x y" cmd` does not turn
# into the bucket `y`.
_CMD_ASSIGN_RE = re.compile(
    r"""^[A-Za-z_][A-Za-z0-9_]*\+?=(?:"[^"]*"|'[^']*'|`[^`]*`|\$\([^)]*\)|[^\s;&|)]*)"""
)
_CMD_SEP_RE = re.compile(r"(?:&&|\|\||;|\||\n)")
_CMD_TOKEN_RE = re.compile(r"^[^\s;&|<>()]+")
_CMD_LEAD_CHARS = " \t\n\r(){}!;&|<>"
# Consumed together with their leading -flags; the interesting program follows.
_CMD_WRAPPERS = frozenset((
    "sudo", "doas", "env", "time", "nohup", "exec", "command", "builtin",
    "nice", "stdbuf", "caffeinate",
))
# Short options of those wrappers that eat the NEXT token, e.g. `sudo -u root X`
# or `nice -n 19 X` — without this the bucket becomes `root` / `19`. Detached
# form only; the attached form (`-o0`) is already handled as a plain flag.
_CMD_WRAPPER_ARG_FLAGS = {
    "sudo": frozenset(("-u", "-g", "-C", "-p", "-r", "-t", "-T", "-D", "-h")),
    "doas": frozenset(("-u", "-C")),
    "env": frozenset(("-u", "-C", "-S")),
    "time": frozenset(("-f", "-o")),
    "nice": frozenset(("-n",)),
    "stdbuf": frozenset(("-i", "-o", "-e")),
    "caffeinate": frozenset(("-t", "-w")),
}
# Shell preamble: never the point of the call. Skip to after the next separator.
_CMD_PREAMBLE = frozenset((
    "cd", "pushd", "popd", "set", "export", "shopt", "umask", "ulimit",
    "unset", "alias", "local", "declare", "readonly",
))
_CMD_ALIASES = {"[": "test", "[[": "test", ".": "source"}


def bash_cmd_name(command: str) -> str:
    """Basename of the program a Bash call effectively runs. '' if undecidable.

    Always lowercase, always a basename, always <= CMD_NAME_MAX_LEN chars, and
    NEVER None — the parser must not write NULL into bash_commands.cmd_name.

    Handles, in this order:
      - comment-only and continuation lines      `# note\\nmv a b`      -> mv
      - leading env assignments                  `FOO="a b" mkdir x`   -> mkdir
      - subshells / braces / stray operators     `(cd x && ls)`        -> ls
      - absolute paths                           `/usr/bin/python3 x`  -> python3
      - wrappers                                 `sudo -n launchctl l` -> launchctl
      - `cd x && y` chains, pipes, `;` chains    `cd /x && npm test`   -> npm
      - heredocs                                 `python3 - <<'EOF'..` -> python3
      - `$VAR` as the program                                          -> '(var)'
    Builtins that ARE the action (`for`, `source`, `test`, `echo`) are kept as
    themselves — an honest label beats a fake one.
    """
    if not isinstance(command, str):
        return ""
    lines = []
    for line in command[:CMD_NAME_MAX_SCAN].split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):  # line continuation
            stripped = stripped[:-1].rstrip()
        if stripped:
            lines.append(stripped)
    rest = "\n".join(lines)

    for _ in range(CMD_NAME_MAX_HOPS):
        rest = rest.lstrip(_CMD_LEAD_CHARS)
        if not rest:
            return ""
        assign = _CMD_ASSIGN_RE.match(rest)
        if assign:
            rest = rest[assign.end():]
            continue
        token_match = _CMD_TOKEN_RE.match(rest)
        if not token_match:
            return ""
        token, rest = token_match.group(0), rest[token_match.end():]
        raw = token.strip("'\"`")
        # Alias on the raw token FIRST: PurePosixPath('.').name is '', so the
        # dot-source builtin would otherwise be lost.
        base = _CMD_ALIASES.get(raw, "")
        if not base:
            base = PurePosixPath(raw).name.lower()
            base = _CMD_ALIASES.get(base, base)
        if not base:
            return ""
        if base in _CMD_WRAPPERS:
            arg_flags = _CMD_WRAPPER_ARG_FLAGS.get(base, frozenset())
            rest = rest.lstrip()
            while True:
                head = rest.split(None, 1)
                if head and len(head[0]) > 1 and head[0].startswith("-"):
                    flag = head[0]
                    rest = rest[len(flag):].lstrip()
                    if flag in arg_flags:  # detached option argument
                        nxt = rest.split(None, 1)
                        if nxt:
                            rest = rest[len(nxt[0]):].lstrip()
                else:
                    break
            if not rest or rest[0] in ">&|;<":
                # Nothing left to wrap (`env | grep x`, `nohup caffeinate -t 60 &`):
                # the "wrapper" IS the command being run.
                return base[:CMD_NAME_MAX_LEN]
            continue
        if base in _CMD_PREAMBLE:
            sep = _CMD_SEP_RE.search(rest)
            if sep:
                rest = rest[sep.end():]
                continue
            return base  # `cd /x` on its own really is a cd
        if base.startswith("$"):
            return "(var)"
        return base[:CMD_NAME_MAX_LEN]
    return ""


# ===========================================================================
# MESSAGE EXTRACTION
# ===========================================================================

def extract_message_content(message: dict) -> Optional[str]:
    """Extract text content from a user or assistant message."""
    msg_type = message.get("type")

    if msg_type == "user":
        content = message.get("message", {}).get("content", "")
        if isinstance(content, str):
            return content
        elif isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        texts.append(item.get("text", ""))
                elif isinstance(item, str):
                    texts.append(item)
            return " ".join(texts) if texts else None

    elif msg_type == "assistant":
        content = message.get("message", {}).get("content", [])
        if isinstance(content, str):
            return content
        elif isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        texts.append(item.get("text", ""))
                    elif item.get("type") == "thinking":
                        texts.append(item.get("thinking", ""))
                elif isinstance(item, str):
                    texts.append(item)
            return " ".join(texts) if texts else None

    return None


def extract_tool_calls(message: dict) -> list:
    """Extract tool calls from an assistant message."""
    tool_calls = []
    if message.get("type") != "assistant":
        return tool_calls

    content = message.get("message", {}).get("content", [])
    if not isinstance(content, list):
        return tool_calls

    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_use":
            tool_calls.append({
                "name": item.get("name"),
                "input": item.get("input", {}),
                "id": item.get("id"),
            })
    return tool_calls


def extract_token_usage(message: dict) -> Optional[dict]:
    """Extract token usage from an assistant message."""
    if message.get("type") != "assistant":
        return None
    msg_data = message.get("message", {})
    usage = msg_data.get("usage", {})
    if not usage:
        return None
    return {
        "model": msg_data.get("model", ""),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
        "api_message_id": msg_data.get("id", "") or "",
    }


# ===========================================================================
# PROCESS FUNCTIONS (all take project_id, user_id, host_id)
# ===========================================================================

def process_token_usage(message: dict, conn: sqlite3.Connection, project_id: int, user_id: int, host_id: int):
    """Process token usage from a message and insert into database."""
    usage = extract_token_usage(message)
    if not usage:
        return

    timestamp = message.get("timestamp", "")
    session_id = message.get("sessionId", "")
    cwd = message.get("cwd", "")

    try:
        conn.execute(
            """INSERT OR IGNORE INTO token_usage
               (timestamp, session_id, project_id, user_id, host_id,
                model, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, cwd,
                api_message_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (timestamp, session_id, project_id, user_id, host_id,
             usage["model"], usage["input_tokens"], usage["output_tokens"],
             usage["cache_creation_tokens"], usage["cache_read_tokens"], cwd,
             usage.get("api_message_id", ""))
        )
    except sqlite3.Error as e:
        print(f"DB error (tokens): {e}")


def process_tool_call(tool_call: dict, message: dict, conn: sqlite3.Connection,
                      project_id: int, user_id: int, host_id: int):
    """Process a single tool call and insert into database."""
    tool_name = tool_call.get("name")
    tool_input = tool_call.get("input", {})
    # LAV-78: the tool_use id (toolu_*/call_*). Correlation key for the matching
    # tool_result — written into every row, but NEVER added to a NOT EXISTS guard:
    # the guards must keep their current keys or a re-parse duplicates rows.
    tool_call_id = tool_call.get("id") or ""

    timestamp = message.get("timestamp", "")
    session_id = message.get("sessionId", "")
    cwd = message.get("cwd", "")
    git_branch = message.get("gitBranch", "")

    if tool_name in FILE_OPERATION_TOOLS:
        file_path = tool_input.get("file_path", "")
        if file_path:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO file_operations
                       (timestamp, session_id, project_id, user_id, host_id, tool, file_path, cwd, git_branch,
                        tool_call_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (timestamp, session_id, project_id, user_id, host_id, tool_name, file_path, cwd, git_branch,
                     tool_call_id)
                )
            except sqlite3.Error as e:
                print(f"DB error (file_ops): {e}")

    elif tool_name == "Bash":
        command = tool_input.get("command", "")
        if not isinstance(command, str):
            command = ""  # never crash on a malformed payload
        # LAV-79: the is_file_related_bash() gate is GONE — bash_commands now
        # records EVERY shell call, not the 24.8% whose first word happens to be
        # in FILE_COMMANDS. The file_operations branch below is unaffected: it
        # has always had its own `bash_category and target_file` guard, and both
        # of those helpers only ever answer for commands that are a strict
        # SUBSET of FILE_COMMANDS, so the gate was pure redundancy there
        # (verified on 48,110 real non-file-related commands: 0 would produce a
        # file_operations row). `command.strip()` reproduces the old behaviour
        # for empty/whitespace-only commands, which the gate also dropped, and
        # keeps this in step with tool_outcomes.tool_row_matches.
        if command.strip():
            description = tool_input.get("description", "")
            target_file = extract_target_file(command)

            bash_category = get_bash_category(command)
            if bash_category and target_file:
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO file_operations
                           (timestamp, session_id, project_id, user_id, host_id, tool, file_path, cwd, git_branch,
                            tool_call_id)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (timestamp, session_id, project_id, user_id, host_id, bash_category, target_file, cwd, git_branch,
                         tool_call_id)
                    )
                except sqlite3.Error as e:
                    print(f"DB error (bash->file_ops): {e}")

            # bash_commands has no UNIQUE constraint; agent files are reprocessed on
            # every incremental run (LAV-65), so guard with NOT EXISTS (LAV-66).
            try:
                conn.execute(
                    """INSERT INTO bash_commands
                       (timestamp, session_id, project_id, user_id, host_id, command, description, target_file, cwd, git_branch,
                        tool_call_id, cmd_name)
                       SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                       WHERE NOT EXISTS (
                           SELECT 1 FROM bash_commands
                           WHERE timestamp = ? AND session_id = ? AND project_id = ? AND command = ?
                       )""",
                    (timestamp, session_id, project_id, user_id, host_id, command, description, target_file, cwd, git_branch,
                     tool_call_id, bash_cmd_name(command),
                     timestamp, session_id, project_id, command)
                )
            except sqlite3.Error as e:
                print(f"DB error (bash): {e}")

    elif tool_name in SEARCH_TOOLS:
        pattern = tool_input.get("pattern", "")
        if pattern:
            path = tool_input.get("path", "")
            output_mode = tool_input.get("output_mode", "")
            # No UNIQUE constraint — NOT EXISTS guard, see bash_commands (LAV-66).
            try:
                conn.execute(
                    """INSERT INTO search_operations
                       (timestamp, session_id, project_id, user_id, host_id, tool, pattern, path, output_mode, cwd,
                        tool_call_id)
                       SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                       WHERE NOT EXISTS (
                           SELECT 1 FROM search_operations
                           WHERE timestamp = ? AND session_id = ? AND project_id = ? AND tool = ? AND pattern = ?
                       )""",
                    (timestamp, session_id, project_id, user_id, host_id, tool_name, pattern, path, output_mode, cwd,
                     tool_call_id,
                     timestamp, session_id, project_id, tool_name, pattern)
                )
            except sqlite3.Error as e:
                print(f"DB error (search): {e}")

    elif tool_name == "Skill":
        skill_name = tool_input.get("skill", "")
        if skill_name:
            args = tool_input.get("args", "")
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO skill_invocations
                       (timestamp, session_id, project_id, user_id, host_id, skill_name, args, cwd, git_branch,
                        tool_call_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (timestamp, session_id, project_id, user_id, host_id, skill_name, args, cwd, git_branch,
                     tool_call_id)
                )
            except sqlite3.Error as e:
                print(f"DB error (skill): {e}")

    elif tool_name in tool_outcomes.SPAWN_TOOLS:
        # LAV-82: was `== "Task"` with an `if subagent_type:` guard. Claude Code
        # renamed the tool to `Agent` on 2026-03-01 and added `Workflow`, so the
        # table stopped filling; and the guard alone dropped 109 of 1.217 Agent
        # calls plus every Workflow call, because those carry no subagent_type.
        # The row is built by the shared helper — see tool_row_matches(), which
        # must reproduce the same key.
        row = tool_outcomes.subagent_row_from_tool_input(tool_name, tool_input)
        if row:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO subagent_invocations
                       (timestamp, session_id, project_id, user_id, host_id, subagent_type, description, prompt, model, run_in_background, cwd, git_branch,
                        tool_call_id, spawn_tool, workflow_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (timestamp, session_id, project_id, user_id, host_id,
                     row["subagent_type"], row["description"], row["prompt"],
                     row["model"], row["run_in_background"], cwd, git_branch,
                     tool_call_id, row["spawn_tool"], row["workflow_id"])
                )
            except sqlite3.Error as e:
                print(f"DB error (subagent): {e}")

    elif tool_name and tool_name.startswith("mcp__"):
        parts = tool_name.split("__")
        if len(parts) >= 3:
            server_name = parts[1]
            mcp_tool = "__".join(parts[2:])
        else:
            server_name = ""
            mcp_tool = tool_name
        # No UNIQUE constraint — NOT EXISTS guard, see bash_commands (LAV-66).
        try:
            conn.execute(
                """INSERT INTO mcp_tool_calls
                   (timestamp, session_id, project_id, user_id, host_id, tool_name, server_name, cwd, git_branch,
                    tool_call_id, kind)
                   SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                   WHERE NOT EXISTS (
                       SELECT 1 FROM mcp_tool_calls
                       WHERE timestamp = ? AND session_id = ? AND project_id = ? AND tool_name = ?
                   )""",
                # LAV-85: a claude_code row only reaches here via the `mcp__` prefix,
                # so it is MCP by construction — but go through tool_kind() anyway
                # rather than hardcoding the literal, so there is exactly one place
                # that decides what `kind` means.
                (timestamp, session_id, project_id, user_id, host_id, mcp_tool, server_name, cwd, git_branch,
                 tool_call_id, tool_outcomes.tool_kind(session_id, server_name, mcp_tool),
                 timestamp, session_id, project_id, mcp_tool)
            )
        except sqlite3.Error as e:
            print(f"DB error (mcp): {e}")


# LAV-78: ceiling on the per-FILE out-of-order tool_result buffer (see
# process_tool_results / flush_pending_tool_results). Measured on 40 real
# transcripts (3731 tool_results): at most 3 entries buffered per file, so this
# is a safety valve against a pathological file, never a normal working size.
MAX_PENDING_TOOL_RESULTS = 200

# LAV-79: running total of out-of-order tool_results DROPPED because the per-FILE
# buffer above was already full. Real-world peak is 9 entries, so this should stay
# at 0 forever — but a pathological file would otherwise degrade tool-outcome
# coverage invisibly. Module-level because `pending` is a plain dict shared by
# three call sites; callers diff dropped_tool_results() around their own run.
_dropped_tool_results = 0


def dropped_tool_results() -> int:
    """LAV-79: process-wide count of tool_results dropped by the pending-buffer cap."""
    return _dropped_tool_results


def process_tool_results(message: dict, conn: sqlite3.Connection, project_id: int,
                         seen_tool_use_ids: Optional[set] = None,
                         pending: Optional[dict] = None) -> int:
    """LAV-78: stamp each tool_result block onto the tool row that produced it.

    Claude Code and Cowork report the outcome of a tool call in the NEXT record: a
    "user" message whose content carries one `tool_result` block per call, keyed by
    `tool_use_id` — the same id process_tool_call() stored in tool_call_id. This
    walks those blocks and hands each one to tool_outcomes.apply_tool_outcome(),
    which sweeps ALL SIX tool tables (a file-related Bash lives in two of them).

    result_ts is the RECORD-level timestamp of this message; duration_ms is derived
    from it in SQL against the tool row's own timestamp, so it stays correct when
    the tool_use was parsed in run N and its result arrives in run N+1.

    OUT-OF-ORDER RESULTS (LAV-78 follow-up): real transcripts sometimes store the
    user record carrying a tool_result BEFORE the assistant record carrying its
    tool_use (reproduced on 951c2618-…: line 52 holds the result of
    toolu_016uNjdYKkJyngtM6nE3GuMi, line 53 its tool_use). Applied inline the
    UPDATE matches nothing and the outcome is lost forever — file order is the
    same on every run, so even --full does not heal it. When the caller passes the
    per-file `pending` dict, a miss is parked there and replayed by
    flush_pending_tool_results() once the whole file has been read.

    `seen_tool_use_ids` holds the tool_use ids already processed for THIS file. A
    miss on an id in that set is NOT out-of-order: the call was seen and simply
    wrote no row (non-file-related Bash, tool that maps to no table — ~53% of all
    tool_results). Those are dropped immediately, which is what keeps the buffer
    at a handful of entries instead of half the transcript.

    Rows never reached here keep is_error NULL — "no tool_result ever seen", NOT
    "assumed success". Returns the number of rows updated.
    """
    global _dropped_tool_results
    if message.get("type") != "user":
        return 0
    session_id = message.get("sessionId", "")
    if not session_id:
        return 0

    msg_data = message.get("message", {})
    if not isinstance(msg_data, dict):
        return 0
    result_ts = message.get("timestamp", "")

    updated = 0
    for block in tool_outcomes.iter_content_blocks(msg_data.get("content", "")):
        if block.get("type") != "tool_result":
            continue
        tool_call_id = block.get("tool_use_id") or ""
        if not tool_call_id:
            continue
        outcome = tool_outcomes.outcome_from_tool_result(block)
        # LAV-82: a Workflow launch is a SUCCESS, so outcome_from_tool_result()
        # returns early and never looks at the body — the wf_ id has to be read
        # in a separate pass. It is '' for every other tool_result.
        workflow_id = tool_outcomes.workflow_id_from_tool_result(block)
        try:
            rows = tool_outcomes.apply_tool_outcome(
                conn, session_id, project_id, tool_call_id, outcome, result_ts
            )
            if workflow_id:
                rows += tool_outcomes.apply_workflow_id(
                    conn, session_id, project_id, tool_call_id, workflow_id
                )
        except sqlite3.Error as e:
            print(f"DB error (tool_result): {e}")
            continue
        updated += rows
        if rows or pending is None:
            continue
        if seen_tool_use_ids is not None and tool_call_id in seen_tool_use_ids:
            continue  # call already processed, it just wrote no row — drop it
        if len(pending) < MAX_PENDING_TOOL_RESULTS:
            pending[(session_id, project_id, tool_call_id)] = (outcome, result_ts, workflow_id)
        else:
            _dropped_tool_results += 1  # LAV-79: surfaced in the per-project stats line
    return updated


def flush_pending_tool_results(conn: sqlite3.Connection, pending: dict) -> int:
    """LAV-78: replay the tool_results that matched no row when first seen.

    Called once per FILE, after its record loop: every tool_use the file contains
    has been inserted by then, so a result that arrived before its call now finds
    its row. The dict is emptied afterwards — the buffer never spans two files.

    A replay that still matches nothing is LEGITIMATE (the call wrote no row at
    all, or its tool_use lives in another file) and is dropped SILENTLY: logging
    it would make the parser spam on every single run. Returns rows updated.
    """
    if not pending:
        return 0
    updated = 0
    for (session_id, project_id, tool_call_id), entry in pending.items():
        outcome, result_ts, workflow_id = entry
        try:
            updated += tool_outcomes.apply_tool_outcome(
                conn, session_id, project_id, tool_call_id, outcome, result_ts
            )
            if workflow_id:
                updated += tool_outcomes.apply_workflow_id(
                    conn, session_id, project_id, tool_call_id, workflow_id
                )
        except sqlite3.Error as e:
            print(f"DB error (tool_result replay): {e}")
    pending.clear()
    return updated


def process_message_content(message: dict, conn: sqlite3.Connection, project_id: int, user_id: int, host_id: int):
    """Save message content to messages table with full structure.

    LAV-39: Claude Code writes one JSONL record per content block (thinking, tool_use, text) but
    all blocks of the same API turn share the same `message.id` and carry the identical `usage`.
    We credit tokens only to the first block seen per api_message_id; later blocks store
    tokens_in/tokens_out = 0 so downstream SUM(tokens_in+tokens_out) in update_interaction
    doesn't double-count. Works across incremental runs: the check queries the DB for any
    existing row of this api_message_id that already has tokens.
    """
    msg_type = message.get("type")
    if msg_type not in ("user", "assistant"):
        return

    raw_content = message.get("message", {}).get("content", "")
    if isinstance(raw_content, (list, dict)):
        content_json = json.dumps(raw_content)
    elif isinstance(raw_content, str):
        content_json = raw_content
    else:
        return

    session_id = message.get("sessionId", "")
    timestamp = message.get("timestamp", "")
    uuid = message.get("uuid", "")
    agent_id = message.get("agentId") or None

    tokens_in = 0
    tokens_out = 0
    model = ""
    api_message_id = ""
    if msg_type == "assistant":
        msg_data = message.get("message", {})
        usage = msg_data.get("usage", {})
        tokens_in = usage.get("input_tokens", 0)
        tokens_out = usage.get("output_tokens", 0)
        model = msg_data.get("model", "")
        api_message_id = msg_data.get("id", "") or ""
        if api_message_id and (tokens_in or tokens_out):
            existing = conn.execute(
                "SELECT 1 FROM messages WHERE session_id=? AND project_id=? AND api_message_id=? "
                "AND (tokens_in>0 OR tokens_out>0) LIMIT 1",
                (session_id, project_id, api_message_id)
            ).fetchone()
            if existing:
                tokens_in = 0
                tokens_out = 0

    try:
        conn.execute(
            """INSERT OR IGNORE INTO messages
               (session_id, project_id, user_id, host_id, uuid, type, content, timestamp, tokens_in, tokens_out, model, api_message_id, agent_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, project_id, user_id, host_id, uuid, msg_type, content_json, timestamp, tokens_in, tokens_out, model, api_message_id, agent_id)
        )
    except sqlite3.Error as e:
        print(f"DB error (messages): {e}")


# ===========================================================================
# CODEX HELPERS
# ===========================================================================

def parse_codex_arguments(raw_args):
    """Parse Codex function_call arguments."""
    if isinstance(raw_args, dict):
        return raw_args
    if not raw_args:
        return {}
    if isinstance(raw_args, str):
        try:
            return json.loads(raw_args)
        except json.JSONDecodeError:
            return {"_raw": raw_args}
    return {}


def extract_codex_patch_text(raw_args, parsed_args: dict) -> str:
    """Extract patch text from Codex apply_patch arguments."""
    if isinstance(parsed_args, dict):
        if "patch" in parsed_args and isinstance(parsed_args.get("patch"), str):
            return parsed_args.get("patch", "")
        if "_raw" in parsed_args and isinstance(parsed_args.get("_raw"), str):
            return parsed_args.get("_raw", "")
    if isinstance(raw_args, str):
        return raw_args
    return ""


def parse_codex_patch_entries(patch_text: str) -> list:
    """Extract file operations from an apply_patch payload."""
    if not patch_text:
        return []
    entries = []
    for line in patch_text.splitlines():
        line = line.strip()
        if line.startswith("*** Add File: "):
            entries.append(("Write", line[len("*** Add File: "):].strip()))
        elif line.startswith("*** Update File: "):
            entries.append(("Edit", line[len("*** Update File: "):].strip()))
        elif line.startswith("*** Delete File: "):
            entries.append(("Edit", line[len("*** Delete File: "):].strip()))
        elif line.startswith("*** Move to: "):
            entries.append(("Edit", line[len("*** Move to: "):].strip()))
    return entries


def process_codex_shell_command(command: str, workdir: str, timestamp: str, session_ctx: dict,
                                conn: sqlite3.Connection, project_id: int, user_id: int, host_id: int,
                                call_id: str = ""):
    """Process a Codex shell_command as a Bash tool call.

    LAV-78: `call_id` is the Codex function_call id, stored as tool_call_id for
    correlation. The outcome itself stays NULL for Codex — function_call_output
    is not parsed at all, so no tool_result is ever seen (and NULL means exactly
    that, never "assumed success").
    """
    if not command:
        return
    cwd = workdir or session_ctx.get("cwd", "")
    tool_call = {
        "name": "Bash",
        "input": {"command": command, "description": ""},
        "id": call_id or "",
    }
    message = {
        "timestamp": timestamp,
        "sessionId": session_ctx.get("session_id", ""),
        "cwd": cwd,
        "gitBranch": session_ctx.get("git_branch", "")
    }
    process_tool_call(tool_call, message, conn, project_id, user_id, host_id)


def process_codex_patch(patch_text: str, timestamp: str, session_ctx: dict,
                        conn: sqlite3.Connection, project_id: int, user_id: int, host_id: int):
    """Process a Codex apply_patch payload into file operations."""
    if not patch_text:
        return
    session_id = session_ctx.get("session_id", "")
    cwd = session_ctx.get("cwd", "")
    git_branch = session_ctx.get("git_branch", "")

    for tool, file_path in parse_codex_patch_entries(patch_text):
        if not file_path:
            continue
        try:
            conn.execute(
                """INSERT OR IGNORE INTO file_operations
                   (timestamp, session_id, project_id, user_id, host_id, tool, file_path, cwd, git_branch)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (timestamp, session_id, project_id, user_id, host_id, tool, file_path, cwd, git_branch)
            )
        except sqlite3.Error as e:
            print(f"DB error (codex patch): {e}")


def process_codex_token_count(event: dict, session_ctx: dict, conn: sqlite3.Connection,
                              project_id: int, user_id: int, host_id: int):
    """Process Codex token_count events into token_usage."""
    payload = event.get("payload", {})
    info = payload.get("info") or {}
    total = info.get("total_token_usage") or {}
    last = info.get("last_token_usage") or {}

    usage = {}
    if total:
        prev_total = session_ctx.get("last_total")
        if prev_total:
            delta = {k: total.get(k, 0) - prev_total.get(k, 0) for k in total.keys()}
            if any(v < 0 for v in delta.values()):
                usage = total
            else:
                usage = delta
        else:
            usage = total
        session_ctx["last_total"] = total
        if not any(v > 0 for v in usage.values()):
            return
    elif last:
        prev_last = session_ctx.get("last_usage")
        if prev_last == last:
            return
        usage = last
        session_ctx["last_usage"] = last
    else:
        return

    timestamp = event.get("timestamp", "")
    session_id = session_ctx.get("session_id", "")
    cwd = session_ctx.get("cwd", "")
    if not timestamp or not session_id:
        return

    model = session_ctx.get("model", "")
    input_tokens = usage.get("input_tokens", 0)
    cache_read_tokens = usage.get("cached_input_tokens", 0)
    net_input_tokens = max(0, input_tokens - cache_read_tokens)

    try:
        conn.execute(
            """INSERT OR IGNORE INTO token_usage
               (timestamp, session_id, project_id, user_id, host_id,
                model, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, cwd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (timestamp, session_id, project_id, user_id, host_id,
             model, net_input_tokens, usage.get("output_tokens", 0), 0, cache_read_tokens, cwd)
        )
        if model:
            conn.execute(
                """UPDATE token_usage
                   SET model = ?
                   WHERE timestamp = ? AND session_id = ? AND project_id = ?
                   AND (model IS NULL OR model = '')""",
                (model, timestamp, session_id, project_id)
            )
    except sqlite3.Error as e:
        print(f"DB error (codex tokens): {e}")


# ===========================================================================
# INTERACTION UPDATE
# ===========================================================================

def update_interaction(session_id: str, project_name: str, conn: sqlite3.Connection,
                       project_id: int, user_id: int, host_id: int,
                       summary: str = None, parent_session_id: str = None, agent_id: str = None,
                       workflow_id: str = None):
    """Aggregate and update interaction metadata from messages.

    NOTE (LAV-82): the write below is INSERT OR REPLACE over the WHOLE row, so
    every column of `interactions` must appear in its column list. A column added
    to the table but omitted here is silently reset to its default on every
    parse — the symptom (correct right after a targeted reparse, empty an hour
    later) reads like a sync bug and costs a day to find.

    Hence `workflow_id=None` means "caller does not know — keep what is stored",
    while `""` means "caller knows there is none — clear it". The codex and cowork
    call sites pass neither and can never be workflow children, so today the
    distinction changes nothing; it exists so that the NEXT caller added to this
    function cannot silently wipe the column.
    """
    try:
        cursor = conn.execute("""
            SELECT
                MIN(timestamp) as first_ts,
                COUNT(*) as msg_count,
                SUM(tokens_in + tokens_out) as msg_tokens,
                MAX(model) as model
            FROM messages
            WHERE session_id = ? AND project_id = ?
        """, (session_id, project_id))
        row = cursor.fetchone()
        if not row or not row[0]:
            return

        first_ts, msg_count, msg_tokens, model = row

        # total_tokens is cache-inclusive: the full token_usage breakdown
        # (input + output + cache write + cache read), deduplicated by
        # api_message_id. Since LAV-66 subagents live under their own synthetic
        # session_id, so this is the session's OWN total; the UI rolls up
        # descendants via parent_session_id. Fall back to the messages'
        # tokens_in+tokens_out only for sources without token_usage
        # (e.g. ChatGPT / claude.ai exports, which have no cache tokens).
        cursor = conn.execute("""
            SELECT
                SUM(input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens) as total_tokens,
                MAX(model) as model
            FROM token_usage
            WHERE session_id = ? AND project_id = ?
        """, (session_id, project_id))
        trow = cursor.fetchone()
        total_tokens = trow[0] if (trow and trow[0] is not None) else (msg_tokens or 0)
        if not model and trow and trow[1]:
            model = trow[1]

        # LAV-66: pick the first user text that is NOT purely a system wrapper
        # (<local-command-caveat>, <ide_opened_file>, ...). Keep the first raw
        # text as fallback so sessions made only of wrappers still get a display.
        cursor = conn.execute("""
            SELECT content FROM messages
            WHERE session_id = ? AND project_id = ? AND type = 'user'
            ORDER BY timestamp ASC LIMIT 10
        """, (session_id, project_id))
        raw_display = ""
        fallback_display = ""
        for (raw_content,) in cursor.fetchall():
            if not raw_content:
                continue
            texts = []
            try:
                content_data = json.loads(raw_content)
                if isinstance(content_data, list):
                    texts = [item.get('text', '') for item in content_data
                             if isinstance(item, dict) and item.get('type') == 'text']
                elif isinstance(content_data, str):
                    texts = [content_data]
            except (json.JSONDecodeError, TypeError):
                texts = [raw_content]
            for text in texts:
                if not text:
                    continue
                if not fallback_display:
                    fallback_display = text
                cleaned = strip_system_tags(text)
                if cleaned:
                    raw_display = cleaned
                    break
            if raw_display:
                break
        if not raw_display:
            raw_display = fallback_display

        if not summary and raw_display:
            summary = smart_title(raw_display)

        display = raw_display[:200] if raw_display else ""

        cursor = conn.execute("""
            SELECT DISTINCT tool FROM file_operations WHERE session_id = ? AND project_id = ?
            UNION
            SELECT DISTINCT skill_name FROM skill_invocations WHERE session_id = ? AND project_id = ?
            UNION
            SELECT DISTINCT subagent_type FROM subagent_invocations WHERE session_id = ? AND project_id = ?
        """, (session_id, project_id, session_id, project_id, session_id, project_id))
        tools = [r[0] for r in cursor.fetchall() if r[0]]
        tools_json = json.dumps(tools) if tools else "[]"

        cursor = conn.execute("""
            SELECT cwd, git_branch FROM file_operations WHERE session_id = ? AND project_id = ? LIMIT 1
        """, (session_id, project_id))
        ctx_row = cursor.fetchone()
        cwd = ctx_row[0] if ctx_row else ""
        git_branch = ctx_row[1] if ctx_row else ""

        # LAV-82: None = "caller does not know" -> carry the stored value across
        # the INSERT OR REPLACE instead of resetting it to the column default.
        if workflow_id is None:
            wf_row = conn.execute(
                "SELECT workflow_id FROM interactions WHERE session_id = ? AND project_id = ?",
                (session_id, project_id),
            ).fetchone()
            workflow_id = (wf_row[0] if wf_row else "") or ""

        conn.execute("""
            INSERT OR REPLACE INTO interactions
            (session_id, project_id, user_id, host_id, timestamp, display, summary, project, model,
             total_tokens, message_count, tools_used, cwd, git_branch, parent_session_id, agent_id,
             workflow_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (session_id, project_id, user_id, host_id, first_ts, display, summary, project_name, model,
              total_tokens or 0, msg_count, tools_json, cwd, git_branch, parent_session_id, agent_id,
              workflow_id))

    except sqlite3.Error as e:
        print(f"DB error (interaction): {e}")


def is_real_agent_id(agent_id: Optional[str]) -> bool:
    """True only for genuine Task/workflow subagents.

    LAV-66: Claude Code writes several kinds of `agent-*.jsonl` files that all
    reuse the parent's sessionId. Real subagents carry a single hexadecimal id
    (e.g. 'a1e94cf', 'abb5998eb0fb92919'). Meta artifacts carry a non-hex first
    segment — 'acompact-…' (auto-compaction checkpoints), 'aprompt_suggestion-…'
    (prompt suggestions), 'aside_question-…' (side questions). Meta files are NOT
    navigable subagents: some merely duplicate the parent's own messages (double
    count), the rest are internal noise. Only hex ids are real subagents.
    """
    if not agent_id:
        return False
    first = agent_id.split('-', 1)[0]
    return bool(first) and all(c in '0123456789abcdef' for c in first)


def purge_meta_children(conn: sqlite3.Connection, project_id: int) -> int:
    """LAV-66: remove phantom meta '::agent-<non-hex>-…' child sessions.

    Compaction/suggestion/side-question files were mis-ingested as subagent
    conversations by the pre-fix parser (duplicating parent messages/tokens or
    adding noise). The parser now skips those files; rows created before the fix
    are swept here. Scoped by project (synthetic ids are globally unique, so this
    is safe regardless of host). Returns rows removed from `messages`.
    """
    bad = []
    for (sid,) in conn.execute(
        "SELECT DISTINCT session_id FROM messages WHERE project_id = ? AND session_id LIKE '%::agent-%'",
        (project_id,)
    ):
        agent_id = sid.split('::agent-', 1)[1] if '::agent-' in sid else ''
        if not is_real_agent_id(agent_id):
            bad.append(sid)
    if not bad:
        return 0
    tables = ("messages", "token_usage", "file_operations", "bash_commands",
              "search_operations", "skill_invocations", "subagent_invocations",
              "mcp_tool_calls", "interaction_metadata", "interactions")
    removed = 0
    for table in tables:
        for i in range(0, len(bad), 500):
            chunk = bad[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            try:
                n = conn.execute(
                    f"DELETE FROM {table} WHERE project_id = ? AND session_id IN ({placeholders})",
                    (project_id, *chunk)
                ).rowcount
                if table == "messages":
                    removed += n
            except sqlite3.Error:
                pass
    return removed


def resolve_agent_parents(conn: sqlite3.Connection, project_id: int):
    """Post-process to find real parent_session_id for agent interactions."""
    parent_mapping = {}

    cursor = conn.execute("""
        SELECT session_id, content FROM messages
        WHERE project_id = ? AND content LIKE '%agent-%.jsonl%'
    """, (project_id,))

    for row in cursor.fetchall():
        parent_session_id, content = row
        matches = re.findall(r'agent-([a-f0-9]+)\.jsonl', content)
        for agent_id in matches:
            if agent_id not in parent_mapping:
                parent_mapping[agent_id] = parent_session_id

    cursor = conn.execute("""
        SELECT m.session_id, m.timestamp, m.content
        FROM messages m
        JOIN interactions c ON m.session_id = c.session_id AND m.project_id = c.project_id
        WHERE m.project_id = ?
          AND m.content LIKE '%"name": "Task"%'
          AND m.type = 'assistant'
          AND c.agent_id IS NULL
    """, (project_id,))

    task_calls = []
    for row in cursor.fetchall():
        parent_sid, timestamp, content = row
        try:
            from datetime import timedelta
            ts = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            task_calls.append((parent_sid, ts))
        except:
            pass

    cursor = conn.execute("""
        SELECT session_id, agent_id, timestamp
        FROM interactions
        WHERE project_id = ?
          AND agent_id IS NOT NULL
          AND (parent_session_id IS NULL OR parent_session_id = session_id)
    """, (project_id,))

    unlinked_agents = []
    for row in cursor.fetchall():
        agent_sid, agent_id, timestamp = row
        if agent_id in parent_mapping:
            continue
        try:
            from datetime import timedelta
            ts = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            unlinked_agents.append((agent_sid, agent_id, ts))
        except:
            pass

    for agent_sid, agent_id, agent_ts in unlinked_agents:
        best_parent = None
        from datetime import timedelta
        best_delta = timedelta(seconds=120)

        for parent_sid, parent_ts in task_calls:
            if parent_sid == agent_sid:
                continue
            delta = agent_ts - parent_ts
            if timedelta(0) <= delta < best_delta:
                best_parent = parent_sid
                best_delta = delta

        if best_parent:
            parent_mapping[agent_id] = best_parent

    updated = 0
    for agent_id, parent_session_id in parent_mapping.items():
        cursor = conn.execute("""
            UPDATE interactions
            SET parent_session_id = ?
            WHERE project_id = ? AND agent_id = ? AND (parent_session_id IS NULL OR parent_session_id = session_id)
        """, (parent_session_id, project_id, agent_id))
        updated += cursor.rowcount

    if updated > 0:
        print(f"  Resolved {updated} agent parent relationships")
    return updated


# ===========================================================================
# MAIN PARSE FUNCTIONS
# ===========================================================================

def parse_project(project_dir: Path, conn: sqlite3.Connection, full_reparse: bool = False,
                  since: Optional[tuple] = None) -> dict:
    """Parse all interactions for a single Claude Code project into the unified DB.

    LAV-79 `since`: the ``(str_z, dt_utc, epoch)`` tuple from _normalize_since(),
    or None. It lowers the watermark that GATES this pass (file-mtime skip and
    per-message skip) and NOTHING else — see stored_ts vs last_ts below.
    """
    project_name = extract_project_name(project_dir)
    since_z = since[0] if since else None

    # Detect user + host
    username = detect_user_from_path(project_dir)
    hostname, os_type, home_dir = detect_host_from_path(project_dir)

    # Resolve IDs
    project_id = get_or_create_project(conn, project_name, str(project_dir))
    user_id = get_or_create_user(conn, username)
    host_id = get_or_create_host(conn, hostname, os_type, home_dir)

    # Scope parse_state by directory so two dirs with same project name
    # don't share incremental cursors (e.g. codex worktree + main dir).
    source_key = f"{SOURCE_CLAUDE_CODE}:{project_dir}"

    print(f"\nProcessing project: {project_name}")
    print(f"  Source: {project_dir}")
    print(f"  User: {username} (id={user_id}), Host: {hostname} (id={host_id})")

    # LAV-79: two distinct variables that must never merge.
    #   stored_ts  = what parse_state holds. Read once, NEVER mutated. It seeds the
    #                running max below and is the floor the write-back must beat.
    #   last_ts    = the EFFECTIVE gate for this pass, possibly lowered by --since.
    # min(), never assignment: a --since NEWER than the stored watermark must not
    # narrow the window. --since may only ever widen.
    stored_ts = None if full_reparse else get_parse_state(conn, "last_parsed", project_id, source_key, host_id)
    last_ts = stored_ts
    if since_z is not None and not full_reparse:
        last_ts = since_z if not stored_ts else min(stored_ts, since_z)
    if last_ts:
        if last_ts != stored_ts:
            print(f"  Incremental from: {last_ts}  [--since; stored watermark {stored_ts} untouched]")
        else:
            print(f"  Incremental from: {last_ts}")
    else:
        print("  Full parse")

    dropped_before = dropped_tool_results()

    jsonl_files = sorted(project_dir.rglob("*.jsonl"))

    if full_reparse:
        # LAV-39: wipe prior claude_code rows for this project/host so re-parse rebuilds clean
        # (without this, old duplicated token_usage rows with empty api_message_id would remain).
        # LAV-66: extended to all child tables — subagent rows previously ingested under
        # the parent's session_id would otherwise stay attributed to the parent (and
        # tables without a UNIQUE constraint would accumulate duplicates on each --full).
        # LAV-66: the wipe is restricted to sessions whose source files still exist on
        # disk. Claude Code prunes files after cleanupPeriodDays; sessions whose files
        # are gone can never be rebuilt, so wiping them would silently lose their data.
        # Session ids are derived from filenames: <sid>.jsonl for top-level sessions,
        # <parent>::agent-<agentId> (the LAV-66 synthetic id) for subagents/**/agent-*.
        present_sids = set()
        for f in jsonl_files:
            if f.name.startswith("agent-"):
                f_agent_id = f.stem.replace("agent-", "")
                if not is_real_agent_id(f_agent_id):
                    continue  # LAV-66: meta artifact, not a subagent session
                # Agent files can sit at the project root (older layout) or under
                # subagents/**; their parent sessionId is only in the records, so
                # read the first line (fall back to the subagents/ folder name).
                f_parent = None
                try:
                    with open(f, "r", encoding="utf-8") as fh:
                        first = fh.readline().strip()
                    if first:
                        rec = json.loads(first)
                        f_parent = rec.get("sessionId") or None
                        f_agent_id = rec.get("agentId") or f_agent_id
                except (OSError, json.JSONDecodeError):
                    pass
                if not f_parent and "subagents" in f.parts:
                    sub_idx = f.parts.index("subagents")
                    if sub_idx > 0:
                        cand = f.parts[sub_idx - 1]
                        if len(cand) == 36 and cand.count("-") == 4:
                            f_parent = cand
                if f_parent and f_agent_id and "::agent-" not in f_parent:
                    present_sids.add(f"{f_parent}::agent-{f_agent_id}")
            else:
                present_sids.add(f.stem)
        wipe_tables = ("messages", "token_usage", "file_operations", "bash_commands",
                       "search_operations", "skill_invocations", "subagent_invocations",
                       "mcp_tool_calls")
        sids = sorted(present_sids)
        wiped = []
        for table in wipe_tables:
            count = 0
            for i in range(0, len(sids), 500):
                chunk = sids[i:i + 500]
                placeholders = ",".join("?" * len(chunk))
                count += conn.execute(
                    f"DELETE FROM {table} WHERE project_id = ? AND host_id = ? "
                    f"AND session_id IN ({placeholders})",
                    (project_id, host_id, *chunk)
                ).rowcount
            if count:
                wiped.append(f"{count} {table}")
        conn.commit()
        if wiped:
            print(f"  Wiped {', '.join(wiped)} rows (full reparse)")

    # LAV-79: seed from the STORED watermark, not the lowered gate. With the naive
    # `last_ts or ""` a --since pass over a project with no message in the window
    # left max_timestamp sitting at the --since instant and then WROTE it —
    # regressing the watermark (reproduced: stored 2030-01-01 -> 2026-05-14).
    max_timestamp = stored_ts or ""
    files_processed = 0
    messages_processed = 0
    tool_calls_processed = 0
    sessions_updated = set()
    session_summaries = {}
    session_agent_info = {}

    # mtime optimization: skip files not modified since last parse
    files_skipped_mtime = 0
    last_ts_epoch = None
    if last_ts and not full_reparse:
        try:
            ts = last_ts.replace("Z", "+00:00")
            last_ts_epoch = datetime.fromisoformat(ts).timestamp()
        except (ValueError, OSError):
            last_ts_epoch = None

    print(f"  Found {len(jsonl_files)} interaction files")

    for jsonl_file in jsonl_files:
        is_agent_file = jsonl_file.name.startswith("agent-")
        agent_id_from_filename = jsonl_file.stem.replace("agent-", "") if is_agent_file else None

        # LAV-66: only hex-id agent files are real Task/workflow subagents. Meta
        # artifacts (agent-acompact-*, agent-aprompt_suggestion-*, agent-aside_question-*)
        # reuse the parent's sessionId but are NOT navigable subagents — some duplicate
        # the parent's messages (double count), all are noise as child conversations.
        if is_agent_file and not is_real_agent_id(agent_id_from_filename):
            continue

        # Subagent/workflow files (agent-*.jsonl) inherit the PARENT session's
        # sessionId but carry the timestamps of when the subagent ran — which is
        # typically EARLIER than the parent session's latest message. The parent
        # session's watermark (last_parsed) therefore sits AFTER these timestamps,
        # so both the file-mtime skip and the per-message timestamp skip below
        # would drop every subagent message on incremental runs (only --full
        # recovered them). Agent files are small and write-once, and all inserts
        # are INSERT OR IGNORE (idempotent), so we always process them fully and
        # let dedup handle re-runs. See UNIQUE(session_id, project_id, uuid).
        if not is_agent_file and last_ts_epoch is not None:
            try:
                if jsonl_file.stat().st_mtime <= last_ts_epoch:
                    files_skipped_mtime += 1
                    continue
            except OSError:
                pass  # file disappeared, let downstream handle it

        files_processed += 1

        # LAV-78: per-FILE state for tool_result correlation. `seen_tool_use_ids`
        # is the set of tool_use ids already processed for this file (ids only,
        # dropped with the file); `pending_tool_results` parks the results whose
        # tool_use has not been read yet — the transcript sometimes stores them
        # in that order — and is replayed after the record loop below.
        seen_tool_use_ids = set()
        pending_tool_results = {}

        parent_from_path = None
        workflow_from_path = ""
        if is_agent_file and "subagents" in jsonl_file.parts:
            subagents_idx = jsonl_file.parts.index("subagents")
            if subagents_idx > 0:
                parent_folder = jsonl_file.parts[subagents_idx - 1]
                if len(parent_folder) == 36 and parent_folder.count("-") == 4:
                    parent_from_path = parent_folder
            # LAV-82: <parent>/subagents/workflows/wf_<id>/agent-*.jsonl. The
            # cohort id was already sitting in the path and was being thrown
            # away, which is why a 12-agent workflow was indistinguishable from
            # 12 unrelated spawns. Plain Task/Agent subagents live one level up
            # (<parent>/subagents/agent-*.jsonl) and correctly get ''.
            parts = jsonl_file.parts
            if (subagents_idx + 2 < len(parts)
                    and parts[subagents_idx + 1] == "workflows"
                    and parts[subagents_idx + 2].startswith("wf_")):
                workflow_from_path = parts[subagents_idx + 2]

        file_session_id = jsonl_file.stem if not is_agent_file else None

        for message in parse_jsonl_file(jsonl_file):
            msg_type = message.get("type", "")
            msg_timestamp = message.get("timestamp", "")

            if is_agent_file:
                # LAV-66: agent files (agent-*.jsonl — root-level, subagents/ and
                # subagents/workflows/wf_*/ alike) reuse the PARENT conversation's
                # sessionId in every record; the agentId is the only distinguishing
                # identity (verified across 7k+ files: none carries its own id).
                # Left as-is they collapse into the parent interaction (inflated
                # counts, corrupted parent agent_id). Rewrite the message identity
                # to a synthetic per-agent session id so the whole pipeline
                # (messages, token_usage, tool tables, session_sources,
                # update_interaction) keys them as a separate child conversation,
                # with the original sessionId as parent_session_id.
                agent_id = message.get("agentId") or agent_id_from_filename
                raw_sid = message.get("sessionId", "")
                if agent_id and raw_sid and "::agent-" not in raw_sid:
                    message["sessionId"] = f"{raw_sid}::agent-{agent_id}"
                sid_for_agent = message.get("sessionId", "")
                if agent_id and sid_for_agent:
                    session_agent_info[sid_for_agent] = (raw_sid or parent_from_path,
                                                         agent_id, workflow_from_path)

            if msg_type in ("summary", "ai-title", "custom-title"):
                if msg_type == "custom-title":
                    title_text = message.get("customTitle", "")
                    priority = 3
                elif msg_type == "ai-title":
                    title_text = message.get("aiTitle", "")
                    priority = 2
                else:
                    title_text = message.get("summary", "")
                    priority = 1
                session_id = message.get("sessionId", file_session_id)
                if title_text and session_id:
                    existing = session_summaries.get(session_id)
                    if existing is None or existing[1] < priority:
                        session_summaries[session_id] = (title_text, priority)
                continue

            if msg_type not in ("user", "assistant"):
                continue

            # Agent files bypass the incremental timestamp filter (see the
            # is_agent_file note above the file-mtime skip): their messages
            # predate the parent session watermark and would otherwise be lost.
            if last_ts and msg_timestamp <= last_ts and not is_agent_file:
                continue

            messages_processed += 1

            if msg_timestamp > max_timestamp:
                max_timestamp = msg_timestamp

            session_id = message.get("sessionId", "")
            if session_id:
                sessions_updated.add(session_id)
                upsert_session_source(
                    conn, session_id, project_id, SOURCE_CLAUDE_CODE,
                    client_version=str(message.get("version", "") or ""),
                )

            process_message_content(message, conn, project_id, user_id, host_id)

            # LAV-78: a "user" record carries the tool_result blocks for the calls
            # made in the previous assistant record — stamp their outcome. Results
            # that arrive before their tool_use go to pending_tool_results.
            process_tool_results(message, conn, project_id,
                                 seen_tool_use_ids, pending_tool_results)

            tool_calls = extract_tool_calls(message)
            for tool_call in tool_calls:
                process_tool_call(tool_call, message, conn, project_id, user_id, host_id)
                tool_call_id = tool_call.get("id")
                if tool_call_id:
                    seen_tool_use_ids.add(tool_call_id)
                tool_calls_processed += 1

            process_token_usage(message, conn, project_id, user_id, host_id)

        # LAV-78: the file is fully read — every tool_use it carries now has its
        # row, so the results parked above can be applied. Still-unmatched ones
        # are dropped silently (their call wrote no row at all).
        flush_pending_tool_results(conn, pending_tool_results)

    for session_id in sessions_updated:
        summary_entry = session_summaries.get(session_id)
        summary = summary_entry[0] if summary_entry else None
        agent_info = session_agent_info.get(session_id)
        parent_sid, agent_id, workflow_id = agent_info if agent_info else (None, None, "")
        update_interaction(session_id, project_name, conn, project_id, user_id, host_id,
                            summary=summary, parent_session_id=parent_sid, agent_id=agent_id,
                            workflow_id=workflow_id)

    # LAV-66: sweep phantom meta children (compaction/suggestion/side-question)
    # left by the pre-fix parser. Duplicated messages already live under the
    # parent's own <sid>.jsonl, so removing the children fixes the double count.
    purged = purge_meta_children(conn, project_id)
    if purged:
        print(f"  Purged {purged} phantom meta-child messages")

    resolve_agent_parents(conn, project_id)

    # LAV-66: heal parent rows corrupted by the pre-fix parser (bug #4). The
    # collapse signature: a non-agent session whose agent_id belongs to one of
    # its OWN children. Normally update_interaction rewrites the parent clean,
    # but a "husk" parent (its own .jsonl is empty/pruned — the row existed
    # only because subagent messages were collapsed into it) has no messages,
    # so update_interaction early-returns and the stale corruption survives.
    # Clear it and re-derive the counts from what actually remains.
    conn.execute("""
        UPDATE interactions SET
            agent_id = NULL,
            parent_session_id = NULL,
            message_count = (SELECT COUNT(*) FROM messages m
                             WHERE m.session_id = interactions.session_id
                               AND m.project_id = interactions.project_id),
            total_tokens = COALESCE((SELECT SUM(input_tokens + output_tokens
                                              + cache_creation_tokens + cache_read_tokens)
                                     FROM token_usage t
                                     WHERE t.session_id = interactions.session_id
                                       AND t.project_id = interactions.project_id), 0)
        WHERE project_id = ?
          AND session_id NOT LIKE '%::agent-%'
          AND agent_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM interactions ch
                      WHERE ch.project_id = interactions.project_id
                        AND ch.parent_session_id = interactions.session_id
                        AND ch.agent_id = interactions.agent_id)
    """, (project_id,))

    # Commit after each project for crash resilience
    conn.commit()

    # LAV-79: monotonic — a --since pass can never lower what is persisted.
    if max_timestamp:
        set_parse_state_monotonic(conn, "last_parsed", max_timestamp, project_id, source_key,
                                  host_id, label=project_name, force=full_reparse)
        conn.commit()

    dropped = dropped_tool_results() - dropped_before
    stats = {
        "project": project_name,
        "project_id": project_id,
        "user": username,
        "host": hostname,
        "files_processed": files_processed,
        "messages_processed": messages_processed,
        "tool_calls_processed": tool_calls_processed,
        "interactions_updated": len(sessions_updated),
        "tool_results_dropped": dropped,
    }

    mtime_msg = f" (skipped {files_skipped_mtime} by mtime)" if files_skipped_mtime else ""
    # LAV-79: only ever printed when non-zero — a full pending buffer means lost
    # tool outcomes, which used to degrade silently.
    drop_msg = f", {dropped} tool_results DROPPED (pending buffer full)" if dropped else ""
    print(f"  Processed: {files_processed} files{mtime_msg}, {messages_processed} messages, {tool_calls_processed} tool calls, {len(sessions_updated)} interactions{drop_msg}")
    return stats


def parse_codex_sessions(
    conn: sqlite3.Connection,
    full_reparse: bool = False,
    project_filter: Optional[str] = None,
    codex_sessions_dirs: Optional[list[Path]] = None,
    since: Optional[tuple] = None,
) -> list:
    """Parse Codex CLI sessions into the unified DB.

    LAV-79 `since`: the ``(str_z, dt_utc, epoch)`` tuple from _normalize_since(),
    or None. Codex compares aware UTC datetimes (31 production watermarks are
    legacy NAIVE local-time strings), so only ``dt_utc`` is used here.
    """
    since_dt = since[1] if since else None
    dirs = codex_sessions_dirs or get_codex_sessions_dirs()
    if not dirs:
        print("Codex sessions directory not found")
        return []

    print("\nParsing Codex sessions from:")
    for d in dirs:
        print(f"  - {d}")

    # Detect host once (Codex runs on the current host)
    hostname, os_type, home_dir = detect_host()
    host_id = get_or_create_host(conn, hostname, os_type, home_dir)

    seen_files = set()
    jsonl_files: list[Path] = []
    for d in dirs:
        for p in d.rglob("*.jsonl"):
            try:
                rp = str(p.resolve())
            except Exception:
                rp = str(p)
            if rp in seen_files:
                continue
            seen_files.add(rp)
            jsonl_files.append(p)

    jsonl_files = sorted(jsonl_files)
    print(f"  Found {len(jsonl_files)} session files")
    if full_reparse:
        print("  Full reparse: ignoring watermark, wiping reparsed Codex rows")

    all_stats = []

    # LAV-74: watermark state loaded ONCE per project (not per event), compared
    # as timezone-aware UTC datetimes, and written only after the whole pass.
    # The internal watermark key stays SOURCE_CODEX_CLI for every Codex surface
    # so incremental cursors survive source re-attribution.
    # LAV-79: project_watermark is the EFFECTIVE gate (lowered by --since);
    # project_stored is what parse_state actually holds and is NEVER lowered. The
    # write-back at the end MUST compare against project_stored — reading the
    # effective dict is exactly how the pre-existing `max_dt > original` guard was
    # defeated (reproduced: legacy naive now() watermark -> 2026-08-01).
    project_watermark: dict = {}   # project_id -> Optional[aware UTC dt] (effective cursor)
    project_stored: dict = {}      # project_id -> Optional[aware UTC dt] (stored cursor, never lowered)
    project_max_ts: dict = {}      # project_id -> aware UTC dt (max event imported this pass)
    project_name_by_id: dict = {}  # project_id -> name, for readable watermark-refusal lines
    wiped_sids: set = set()        # full_reparse: sessions already wiped this pass

    # Tables the Codex parser writes; bash_commands/search_operations/mcp_tool_calls
    # have no UNIQUE constraint, so a re-import would duplicate them. On full
    # reparse we wipe the reparsed sessions clean first (session ids carry the
    # 'codex:' prefix, so scoping by session_id+host_id can't touch other sources).
    _CODEX_WIPE_TABLES = (
        "messages", "token_usage", "file_operations",
        "bash_commands", "search_operations", "mcp_tool_calls",
    )

    def _watermark_for(pid: int) -> Optional[datetime]:
        if pid not in project_watermark:
            raw = None if full_reparse else get_parse_state(
                conn, "last_parsed", pid, SOURCE_CODEX_CLI, host_id)
            stored = _parse_codex_watermark_ts(raw)
            project_stored[pid] = stored
            effective = stored
            if since_dt is not None and not full_reparse:
                # min(), never assignment: --since may only widen the window.
                effective = since_dt if stored is None else min(stored, since_dt)
            project_watermark[pid] = effective
        return project_watermark[pid]

    def _wipe_codex_session(sid: str) -> None:
        if not sid or sid in wiped_sids:
            return
        for table in _CODEX_WIPE_TABLES:
            try:
                conn.execute(
                    f"DELETE FROM {table} WHERE session_id = ? AND host_id = ?",
                    (sid, host_id),
                )
            except sqlite3.Error:
                pass
        wiped_sids.add(sid)

    for jsonl_file in jsonl_files:
        session_ctx = {
            "session_id": "",
            "cwd": "",
            "git_branch": "",
            "model": "",
            "last_total": None,
            "last_usage": None,
        }
        project_name = "codex_default"
        project_id = None
        user_id = None
        messages_processed = 0
        tool_calls_processed = 0
        sessions_updated = set()

        for event in parse_jsonl_file(jsonl_file):
            event_type = event.get("type", "")

            if event_type == "session_meta":
                payload = event.get("payload", {})
                session_ctx["session_id"] = format_codex_session_id(
                    payload.get("id", "") or payload.get("session_id", ""))
                session_ctx["cwd"] = payload.get("cwd", "")
                git = payload.get("git", {})
                session_ctx["git_branch"] = git.get("branch", "") if isinstance(git, dict) else ""
                if session_ctx["cwd"]:
                    project_name = extract_project_name(Path(session_ctx["cwd"]))
                    username = detect_user_from_path(Path(session_ctx["cwd"]))
                else:
                    project_name = "codex_default"
                    username = getpass.getuser()

                if project_filter and project_name != project_filter:
                    break

                project_id = get_or_create_project(conn, project_name, session_ctx["cwd"])
                user_id = get_or_create_user(conn, username)

                if full_reparse:
                    _wipe_codex_session(session_ctx["session_id"])

                # LAV-74: attribute the real surface from originator, not the
                # editor host in payload.source. Unknown originators fall back
                # to codex_local with the raw value kept in meta_json.
                originator = payload.get("originator") if isinstance(payload, dict) else ""
                source, recognized = map_codex_source(originator)
                upsert_session_source(
                    conn, session_ctx["session_id"], project_id, source,
                    meta={
                        "originator": (originator or None) if isinstance(originator, str) else None,
                        "source": payload.get("source") if isinstance(payload, dict) else None,
                    },
                    override_sources=({SOURCE_CODEX_CLI} if recognized else None),
                )
                continue

            elif event_type == "turn_context":
                payload = event.get("payload", {})
                model = payload.get("model", "")
                if model:
                    session_ctx["model"] = model
                cwd = payload.get("cwd", "")
                if cwd:
                    session_ctx["cwd"] = cwd
                    project_name = extract_project_name(Path(cwd))
                    if project_filter and project_name != project_filter:
                        continue
                    project_id = get_or_create_project(conn, project_name, cwd)
                    user_id = get_or_create_user(conn, detect_user_from_path(Path(cwd)))
                continue

            if project_id is None:
                continue

            msg_timestamp = event.get("timestamp", "")
            if not msg_timestamp:
                continue

            event_dt = _parse_codex_event_ts(msg_timestamp)
            if event_dt is None:
                continue

            # Incremental skip: aware UTC comparison against the once-loaded
            # watermark. full_reparse imports everything (wipe handled above).
            watermark = _watermark_for(project_id)
            if not full_reparse and watermark is not None and event_dt <= watermark:
                continue

            cur_max = project_max_ts.get(project_id)
            if cur_max is None or event_dt > cur_max:
                project_max_ts[project_id] = event_dt

            if event_type == "response_item":
                payload = event.get("payload", {})
                if payload.get("type") == "message":
                    role = payload.get("role", "")
                    content = payload.get("content", [])

                    text_parts = []
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict):
                                if "text" in item and isinstance(item.get("text"), str):
                                    text_parts.append(item.get("text", ""))
                            elif isinstance(item, str):
                                text_parts.append(item)
                    elif isinstance(content, str):
                        text_parts.append(content)

                    normalized_content = [{"type": "text", "text": "\n".join([t for t in text_parts if t]).strip()}]
                    msg_type = "assistant" if role == "assistant" else "user"

                    raw_key = json.dumps(payload, sort_keys=True, ensure_ascii=False)
                    digest = hashlib.sha1((msg_timestamp + "|" + raw_key).encode("utf-8")).hexdigest()

                    msg = {
                        "type": msg_type,
                        "timestamp": msg_timestamp,
                        "sessionId": session_ctx.get("session_id", ""),
                        "uuid": f"codex:{digest}",
                        "cwd": session_ctx.get("cwd", ""),
                        "gitBranch": session_ctx.get("git_branch", ""),
                        "message": {
                            "content": normalized_content,
                            "model": session_ctx.get("model", ""),
                            "usage": {},
                        },
                    }

                    process_message_content(msg, conn, project_id, user_id, host_id)
                    messages_processed += 1
                    sessions_updated.add(session_ctx.get("session_id", ""))

                elif payload.get("type") == "function_call":
                    tool_name = payload.get("name", "")
                    raw_args = payload.get("arguments", "")
                    args = parse_codex_arguments(raw_args)
                    # LAV-78: Codex's correlation id. Stored for correlation only —
                    # function_call_output is never parsed, so the outcome columns
                    # stay NULL ("no tool_result ever seen") for Codex rows.
                    call_id = payload.get("call_id", "") or ""

                    if tool_name == "shell_command":
                        command = args.get("command") if isinstance(args, dict) else None
                        workdir = args.get("workdir") if isinstance(args, dict) else None
                        process_codex_shell_command(command, workdir, msg_timestamp, session_ctx, conn, project_id, user_id, host_id,
                                                    call_id=call_id)
                        tool_calls_processed += 1
                    elif tool_name == "apply_patch":
                        patch_text = extract_codex_patch_text(raw_args, args)
                        process_codex_patch(patch_text, msg_timestamp, session_ctx, conn, project_id, user_id, host_id)
                        tool_calls_processed += 1
                    elif tool_name in ("list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"):
                        server_name = ""
                        if isinstance(args, dict):
                            server_name = args.get("server", "") or args.get("server_name", "")
                        # LAV-79: was INSERT OR IGNORE, which ignored NOTHING —
                        # mcp_tool_calls has no UNIQUE constraint, so every
                        # re-import of a rollout duplicated these rows (proven:
                        # 2 -> 4 on a two-call fixture). Same NOT EXISTS guard the
                        # claude_code branch uses, on the same natural key
                        # (timestamp, session_id, project_id, tool_name). Pre-existing
                        # bug; --since is what would have triggered it at scale.
                        sid_mcp = session_ctx.get("session_id", "")
                        try:
                            conn.execute(
                                """INSERT INTO mcp_tool_calls
                                   (timestamp, session_id, project_id, user_id, host_id, tool_name, server_name, cwd, git_branch,
                                    tool_call_id, kind)
                                   SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                                   WHERE NOT EXISTS (
                                       SELECT 1 FROM mcp_tool_calls
                                       WHERE timestamp = ? AND session_id = ? AND project_id = ? AND tool_name = ?
                                   )""",
                                (msg_timestamp, sid_mcp, project_id, user_id, host_id,
                                 tool_name, server_name, session_ctx.get("cwd", ""), session_ctx.get("git_branch", ""),
                                 call_id, tool_outcomes.tool_kind(sid_mcp, server_name, tool_name),
                                 msg_timestamp, sid_mcp, project_id, tool_name)
                            )
                        except sqlite3.Error as e:
                            print(f"DB error (codex mcp): {e}")
                        tool_calls_processed += 1

            elif event_type == "event_msg":
                payload = event.get("payload", {})
                if payload.get("type") == "token_count":
                    process_codex_token_count(event, session_ctx, conn, project_id, user_id, host_id)

        # Update interactions for this file
        if project_id is not None:
            project_name_by_id.setdefault(project_id, project_name)  # LAV-79: refusal label
            for sid in sessions_updated:
                if sid:
                    update_interaction(sid, project_name, conn, project_id, user_id, host_id)
            conn.commit()

            all_stats.append({
                "project": project_name,
                "messages_processed": messages_processed,
                "tool_calls_processed": tool_calls_processed,
            })

    # LAV-74: persist parse_state ONCE, after the whole pass, as the true max
    # event timestamp observed per project (aware UTC ISO string). Only advance
    # if we imported something newer than the existing cursor.
    # LAV-79: `original` now comes from project_stored (the un-lowered cursor).
    # Reading project_watermark here is what made this guard a no-op under --since.
    # set_parse_state_monotonic re-reads parse_state and re-checks, so the write is
    # monotone even if this seeding line is ever edited again.
    for pid, max_dt in project_max_ts.items():
        original = project_stored.get(pid)
        if original is not None and max_dt <= original:
            continue
        set_parse_state_monotonic(
            conn, "last_parsed", max_dt.isoformat(), pid, SOURCE_CODEX_CLI, host_id,
            compare=_parse_codex_watermark_ts,
            label=f"codex/{project_name_by_id.get(pid, pid)}", force=full_reparse)
    conn.commit()

    return all_stats


def parse_cowork_sessions(
    conn: sqlite3.Connection,
    full_reparse: bool = False,
    project_filter: Optional[str] = None,
    cowork_sessions_dirs: Optional[list[Path]] = None,
    since: Optional[tuple] = None,
) -> list:
    """Parse Cowork/Claude Desktop local-agent-mode audit logs into the unified DB.

    LAV-79 `since`: the ``(str_z, dt_utc, epoch)`` tuple from _normalize_since(),
    or None. ``_audit_timestamp`` is the same 24-char ISO-Z shape as claude_code,
    so only the lexicographic ``str_z`` form is used here.
    """
    since_z = since[0] if since else None
    dirs = cowork_sessions_dirs or get_cowork_sessions_dirs()
    if not dirs:
        print("Cowork sessions directory not found")
        return []

    default_project_name = "cowork_default"

    print("\nParsing Cowork/Claude Desktop sessions from:")
    for d in dirs:
        print(f"  - {d}")

    # Detect host once
    hostname, os_type, home_dir = detect_host()
    host_id = get_or_create_host(conn, hostname, os_type, home_dir)

    audit_files: list[Path] = []
    seen = set()
    for d in dirs:
        for p in d.rglob("audit.jsonl"):
            try:
                rp = str(p.resolve())
            except Exception:
                rp = str(p)
            if rp in seen:
                continue
            seen.add(rp)
            audit_files.append(p)

    audit_files = sorted(audit_files)
    print(f"  Found {len(audit_files)} audit files")

    session_project = {}
    all_stats = []
    total_messages = 0
    total_tools = 0

    def infer_project_from_event(event: dict) -> Optional[str]:
        # Cowork-aware project resolution: reject sandbox/scratch/OS paths and generic
        # folders/filenames, return a real project root or None (-> cowork_default).
        cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else ""
        if cwd:
            proj = infer_cowork_project(cwd)
            if proj:
                return proj
        msg = event.get("message")
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") != "tool_use":
                        continue
                    tool_name = item.get("name")
                    tool_input = item.get("input") or {}
                    if tool_name in ("Read", "Write", "Edit"):
                        fp = tool_input.get("file_path") if isinstance(tool_input, dict) else ""
                        if isinstance(fp, str) and fp.startswith("/"):
                            proj = infer_cowork_project(fp)
                            if proj:
                                return proj
                    if tool_name == "Bash" and isinstance(tool_input, dict):
                        cmd = tool_input.get("command", "")
                        if isinstance(cmd, str):
                            for token in cmd.split():
                                if token.startswith("/"):
                                    proj = infer_cowork_project(token)
                                    if proj:
                                        return proj
        return None

    max_audit_ts = ""
    dropped_before = dropped_tool_results()

    # LAV-79: the watermark used to be re-SELECTed once per EVENT. Memoized here as
    # {project_id: (stored, effective)} — `stored` is what parse_state holds and is
    # never lowered, `effective` is the gate, possibly lowered by --since.
    cowork_watermarks: dict = {}

    def _cowork_gate(pid: int) -> Optional[str]:
        if pid not in cowork_watermarks:
            stored = None if full_reparse else get_parse_state(
                conn, "last_parsed", pid, SOURCE_COWORK_DESKTOP, host_id)
            effective = stored
            if since_z is not None and not full_reparse:
                # min(), never assignment: --since may only widen the window.
                effective = since_z if not stored else min(stored, since_z)
            cowork_watermarks[pid] = (stored, effective)
        return cowork_watermarks[pid][1]

    for audit_path in audit_files:
        # A1. Master session id for this conversation = audit folder name with the
        # 'local_' folder prefix stripped. NB: only 'local_' is the folder prefix —
        # for a 'local_ditto_<uuid>' folder the in-file master session_id is
        # 'ditto_<uuid>' (the 'ditto_' belongs to the real sid), so we must NOT strip it.
        # If the prefix is absent, master_sid=None (defensive: each sid is its own master).
        folder = audit_path.parent.name
        raw_master = folder[len("local_"):] if folder.startswith("local_") else None
        master_sid = format_cowork_session_id(raw_master) if raw_master else None

        # Pre-scan the conversation (one audit file) once to determine:
        #  - file_project: the human prompt rarely carries a path — the project signal
        #    comes from the agent's tool calls — so the first event that yields a project
        #    sets the project for the WHOLE conversation (master + everything merged in).
        #  - sids_in_file / has_inner: Cowork logs ONE conversation as a folder-uuid
        #    "shell" (human turns only) PLUS an inner agent-execution session (which echoes
        #    the human turns + carries all assistant work + tokens). They are the SAME
        #    conversation, so when an inner session exists we MERGE everything under the
        #    folder-uuid master and drop the shell's duplicate turns.
        #  - shell_first_user: the human's opening prompt, used to title the merged row
        #    (the inner session's first user line is often a seed, not the real prompt).
        file_project = None
        sids_in_file = set()
        shell_first_user = None
        for ev in parse_jsonl_file(audit_path):
            rs = format_cowork_session_id(ev.get("session_id", ""))
            if rs:
                sids_in_file.add(rs)
            if file_project is None:
                p = infer_project_from_event(ev)
                if p:
                    file_project = p
            if (shell_first_user is None and master_sid and rs == master_sid
                    and ev.get("type") == "user"):
                txt = _cowork_user_text(ev)
                if txt:
                    shell_first_user = txt
        if not file_project:
            file_project = default_project_name
        has_inner = master_sid is not None and any(s != master_sid for s in sids_in_file)
        merged_summary = smart_title(shell_first_user) if shell_first_user else None

        # LAV-78: same per-FILE tool_result correlation state as parse_project.
        seen_tool_use_ids = set()
        pending_tool_results = {}

        for event in parse_jsonl_file(audit_path):
            msg_type = event.get("type", "")
            audit_ts = event.get("_audit_timestamp") or ""
            if not audit_ts:
                continue

            if audit_ts > max_audit_ts:
                max_audit_ts = audit_ts

            raw_sid = event.get("session_id", "")
            sid_orig = format_cowork_session_id(raw_sid)
            if not sid_orig:
                continue

            # MERGE: a Cowork conversation = one audit file. The folder-uuid "shell"
            # (human turns) and the inner agent-execution session are the same dialogue,
            # so we relabel every event to the folder-uuid master and drop the shell's
            # duplicate turns (they are re-logged inside the agent session). When there is
            # no inner session (shell-only file) we keep events under their own id.
            if master_sid is not None and has_inner:
                if sid_orig == master_sid:
                    continue  # shell duplicate — the inner agent session carries this turn
                sid = master_sid
            else:
                sid = sid_orig
            parent_sid = None  # Cowork: one conversation per file, no slaves

            # Every session in this file (master + slaves) inherits the one file_project.
            project_name = file_project
            session_project.setdefault(sid, file_project)

            if project_filter and project_name != project_filter:
                continue

            username = detect_user_from_path(Path(event.get("cwd", "") or str(Path.home())))
            project_id = get_or_create_project(conn, project_name)
            user_id = get_or_create_user(conn, username)

            # A4. Honor full_reparse: a None watermark forces re-emission of all
            # historical events (mirrors parse_project). Normal runs stay incremental.
            # LAV-79: memoized per project (was one SELECT per event) and lowered by
            # --since. max_audit_ts above is accumulated BEFORE this gate, from every
            # event on disk, so the value written back is unaffected by --since.
            last_ts = _cowork_gate(project_id)
            if last_ts and audit_ts <= last_ts:
                continue

            if msg_type == "system":
                if sid:
                    upsert_session_source(
                        conn, sid, project_id, SOURCE_COWORK_DESKTOP,
                        client_version=str(event.get("claude_code_version", "") or ""),
                        process_name=str(event.get("subtype", "") or ""),
                        meta={
                            "model": event.get("model"),
                            "cwd": event.get("cwd"),
                            "permissionMode": event.get("permissionMode"),
                            "apiKeySource": event.get("apiKeySource"),
                        },
                    )
                continue

            if msg_type not in ("user", "assistant"):
                continue

            upsert_session_source(conn, sid, project_id, SOURCE_COWORK_DESKTOP)

            msg = {
                "type": msg_type,
                "timestamp": audit_ts,
                "sessionId": sid,
                "uuid": event.get("uuid", ""),
                "cwd": "",
                "gitBranch": "",
                "message": event.get("message", {}) if isinstance(event.get("message"), dict) else {},
            }

            total_messages += 1

            process_message_content(msg, conn, project_id, user_id, host_id)

            # LAV-78: same shape as claude_code — the "user" event carries the
            # tool_result blocks for the previous assistant turn's calls.
            process_tool_results(msg, conn, project_id,
                                 seen_tool_use_ids, pending_tool_results)

            tool_calls = extract_tool_calls(msg)
            for tool_call in tool_calls:
                process_tool_call(tool_call, msg, conn, project_id, user_id, host_id)
                tool_call_id = tool_call.get("id")
                if tool_call_id:
                    seen_tool_use_ids.add(tool_call_id)
                total_tools += 1

            process_token_usage(msg, conn, project_id, user_id, host_id)

            # Update the merged conversation row; title it with the human's opening prompt.
            update_interaction(sid, project_name, conn, project_id, user_id, host_id,
                               summary=merged_summary, parent_session_id=parent_sid)

        # LAV-78: audit file fully read — apply the results that arrived before
        # their tool_use (see parse_project for the full rationale).
        flush_pending_tool_results(conn, pending_tool_results)

        conn.commit()

    # Update parse state for all cowork projects (use max observed audit_ts, not now())
    # LAV-79: both writes were UNGUARDED — if audit files are pruned this regressed
    # on its own, --since or not. Now monotonic, like the other two paths.
    if max_audit_ts:
        for sid, pname in session_project.items():
            pid = get_or_create_project(conn, pname)
            set_parse_state_monotonic(conn, "last_parsed", max_audit_ts, pid,
                                      SOURCE_COWORK_DESKTOP, host_id,
                                      label=f"cowork/{pname}", force=full_reparse)
        if default_project_name not in session_project.values():
            pid = get_or_create_project(conn, default_project_name)
            set_parse_state_monotonic(conn, "last_parsed", max_audit_ts, pid,
                                      SOURCE_COWORK_DESKTOP, host_id,
                                      label=f"cowork/{default_project_name}", force=full_reparse)
        conn.commit()

    dropped = dropped_tool_results() - dropped_before
    if dropped:  # LAV-79: silent degradation made visible; never printed when zero
        print(f"  {dropped} tool_results DROPPED (pending buffer full)")

    all_stats.append({
        "source": "cowork_desktop",
        "messages_processed": total_messages,
        "tool_calls_processed": total_tools,
        "tool_results_dropped": dropped,
    })

    return all_stats


# ===========================================================================
# MAIN
# ===========================================================================

def _launch_background_classify():
    """Launch lav-classify as a detached background process after parsing.

    Non-blocking: the subprocess runs independently so lav-parse exits immediately.
    Skips silently if OPENAI_API_KEY is not set or lav-classify is not installed.
    """
    import os
    if not os.getenv("OPENAI_API_KEY"):
        print("[classify] Skipped — OPENAI_API_KEY not set")
        return

    import subprocess
    import sys
    try:
        # Use the same Python interpreter to ensure correct venv
        proc = subprocess.Popen(
            [sys.executable, "-m", "lav.classifiers.sql_classifier"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # detach from parent process
        )
        print(f"[classify] Background classification started (PID {proc.pid})")
    except Exception as e:
        print(f"[classify] Could not launch background classification: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="LocalAgentViewer - Parse AI agent interactions into unified analytics DB"
    )
    parser.add_argument("--full", "-f", action="store_true", help="Force full reparse")
    parser.add_argument("--since", metavar="ISO",
                        help="Additive bounded reparse: temporarily lower the incremental "
                             "watermark to this instant (YYYY-MM-DD or full ISO-8601, UTC "
                             "if no offset). Inserts only; deletes nothing; never lowers a "
                             "stored watermark.")
    parser.add_argument("--project", "-p", type=str, help="Parse only a specific project")
    parser.add_argument("--list", "-l", action="store_true", help="List all available projects")
    parser.add_argument("--include-codex", action="store_true",
                        help="Deprecated no-op: Codex CLI sessions are now included by default")
    parser.add_argument("--exclude-codex", action="store_true", help="Skip Codex CLI sessions")
    parser.add_argument("--include-cowork", action="store_true", help="Include Cowork/Claude Desktop sessions")
    parser.add_argument("--claude-projects-dir", action="append", default=None)
    parser.add_argument("--codex-sessions-dir", action="append", default=None)
    parser.add_argument("--cowork-sessions-dir", action="append", default=None)

    args = parser.parse_args()

    # LAV-79: normalize ONCE here, not per project, so all three paths compare the
    # exact same instant in the exact same three forms.
    since = None
    # `is not None`, not truthiness: `--since ""` (an unset shell variable) would
    # otherwise degrade to a plain incremental run reporting success while
    # recovering nothing — and `--full --since ""` would slip past the mutual
    # exclusion below and run the destructive reparse.
    if args.since is not None:
        if args.full:
            parser.error("--since and --full are mutually exclusive: --full is destructive "
                         "(it wipes and rebuilds) and already ignores the watermark.")
        try:
            since = _normalize_since(args.since)
        except (ValueError, TypeError) as e:
            parser.error(f"--since: cannot parse {args.since!r} ({e}). "
                         "Use YYYY-MM-DD or an ISO-8601 timestamp.")

    claude_roots = [
        d for d in get_claude_projects_dirs(args.claude_projects_dir, include_desktop_hint=False)
        if d.exists() and d.is_dir()
    ]
    codex_roots = get_codex_sessions_dirs(args.codex_sessions_dir)
    cowork_roots = get_cowork_sessions_dirs(args.cowork_sessions_dir)

    if args.list:
        print("Available projects in Claude Code:")
        for root in claude_roots:
            for project_dir in sorted(root.iterdir()):
                if project_dir.is_dir():
                    name = extract_project_name(project_dir)
                    count = len(list(project_dir.glob("*.jsonl")))
                    print(f"  {name}: {count} interaction files")
        return

    # Initialize unified DB
    conn = init_db()
    print(f"Database: {UNIFIED_DB_PATH}")

    if since:
        print(f"[since] Additive reparse from {since[0]} — inserts only, "
              "stored watermarks can only move forward")

    if args.project:
        found = False
        for root in claude_roots:
            for project_dir in root.iterdir():
                if project_dir.is_dir():
                    name = extract_project_name(project_dir)
                    if name == args.project:
                        parse_project(project_dir, conn, args.full, since=since)
                        found = True
                        break
            if found:
                break
        if not found:
            print(f"Project not found: {args.project}")
            print("Use --list to see available projects")
        if not args.exclude_codex:
            parse_codex_sessions(conn, args.full, project_filter=args.project,
                                 codex_sessions_dirs=codex_roots, since=since)
        if args.include_cowork:
            parse_cowork_sessions(conn, args.full, project_filter=args.project,
                                  cowork_sessions_dirs=cowork_roots, since=since)
    else:
        print("Parsing all projects from:")
        for root in claude_roots:
            print(f"  - {root}")

        all_stats = []
        for root in claude_roots:
            for project_dir in sorted(root.iterdir()):
                if project_dir.is_dir():
                    stats = parse_project(project_dir, conn, args.full, since=since)
                    all_stats.append(stats)

        if not args.exclude_codex:
            parse_codex_sessions(conn, args.full, codex_sessions_dirs=codex_roots, since=since)
        if args.include_cowork:
            parse_cowork_sessions(conn, args.full, cowork_sessions_dirs=cowork_roots, since=since)

        print(f"\n{'='*60}")
        print("Summary")
        print(f"{'='*60}")
        total_messages = sum(s["messages_processed"] for s in all_stats)
        total_tools = sum(s["tool_calls_processed"] for s in all_stats)
        print(f"Projects processed: {len(all_stats)}")
        print(f"Total messages: {total_messages}")
        print(f"Total tool calls: {total_tools}")

    conn.close()

    if since:
        # queries.export_sessions filters child rows on `timestamp > last_pull`;
        # rows recovered by --since carry their ORIGINAL (old) timestamps, far below
        # that cursor, so the collector will never pull them. Run --since per node.
        print(f"\n[since] Recovered rows keep their original timestamps and will NOT "
              f"propagate through the normal collector pull — run "
              f"`lav-parse --since {since[0]}` on each node.")

    # Auto-classification on parse is DISABLED — gpt-4.1 auto-classify was too costly
    # and the taxonomy is being reworked; future classification runs on-demand (local).
    # Re-enable by uncommenting the call below.
    # _launch_background_classify()

    # Notify collector to pull from this agent (if configured)
    notify_collector(load_runtime_config())


# ===========================================================================
# COLLECTOR NOTIFICATION (push-triggered pull)
# ===========================================================================

def notify_collector(runtime_config: dict) -> None:
    """Notify the collector to pull from this agent after a parse run.

    Non-blocking HTTP POST to {collector_url}/api/sync. Silent on error.
    Only runs when role=agent and collector_url is set in local config.
    Cross-platform: works on Mac, Linux, Windows.

    Config example (~/.local/share/local-agent-viewer/config.json on agent):
        {"role": "agent", "port": 8764, "collector_url": "http://collector.local:8764"}
    """
    collector_url = runtime_config.get("collector_url", "")
    if not collector_url:
        return
    role = runtime_config.get("role", "both")
    if role != "agent":
        return  # both/collector handle their own pulls; only agent notifies

    import threading
    import urllib.request

    import os
    api_key = os.environ.get("LAV_API_KEY", "")

    def _post():
        try:
            payload = json.dumps({"api_key": api_key, "scope": "agent"}).encode()
            req = urllib.request.Request(
                f"{collector_url}/api/sync",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
            print(f"[notify_collector] Pull trigger sent to {collector_url}")
        except Exception as e:
            print(f"[notify_collector] Could not reach collector at {collector_url}: {e}")

    t = threading.Thread(target=_post, daemon=True)
    t.start()
    t.join(timeout=6)


# ===========================================================================
# REMOTE INGESTION (for collector pull)
# ===========================================================================

def _heal_tool_outcomes(conn: sqlite3.Connection, table: str, key_sql: str,
                        key_params: tuple, payload: dict) -> int:
    """LAV-78: fill the outcome columns of a tool row the collector ALREADY has.

    Every child insert in ingest_remote_sessions is guarded (INSERT OR IGNORE /
    NOT EXISTS), so a row pulled once is never rewritten. That was harmless while
    child rows were immutable; LAV-78 made them mutable and opened a permanent
    drift: the agent ships a tool_use whose tool_result has not landed on disk yet
    (is_error NULL), the collector stores the NULL row, and when the agent later
    stamps the real outcome the row is never re-shipped — export_sessions() filters
    child rows on their own (tool-call) timestamp, which never moves.

    This heals the collector side: for a row that already exists, ONLY the outcome
    columns of tool_outcomes.OUTCOME_COLUMNS[table] are written — identity and
    descriptive columns (timestamp, session_id, command, file_path, …) are never
    in the SET list, and `key_sql` is the same natural key the insert guard uses.

    FILL-ONLY semantics, deliberately stricter than "don't overwrite with NULL":
    a column is written only where the collector's own value is still NULL (or ''
    for the TEXT columns), so an agent that has not seen the tool_result yet can
    never erase a value the collector's own backfill already derived. That also
    makes it idempotent for free — after the first heal nothing is empty any
    more, the WHERE tail is false and the second ingest updates 0 rows.

    Returns the number of rows updated.
    """
    set_parts = []
    empty_parts = []
    values = []
    for column, _decl in tool_outcomes.OUTCOME_COLUMNS.get(table, []):
        value = payload.get(column)
        if isinstance(value, str):
            if not value:
                continue  # '' = "agent has nothing to say about this column"
            set_parts.append(f"{column} = COALESCE(NULLIF({column}, ''), ?)")
            empty_parts.append(f"NULLIF({column}, '') IS NULL")
        else:
            if value is None:
                continue  # NULL = "no tool_result seen"; never propagated
            set_parts.append(f"{column} = COALESCE({column}, ?)")
            empty_parts.append(f"{column} IS NULL")
        values.append(value)

    if not set_parts:
        return 0

    sql = "UPDATE {t} SET {sets} WHERE {key} AND ({empty})".format(
        t=table, sets=", ".join(set_parts), key=key_sql, empty=" OR ".join(empty_parts)
    )
    try:
        cur = conn.execute(sql, values + list(key_params))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    except sqlite3.Error:
        return 0


def ingest_remote_sessions(conn: sqlite3.Connection, sessions: list,
                           host_info: dict, user_info: dict) -> dict:
    """Ingest sessions received from a remote agent's /api/export.

    Uses INSERT OR IGNORE on composite PKs to prevent duplicates.
    Returns stats dict with counts of ingested data.
    """
    stats = {"sessions": 0, "messages": 0, "token_usage": 0,
             "file_operations": 0, "bash_commands": 0, "search_operations": 0,
             "skill_invocations": 0, "subagent_invocations": 0, "mcp_tool_calls": 0}

    if not sessions:
        return stats

    # Resolve host and user IDs. Guard against a pre-fix agent exporting a
    # corrupted/generic hostname (LAV-68) — never create a mojibake host record.
    hostname = _normalize_hostname(host_info.get("hostname", "unknown"))
    if not _is_valid_hostname(hostname):
        hostname = "unknown"
    os_type = host_info.get("os_type", "")
    home_dir = host_info.get("home_dir", "")
    host_id = get_or_create_host(conn, hostname, os_type, home_dir)

    username = user_info.get("username", "unknown")
    user_id = get_or_create_user(conn, username)

    for session_data in sessions:
        conv = session_data.get("interaction", session_data.get("conversation", {}))
        session_id = conv.get("session_id")
        if not session_id:
            continue

        # LAV-66: never ingest phantom meta "children" (compaction/suggestion/
        # side-question). A pre-fix agent may still export them; they are
        # duplicated parent messages or noise, not real subagent sessions.
        if "::agent-" in session_id and not is_real_agent_id(session_id.split("::agent-", 1)[1]):
            continue

        # Resolve project
        project_name = conv.get("project_name") or conv.get("project") or "unknown"
        source_path = ""
        project_id = get_or_create_project(conn, project_name, source_path)

        # Use remote host/user IDs, not local ones
        r_host_id = host_id
        r_user_id = user_id

        # Insert interaction (OR IGNORE = anti-duplicate on PK)
        try:
            conn.execute("""
                INSERT OR IGNORE INTO interactions
                (session_id, project_id, user_id, host_id, timestamp, display, summary,
                 project, model, total_tokens, message_count, tools_used, cwd, git_branch,
                 parent_session_id, agent_id, workflow_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                session_id, project_id, r_user_id, r_host_id,
                conv.get("timestamp", ""),
                conv.get("display", ""),
                conv.get("summary", ""),
                conv.get("project", ""),
                conv.get("model", ""),
                conv.get("total_tokens", 0),
                conv.get("message_count", 0),
                conv.get("tools_used", "[]"),
                conv.get("cwd", ""),
                conv.get("git_branch", ""),
                conv.get("parent_session_id"),
                conv.get("agent_id"),
                conv.get("workflow_id", "") or "",
            ))
            stats["sessions"] += 1
        except sqlite3.Error:
            continue  # Already exists or error — skip

        # Insert session_sources
        client_source = conv.get("client_source", "claude_code")
        try:
            conn.execute("""
                INSERT OR IGNORE INTO session_sources
                (session_id, project_id, source)
                VALUES (?, ?, ?)
            """, (session_id, project_id, client_source))
            # LAV-74: propagate Codex re-attribution from the agent. A session
            # pulled before the fix sits here as generic codex_cli/codex_local;
            # when the agent now reports a recognized surface, upgrade it in
            # place. Never downgrade or touch non-Codex sources.
            if client_source in _CODEX_RECOGNIZED_SOURCES:
                conn.execute("""
                    UPDATE session_sources SET source = ?
                    WHERE session_id = ? AND project_id = ?
                      AND source IN (?, ?) AND source <> ?
                """, (client_source, session_id, project_id,
                      SOURCE_CODEX_CLI, SOURCE_CODEX_LOCAL, client_source))
        except sqlite3.Error:
            pass

        # Insert child records (all use INSERT OR IGNORE where UNIQUE exists)
        for msg in session_data.get("messages", []):
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO messages
                    (session_id, project_id, user_id, host_id, uuid, type, content,
                     timestamp, tokens_in, tokens_out, model, api_message_id, agent_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    session_id, project_id, r_user_id, r_host_id,
                    msg.get("uuid"), msg.get("type", ""),
                    msg.get("content", ""), msg.get("timestamp", ""),
                    msg.get("tokens_in", 0), msg.get("tokens_out", 0),
                    msg.get("model", ""),
                    msg.get("api_message_id", "") or "",
                    msg.get("agent_id"),
                ))
                stats["messages"] += 1
            except sqlite3.Error:
                pass

        for tu in session_data.get("token_usage", []):
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO token_usage
                    (timestamp, session_id, project_id, user_id, host_id, model,
                     input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, cwd,
                     api_message_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    tu.get("timestamp", ""), session_id, project_id, r_user_id, r_host_id,
                    tu.get("model", ""),
                    tu.get("input_tokens", 0), tu.get("output_tokens", 0),
                    tu.get("cache_creation_tokens", 0), tu.get("cache_read_tokens", 0),
                    tu.get("cwd", ""),
                    tu.get("api_message_id", "") or "",
                ))
                stats["token_usage"] += 1
            except sqlite3.Error:
                pass

        # LAV-78: the outcome columns must be listed here too, or the agent ships
        # them in the payload and the collector silently drops them. is_error /
        # duration_ms / exit_code default to None -> NULL ("never seen"), which is
        # the correct value for an agent that has not been upgraded yet.
        for fo in session_data.get("file_operations", []):
            try:
                cur = conn.execute("""
                    INSERT OR IGNORE INTO file_operations
                    (timestamp, session_id, project_id, user_id, host_id, tool, file_path, cwd, git_branch,
                     tool_call_id, is_error, duration_ms)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    fo.get("timestamp", ""), session_id, project_id, r_user_id, r_host_id,
                    fo.get("tool", ""), fo.get("file_path", ""),
                    fo.get("cwd", ""), fo.get("git_branch", ""),
                    fo.get("tool_call_id", "") or "", fo.get("is_error"), fo.get("duration_ms"),
                ))
                stats["file_operations"] += 1
                if cur.rowcount == 0:
                    # Row already here from an earlier pull — heal its outcome only.
                    _heal_tool_outcomes(
                        conn, "file_operations",
                        "timestamp = ? AND session_id = ? AND project_id = ? "
                        "AND tool = ? AND file_path = ?",
                        (fo.get("timestamp", ""), session_id, project_id,
                         fo.get("tool", ""), fo.get("file_path", "")),
                        fo,
                    )
            except sqlite3.Error:
                pass

        for bc in session_data.get("bash_commands", []):
            # No UNIQUE constraint — NOT EXISTS guard so re-pulls stay idempotent (LAV-66).
            try:
                cur = conn.execute("""
                    INSERT INTO bash_commands
                    (timestamp, session_id, project_id, user_id, host_id, command, description,
                     target_file, cwd, git_branch,
                     tool_call_id, is_error, duration_ms, error_text, exit_code, cmd_name)
                    SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    WHERE NOT EXISTS (
                        SELECT 1 FROM bash_commands
                        WHERE timestamp = ? AND session_id = ? AND project_id = ? AND command = ?
                    )
                """, (
                    bc.get("timestamp", ""), session_id, project_id, r_user_id, r_host_id,
                    bc.get("command", ""), bc.get("description", ""),
                    bc.get("target_file", ""), bc.get("cwd", ""), bc.get("git_branch", ""),
                    bc.get("tool_call_id", "") or "", bc.get("is_error"), bc.get("duration_ms"),
                    bc.get("error_text", "") or "", bc.get("exit_code"),
                    # LAV-79: recomputed locally, never trusted from the payload —
                    # an agent on older code sends no cmd_name at all, and version
                    # skew in either direction then costs nothing.
                    bash_cmd_name(bc.get("command", "")),
                    bc.get("timestamp", ""), session_id, project_id, bc.get("command", ""),
                ))
                stats["bash_commands"] += 1
                if cur.rowcount == 0:
                    _heal_tool_outcomes(
                        conn, "bash_commands",
                        "timestamp = ? AND session_id = ? AND project_id = ? AND command = ?",
                        (bc.get("timestamp", ""), session_id, project_id,
                         bc.get("command", "")),
                        bc,
                    )
            except sqlite3.Error:
                pass

        for so in session_data.get("search_operations", []):
            # No UNIQUE constraint — NOT EXISTS guard so re-pulls stay idempotent (LAV-66).
            try:
                cur = conn.execute("""
                    INSERT INTO search_operations
                    (timestamp, session_id, project_id, user_id, host_id, tool, pattern, path, output_mode, cwd,
                     tool_call_id, is_error, duration_ms)
                    SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    WHERE NOT EXISTS (
                        SELECT 1 FROM search_operations
                        WHERE timestamp = ? AND session_id = ? AND project_id = ? AND tool = ? AND pattern = ?
                    )
                """, (
                    so.get("timestamp", ""), session_id, project_id, r_user_id, r_host_id,
                    so.get("tool", ""), so.get("pattern", ""),
                    so.get("path", ""), so.get("output_mode", ""), so.get("cwd", ""),
                    so.get("tool_call_id", "") or "", so.get("is_error"), so.get("duration_ms"),
                    so.get("timestamp", ""), session_id, project_id, so.get("tool", ""), so.get("pattern", ""),
                ))
                stats["search_operations"] += 1
                if cur.rowcount == 0:
                    _heal_tool_outcomes(
                        conn, "search_operations",
                        "timestamp = ? AND session_id = ? AND project_id = ? "
                        "AND tool = ? AND pattern = ?",
                        (so.get("timestamp", ""), session_id, project_id,
                         so.get("tool", ""), so.get("pattern", "")),
                        so,
                    )
            except sqlite3.Error:
                pass

        for si in session_data.get("skill_invocations", []):
            try:
                cur = conn.execute("""
                    INSERT OR IGNORE INTO skill_invocations
                    (timestamp, session_id, project_id, user_id, host_id, skill_name, args, cwd, git_branch,
                     tool_call_id, is_error, duration_ms)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    si.get("timestamp", ""), session_id, project_id, r_user_id, r_host_id,
                    si.get("skill_name", ""), si.get("args", ""),
                    si.get("cwd", ""), si.get("git_branch", ""),
                    si.get("tool_call_id", "") or "", si.get("is_error"), si.get("duration_ms"),
                ))
                stats["skill_invocations"] += 1
                if cur.rowcount == 0:
                    _heal_tool_outcomes(
                        conn, "skill_invocations",
                        "timestamp = ? AND session_id = ? AND project_id = ? AND skill_name = ?",
                        (si.get("timestamp", ""), session_id, project_id,
                         si.get("skill_name", "")),
                        si,
                    )
            except sqlite3.Error:
                pass

        for sa in session_data.get("subagent_invocations", []):
            try:
                cur = conn.execute("""
                    INSERT OR IGNORE INTO subagent_invocations
                    (timestamp, session_id, project_id, user_id, host_id, subagent_type, description,
                     prompt, model, run_in_background, cwd, git_branch,
                     tool_call_id, is_error, duration_ms, spawn_tool, workflow_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    sa.get("timestamp", ""), session_id, project_id, r_user_id, r_host_id,
                    sa.get("subagent_type", ""), sa.get("description", ""),
                    sa.get("prompt", ""), sa.get("model", ""),
                    sa.get("run_in_background", 0),
                    sa.get("cwd", ""), sa.get("git_branch", ""),
                    sa.get("tool_call_id", "") or "", sa.get("is_error"), sa.get("duration_ms"),
                    # LAV-82: taken from the payload, NOT re-derived — unlike `kind`
                    # these are not functions of columns that travel. spawn_tool comes
                    # from the tool_use name and workflow_id from a tool_result the
                    # collector never sees, so the agent is the only source. An agent
                    # on older code ships neither and the '' default applies.
                    sa.get("spawn_tool", "") or "", sa.get("workflow_id", "") or "",
                ))
                stats["subagent_invocations"] += 1
                if cur.rowcount == 0:
                    # `description IS ?` (not `= ?`): the column is nullable and
                    # older collector rows can hold NULL — IS behaves like = for
                    # non-NULL values and still matches NULL against NULL.
                    _heal_tool_outcomes(
                        conn, "subagent_invocations",
                        "timestamp = ? AND session_id = ? AND project_id = ? "
                        "AND subagent_type = ? AND description IS ?",
                        (sa.get("timestamp", ""), session_id, project_id,
                         sa.get("subagent_type", ""), sa.get("description", "")),
                        sa,
                    )
            except sqlite3.Error:
                pass

        for mc in session_data.get("mcp_tool_calls", []):
            # No UNIQUE constraint — NOT EXISTS guard so re-pulls stay idempotent (LAV-66).
            try:
                cur = conn.execute("""
                    INSERT INTO mcp_tool_calls
                    (timestamp, session_id, project_id, user_id, host_id, tool_name, server_name, cwd, git_branch,
                     tool_call_id, is_error, duration_ms, error_text, kind)
                    SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    WHERE NOT EXISTS (
                        SELECT 1 FROM mcp_tool_calls
                        WHERE timestamp = ? AND session_id = ? AND project_id = ? AND tool_name = ?
                    )
                """, (
                    mc.get("timestamp", ""), session_id, project_id, r_user_id, r_host_id,
                    mc.get("tool_name", ""), mc.get("server_name", ""),
                    mc.get("cwd", ""), mc.get("git_branch", ""),
                    mc.get("tool_call_id", "") or "", mc.get("is_error"), mc.get("duration_ms"),
                    mc.get("error_text", "") or "",
                    # LAV-85: RECOMPUTED locally, never taken from the payload — same
                    # rule LAV-79 applies to cmd_name. kind is a pure function of three
                    # columns that travel anyway, so deriving it here makes agent/
                    # collector version skew a non-issue in both directions: an agent on
                    # older code ships no kind, and a collector on older code ignores it.
                    tool_outcomes.tool_kind(session_id, mc.get("server_name", ""),
                                            mc.get("tool_name", "")),
                    mc.get("timestamp", ""), session_id, project_id, mc.get("tool_name", ""),
                ))
                stats["mcp_tool_calls"] += 1
                if cur.rowcount == 0:
                    _heal_tool_outcomes(
                        conn, "mcp_tool_calls",
                        "timestamp = ? AND session_id = ? AND project_id = ? AND tool_name = ?",
                        (mc.get("timestamp", ""), session_id, project_id,
                         mc.get("tool_name", "")),
                        mc,
                    )
            except sqlite3.Error:
                pass

        # Re-materialize the interaction summary from the child rows just
        # ingested. The INSERT OR IGNORE above only writes total_tokens /
        # message_count on the FIRST pull of a session; on later pulls the PK
        # already exists so the agent's newer aggregates are dropped, leaving
        # the summary frozen at first-seen values (e.g. 130K tokens / 15 msgs
        # for a session that has since grown to 77M / 1780). Recompute here
        # from the collector's own messages/token_usage — the same canonical
        # formula update_interaction() uses — so growing sessions stay correct.
        # LAV-66: also refresh the descriptive fields (display/summary when the
        # agent ships a non-empty value, parent_session_id/agent_id always) so
        # rows corrupted by the pre-fix parser self-heal on the next pull.
        try:
            mrow = conn.execute("""
                SELECT COUNT(*), SUM(tokens_in + tokens_out), MAX(model)
                FROM messages WHERE session_id = ? AND project_id = ?
            """, (session_id, project_id)).fetchone()
            if mrow and mrow[0]:
                msg_count = mrow[0]
                msg_tokens = mrow[1] or 0
                model = mrow[2]
                trow = conn.execute("""
                    SELECT SUM(input_tokens + output_tokens
                               + cache_creation_tokens + cache_read_tokens),
                           MAX(model)
                    FROM token_usage WHERE session_id = ? AND project_id = ?
                """, (session_id, project_id)).fetchone()
                total_tokens = trow[0] if (trow and trow[0] is not None) else msg_tokens
                if not model and trow and trow[1]:
                    model = trow[1]
                conn.execute("""
                    UPDATE interactions
                    SET total_tokens = ?, message_count = ?,
                        model = COALESCE(NULLIF(?, ''), model),
                        display = COALESCE(NULLIF(?, ''), display),
                        summary = COALESCE(NULLIF(?, ''), summary),
                        parent_session_id = ?,
                        agent_id = ?,
                        -- LAV-82: FILL-ONLY, unlike the two above. The wf_ id is
                        -- derived from the transcript PATH, which exists only on
                        -- the agent's disk; an agent on older code ships '' and
                        -- must not be able to erase an id the collector already
                        -- recovered from its own messages.
                        workflow_id = COALESCE(NULLIF(?, ''), NULLIF(workflow_id, ''), '')
                    WHERE session_id = ? AND project_id = ?
                """, (total_tokens, msg_count, model or "",
                      conv.get("display", "") or "",
                      conv.get("summary", "") or "",
                      conv.get("parent_session_id"),
                      conv.get("agent_id"),
                      conv.get("workflow_id", "") or "",
                      session_id, project_id))
        except sqlite3.Error:
            pass

    conn.commit()
    return stats


if __name__ == "__main__":
    main()
