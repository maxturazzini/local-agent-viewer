#!/usr/bin/env python3
"""
LocalAgentViewer - Multi-user, multi-host AI agent conversation parser.

Parses Claude Code, Codex CLI, and Claude Desktop conversation files (.jsonl)
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
import platform
import re
import socket
import sqlite3
from datetime import datetime
from pathlib import Path
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
    get_claude_projects_dirs,
    get_codex_sessions_dirs,
    get_cowork_sessions_dirs,
    load_runtime_config,
)

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

CREATE TABLE IF NOT EXISTS conversations (
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
    git_branch TEXT
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
    cwd TEXT
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
    git_branch TEXT
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
    UNIQUE(timestamp, session_id, project_id)
);

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
CREATE INDEX IF NOT EXISTS idx_conv_project_ts ON conversations(project_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id);
CREATE INDEX IF NOT EXISTS idx_conv_host ON conversations(host_id);
CREATE INDEX IF NOT EXISTS idx_conv_user_project ON conversations(user_id, project_id);

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
CREATE INDEX IF NOT EXISTS idx_conv_timestamp ON conversations(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_msg_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_session_sources_source ON session_sources(source);

-- Conversation metadata (SQL-based classification, independent from Qdrant)
CREATE TABLE IF NOT EXISTS conversation_metadata (
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
    FOREIGN KEY (session_id, project_id) REFERENCES conversations(session_id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_convmeta_classification ON conversation_metadata(classification);
CREATE INDEX IF NOT EXISTS idx_convmeta_sensitivity ON conversation_metadata(data_sensitivity);
CREATE INDEX IF NOT EXISTS idx_convmeta_project ON conversation_metadata(project_id);
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


def init_db(db_path: Path = UNIFIED_DB_PATH) -> sqlite3.Connection:
    """Initialize the unified SQLite database with schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(PRAGMAS)
    conn.executescript(SCHEMA)
    conn.commit()
    # Migrate existing DBs that lack host_id in parse_state
    try:
        _migrate_parse_state(conn)
    except Exception as e:
        print(f"  Migration check skipped: {e}")
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


def _normalize_hostname(hostname: str) -> str:
    """Strip .local, .localdomain etc. to avoid duplicate host records."""
    for suffix in ('.localdomain', '.local'):
        if hostname.endswith(suffix):
            hostname = hostname[:-len(suffix)]
    return hostname


def detect_host() -> tuple[str, str, str]:
    """Detect current host info. Returns (hostname, os_type, home_dir)."""
    hostname = _normalize_hostname(socket.gethostname())
    os_type = platform.system().lower()
    home_dir = str(Path.home())
    return (hostname, os_type, home_dir)


def detect_host_from_path(source_path: Path) -> tuple[str, str, str]:
    """Detect host info from path + runtime. Returns (hostname, os_type, home_dir)."""
    hostname = _normalize_hostname(socket.gethostname())
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


# ===========================================================================
# HELPERS
# ===========================================================================

def extract_project_name(project_path: Path) -> str:
    """Extract a clean project name from a Claude projects directory path."""
    name = project_path.name
    parts = name.split('-')
    meaningful = [p for p in parts if p and p not in ('Users', getpass.getuser(), 'Library', 'CloudStorage')]
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


def format_cowork_session_id(session_id: str) -> str:
    """Prefix Cowork/Claude Desktop session IDs to avoid collisions."""
    if not session_id:
        return ""
    if session_id.startswith("cowork:"):
        return session_id
    return f"cowork:{session_id}"


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
):
    """Upsert session source with project scope."""
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
                model, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, cwd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (timestamp, session_id, project_id, user_id, host_id,
             usage["model"], usage["input_tokens"], usage["output_tokens"],
             usage["cache_creation_tokens"], usage["cache_read_tokens"], cwd)
        )
    except sqlite3.Error as e:
        print(f"DB error (tokens): {e}")


def process_tool_call(tool_call: dict, message: dict, conn: sqlite3.Connection,
                      project_id: int, user_id: int, host_id: int):
    """Process a single tool call and insert into database."""
    tool_name = tool_call.get("name")
    tool_input = tool_call.get("input", {})

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
                       (timestamp, session_id, project_id, user_id, host_id, tool, file_path, cwd, git_branch)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (timestamp, session_id, project_id, user_id, host_id, tool_name, file_path, cwd, git_branch)
                )
            except sqlite3.Error as e:
                print(f"DB error (file_ops): {e}")

    elif tool_name == "Bash":
        command = tool_input.get("command", "")
        if is_file_related_bash(command):
            description = tool_input.get("description", "")
            target_file = extract_target_file(command)

            bash_category = get_bash_category(command)
            if bash_category and target_file:
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO file_operations
                           (timestamp, session_id, project_id, user_id, host_id, tool, file_path, cwd, git_branch)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (timestamp, session_id, project_id, user_id, host_id, bash_category, target_file, cwd, git_branch)
                    )
                except sqlite3.Error as e:
                    print(f"DB error (bash->file_ops): {e}")

            try:
                conn.execute(
                    """INSERT OR IGNORE INTO bash_commands
                       (timestamp, session_id, project_id, user_id, host_id, command, description, target_file, cwd, git_branch)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (timestamp, session_id, project_id, user_id, host_id, command, description, target_file, cwd, git_branch)
                )
            except sqlite3.Error as e:
                print(f"DB error (bash): {e}")

    elif tool_name in SEARCH_TOOLS:
        pattern = tool_input.get("pattern", "")
        if pattern:
            path = tool_input.get("path", "")
            output_mode = tool_input.get("output_mode", "")
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO search_operations
                       (timestamp, session_id, project_id, user_id, host_id, tool, pattern, path, output_mode, cwd)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (timestamp, session_id, project_id, user_id, host_id, tool_name, pattern, path, output_mode, cwd)
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
                       (timestamp, session_id, project_id, user_id, host_id, skill_name, args, cwd, git_branch)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (timestamp, session_id, project_id, user_id, host_id, skill_name, args, cwd, git_branch)
                )
            except sqlite3.Error as e:
                print(f"DB error (skill): {e}")

    elif tool_name == "Task":
        subagent_type = tool_input.get("subagent_type", "")
        if subagent_type:
            description = tool_input.get("description", "")
            prompt = tool_input.get("prompt", "")
            model = tool_input.get("model", "")
            run_in_background = 1 if tool_input.get("run_in_background", False) else 0
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO subagent_invocations
                       (timestamp, session_id, project_id, user_id, host_id, subagent_type, description, prompt, model, run_in_background, cwd, git_branch)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (timestamp, session_id, project_id, user_id, host_id, subagent_type, description, prompt, model, run_in_background, cwd, git_branch)
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
        try:
            conn.execute(
                """INSERT OR IGNORE INTO mcp_tool_calls
                   (timestamp, session_id, project_id, user_id, host_id, tool_name, server_name, cwd, git_branch)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (timestamp, session_id, project_id, user_id, host_id, mcp_tool, server_name, cwd, git_branch)
            )
        except sqlite3.Error as e:
            print(f"DB error (mcp): {e}")


def process_message_content(message: dict, conn: sqlite3.Connection, project_id: int, user_id: int, host_id: int):
    """Save message content to messages table with full structure."""
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

    tokens_in = 0
    tokens_out = 0
    model = ""
    if msg_type == "assistant":
        usage = message.get("message", {}).get("usage", {})
        tokens_in = usage.get("input_tokens", 0)
        tokens_out = usage.get("output_tokens", 0)
        model = message.get("message", {}).get("model", "")

    try:
        conn.execute(
            """INSERT OR IGNORE INTO messages
               (session_id, project_id, user_id, host_id, uuid, type, content, timestamp, tokens_in, tokens_out, model)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, project_id, user_id, host_id, uuid, msg_type, content_json, timestamp, tokens_in, tokens_out, model)
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
                                conn: sqlite3.Connection, project_id: int, user_id: int, host_id: int):
    """Process a Codex shell_command as a Bash tool call."""
    if not command:
        return
    cwd = workdir or session_ctx.get("cwd", "")
    tool_call = {
        "name": "Bash",
        "input": {"command": command, "description": ""}
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
# CONVERSATION UPDATE
# ===========================================================================

def update_conversation(session_id: str, project_name: str, conn: sqlite3.Connection,
                        project_id: int, user_id: int, host_id: int,
                        summary: str = None, parent_session_id: str = None, agent_id: str = None):
    """Aggregate and update conversation metadata from messages."""
    try:
        cursor = conn.execute("""
            SELECT
                MIN(timestamp) as first_ts,
                COUNT(*) as msg_count,
                SUM(tokens_in + tokens_out) as total_tokens,
                MAX(model) as model
            FROM messages
            WHERE session_id = ? AND project_id = ?
        """, (session_id, project_id))
        row = cursor.fetchone()
        if not row or not row[0]:
            return

        first_ts, msg_count, total_tokens, model = row

        if not total_tokens or total_tokens == 0:
            cursor = conn.execute("""
                SELECT
                    SUM(input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens) as total_tokens,
                    MAX(model) as model
                FROM token_usage
                WHERE session_id = ? AND project_id = ?
            """, (session_id, project_id))
            trow = cursor.fetchone()
            if trow:
                total_tokens = trow[0] or 0
                if not model and trow[1]:
                    model = trow[1]

        cursor = conn.execute("""
            SELECT content FROM messages
            WHERE session_id = ? AND project_id = ? AND type = 'user'
            ORDER BY timestamp ASC LIMIT 10
        """, (session_id, project_id))
        raw_display = ""
        for (raw_content,) in cursor.fetchall():
            if not raw_content:
                continue
            try:
                content_data = json.loads(raw_content)
                if isinstance(content_data, list):
                    for item in content_data:
                        if isinstance(item, dict) and item.get('type') == 'text':
                            raw_display = item.get('text', '')
                            break
                elif isinstance(content_data, str):
                    raw_display = content_data
            except (json.JSONDecodeError, TypeError):
                raw_display = raw_content
            if raw_display:
                break

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

        conn.execute("""
            INSERT OR REPLACE INTO conversations
            (session_id, project_id, user_id, host_id, timestamp, display, summary, project, model,
             total_tokens, message_count, tools_used, cwd, git_branch, parent_session_id, agent_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (session_id, project_id, user_id, host_id, first_ts, display, summary, project_name, model,
              total_tokens or 0, msg_count, tools_json, cwd, git_branch, parent_session_id, agent_id))

    except sqlite3.Error as e:
        print(f"DB error (conversation): {e}")


def resolve_agent_parents(conn: sqlite3.Connection, project_id: int):
    """Post-process to find real parent_session_id for agent conversations."""
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
        JOIN conversations c ON m.session_id = c.session_id AND m.project_id = c.project_id
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
        FROM conversations
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
            UPDATE conversations
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

def parse_project(project_dir: Path, conn: sqlite3.Connection, full_reparse: bool = False) -> dict:
    """Parse all conversations for a single Claude Code project into the unified DB."""
    project_name = extract_project_name(project_dir)

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

    last_ts = None if full_reparse else get_parse_state(conn, "last_parsed", project_id, source_key, host_id)
    if last_ts:
        print(f"  Incremental from: {last_ts}")
    else:
        print("  Full parse")

    max_timestamp = last_ts or ""
    files_processed = 0
    messages_processed = 0
    tool_calls_processed = 0
    sessions_updated = set()
    session_summaries = {}
    session_agent_info = {}

    jsonl_files = sorted(project_dir.rglob("*.jsonl"))
    print(f"  Found {len(jsonl_files)} conversation files")

    for jsonl_file in jsonl_files:
        files_processed += 1

        is_agent_file = jsonl_file.name.startswith("agent-")
        agent_id_from_filename = jsonl_file.stem.replace("agent-", "") if is_agent_file else None

        parent_from_path = None
        if is_agent_file and "subagents" in jsonl_file.parts:
            subagents_idx = jsonl_file.parts.index("subagents")
            if subagents_idx > 0:
                parent_folder = jsonl_file.parts[subagents_idx - 1]
                if len(parent_folder) == 36 and parent_folder.count("-") == 4:
                    parent_from_path = parent_folder

        file_session_id = jsonl_file.stem if not is_agent_file else None

        for message in parse_jsonl_file(jsonl_file):
            msg_type = message.get("type", "")
            msg_timestamp = message.get("timestamp", "")

            if msg_type == "summary":
                summary_text = message.get("summary", "")
                session_id = message.get("sessionId", file_session_id)
                if summary_text and session_id:
                    session_summaries[session_id] = summary_text
                continue

            if msg_type not in ("user", "assistant"):
                if is_agent_file and msg_type == "user":
                    session_id = message.get("sessionId", "")
                    agent_id = message.get("agentId", agent_id_from_filename)
                    if session_id and agent_id:
                        session_agent_info[session_id] = (parent_from_path, agent_id)
                continue

            if last_ts and msg_timestamp <= last_ts:
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

            if is_agent_file and session_id:
                agent_id = message.get("agentId", agent_id_from_filename)
                if agent_id:
                    session_agent_info[session_id] = (parent_from_path, agent_id)

            process_message_content(message, conn, project_id, user_id, host_id)

            tool_calls = extract_tool_calls(message)
            for tool_call in tool_calls:
                process_tool_call(tool_call, message, conn, project_id, user_id, host_id)
                tool_calls_processed += 1

            process_token_usage(message, conn, project_id, user_id, host_id)

    for session_id in sessions_updated:
        summary = session_summaries.get(session_id)
        agent_info = session_agent_info.get(session_id)
        parent_sid, agent_id = agent_info if agent_info else (None, None)
        update_conversation(session_id, project_name, conn, project_id, user_id, host_id,
                            summary=summary, parent_session_id=parent_sid, agent_id=agent_id)

    resolve_agent_parents(conn, project_id)

    # Commit after each project for crash resilience
    conn.commit()

    if max_timestamp:
        set_parse_state(conn, "last_parsed", max_timestamp, project_id, source_key, host_id)
        conn.commit()

    stats = {
        "project": project_name,
        "project_id": project_id,
        "user": username,
        "host": hostname,
        "files_processed": files_processed,
        "messages_processed": messages_processed,
        "tool_calls_processed": tool_calls_processed,
        "conversations_updated": len(sessions_updated),
    }

    print(f"  Processed: {messages_processed} messages, {tool_calls_processed} tool calls, {len(sessions_updated)} conversations")
    return stats


def parse_codex_sessions(
    conn: sqlite3.Connection,
    full_reparse: bool = False,
    project_filter: Optional[str] = None,
    codex_sessions_dirs: Optional[list[Path]] = None,
) -> list:
    """Parse Codex CLI sessions into the unified DB."""
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

    all_stats = []

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
                session_ctx["session_id"] = format_codex_session_id(payload.get("id", ""))
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

                upsert_session_source(
                    conn, session_ctx["session_id"], project_id, SOURCE_CODEX_CLI,
                    meta={"source": payload.get("source") if isinstance(payload, dict) else None},
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

            last_ts = get_parse_state(conn, "last_parsed", project_id, SOURCE_CODEX_CLI, host_id)
            is_new = not last_ts or msg_timestamp > last_ts

            if not is_new:
                continue

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

                    if tool_name == "shell_command":
                        command = args.get("command") if isinstance(args, dict) else None
                        workdir = args.get("workdir") if isinstance(args, dict) else None
                        process_codex_shell_command(command, workdir, msg_timestamp, session_ctx, conn, project_id, user_id, host_id)
                        tool_calls_processed += 1
                    elif tool_name == "apply_patch":
                        patch_text = extract_codex_patch_text(raw_args, args)
                        process_codex_patch(patch_text, msg_timestamp, session_ctx, conn, project_id, user_id, host_id)
                        tool_calls_processed += 1
                    elif tool_name in ("list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"):
                        server_name = ""
                        if isinstance(args, dict):
                            server_name = args.get("server", "") or args.get("server_name", "")
                        try:
                            conn.execute(
                                """INSERT OR IGNORE INTO mcp_tool_calls
                                   (timestamp, session_id, project_id, user_id, host_id, tool_name, server_name, cwd, git_branch)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (msg_timestamp, session_ctx.get("session_id", ""), project_id, user_id, host_id,
                                 tool_name, server_name, session_ctx.get("cwd", ""), session_ctx.get("git_branch", ""))
                            )
                        except sqlite3.Error as e:
                            print(f"DB error (codex mcp): {e}")
                        tool_calls_processed += 1

            elif event_type == "event_msg":
                payload = event.get("payload", {})
                if payload.get("type") == "token_count":
                    process_codex_token_count(event, session_ctx, conn, project_id, user_id, host_id)

        # Update conversations for this file
        if project_id is not None:
            for sid in sessions_updated:
                if sid:
                    update_conversation(sid, project_name, conn, project_id, user_id, host_id)
            conn.commit()

            if messages_processed > 0:
                # Update parse state with latest timestamp
                latest_ts = get_parse_state(conn, "last_parsed", project_id, SOURCE_CODEX_CLI, host_id) or ""
                # We can't easily track max_ts per project here, just use current time
                set_parse_state(conn, "last_parsed", datetime.now().isoformat(), project_id, SOURCE_CODEX_CLI, host_id)
                conn.commit()

            all_stats.append({
                "project": project_name,
                "messages_processed": messages_processed,
                "tool_calls_processed": tool_calls_processed,
            })

    return all_stats


def parse_cowork_sessions(
    conn: sqlite3.Connection,
    full_reparse: bool = False,
    project_filter: Optional[str] = None,
    cowork_sessions_dirs: Optional[list[Path]] = None,
) -> list:
    """Parse Cowork/Claude Desktop local-agent-mode audit logs into the unified DB."""
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
        cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else ""
        if cwd and not cwd.startswith("/sessions/"):
            return extract_project_name(Path(cwd))
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
                            return extract_project_name(Path(fp))
                    if tool_name == "Bash" and isinstance(tool_input, dict):
                        cmd = tool_input.get("command", "")
                        if isinstance(cmd, str):
                            for token in cmd.split():
                                if token.startswith("/"):
                                    return extract_project_name(Path(token))
        return None

    for audit_path in audit_files:
        for event in parse_jsonl_file(audit_path):
            msg_type = event.get("type", "")
            audit_ts = event.get("_audit_timestamp") or ""
            if not audit_ts:
                continue

            raw_sid = event.get("session_id", "")
            sid = format_cowork_session_id(raw_sid)
            if not sid:
                continue

            inferred = infer_project_from_event(event)
            if inferred:
                session_project.setdefault(sid, inferred)
            project_name = session_project.get(sid, default_project_name)

            if project_filter and project_name != project_filter:
                continue

            username = detect_user_from_path(Path(event.get("cwd", "") or str(Path.home())))
            project_id = get_or_create_project(conn, project_name)
            user_id = get_or_create_user(conn, username)

            last_ts = get_parse_state(conn, "last_parsed", project_id, SOURCE_COWORK_DESKTOP, host_id)
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

            tool_calls = extract_tool_calls(msg)
            for tool_call in tool_calls:
                process_tool_call(tool_call, msg, conn, project_id, user_id, host_id)
                total_tools += 1

            process_token_usage(msg, conn, project_id, user_id, host_id)

            # Update conversation for this session
            update_conversation(sid, project_name, conn, project_id, user_id, host_id)

        conn.commit()

    # Update parse state for all cowork projects
    now_ts = datetime.now().isoformat()
    for sid, pname in session_project.items():
        pid = get_or_create_project(conn, pname)
        set_parse_state(conn, "last_parsed", now_ts, pid, SOURCE_COWORK_DESKTOP, host_id)
    if default_project_name not in session_project.values():
        pid = get_or_create_project(conn, default_project_name)
        set_parse_state(conn, "last_parsed", now_ts, pid, SOURCE_COWORK_DESKTOP, host_id)
    conn.commit()

    all_stats.append({
        "source": "cowork_desktop",
        "messages_processed": total_messages,
        "tool_calls_processed": total_tools,
    })

    return all_stats


# ===========================================================================
# MAIN
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="LocalAgentViewer - Parse AI agent conversations into unified analytics DB"
    )
    parser.add_argument("--full", "-f", action="store_true", help="Force full reparse")
    parser.add_argument("--project", "-p", type=str, help="Parse only a specific project")
    parser.add_argument("--list", "-l", action="store_true", help="List all available projects")
    parser.add_argument("--include-codex", action="store_true", help="Include Codex CLI sessions")
    parser.add_argument("--include-cowork", action="store_true", help="Include Cowork/Claude Desktop sessions")
    parser.add_argument("--claude-projects-dir", action="append", default=None)
    parser.add_argument("--codex-sessions-dir", action="append", default=None)
    parser.add_argument("--cowork-sessions-dir", action="append", default=None)

    args = parser.parse_args()
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
                    print(f"  {name}: {count} conversation files")
        return

    # Initialize unified DB
    conn = init_db()
    print(f"Database: {UNIFIED_DB_PATH}")

    if args.project:
        found = False
        for root in claude_roots:
            for project_dir in root.iterdir():
                if project_dir.is_dir():
                    name = extract_project_name(project_dir)
                    if name == args.project:
                        parse_project(project_dir, conn, args.full)
                        found = True
                        break
            if found:
                break
        if not found:
            print(f"Project not found: {args.project}")
            print("Use --list to see available projects")
        if args.include_codex:
            parse_codex_sessions(conn, args.full, project_filter=args.project, codex_sessions_dirs=codex_roots)
        if args.include_cowork:
            parse_cowork_sessions(conn, args.full, project_filter=args.project, cowork_sessions_dirs=cowork_roots)
    else:
        print("Parsing all projects from:")
        for root in claude_roots:
            print(f"  - {root}")

        all_stats = []
        for root in claude_roots:
            for project_dir in sorted(root.iterdir()):
                if project_dir.is_dir():
                    stats = parse_project(project_dir, conn, args.full)
                    all_stats.append(stats)

        if args.include_codex:
            parse_codex_sessions(conn, args.full, codex_sessions_dirs=codex_roots)
        if args.include_cowork:
            parse_cowork_sessions(conn, args.full, cowork_sessions_dirs=cowork_roots)

        print(f"\n{'='*60}")
        print("Summary")
        print(f"{'='*60}")
        total_messages = sum(s["messages_processed"] for s in all_stats)
        total_tools = sum(s["tool_calls_processed"] for s in all_stats)
        print(f"Projects processed: {len(all_stats)}")
        print(f"Total messages: {total_messages}")
        print(f"Total tool calls: {total_tools}")

    conn.close()

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

    # Resolve host and user IDs
    hostname = _normalize_hostname(host_info.get("hostname", "unknown"))
    os_type = host_info.get("os_type", "")
    home_dir = host_info.get("home_dir", "")
    host_id = get_or_create_host(conn, hostname, os_type, home_dir)

    username = user_info.get("username", "unknown")
    user_id = get_or_create_user(conn, username)

    for session_data in sessions:
        conv = session_data.get("conversation", {})
        session_id = conv.get("session_id")
        if not session_id:
            continue

        # Resolve project
        project_name = conv.get("project_name") or conv.get("project") or "unknown"
        source_path = ""
        project_id = get_or_create_project(conn, project_name, source_path)

        # Use remote host/user IDs, not local ones
        r_host_id = host_id
        r_user_id = user_id

        # Insert conversation (OR IGNORE = anti-duplicate on PK)
        try:
            conn.execute("""
                INSERT OR IGNORE INTO conversations
                (session_id, project_id, user_id, host_id, timestamp, display, summary,
                 project, model, total_tokens, message_count, tools_used, cwd, git_branch,
                 parent_session_id, agent_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        except sqlite3.Error:
            pass

        # Insert child records (all use INSERT OR IGNORE where UNIQUE exists)
        for msg in session_data.get("messages", []):
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO messages
                    (session_id, project_id, user_id, host_id, uuid, type, content,
                     timestamp, tokens_in, tokens_out, model)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    session_id, project_id, r_user_id, r_host_id,
                    msg.get("uuid"), msg.get("type", ""),
                    msg.get("content", ""), msg.get("timestamp", ""),
                    msg.get("tokens_in", 0), msg.get("tokens_out", 0),
                    msg.get("model", ""),
                ))
                stats["messages"] += 1
            except sqlite3.Error:
                pass

        for tu in session_data.get("token_usage", []):
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO token_usage
                    (timestamp, session_id, project_id, user_id, host_id, model,
                     input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, cwd)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    tu.get("timestamp", ""), session_id, project_id, r_user_id, r_host_id,
                    tu.get("model", ""),
                    tu.get("input_tokens", 0), tu.get("output_tokens", 0),
                    tu.get("cache_creation_tokens", 0), tu.get("cache_read_tokens", 0),
                    tu.get("cwd", ""),
                ))
                stats["token_usage"] += 1
            except sqlite3.Error:
                pass

        for fo in session_data.get("file_operations", []):
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO file_operations
                    (timestamp, session_id, project_id, user_id, host_id, tool, file_path, cwd, git_branch)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    fo.get("timestamp", ""), session_id, project_id, r_user_id, r_host_id,
                    fo.get("tool", ""), fo.get("file_path", ""),
                    fo.get("cwd", ""), fo.get("git_branch", ""),
                ))
                stats["file_operations"] += 1
            except sqlite3.Error:
                pass

        for bc in session_data.get("bash_commands", []):
            try:
                conn.execute("""
                    INSERT INTO bash_commands
                    (timestamp, session_id, project_id, user_id, host_id, command, description,
                     target_file, cwd, git_branch)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    bc.get("timestamp", ""), session_id, project_id, r_user_id, r_host_id,
                    bc.get("command", ""), bc.get("description", ""),
                    bc.get("target_file", ""), bc.get("cwd", ""), bc.get("git_branch", ""),
                ))
                stats["bash_commands"] += 1
            except sqlite3.Error:
                pass

        for so in session_data.get("search_operations", []):
            try:
                conn.execute("""
                    INSERT INTO search_operations
                    (timestamp, session_id, project_id, user_id, host_id, tool, pattern, path, output_mode, cwd)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    so.get("timestamp", ""), session_id, project_id, r_user_id, r_host_id,
                    so.get("tool", ""), so.get("pattern", ""),
                    so.get("path", ""), so.get("output_mode", ""), so.get("cwd", ""),
                ))
                stats["search_operations"] += 1
            except sqlite3.Error:
                pass

        for si in session_data.get("skill_invocations", []):
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO skill_invocations
                    (timestamp, session_id, project_id, user_id, host_id, skill_name, args, cwd, git_branch)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    si.get("timestamp", ""), session_id, project_id, r_user_id, r_host_id,
                    si.get("skill_name", ""), si.get("args", ""),
                    si.get("cwd", ""), si.get("git_branch", ""),
                ))
                stats["skill_invocations"] += 1
            except sqlite3.Error:
                pass

        for sa in session_data.get("subagent_invocations", []):
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO subagent_invocations
                    (timestamp, session_id, project_id, user_id, host_id, subagent_type, description,
                     prompt, model, run_in_background, cwd, git_branch)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    sa.get("timestamp", ""), session_id, project_id, r_user_id, r_host_id,
                    sa.get("subagent_type", ""), sa.get("description", ""),
                    sa.get("prompt", ""), sa.get("model", ""),
                    sa.get("run_in_background", 0),
                    sa.get("cwd", ""), sa.get("git_branch", ""),
                ))
                stats["subagent_invocations"] += 1
            except sqlite3.Error:
                pass

        for mc in session_data.get("mcp_tool_calls", []):
            try:
                conn.execute("""
                    INSERT INTO mcp_tool_calls
                    (timestamp, session_id, project_id, user_id, host_id, tool_name, server_name, cwd, git_branch)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    mc.get("timestamp", ""), session_id, project_id, r_user_id, r_host_id,
                    mc.get("tool_name", ""), mc.get("server_name", ""),
                    mc.get("cwd", ""), mc.get("git_branch", ""),
                ))
                stats["mcp_tool_calls"] += 1
            except sqlite3.Error:
                pass

    conn.commit()
    return stats


if __name__ == "__main__":
    main()
